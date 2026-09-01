"""QE-Central Phase-7 — per-tenant quota + plan-tiering (fair, fail-closed).

WHY THIS EXISTS
===============
At fleet scale (20+ clients, 10k+ apps sharing ONE control plane) the existing
admission gate (:mod:`app.controlplane.scheduling.admission`) keeps a single
cycle POLITE against a customer host and keeps concurrency FAIR at the moment a
cycle starts — but nothing yet bounds a tenant's *aggregate* footprint: how many
apps they may register, how many cycles may run at once, how much metered spend
they may burn in a month, or which default politeness tier they run at.  This
module is that bound: a small, PURE plan model plus a config-driven resolver and
fail-closed enforcement helpers the crawl/cycle triggers and the app registry
call.

BACKWARD-COMPAT IS LOAD-BEARING (never change a default that alters a test)
==========================================================================
Every limit is OPT-IN.  A :class:`QuotaPlan` field left ``None`` means UNLIMITED,
and the :data:`DEFAULT_PLAN` a tenant resolves to when nothing is configured has
EVERY field ``None``.  :func:`check_quota` returns *Allowed* for an unlimited
resource without ever looking at usage, and the async ``enforce_*`` helpers
SHORT-CIRCUIT (open no session, run no query) when the relevant limit is
unlimited.  So an un-provisioned tenant — which is every tenant today — behaves
byte-for-byte as before: no quota query runs, nothing ever trips.  A limit bites
ONLY when an operator explicitly (a) defines a plan with that limit set AND
(b) assigns the tenant to it.

FAIL-CLOSED
===========
When a limit IS set and usage has reached it, the trigger is REFUSED with a
clear, auditable reason (:class:`QuotaExceeded`, mapped by the routers to a
429/409).  A malformed plan/assignment in the environment degrades toward the
GENEROUS default (logged loudly) rather than crashing a request — availability
is preserved, and a misconfiguration can never silently *tighten* an unrelated
tenant.

HOW A PLAN IS ATTACHED TO A TENANT
==================================
Two env-driven config layers, both parsed lazily at call time (never import
time), so a deploy tunes them without a code change and a test can set them per
case:

  * ``QEC_QUOTA_PLANS``  — JSON ``{plan_name: {max_apps, max_concurrent_cycles,
    monthly_browser_seconds, monthly_llm_tokens, max_rps_default,
    retention_days}}``.  Overlays :data:`BUILTIN_PLANS` (the reference tiers).
  * ``QEC_TENANT_PLANS`` — JSON ``{tenant_id: plan_name}`` assigning a tenant to
    a named plan.  An unassigned tenant → :data:`DEFAULT_PLAN` (unlimited); an
    assignment to an unknown name → the default too (never a surprise tighten).

:func:`resolve_plan` also accepts an explicit ``plan_name`` hint (e.g. a caller
that already holds a tenant record's ``plan`` column) — an additive future seam
that no existing caller uses.

ZERO LLM.  The plan model + :func:`check_quota` + :func:`sum_unit` +
:func:`effective_max_rps` + :func:`retention_cutoff` are pure and unit-tested
with no database; the async ``enforce_*`` / usage helpers read the tenant-scoped
qecentral session and reuse :mod:`app.controlplane.cost.meter` for aggregation.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..controlplane.cost import meter
from ..db import tenant_scoped_qec_session, utc_now
from ..db.controlplane_models import TERMINAL_CYCLE_STATES, AppCycleRow, CostLedgerRow
from ..db.models import ClientAppRow, QEExplorationRow
from ..observability import record_quota_decision

logger = logging.getLogger(__name__)


# ── Env knobs (config-driven; the default of every knob preserves today's
#    unlimited behaviour) ────────────────────────────────────────────────────
#: JSON ``{plan_name: {limits...}}`` overlaying :data:`BUILTIN_PLANS`.
ENV_QUOTA_PLANS = "QEC_QUOTA_PLANS"
#: JSON ``{tenant_id: plan_name}`` attaching a tenant to a named plan.
ENV_TENANT_PLANS = "QEC_TENANT_PLANS"


# ── Quota resources (the dimensions a plan may cap) ─────────────────────────
RESOURCE_APPS = "apps"
RESOURCE_CONCURRENT_CYCLES = "concurrent_cycles"
#: M3.4 / T-RS-03 - non-terminal CRAWLS a tenant may have in flight at once.
#: Distinct from ``concurrent_cycles``: a cycle is the scheduled regression unit,
#: a crawl is one exploration dispatch, and the crawl dispatch path
#: (``POST /explorations`` and its resume sibling) reaches a worker WITHOUT ever
#: creating a cycle.  Capping cycles therefore left the fleet's most expensive
#: resource - a browser on a worker - entirely unmetered from that direction.
RESOURCE_CONCURRENT_CRAWLS = "concurrent_crawls"
RESOURCE_MONTHLY_BROWSER_SECONDS = "monthly_browser_seconds"
RESOURCE_MONTHLY_LLM_TOKENS = "monthly_llm_tokens"

#: Every resource :func:`check_quota` understands.
ALL_RESOURCES = frozenset({
    RESOURCE_APPS,
    RESOURCE_CONCURRENT_CYCLES,
    RESOURCE_CONCURRENT_CRAWLS,
    RESOURCE_MONTHLY_BROWSER_SECONDS,
    RESOURCE_MONTHLY_LLM_TOKENS,
})

#: The monthly-spend resources → the canonical ``cost_ledger`` unit they cap.
_RESOURCE_TO_METER_UNIT = {
    RESOURCE_MONTHLY_BROWSER_SECONDS: meter.UNIT_BROWSER_SECONDS,
    RESOURCE_MONTHLY_LLM_TOKENS: meter.UNIT_LLM_TOKENS,
}

#: The stable, machine-readable refusal reason per resource (surfaced in the
#: :class:`QuotaExceeded` detail + the structured deny log).
REASON_ALLOWED = ""
_RESOURCE_REASON = {
    RESOURCE_APPS: "max_apps_exceeded",
    RESOURCE_CONCURRENT_CYCLES: "max_concurrent_cycles_exceeded",
    RESOURCE_CONCURRENT_CRAWLS: "max_concurrent_crawls_exceeded",
    RESOURCE_MONTHLY_BROWSER_SECONDS: "monthly_browser_seconds_exceeded",
    RESOURCE_MONTHLY_LLM_TOKENS: "monthly_llm_tokens_exceeded",
}

#: HTTP status a router maps a refusal to, per resource.  A structural capacity
#: cap (max_apps) is a 409 Conflict; a throughput/spend cap is a 429.
_RESOURCE_STATUS = {
    RESOURCE_APPS: 409,
    RESOURCE_CONCURRENT_CYCLES: 429,
    RESOURCE_CONCURRENT_CRAWLS: 429,
    RESOURCE_MONTHLY_BROWSER_SECONDS: 429,
    RESOURCE_MONTHLY_LLM_TOKENS: 429,
}


# ── Coercion helpers (lenient parse → generous default on garbage) ──────────
def _as_decimal(value: Any) -> Decimal:
    """Coerce ``value`` to a non-negative :class:`Decimal` (``0`` on garbage).

    Usage counts/sums are never negative; a non-numeric value is treated as
    zero usage (the safe reading — it can only make a check MORE permissive,
    never wrongly deny).
    """
    if isinstance(value, bool):  # bool is an int subclass — reject the footgun.
        return Decimal("0")
    if isinstance(value, Decimal):
        return value if value >= 0 else Decimal("0")
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")
    return dec if dec >= 0 else Decimal("0")


def _opt_int(data: Mapping, key: str) -> Optional[int]:
    """Parse an optional NON-NEGATIVE int limit; ``None`` (unlimited) on absent/bad."""
    if key not in data or data[key] is None:
        return None
    try:
        val = int(data[key])
    except (TypeError, ValueError):
        logger.warning("qec.quota.limit_unparseable", extra={"field": key, "kind": "int"})
        return None
    return val if val >= 0 else None


def _opt_decimal(data: Mapping, key: str) -> Optional[Decimal]:
    """Parse an optional NON-NEGATIVE Decimal limit; ``None`` on absent/bad."""
    if key not in data or data[key] is None:
        return None
    try:
        val = Decimal(str(data[key]))
    except (InvalidOperation, ValueError, TypeError):
        logger.warning("qec.quota.limit_unparseable", extra={"field": key, "kind": "decimal"})
        return None
    return val if val >= 0 else None


def _opt_float(data: Mapping, key: str) -> Optional[float]:
    """Parse an optional POSITIVE float (rate tier); ``None`` on absent/non-positive."""
    if key not in data or data[key] is None:
        return None
    try:
        val = float(data[key])
    except (TypeError, ValueError):
        logger.warning("qec.quota.limit_unparseable", extra={"field": key, "kind": "float"})
        return None
    return val if val > 0 else None


# ── The plan model (pure) ───────────────────────────────────────────────────
@dataclass(frozen=True)
class QuotaPlan:
    """One pricing/fairness tier.  EVERY limit is optional; ``None`` = UNLIMITED.

    Fields (all ``None`` by default so the bare :class:`QuotaPlan` is unlimited):
      * ``max_apps`` — the count of non-deleted registered apps a tenant may hold.
      * ``max_concurrent_cycles`` — non-terminal cycles a tenant may have at once
        (a tenant-wide fairness cap ABOVE the admission gate's per-tenant slot
        cap; enforced at cycle CREATION so a fleet stays fair before work starts).
      * ``monthly_browser_seconds`` — the summed ``browser_seconds`` a tenant may
        meter in the current calendar month (the primary "monthly cost" cap).
      * ``monthly_llm_tokens`` — the summed ``llm_tokens`` per calendar month.
      * ``max_rps_default`` — the DEFAULT per-host politeness rate applied when an
        app sets no ``fences.max_rps`` of its own (:func:`effective_max_rps`);
        ``None`` keeps today's fail-closed posture (an app with no rate is
        refused, never silently crawled).
      * ``retention_days`` — the evidence-retention window for the tier
        (:func:`retention_cutoff`); consumed by the data-lifecycle subsystem.

    Frozen + hashable so a plan is safe to cache and share across requests.
    """

    name: str
    max_apps: Optional[int] = None
    max_concurrent_cycles: Optional[int] = None
    max_concurrent_crawls: Optional[int] = None
    monthly_browser_seconds: Optional[Decimal] = None
    monthly_llm_tokens: Optional[Decimal] = None
    max_rps_default: Optional[float] = None
    retention_days: Optional[int] = None

    def limit_for(self, resource: str) -> Optional[Decimal | int]:
        """Return the limit for ``resource`` (``None`` = unlimited).

        Raises ``ValueError`` for an unknown resource — a typo in a wiring call
        must fail LOUD, never silently resolve to "unlimited" and skip a check.
        """
        if resource == RESOURCE_APPS:
            return self.max_apps
        if resource == RESOURCE_CONCURRENT_CYCLES:
            return self.max_concurrent_cycles
        if resource == RESOURCE_CONCURRENT_CRAWLS:
            return self.max_concurrent_crawls
        if resource == RESOURCE_MONTHLY_BROWSER_SECONDS:
            return self.monthly_browser_seconds
        if resource == RESOURCE_MONTHLY_LLM_TOKENS:
            return self.monthly_llm_tokens
        raise ValueError(
            f"unknown quota resource {resource!r}; expected one of {sorted(ALL_RESOURCES)}"
        )

    @classmethod
    def from_mapping(cls, name: str, data: Mapping) -> "QuotaPlan":
        """Build a plan from a config mapping (unknown/garbage limits → unlimited).

        A field that is absent, ``None``, non-numeric, or negative parses to
        ``None`` (unlimited) with a WARNING — a broken limit degrades toward the
        generous default rather than crashing the fleet, and can never silently
        tighten a tenant it was not meant to.
        """
        d = dict(data or {})
        return cls(
            name=str(name),
            max_apps=_opt_int(d, "max_apps"),
            max_concurrent_cycles=_opt_int(d, "max_concurrent_cycles"),
            max_concurrent_crawls=_opt_int(d, "max_concurrent_crawls"),
            monthly_browser_seconds=_opt_decimal(d, "monthly_browser_seconds"),
            monthly_llm_tokens=_opt_decimal(d, "monthly_llm_tokens"),
            max_rps_default=_opt_float(d, "max_rps_default"),
            retention_days=_opt_int(d, "retention_days"),
        )

    def as_dict(self) -> dict:
        """A JSON-serialisable view of the plan (Decimals → str; None preserved)."""
        return {
            "name": self.name,
            "max_apps": self.max_apps,
            "max_concurrent_cycles": self.max_concurrent_cycles,
            "max_concurrent_crawls": self.max_concurrent_crawls,
            "monthly_browser_seconds": (
                str(self.monthly_browser_seconds)
                if self.monthly_browser_seconds is not None else None
            ),
            "monthly_llm_tokens": (
                str(self.monthly_llm_tokens)
                if self.monthly_llm_tokens is not None else None
            ),
            "max_rps_default": self.max_rps_default,
            "retention_days": self.retention_days,
        }


# ── The default (unlimited) plan + reference tiers ──────────────────────────
DEFAULT_PLAN_NAME = "default"

#: The plan every un-provisioned tenant resolves to — EVERY limit unlimited, so
#: it can NEVER trip.  This is the invariant that keeps the existing suite green.
DEFAULT_PLAN = QuotaPlan(name=DEFAULT_PLAN_NAME)

#: Reference tiers — a STARTING POINT operators tune via ``QEC_QUOTA_PLANS``.  A
#: tenant receives one of these ONLY through an explicit ``QEC_TENANT_PLANS``
#: assignment; an unassigned tenant always resolves to :data:`DEFAULT_PLAN`, so
#: shipping these concrete numbers changes NO existing behaviour.  ``enterprise``
#: is unlimited by contract (bespoke terms) except for its politeness/retention
#: defaults.
BUILTIN_PLANS: dict[str, QuotaPlan] = {
    DEFAULT_PLAN_NAME: DEFAULT_PLAN,
    "starter": QuotaPlan(
        name="starter",
        max_apps=10,
        max_concurrent_cycles=2,
        max_concurrent_crawls=2,
        monthly_browser_seconds=Decimal("360000"),      # ~100 browser-hours/mo
        monthly_llm_tokens=Decimal("5000000"),
        max_rps_default=1.0,
        retention_days=30,
    ),
    "growth": QuotaPlan(
        name="growth",
        max_apps=100,
        max_concurrent_cycles=8,
        max_concurrent_crawls=4,
        monthly_browser_seconds=Decimal("3600000"),
        monthly_llm_tokens=Decimal("50000000"),
        max_rps_default=2.0,
        retention_days=90,
    ),
    "scale": QuotaPlan(
        name="scale",
        max_apps=1000,
        max_concurrent_cycles=32,
        max_concurrent_crawls=16,
        monthly_browser_seconds=Decimal("36000000"),
        monthly_llm_tokens=Decimal("500000000"),
        max_rps_default=4.0,
        retention_days=180,
    ),
    "enterprise": QuotaPlan(
        name="enterprise",
        max_rps_default=8.0,
        retention_days=365,
    ),
}


# ── Config-driven resolution (how a plan attaches to a tenant) ──────────────
def load_plan_registry(env: Optional[Mapping[str, str]] = None) -> dict[str, QuotaPlan]:
    """Build the ``{name: QuotaPlan}`` registry from :data:`BUILTIN_PLANS` +
    the ``QEC_QUOTA_PLANS`` overlay.

    A malformed ``QEC_QUOTA_PLANS`` (bad JSON / wrong shape / a single bad plan)
    is logged and IGNORED — the registry degrades to the built-ins rather than
    failing a request.  The ``default`` (unlimited) plan is always present.
    """
    source = os.environ if env is None else env
    registry: dict[str, QuotaPlan] = dict(BUILTIN_PLANS)

    raw = (source.get(ENV_QUOTA_PLANS) or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("qec.quota.plans_unparseable", extra={"env": ENV_QUOTA_PLANS})
            data = None
        if isinstance(data, Mapping):
            for name, spec in data.items():
                if not isinstance(spec, Mapping):
                    logger.warning("qec.quota.plan_shape_invalid", extra={"plan": str(name)})
                    continue
                try:
                    registry[str(name)] = QuotaPlan.from_mapping(str(name), spec)
                except Exception:  # noqa: BLE001 - one bad plan never breaks the rest
                    logger.warning("qec.quota.plan_invalid", extra={"plan": str(name)})

    # The unlimited default is non-negotiable — a deploy can never remove it.
    registry[DEFAULT_PLAN_NAME] = registry.get(DEFAULT_PLAN_NAME) or DEFAULT_PLAN
    return registry


def load_tenant_assignments(env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Parse ``QEC_TENANT_PLANS`` → ``{tenant_id: plan_name}`` (``{}`` on absent/bad)."""
    source = os.environ if env is None else env
    raw = (source.get(ENV_TENANT_PLANS) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("qec.quota.assignments_unparseable", extra={"env": ENV_TENANT_PLANS})
        return {}
    if not isinstance(data, Mapping):
        logger.warning("qec.quota.assignments_shape_invalid", extra={"env": ENV_TENANT_PLANS})
        return {}
    return {str(k): str(v).strip() for k, v in data.items() if str(v).strip()}


def resolve_plan(
    tenant_id: str,
    *,
    plan_name: Optional[str] = None,
    registry: Optional[Mapping[str, QuotaPlan]] = None,
    assignments: Optional[Mapping[str, str]] = None,
) -> QuotaPlan:
    """Resolve the :class:`QuotaPlan` in force for ``tenant_id``.

    Precedence: an explicit ``plan_name`` hint (a future seam — no current caller
    passes it) → the per-tenant ``QEC_TENANT_PLANS`` assignment → the unlimited
    :data:`DEFAULT_PLAN`.  An unknown plan name resolves to the default too (with
    a WARNING), so a typo/stale assignment can never wrongly tighten a tenant.

    ``registry`` / ``assignments`` may be injected (tests); otherwise they are
    parsed lazily from the environment at call time.
    """
    reg = registry if registry is not None else load_plan_registry()
    asg = assignments if assignments is not None else load_tenant_assignments()

    name = str(plan_name or asg.get(str(tenant_id)) or DEFAULT_PLAN_NAME)
    plan = reg.get(name)
    if plan is None:
        logger.warning(
            "qec.quota.unknown_plan",
            extra={"plan": name, "tenant_id": str(tenant_id)},
        )
        plan = reg.get(DEFAULT_PLAN_NAME, DEFAULT_PLAN)
    return plan


# ── The decision + the refusal ──────────────────────────────────────────────
@dataclass(frozen=True)
class QuotaDecision:
    """The (pure) outcome of one quota check.

    ``allowed`` ⇒ the resource is under its cap (or unlimited) and ``reason`` is
    empty.  Otherwise ``reason`` is the stable machine reason and ``limit`` /
    ``usage`` name the breached cap and the observed usage.  ``limit is None``
    means the resource was unlimited (the generous-default path).
    """

    allowed: bool
    resource: str
    reason: str = REASON_ALLOWED
    limit: Optional[Decimal] = None
    usage: Optional[Decimal] = None
    plan: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "resource": self.resource,
            "reason": self.reason,
            "limit": (str(self.limit) if self.limit is not None else None),
            "usage": (str(self.usage) if self.usage is not None else None),
            "plan": self.plan,
        }


class QuotaExceeded(Exception):
    """Raised by an ``enforce_*`` helper when a tenant is over a hard cap.

    Mirrors :class:`app.security.prod_guard.OnboardingRefused`: it carries an HTTP
    ``status_code`` (429 for a throughput/spend cap, 409 for the structural
    ``max_apps`` cap) and a structured, JSON-serialisable :meth:`as_http_detail`
    so a router maps it to a clean refusal without leaking internals.
    """

    def __init__(
        self,
        *,
        status_code: int = 429,
        resource: str = "",
        reason: str = "",
        plan: str = "",
        limit: Optional[Decimal] = None,
        usage: Optional[Decimal] = None,
        detail: Optional[str] = None,
    ) -> None:
        self.status_code = int(status_code)
        self.resource = str(resource)
        self.reason = str(reason)
        self.plan = str(plan)
        self.limit = limit
        self.usage = usage
        self.detail = detail or self._build_detail()
        super().__init__(self.detail)

    def _build_detail(self) -> str:
        cap = "" if self.limit is None else f" (limit={self.limit}, usage={self.usage})"
        return (
            f"quota exceeded for {self.resource!r} on plan {self.plan!r}"
            f"{cap} — {self.reason or 'over quota'}"
        )

    @classmethod
    def from_decision(cls, decision: QuotaDecision, *, status_code: Optional[int] = None) -> "QuotaExceeded":
        """Build a refusal from a denied :class:`QuotaDecision`."""
        code = status_code if status_code is not None else _RESOURCE_STATUS.get(decision.resource, 429)
        return cls(
            status_code=code,
            resource=decision.resource,
            reason=decision.reason,
            plan=decision.plan,
            limit=decision.limit,
            usage=decision.usage,
        )

    def as_http_detail(self) -> dict:
        """A structured HTTP ``detail`` body (never a bare string)."""
        return {
            "refused": True,
            "reason": "quota_exceeded",
            "resource": self.resource,
            "quota_reason": self.reason,
            "plan": self.plan,
            "limit": (str(self.limit) if self.limit is not None else None),
            "usage": (str(self.usage) if self.usage is not None else None),
            "message": self.detail,
        }


# ── The pure check (the heart — no DB, no env) ──────────────────────────────
def check_quota(plan: QuotaPlan, resource: str, current_usage: Any) -> QuotaDecision:
    """Decide whether ``resource`` is within ``plan``'s cap given ``current_usage``.

    FAIL-CLOSED at the boundary: admitting one more unit requires
    ``current_usage < limit`` — so ``usage >= limit`` DENIES.  For a counting
    resource (apps / concurrent cycles) ``current_usage`` is the CURRENT count
    (registering item #(N+1) when the cap is N ⇒ ``usage == N == limit`` ⇒ deny).
    For a monthly-spend resource it is the month-to-date sum (already at/over the
    cap ⇒ deny the next cycle).

    An UNLIMITED resource (``limit is None`` — the default-plan path) is *always*
    allowed and NEVER inspects ``current_usage``, so the generous default cannot
    trip regardless of what usage is passed.
    """
    limit = plan.limit_for(resource)  # raises on an unknown resource (loud typo guard)
    usage = _as_decimal(current_usage)

    if limit is None:
        return QuotaDecision(
            allowed=True, resource=resource, reason=REASON_ALLOWED,
            limit=None, usage=usage, plan=plan.name,
        )

    limit_dec = _as_decimal(limit)
    if usage >= limit_dec:
        return QuotaDecision(
            allowed=False, resource=resource,
            reason=_RESOURCE_REASON.get(resource, "quota_exceeded"),
            limit=limit_dec, usage=usage, plan=plan.name,
        )
    return QuotaDecision(
        allowed=True, resource=resource, reason=REASON_ALLOWED,
        limit=limit_dec, usage=usage, plan=plan.name,
    )


# ── Politeness-tier + retention helpers (pure) ──────────────────────────────
def effective_max_rps(plan: QuotaPlan, fence_max_rps: Any) -> Optional[float]:
    """The per-host politeness rate to enforce, applying the plan's rps TIER.

    An app's own ``fences.max_rps`` ALWAYS wins when it is a positive number;
    otherwise the plan's ``max_rps_default`` tier is applied.  Returns ``None``
    when neither is set — the admission gate then fail-closes (``rate_unconfigured``),
    exactly as today, so the default plan (``max_rps_default is None``) preserves
    the current fail-closed politeness posture.
    """
    try:
        fence = float(fence_max_rps) if fence_max_rps is not None else 0.0
    except (TypeError, ValueError):
        fence = 0.0
    if fence > 0:
        return fence
    return plan.max_rps_default


def retention_cutoff(plan: QuotaPlan, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """The retention CUTOFF for the plan's tier (evidence older than this is
    eligible for lifecycle expiry), or ``None`` when retention is unlimited.

    Pure — the data-lifecycle subsystem consumes this to bound a tenant's
    evidence footprint; ``None`` (the default) keeps every artifact forever,
    today's behaviour.
    """
    if plan.retention_days is None:
        return None
    base = now or utc_now()
    return base - timedelta(days=int(plan.retention_days))


# ── Usage aggregation (pure over ledger entries; reuses the meter) ──────────
def month_start(now: datetime) -> datetime:
    """The first instant of ``now``'s calendar month (same tz as ``now``)."""
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def sum_unit(
    entries: Iterable[Any],
    unit: str,
    *,
    window: Optional[tuple[Optional[datetime], Optional[datetime]]] = None,
) -> Decimal:
    """Sum the ``quantity`` of one metered ``unit`` across ledger ``entries``.

    Reuses the meter's pure aggregator (:func:`app.controlplane.cost.meter.aggregate_cost`)
    so the monthly-usage number is computed with the SAME accounting logic the
    cost snapshot + CI budget gate use.  ``window`` optionally filters by
    ``created_at``.  Gap flags (``unmetered_run``) are never summed as spend.
    """
    aggregates = meter.aggregate_cost(entries, group_by="unit", window=window)
    agg = aggregates.get(unit)
    if agg is None:
        return Decimal("0")
    return agg.units.get(unit, Decimal("0"))


# ── Async DB reads (tenant-scoped; only run when a limit actually applies) ──
async def _count_active_apps(session: AsyncSession, tenant_id: str) -> int:
    """Count a tenant's NON-deleted registered apps (RLS + explicit WHERE)."""
    result = await session.execute(
        select(func.count())
        .select_from(ClientAppRow)
        .where(
            ClientAppRow.tenant_id == tenant_id,
            ClientAppRow.status != "deleted",
        )
    )
    return int(result.scalar() or 0)


async def _count_active_cycles(session: AsyncSession, tenant_id: str) -> int:
    """Count a tenant's NON-terminal (in-flight) cycles across all apps."""
    result = await session.execute(
        select(func.count())
        .select_from(AppCycleRow)
        .where(
            AppCycleRow.tenant_id == tenant_id,
            AppCycleRow.state.notin_(tuple(sorted(TERMINAL_CYCLE_STATES))),
        )
    )
    return int(result.scalar() or 0)


#: Exploration statuses that are FINISHED and therefore hold no worker.
#: Everything else ("pending", "dispatched", "writing", ...) is in flight and
#: counts against the tenant's crawl concurrency.  Expressed as the TERMINAL set
#: rather than the active one so a status added later counts as active by
#: default - a new state that silently escaped the cap would be a quota hole,
#: while one that is briefly over-counted is merely conservative.
TERMINAL_EXPLORATION_STATUSES = frozenset({"completed", "failed", "refused", "stalled"})


async def _count_active_crawls(session: AsyncSession, tenant_id: str) -> int:
    """Count a tenant's NON-terminal (in-flight) explorations across all apps."""
    result = await session.execute(
        select(func.count())
        .select_from(QEExplorationRow)
        .where(
            QEExplorationRow.tenant_id == tenant_id,
            QEExplorationRow.status.notin_(
                tuple(sorted(TERMINAL_EXPLORATION_STATUSES))),
        )
    )
    return int(result.scalar() or 0)


async def _load_month_usage(
    session: AsyncSession, tenant_id: str, units: list[str], *, now: datetime,
) -> dict[str, Decimal]:
    """Sum this calendar month's metered ``units`` for the tenant (reuses meter).

    Loads only the relevant units' rows since the month start (RLS-scoped) and
    aggregates them with :func:`sum_unit`, so the monthly cap check reads the
    ledger through the same lens as every other cost surface.
    """
    start = month_start(now)
    result = await session.execute(
        select(CostLedgerRow).where(
            CostLedgerRow.tenant_id == tenant_id,
            CostLedgerRow.created_at >= start,
            CostLedgerRow.unit.in_(list(units)),
        )
    )
    rows = list(result.scalars().all())
    entries = meter.rows_to_entry_dicts(rows)
    return {u: sum_unit(entries, u) for u in units}


def _emit_and_maybe_refuse(decision: QuotaDecision, *, status_code: Optional[int] = None) -> None:
    """Emit the quota metric, and on a denial log + raise :class:`QuotaExceeded`.

    The metric helper is a hard no-op when prometheus is absent/disabled and
    never raises, so this is safe on every path (incl. the test env).
    """
    record_quota_decision(resource=decision.resource, allowed=decision.allowed)
    if decision.denied:
        logger.warning(
            "qec.quota.denied",
            extra={
                "resource": decision.resource,
                "plan": decision.plan,
                "reason": decision.reason,
                "limit": (str(decision.limit) if decision.limit is not None else None),
                "usage": (str(decision.usage) if decision.usage is not None else None),
            },
        )
        raise QuotaExceeded.from_decision(decision, status_code=status_code)


# ── Fail-closed enforcement (the crawl/cycle trigger + app-registry seams) ──
async def enforce_app_registration_quota(
    tenant_id: str,
    *,
    session: Optional[AsyncSession] = None,
    plan: Optional[QuotaPlan] = None,
) -> None:
    """Refuse a NEW app registration when the tenant is at its ``max_apps`` cap.

    Backward-compat SHORT-CIRCUIT: when the resolved plan leaves ``max_apps``
    unlimited (the default), this returns IMMEDIATELY — it opens no session and
    runs no query, so an un-provisioned tenant's registration path is byte-for-
    byte unchanged.  A limit bites only when a plan sets ``max_apps``; then the
    tenant's non-deleted app count is checked fail-closed and a breach raises
    :class:`QuotaExceeded` (409).

    ``session`` may be supplied to reuse an open tenant-scoped session; when
    omitted (the router path) one is opened ONLY if a limit actually applies.
    """
    plan = plan or resolve_plan(tenant_id)
    limit = plan.limit_for(RESOURCE_APPS)
    if limit is None:
        return  # unlimited — no query, no behaviour change (the default path)

    if session is not None:
        count = await _count_active_apps(session, tenant_id)
    else:
        async with tenant_scoped_qec_session(tenant_id) as s:
            count = await _count_active_apps(s, tenant_id)

    decision = check_quota(plan, RESOURCE_APPS, count)
    _emit_and_maybe_refuse(decision, status_code=409)


async def enforce_cycle_quota(
    tenant_id: str,
    *,
    session: Optional[AsyncSession] = None,
    plan: Optional[QuotaPlan] = None,
    now: Optional[datetime] = None,
) -> None:
    """Refuse a NEW cycle when the tenant is over a concurrency or monthly cap.

    Checks (each independently OPT-IN — an unset limit is skipped, so the default
    plan runs NO query and NEVER trips):
      1. ``max_concurrent_cycles`` — the tenant's non-terminal cycle count;
      2. ``monthly_browser_seconds`` / ``monthly_llm_tokens`` — this calendar
         month's metered spend for the tenant (the "monthly cost" caps).

    A breach raises :class:`QuotaExceeded` (429).  ``session`` is reused when
    supplied (the driver's open cycle-creation transaction); otherwise a
    tenant-scoped session is opened ONLY when at least one cap applies.
    """
    plan = plan or resolve_plan(tenant_id)
    now = now or utc_now()

    concurrency_capped = plan.limit_for(RESOURCE_CONCURRENT_CYCLES) is not None
    monthly_resources = [
        r for r in (RESOURCE_MONTHLY_BROWSER_SECONDS, RESOURCE_MONTHLY_LLM_TOKENS)
        if plan.limit_for(r) is not None
    ]
    if not concurrency_capped and not monthly_resources:
        return  # fully unlimited — no query, no behaviour change (default path)

    async def _run(s: AsyncSession) -> None:
        if concurrency_capped:
            count = await _count_active_cycles(s, tenant_id)
            _emit_and_maybe_refuse(
                check_quota(plan, RESOURCE_CONCURRENT_CYCLES, count), status_code=429,
            )
        if monthly_resources:
            units = [_RESOURCE_TO_METER_UNIT[r] for r in monthly_resources]
            usage = await _load_month_usage(s, tenant_id, units, now=now)
            for resource in monthly_resources:
                unit = _RESOURCE_TO_METER_UNIT[resource]
                _emit_and_maybe_refuse(
                    check_quota(plan, resource, usage.get(unit, Decimal("0"))),
                    status_code=429,
                )

    if session is not None:
        await _run(session)
    else:
        async with tenant_scoped_qec_session(tenant_id) as s:
            await _run(s)


async def enforce_crawl_quota(
    tenant_id: str,
    *,
    session: Optional[AsyncSession] = None,
    plan: Optional[QuotaPlan] = None,
    now: Optional[datetime] = None,
) -> None:
    """Refuse a NEW crawl dispatch when the tenant is over a crawl or spend cap.

    THE BYPASS THIS CLOSES (M3.4 / T-RS-03).  Quota enforcement existed and was
    wired into ``run_cycle`` - so the SCHEDULED path was capped and the DIRECT
    one was not.  ``POST /explorations`` and ``POST /explorations/{id}/resume``
    both reach a worker through ``_dispatch_explorer`` without ever creating a
    cycle, so a tenant at its monthly ceiling could still burn unlimited browser
    time by dispatching crawls straight at the fleet.  A cap that one of two
    doors ignores is not a cap.

    Enforced at the ONE choke point both doors pass through, which is what makes
    "another dispatch path" a contradiction rather than a gap to be re-audited:
    a new route that dispatches a crawl must go through ``_dispatch_explorer``
    to reach a worker, and it is checked there.

    Checks (each independently OPT-IN, exactly like :func:`enforce_cycle_quota`):
      1. ``max_concurrent_crawls`` - the tenant's non-terminal exploration count;
      2. ``monthly_browser_seconds`` / ``monthly_llm_tokens`` - this month's
         metered spend, which a crawl burns just as a cycle does.

    Backward-compat SHORT-CIRCUIT: the default plan leaves all of these
    unlimited, so an un-provisioned tenant opens no session, runs no query, and
    behaves byte-for-byte as before.  A breach raises :class:`QuotaExceeded`
    (429), which the router maps to an honest refusal.
    """
    plan = plan or resolve_plan(tenant_id)
    now = now or utc_now()

    crawl_capped = plan.limit_for(RESOURCE_CONCURRENT_CRAWLS) is not None
    monthly_resources = [
        r for r in (RESOURCE_MONTHLY_BROWSER_SECONDS, RESOURCE_MONTHLY_LLM_TOKENS)
        if plan.limit_for(r) is not None
    ]
    if not crawl_capped and not monthly_resources:
        return  # fully unlimited — no query, no behaviour change (default path)

    async def _run(s: AsyncSession) -> None:
        if crawl_capped:
            count = await _count_active_crawls(s, tenant_id)
            _emit_and_maybe_refuse(
                check_quota(plan, RESOURCE_CONCURRENT_CRAWLS, count), status_code=429,
            )
        if monthly_resources:
            units = [_RESOURCE_TO_METER_UNIT[r] for r in monthly_resources]
            usage = await _load_month_usage(s, tenant_id, units, now=now)
            for resource in monthly_resources:
                unit = _RESOURCE_TO_METER_UNIT[resource]
                _emit_and_maybe_refuse(
                    check_quota(plan, resource, usage.get(unit, Decimal("0"))),
                    status_code=429,
                )

    if session is not None:
        await _run(session)
    else:
        async with tenant_scoped_qec_session(tenant_id) as s:
            await _run(s)


__all__ = [
    # env knobs
    "ENV_QUOTA_PLANS",
    "ENV_TENANT_PLANS",
    # resources
    "RESOURCE_APPS",
    "RESOURCE_CONCURRENT_CYCLES",
    "RESOURCE_CONCURRENT_CRAWLS",
    "RESOURCE_MONTHLY_BROWSER_SECONDS",
    "RESOURCE_MONTHLY_LLM_TOKENS",
    "ALL_RESOURCES",
    # model + resolution
    "QuotaPlan",
    "DEFAULT_PLAN",
    "DEFAULT_PLAN_NAME",
    "BUILTIN_PLANS",
    "load_plan_registry",
    "load_tenant_assignments",
    "resolve_plan",
    # decision + refusal
    "QuotaDecision",
    "QuotaExceeded",
    "check_quota",
    # helpers
    "effective_max_rps",
    "retention_cutoff",
    "month_start",
    "sum_unit",
    # enforcement
    "enforce_app_registration_quota",
    "enforce_cycle_quota",
    "enforce_crawl_quota",
    "TERMINAL_EXPLORATION_STATUSES",
]
