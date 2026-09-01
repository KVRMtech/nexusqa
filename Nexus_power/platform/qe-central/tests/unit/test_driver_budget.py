"""Phase-4 exit invariant (a): a budget breach ends a cycle in the terminal
``budget_stopped`` state — NEVER a partial ``done``.

Drives the DB-free :func:`execute_cycle` with a fully-fake CycleClient and a
zero wall-clock budget so the first phase-boundary assessment trips the guard,
plus a direct unit test of :class:`CycleBudget` for the wall-clock and metered
dimensions. No network, no database.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.controlplane.cost import meter
from app.controlplane.cycle.driver import (
    AppConfig,
    CycleBudget,
    _BudgetStop,
    execute_cycle,
    is_terminal_cycle_state,
    CYCLE_STATE_BUDGET_STOPPED,
    CYCLE_STATE_DONE,
)


# ─────────────────────────── a fully-benign fake ─────────────────────────


class _FakeClient:
    """Every CycleClient method returns a benign, well-shaped value so the
    state machine can walk regardless of call order."""

    async def fetch_journey_graph(self, *, tenant_id, artifact_id):
        return {"pages": []}

    async def get_rtm(self, *, tenant_id, artifact_id):
        return {"scripts": []}

    async def generate(self, *, tenant_id, artifact_id, answer_key=None):
        return {"generated": 0}

    async def run_playwright(self, *, tenant_id, artifact_id, test_ids, base_url, env_context=None):
        return {"run_id": "r1", "status": "queued"}

    async def poll_run(self, *, tenant_id, artifact_id, run_id):
        return {"run_id": run_id, "status": "completed", "failed": []}

    async def auto_heal(self, *, tenant_id, artifact_id, test_ids, base_url, env_context=None):
        return {"healed": []}

    async def verify(self, *, tenant_id, artifact_id, test_id, base_url):
        return {"certification_level": "CERTIFIED-EVIDENCED"}

    async def triage(self, *, tenant_id, artifact_id):
        return {"classes": {}}

    async def get_run(self, *, tenant_id, artifact_id, run_id):
        return {"run_id": run_id, "duration_ms": 1000}


def _app(budgets=None) -> AppConfig:
    return AppConfig(
        app_id="a1", tenant_id="t1", base_url="https://uat.example",
        canonical_host="example", max_rps=1.0, latest_artifact_id="art1",
        baseline_page_fingerprints={}, repo_binding={}, budgets=budgets or {},
        fences={},
    )


# ─────────────────────── execute_cycle → budget_stopped ───────────────────


def test_budget_breach_ends_in_budget_stopped_never_done():
    # A zero wall-clock budget: the FIRST phase-boundary assessment trips.
    budget = CycleBudget({meter.UNIT_WALLCLOCK_SECONDS: Decimal("0")})
    outcome = asyncio.run(execute_cycle(
        cycle_id="c1", mode="full", trigger="manual",
        app=_app(), client=_FakeClient(), budget=budget,
    ))
    assert outcome.state == CYCLE_STATE_BUDGET_STOPPED
    assert outcome.state != CYCLE_STATE_DONE
    assert is_terminal_cycle_state(outcome.state)


def test_generous_budget_allows_completion_to_a_terminal_state():
    # With no binding budget the same cycle reaches a NON-budget_stopped
    # terminal state (done/failed) — proving the stop is caused by the budget,
    # not by the machinery always stopping.
    outcome = asyncio.run(execute_cycle(
        cycle_id="c2", mode="full", trigger="manual",
        app=_app(), client=_FakeClient(),
        budget=CycleBudget({}, hard_wallclock_ceiling_s=3600.0),
    ))
    assert outcome.state != CYCLE_STATE_BUDGET_STOPPED
    assert is_terminal_cycle_state(outcome.state)


# ───────────────────────────── CycleBudget unit ──────────────────────────


def test_cyclebudget_wallclock_breach_raises():
    # First clock() call is consumed at construction (self._started); the next
    # is read inside assess(). started=0, assess-time=10 ⇒ elapsed 10 >= 5.
    ticks = iter([0.0, 10.0, 10.0])
    budget = CycleBudget({meter.UNIT_WALLCLOCK_SECONDS: Decimal("5")},
                         clock=lambda: next(ticks))
    with pytest.raises(_BudgetStop) as exc:
        budget.assess({}, phase="running")
    assert exc.value.breach.unit == meter.UNIT_WALLCLOCK_SECONDS
    assert exc.value.breach.phase == "running"


def test_cyclebudget_metered_unit_breach_raises():
    budget = CycleBudget({meter.UNIT_BROWSER_SECONDS: Decimal("100")},
                         hard_wallclock_ceiling_s=0.0)
    # under budget → no raise
    budget.assess({meter.UNIT_BROWSER_SECONDS: Decimal("50")}, phase="running")
    # over budget → raise
    with pytest.raises(_BudgetStop) as exc:
        budget.assess({meter.UNIT_BROWSER_SECONDS: Decimal("150")}, phase="running")
    assert exc.value.breach.unit == meter.UNIT_BROWSER_SECONDS


def test_cyclebudget_missing_dimension_is_unbounded():
    # A unit with no budget set is never a false stop.
    budget = CycleBudget({meter.UNIT_BROWSER_SECONDS: Decimal("10")},
                         hard_wallclock_ceiling_s=0.0)
    budget.assess({meter.UNIT_LLM_TOKENS: Decimal("999999")}, phase="running")  # no raise
