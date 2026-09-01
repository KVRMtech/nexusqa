"""M3.1 / T-VIS-04 + T-VIS-03 — the vision GATE and the vision BUDGET.

The gate is a four-row truth table and all four rows are asserted, because the
row that was live in production is ``(not attested, tenant enabled)`` — a tenant
who had switched vision on could point a crawl at their real portal and have
full-page screenshots of real customers leave the container.

The budget is asserted as an ISOLATION property, not a counter: exhausting
vision must leave every other budget in the crawl untouched, which is the whole
of T-VIS-03.  It shared the medic oracle's cap, timeout and breaker before this
milestone, so a canvas page that burned ten perceive calls silently took ten
repair calls away from the DOM interaction ladder.
"""
from __future__ import annotations

import time

import pytest

from app.guard import Attestation
from app.vision_gate import (
    REASON_NO_ATTESTATION,
    REASON_OK,
    REASON_TENANT_DISABLED,
    RUNG_DISPOSABLE_ATTESTATION,
    RUNG_NONE,
    RUNG_SIGNED_PROOF,
    SPEND_BREAKER_OPEN,
    SPEND_CAP_REACHED,
    SPEND_GATE_CLOSED,
    SPEND_OK,
    VisionBudget,
    attestation_rung,
    closed_budget,
    decide_gate,
    gate_for_crawl,
)


# ── T-VIS-04 · the truth table, exhaustively ────────────────────────────────

@pytest.mark.parametrize("attested,tenant,expected_on,expected_reason", [
    (False, False, False, REASON_NO_ATTESTATION),
    (False, True,  False, REASON_NO_ATTESTATION),   # <- the row that shipped ON
    (True,  False, False, REASON_TENANT_DISABLED),
    (True,  True,  True,  REASON_OK),
])
def test_all_four_combinations(attested, tenant, expected_on, expected_reason):
    gate = decide_gate(attested=attested, tenant_enabled=tenant,
                       rung=RUNG_DISPOSABLE_ATTESTATION)
    assert gate.enabled is expected_on
    assert gate.reason == expected_reason
    # The decision carries its own inputs, so an audit re-derives it without
    # having to reconstruct the crawl.
    assert (gate.attested, gate.tenant_enabled) == (attested, tenant)


def test_a_refused_gate_names_the_missing_attestation_first():
    """When BOTH halves are missing the attestation is the reported cause.

    "we were pointed at an unattested target" is the finding an operator has to
    see; "the tenant flag was off" is the one they already know.
    """
    assert decide_gate(attested=False, tenant_enabled=False).reason == \
        REASON_NO_ATTESTATION


# ── the attestation ladder ──────────────────────────────────────────────────

def _fresh_disposable() -> Attestation:
    return Attestation(attested_by="ops", env_kind="disposable",
                       reset_procedure="rebuild",
                       expires_at_ms=int(time.time() * 1000) + 600_000)


def test_a_verified_signed_proof_is_the_strongest_rung():
    rung = attestation_rung(attestation=None, walk_authorization=object())
    assert rung == RUNG_SIGNED_PROOF


def test_an_unsigned_disposable_attestation_is_the_weaker_rung_and_says_so():
    """Accepted, but NAMED — so an audit reads "vision ran under an unsigned
    attestation" rather than "vision ran"."""
    gate = gate_for_crawl(tenant_enabled=True, attestation=_fresh_disposable())
    assert gate.enabled is True
    assert gate.rung == RUNG_DISPOSABLE_ATTESTATION


@pytest.mark.parametrize("att", [
    None,
    Attestation(attested_by="", env_kind="disposable", expires_at_ms=2 ** 41),
    Attestation(attested_by="ops", env_kind="production", expires_at_ms=2 ** 41),
    Attestation(attested_by="ops", env_kind="disposable", expires_at_ms=None),
    Attestation(attested_by="ops", env_kind="disposable", expires_at_ms=1),  # expired
])
def test_anything_that_is_not_a_disposable_attestation_attests_nothing(att):
    assert attestation_rung(attestation=att) == RUNG_NONE
    assert gate_for_crawl(tenant_enabled=True, attestation=att).enabled is False


def test_an_unreadable_attestation_is_no_attestation():
    """Fail-closed on our OWN error, not just on the caller's."""
    class _Explodes:
        def is_submit_capable(self, *a):
            raise RuntimeError("boom")

    assert attestation_rung(attestation=_Explodes()) == RUNG_NONE


# ── T-VIS-03 · the budget is the ONLY door ──────────────────────────────────

def _open_budget(**kw) -> VisionBudget:
    gate = decide_gate(attested=True, tenant_enabled=True,
                       rung=RUNG_DISPOSABLE_ATTESTATION)
    return VisionBudget(gate=gate, **kw)


def test_a_shut_gate_refuses_every_call_even_when_the_cap_is_generous():
    """The gate is enforced on the EXECUTION path, not by hiding a flag.

    A caller that forgot to check the gate still cannot spend, because spending
    goes through the object that holds it.
    """
    b = VisionBudget(gate=decide_gate(attested=False, tenant_enabled=True),
                     max_calls=1000)
    assert b.try_spend() == (False, SPEND_GATE_CLOSED)
    assert b.calls == 0


def test_the_cap_is_vision_s_own_and_is_reported_when_it_bites():
    b = _open_budget(max_calls=2)
    assert b.try_spend()[0] is True
    assert b.try_spend()[0] is True
    assert b.try_spend() == (False, SPEND_CAP_REACHED)
    assert b.telemetry()["refusals"] == {SPEND_CAP_REACHED: 1}


def test_repeated_failures_trip_the_breaker_and_it_never_re_closes():
    b = _open_budget(max_calls=100, breaker_threshold=3)
    for _ in range(3):
        assert b.try_spend()[0] is True
        b.note_failure()
    assert b.breaker_open is True
    assert b.try_spend() == (False, SPEND_BREAKER_OPEN)
    # A later success cannot re-close it: a provider that failed three times in
    # a row is not something to keep paying to re-discover, and a half-open
    # probe would reintroduce the unbounded spend the cap exists to prevent.
    b.note_success()
    assert b.breaker_open is True
    assert b.try_spend() == (False, SPEND_BREAKER_OPEN)


def test_an_intermittent_failure_does_not_trip_the_breaker():
    b = _open_budget(max_calls=100, breaker_threshold=3)
    for _ in range(10):
        b.note_failure()
        b.note_success()
    assert b.breaker_open is False
    assert b.failures == 10          # …and the failures are still counted


def test_vision_exhaustion_consumes_no_other_budget():
    """T-VIS-03's acceptance, stated as an isolation property.

    Two budgets, one shared threshold value, one of them driven to exhaustion.
    The other must be untouched — which it structurally was NOT before this
    milestone, because both read ``settings.medic_oracle_*``.
    """
    vision = _open_budget(max_calls=3, breaker_threshold=2)
    medic = _open_budget(max_calls=3, breaker_threshold=2)
    while vision.try_spend()[0]:
        vision.note_failure()
    assert vision.exhausted is True
    assert (medic.calls, medic.failures, medic.breaker_open) == (0, 0, False)
    assert medic.try_spend() == (True, SPEND_OK)


def test_the_telemetry_says_WHY_a_crawl_made_no_vision_calls():
    """An absence a reader has to interpret is not observability."""
    b = closed_budget()
    b.try_spend()
    t = b.telemetry()
    assert t["calls"] == 0
    assert t["refusals"] == {SPEND_GATE_CLOSED: 1}
    assert t["gate"]["enabled"] is False
    assert t["gate"]["attestation_rung"] == RUNG_NONE


def test_a_zero_cap_disables_vision_without_any_other_switch_changing():
    b = _open_budget(max_calls=0)
    assert b.gate.enabled is True          # the gate is open…
    assert b.try_spend() == (False, SPEND_CAP_REACHED)   # …and nothing can spend


# ── T-VIS-04 · THE GATE ON THE EXECUTION PATH, not on a UI ──────────────────
#
# "the tenant cannot see the toggle" is not a control. These assert the gate
# where it has to hold: in the object that performs the crawl, and in the
# callable that spends the money.

def _crawler_with(gate, tmp_path, *, oracle=object()):
    from app.config import Settings
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack

    pack = load_refuse_pack(Settings().refuse_pack_path)

    async def _oracle(*a, **kw):
        raise AssertionError("the gate let a vision call through")

    return Crawler(
        object(), crawl_id="gate", tenant_id="t", target_url="https://app/x",
        work_dir=str(tmp_path), refuse_pack=pack,
        budget=Budget(max_states=1, rate_per_s=0), explorer_version="t",
        guard_version="t", refuse_pack_version=pack.version,
        config_fingerprint="fp", guard_context=GuardContext(refuse_pack=pack),
        vision_oracle=_oracle,
        vision_budget=VisionBudget(gate=gate, max_calls=5),
    )


@pytest.mark.parametrize("attested,tenant,expect_escalation", [
    (False, False, False),
    (False, True,  False),
    (True,  False, False),
    (True,  True,  True),
])
def test_the_crawler_builds_an_escalation_only_on_the_ON_row(
        attested, tenant, expect_escalation, tmp_path):
    """A vision oracle can be WIRED on every row; only one row may USE it."""
    gate = decide_gate(attested=attested, tenant_enabled=tenant,
                       rung=RUNG_DISPOSABLE_ATTESTATION)
    crawler = _crawler_with(gate, tmp_path)
    assert (crawler._vision is not None) is expect_escalation
    # …and the budget travels with the crawl either way, so the coverage report
    # can always say WHY no vision call was made.
    assert crawler._vision_budget.gate.enabled is expect_escalation


def test_a_wrongly_wired_oracle_still_cannot_spend(tmp_path):
    """Defence in depth: the escalation is not built on a shut gate, AND the
    budget refuses every call, AND the oracle callable itself goes through the
    budget. Three independent doors, all shut by one decision."""
    import asyncio

    from app.main import _make_vision_oracle

    shut = decide_gate(attested=False, tenant_enabled=True)
    budget = VisionBudget(gate=shut, max_calls=99)

    class _Detonate:
        async def post(self, *a, **kw):
            raise AssertionError("a vision request reached the HTTP client")

    perceive = _make_vision_oracle(_Detonate(), "t", "c", None, budget=budget)
    assert asyncio.run(perceive("aGVsbG8=", {})) == {
        "controls": [], "displayed_values": []}
    assert budget.calls == 0
    assert budget.refusals == {SPEND_GATE_CLOSED: 1}


def test_the_default_budget_is_CLOSED(tmp_path):
    """An oracle built without a budget spends nothing.

    The capability is off unless something explicitly turned it on — so a future
    call site that forgets to pass a budget loses vision, never the gate.
    """
    import asyncio

    from app.main import _make_vision_oracle

    class _Detonate:
        async def post(self, *a, **kw):
            raise AssertionError("a vision request reached the HTTP client")

    perceive = _make_vision_oracle(_Detonate(), "t", "c", None)
    assert asyncio.run(perceive("aGVsbG8=", {}))["controls"] == []
