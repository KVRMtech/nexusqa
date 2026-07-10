"""QE-Central Phase-7 — per-tenant quota + plan-tiering tests (no DB, no network).

Pins the fleet-scale fairness contract (app/fleet/quota.py):

  * **backward-compat is load-bearing** — the DEFAULT (unlimited) plan a tenant
    resolves to when nothing is configured NEVER denies, and the async
    ``enforce_*`` helpers SHORT-CIRCUIT (run no query) for it.  This is the exact
    property that keeps the whole existing suite green;
  * a SMALL plan caps the fleet fail-closed — app #(N+1) is refused, a
    monthly-spend breach refuses the next cycle, a concurrency breach refuses,
    and the rps TIER is applied;
  * usage is aggregated from a synthetic cost ledger through the SAME meter the
    cost snapshot uses (:func:`quota.sum_unit`);
  * a denial is FAIL-CLOSED, carries a stable machine reason + HTTP status, and
    logs (:class:`quota.QuotaExceeded`);
  * plan resolution is config-driven and degrades toward the GENEROUS default on
    a malformed/unknown assignment (never a surprise tighten).

The pure model + checks need no database; the ``enforce_*`` helpers are driven
with a tiny fake session so the deny/short-circuit paths are exercised without a
real Postgres.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.controlplane.cost.meter import UNIT_BROWSER_SECONDS, UNIT_LLM_TOKENS, UNIT_UNMETERED_RUN
from app.fleet import quota
from app.fleet.quota import (
    DEFAULT_PLAN,
    RESOURCE_APPS,
    RESOURCE_CONCURRENT_CYCLES,
    RESOURCE_MONTHLY_BROWSER_SECONDS,
    RESOURCE_MONTHLY_LLM_TOKENS,
    QuotaExceeded,
    QuotaPlan,
    check_quota,
)


# ══════════════════════ fakes (DB-free enforce_* driving) ═══════════════════
class _FakeResult:
    """A stand-in for a SQLAlchemy ``Result`` supporting the two shapes the quota
    reads use: ``.scalar()`` (counts) and ``.scalars().all()`` (ledger rows)."""

    def __init__(self, *, scalar=None, rows=None) -> None:
        self._scalar = scalar
        self._rows = list(rows or [])

    def scalar(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Returns queued results per ``execute`` and RAISES on an unexpected query.

    An empty queue makes any ``execute`` a hard error — that is how the
    short-circuit tests prove the default/unlimited path opens no query at all.
    """

    def __init__(self, results=None) -> None:
        self._results = list(results or [])
        self.execute_calls = 0

    async def execute(self, *_a, **_k):
        self.execute_calls += 1
        if not self._results:
            raise AssertionError("unexpected DB query — the check should have short-circuited")
        return self._results.pop(0)


def _ledger_row(unit: str, qty, *, created_at: datetime):
    """A minimal ``cost_ledger``-row stand-in (attribute-compatible with the meter)."""
    return SimpleNamespace(
        entry_id="e", tenant_id="t1", app_id="", cycle_id="",
        unit=unit, quantity=Decimal(str(qty)), source_ref="",
        unit_cost_usd=None, created_at=created_at,
    )


def _now() -> datetime:
    return datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


# ══════════════════════ 1) default plan = never denied (backward-compat) ════
class TestDefaultPlanNeverDenies:
    @pytest.mark.parametrize(
        "resource",
        [
            RESOURCE_APPS,
            RESOURCE_CONCURRENT_CYCLES,
            RESOURCE_MONTHLY_BROWSER_SECONDS,
            RESOURCE_MONTHLY_LLM_TOKENS,
        ],
    )
    def test_unlimited_resource_allows_any_usage(self, resource):
        # Even an absurd usage is allowed — the default plan cannot trip.
        decision = check_quota(DEFAULT_PLAN, resource, Decimal("999999999999"))
        assert decision.allowed is True
        assert decision.denied is False
        assert decision.reason == quota.REASON_ALLOWED
        assert decision.limit is None  # unlimited path never inspected usage

    def test_default_plan_every_limit_is_none(self):
        for res in quota.ALL_RESOURCES:
            assert DEFAULT_PLAN.limit_for(res) is None


# ══════════════════════ 2) QuotaPlan model ═════════════════════════════════
class TestQuotaPlan:
    def test_limit_for_returns_each_field(self):
        plan = QuotaPlan(
            name="p", max_apps=5, max_concurrent_cycles=3,
            monthly_browser_seconds=Decimal("100"), monthly_llm_tokens=Decimal("200"),
        )
        assert plan.limit_for(RESOURCE_APPS) == 5
        assert plan.limit_for(RESOURCE_CONCURRENT_CYCLES) == 3
        assert plan.limit_for(RESOURCE_MONTHLY_BROWSER_SECONDS) == Decimal("100")
        assert plan.limit_for(RESOURCE_MONTHLY_LLM_TOKENS) == Decimal("200")

    def test_unknown_resource_raises(self):
        with pytest.raises(ValueError, match="unknown quota resource"):
            DEFAULT_PLAN.limit_for("gpu_hours")

    def test_from_mapping_parses_valid(self):
        plan = QuotaPlan.from_mapping("small", {
            "max_apps": 3, "max_concurrent_cycles": 1,
            "monthly_browser_seconds": "1000", "monthly_llm_tokens": 2000,
            "max_rps_default": 1.5, "retention_days": 30,
        })
        assert plan.max_apps == 3
        assert plan.monthly_browser_seconds == Decimal("1000")
        assert plan.max_rps_default == 1.5
        assert plan.retention_days == 30

    def test_from_mapping_coerces_garbage_to_unlimited(self):
        # Non-numeric / negative / absent → None (unlimited): a broken limit
        # degrades toward the generous default, never crashes and never tightens.
        plan = QuotaPlan.from_mapping("bad", {
            "max_apps": "not-a-number", "max_concurrent_cycles": -4,
            "monthly_browser_seconds": None, "max_rps_default": 0,
        })
        assert plan.max_apps is None
        assert plan.max_concurrent_cycles is None
        assert plan.monthly_browser_seconds is None
        assert plan.max_rps_default is None  # 0 is non-positive → no tier
        assert plan.retention_days is None

    def test_as_dict_is_json_safe(self):
        d = QuotaPlan(name="p", monthly_browser_seconds=Decimal("1.5")).as_dict()
        assert d["name"] == "p"
        assert d["monthly_browser_seconds"] == "1.5"  # Decimal → str
        assert d["max_apps"] is None


# ══════════════════════ 3) check_quota (the fail-closed heart) ══════════════
class TestCheckQuota:
    def test_app_n_plus_one_refused_at_cap(self):
        plan = QuotaPlan(name="small", max_apps=3)
        # 3 apps already exist → registering #4 (usage == limit) is refused.
        denied = check_quota(plan, RESOURCE_APPS, 3)
        assert denied.denied is True
        assert denied.reason == "max_apps_exceeded"
        assert denied.limit == Decimal("3") and denied.usage == Decimal("3")
        assert denied.plan == "small"
        # 2 apps → #3 is still allowed.
        assert check_quota(plan, RESOURCE_APPS, 2).allowed is True

    def test_monthly_cost_exceeded_refuses(self):
        plan = QuotaPlan(name="small", monthly_browser_seconds=Decimal("1000"))
        assert check_quota(plan, RESOURCE_MONTHLY_BROWSER_SECONDS, Decimal("1500")).denied
        assert check_quota(plan, RESOURCE_MONTHLY_BROWSER_SECONDS, Decimal("999")).allowed

    def test_exactly_at_limit_is_denied(self):
        # Boundary: usage == limit denies (fail-closed — admitting one more needs
        # strictly-less-than the cap).
        plan = QuotaPlan(name="p", monthly_llm_tokens=Decimal("500"))
        assert check_quota(plan, RESOURCE_MONTHLY_LLM_TOKENS, Decimal("500")).denied

    def test_garbage_usage_reads_as_zero(self):
        # A non-numeric usage can only make the check MORE permissive, never
        # wrongly deny (it reads as zero usage).
        plan = QuotaPlan(name="p", max_apps=1)
        assert check_quota(plan, RESOURCE_APPS, None).allowed is True
        assert check_quota(plan, RESOURCE_APPS, "junk").allowed is True


# ══════════════════════ 4) plan resolution (attach a plan to a tenant) ══════
class TestResolvePlan:
    def test_unassigned_tenant_is_default(self):
        plan = quota.resolve_plan("t1", registry=quota.load_plan_registry({}), assignments={})
        assert plan.name == quota.DEFAULT_PLAN_NAME
        assert plan.max_apps is None

    def test_assignment_attaches_named_plan(self):
        reg = quota.load_plan_registry({})
        plan = quota.resolve_plan("t1", assignments={"t1": "starter"}, registry=reg)
        assert plan.name == "starter"
        assert plan.max_apps == quota.BUILTIN_PLANS["starter"].max_apps

    def test_unknown_assignment_falls_back_to_default(self):
        reg = quota.load_plan_registry({})
        plan = quota.resolve_plan("t1", assignments={"t1": "does-not-exist"}, registry=reg)
        assert plan.name == quota.DEFAULT_PLAN_NAME

    def test_explicit_plan_name_hint_wins(self):
        reg = quota.load_plan_registry({})
        plan = quota.resolve_plan("t1", plan_name="growth", assignments={"t1": "starter"}, registry=reg)
        assert plan.name == "growth"

    def test_env_registry_and_assignment_end_to_end(self):
        env = {
            quota.ENV_QUOTA_PLANS: '{"tiny": {"max_apps": 2}}',
            quota.ENV_TENANT_PLANS: '{"acme": "tiny"}',
        }
        reg = quota.load_plan_registry(env)
        asg = quota.load_tenant_assignments(env)
        assert quota.resolve_plan("acme", registry=reg, assignments=asg).max_apps == 2
        # An unlisted tenant still gets the unlimited default.
        assert quota.resolve_plan("other", registry=reg, assignments=asg).max_apps is None

    def test_malformed_plans_env_degrades_to_builtins(self):
        reg = quota.load_plan_registry({quota.ENV_QUOTA_PLANS: "{not json"})
        assert quota.DEFAULT_PLAN_NAME in reg
        assert reg["starter"].max_apps == quota.BUILTIN_PLANS["starter"].max_apps

    def test_malformed_assignments_env_is_empty(self):
        assert quota.load_tenant_assignments({quota.ENV_TENANT_PLANS: "[not,a,map]"}) == {}
        assert quota.load_tenant_assignments({quota.ENV_TENANT_PLANS: "garbage"}) == {}

    def test_default_plan_always_present_even_if_overridden(self):
        # A deploy cannot remove the unlimited default (backward-compat anchor).
        reg = quota.load_plan_registry({quota.ENV_QUOTA_PLANS: '{"default": {"max_apps": 1}}'})
        # Overriding "default" with a limit is allowed, but the KEY still exists.
        assert quota.DEFAULT_PLAN_NAME in reg


# ══════════════════════ 5) rps tier applied ════════════════════════════════
class TestEffectiveMaxRps:
    def test_app_fence_rate_wins(self):
        plan = QuotaPlan(name="p", max_rps_default=1.0)
        assert quota.effective_max_rps(plan, 5) == 5.0
        assert quota.effective_max_rps(plan, "2.5") == 2.5

    def test_plan_tier_applies_when_no_fence(self):
        plan = QuotaPlan(name="p", max_rps_default=3.0)
        assert quota.effective_max_rps(plan, None) == 3.0
        assert quota.effective_max_rps(plan, 0) == 3.0  # non-positive fence → tier

    def test_none_when_neither_set_preserves_fail_closed(self):
        # Default plan (no tier) + no fence → None → admission fail-closes exactly
        # as today (rate_unconfigured); the feature never weakens the default.
        assert quota.effective_max_rps(DEFAULT_PLAN, None) is None
        assert quota.effective_max_rps(DEFAULT_PLAN, "bad") is None


# ══════════════════════ 6) retention tier ══════════════════════════════════
class TestRetentionCutoff:
    def test_unlimited_retention_is_none(self):
        assert quota.retention_cutoff(DEFAULT_PLAN, now=_now()) is None

    def test_cutoff_is_now_minus_days(self):
        plan = QuotaPlan(name="p", retention_days=30)
        assert quota.retention_cutoff(plan, now=_now()) == _now() - timedelta(days=30)


# ══════════════════════ 7) usage aggregation (synthetic ledger) ════════════
class TestSumUnit:
    def _entry(self, unit, qty, *, created_at):
        return {
            "tenant_id": "t1", "app_id": "a", "cycle_id": "c", "unit": unit,
            "quantity": Decimal(str(qty)), "unit_cost_usd": None, "created_at": created_at,
        }

    def test_sums_one_unit_across_entries(self):
        entries = [
            self._entry(UNIT_BROWSER_SECONDS, "100", created_at=_now()),
            self._entry(UNIT_BROWSER_SECONDS, "250", created_at=_now()),
            self._entry(UNIT_LLM_TOKENS, "9999", created_at=_now()),  # other unit ignored
        ]
        assert quota.sum_unit(entries, UNIT_BROWSER_SECONDS) == Decimal("350")

    def test_gap_flag_never_counted_as_spend(self):
        entries = [
            self._entry(UNIT_BROWSER_SECONDS, "100", created_at=_now()),
            self._entry(UNIT_UNMETERED_RUN, "1", created_at=_now()),
        ]
        # unmetered_run is a gap flag, not spend — summing browser_seconds is 100.
        assert quota.sum_unit(entries, UNIT_BROWSER_SECONDS) == Decimal("100")
        # And it does not masquerade as browser_seconds usage.
        assert quota.sum_unit(entries, UNIT_UNMETERED_RUN) == Decimal("0")

    def test_window_filters_out_of_month_entries(self):
        last_month = _now() - timedelta(days=40)
        entries = [
            self._entry(UNIT_BROWSER_SECONDS, "100", created_at=_now()),
            self._entry(UNIT_BROWSER_SECONDS, "500", created_at=last_month),
        ]
        window = (quota.month_start(_now()), None)
        assert quota.sum_unit(entries, UNIT_BROWSER_SECONDS, window=window) == Decimal("100")


# ══════════════════════ 8) QuotaExceeded (fail-closed refusal) ══════════════
class TestQuotaExceeded:
    def test_from_decision_maps_status_per_resource(self):
        apps_deny = check_quota(QuotaPlan(name="p", max_apps=1), RESOURCE_APPS, 1)
        exc = QuotaExceeded.from_decision(apps_deny)
        assert exc.status_code == 409  # structural capacity cap
        cost_deny = check_quota(
            QuotaPlan(name="p", monthly_browser_seconds=Decimal("1")),
            RESOURCE_MONTHLY_BROWSER_SECONDS, Decimal("2"),
        )
        assert QuotaExceeded.from_decision(cost_deny).status_code == 429  # throughput cap

    def test_as_http_detail_shape(self):
        deny = check_quota(QuotaPlan(name="small", max_apps=2), RESOURCE_APPS, 2)
        detail = QuotaExceeded.from_decision(deny).as_http_detail()
        assert detail["refused"] is True
        assert detail["reason"] == "quota_exceeded"
        assert detail["resource"] == RESOURCE_APPS
        assert detail["quota_reason"] == "max_apps_exceeded"
        assert detail["plan"] == "small"
        assert detail["limit"] == "2" and detail["usage"] == "2"
        assert isinstance(detail["message"], str) and detail["message"]


# ══════════════════════ 9) enforce_app_registration_quota (async) ══════════
class TestEnforceAppQuota:
    @pytest.mark.asyncio
    async def test_default_plan_short_circuits_no_query(self):
        session = _FakeSession(results=[])  # any query raises AssertionError
        await quota.enforce_app_registration_quota("t1", session=session, plan=DEFAULT_PLAN)
        assert session.execute_calls == 0  # PROVES no DB work on the default path

    @pytest.mark.asyncio
    async def test_small_plan_refuses_at_cap(self):
        plan = QuotaPlan(name="small", max_apps=3)
        session = _FakeSession(results=[_FakeResult(scalar=3)])  # 3 apps already
        with pytest.raises(QuotaExceeded) as ei:
            await quota.enforce_app_registration_quota("t1", session=session, plan=plan)
        assert ei.value.status_code == 409
        assert ei.value.resource == RESOURCE_APPS
        assert ei.value.reason == "max_apps_exceeded"

    @pytest.mark.asyncio
    async def test_small_plan_allows_under_cap(self):
        plan = QuotaPlan(name="small", max_apps=3)
        session = _FakeSession(results=[_FakeResult(scalar=2)])
        await quota.enforce_app_registration_quota("t1", session=session, plan=plan)
        assert session.execute_calls == 1  # queried, allowed, no raise


# ══════════════════════ 10) enforce_cycle_quota (async) ════════════════════
class TestEnforceCycleQuota:
    @pytest.mark.asyncio
    async def test_default_plan_short_circuits_no_query(self):
        session = _FakeSession(results=[])
        await quota.enforce_cycle_quota("t1", session=session, plan=DEFAULT_PLAN, now=_now())
        assert session.execute_calls == 0

    @pytest.mark.asyncio
    async def test_concurrency_cap_refuses(self):
        plan = QuotaPlan(name="p", max_concurrent_cycles=2)
        session = _FakeSession(results=[_FakeResult(scalar=2)])  # 2 in-flight already
        with pytest.raises(QuotaExceeded) as ei:
            await quota.enforce_cycle_quota("t1", session=session, plan=plan, now=_now())
        assert ei.value.status_code == 429
        assert ei.value.resource == RESOURCE_CONCURRENT_CYCLES

    @pytest.mark.asyncio
    async def test_monthly_cost_exceeded_refuses_cycle(self):
        plan = QuotaPlan(name="p", monthly_browser_seconds=Decimal("1000"))
        rows = [_ledger_row(UNIT_BROWSER_SECONDS, "1500", created_at=_now())]
        session = _FakeSession(results=[_FakeResult(rows=rows)])
        with pytest.raises(QuotaExceeded) as ei:
            await quota.enforce_cycle_quota("t1", session=session, plan=plan, now=_now())
        assert ei.value.status_code == 429
        assert ei.value.resource == RESOURCE_MONTHLY_BROWSER_SECONDS

    @pytest.mark.asyncio
    async def test_monthly_under_budget_allows(self):
        plan = QuotaPlan(name="p", monthly_browser_seconds=Decimal("1000"))
        rows = [_ledger_row(UNIT_BROWSER_SECONDS, "500", created_at=_now())]
        session = _FakeSession(results=[_FakeResult(rows=rows)])
        await quota.enforce_cycle_quota("t1", session=session, plan=plan, now=_now())
        assert session.execute_calls == 1  # ledger read, under budget, no raise
