"""QE-Central S5 — cost-meter pure-logic tests (no DB, no network).

Pins the two honesty invariants of the meter (design §3.5 / §7 honesty gate):

  * **can only UNDER-count** — an uncorrelated run NEVER inflates
    ``browser_seconds``; it is recorded as an ``unmetered_run`` gap flag, and
    adding one to a batch leaves the metered seconds unchanged;
  * **RAW UNITS first** — an aggregate publishes a USD figure only when every
    contributing entry is priced; a mix ⇒ ``usd is None`` (never invented).

Plus the append-only guards: an unknown unit or a NEGATIVE quantity is refused
before any row is built.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.controlplane.cost.meter import (
    UNIT_BROWSER_SECONDS,
    UNIT_LLM_TOKENS,
    UNIT_RUNNER_RUNS,
    UNIT_UNMETERED_RUN,
    aggregate_cost,
    browser_seconds_from_run,
    build_cost_entries,
    reconcile_runs,
)


@dataclass
class _FakeRun:
    """An object-shaped run record (the meter must accept dicts AND objects)."""

    run_id: str
    duration_ms: int


# ── browser_seconds_from_run ───────────────────────────────────────────────
class TestBrowserSecondsFromRun:
    def test_correlated_dict_meters_seconds(self):
        s = browser_seconds_from_run({"run_id": "r1", "duration_ms": 4200})
        assert s.unmetered is False
        assert s.run_id == "r1"
        assert s.browser_seconds == Decimal("4.2")

    def test_correlated_object_meters_seconds(self):
        s = browser_seconds_from_run(_FakeRun(run_id="r2", duration_ms=1500))
        assert s.unmetered is False
        assert s.browser_seconds == Decimal("1.5")

    def test_zero_duration_is_correlated_zero_not_a_gap(self):
        # We HAVE the run record — an honest 0 seconds, never an unmetered gap.
        s = browser_seconds_from_run({"run_id": "r3", "duration_ms": 0})
        assert s.unmetered is False
        assert s.browser_seconds == Decimal("0")

    def test_none_record_is_unmetered(self):
        s = browser_seconds_from_run(None)
        assert s.unmetered is True
        assert s.browser_seconds is None
        assert s.reason == "missing_run_record"

    def test_missing_run_id_is_unmetered(self):
        s = browser_seconds_from_run({"duration_ms": 100})
        assert s.unmetered is True
        assert s.reason == "missing_run_id"

    def test_missing_duration_is_unmetered(self):
        s = browser_seconds_from_run({"run_id": "r4"})
        assert s.unmetered is True
        assert s.reason == "missing_duration"

    def test_non_numeric_duration_is_unmetered(self):
        s = browser_seconds_from_run({"run_id": "r5", "duration_ms": "abc"})
        assert s.unmetered is True
        assert s.reason == "non_numeric_duration"

    def test_negative_duration_is_unmetered_never_negative_cost(self):
        s = browser_seconds_from_run({"run_id": "r6", "duration_ms": -10})
        assert s.unmetered is True
        assert s.reason == "negative_duration"
        assert s.browser_seconds is None


# ── reconcile_runs — the can-only-under-count property ─────────────────────
class TestReconcileRunsUnderCount:
    def test_all_correlated_sums_exactly(self):
        runs = [
            {"run_id": "a", "duration_ms": 1000},
            {"run_id": "b", "duration_ms": 2500},
        ]
        rec = reconcile_runs(runs)
        assert rec.metered_browser_seconds == Decimal("3.5")
        assert rec.metered_runs == 2
        assert rec.unmetered_runs == 0

    def test_adding_uncorrelated_run_does_not_change_metered_seconds(self):
        base = [{"run_id": "a", "duration_ms": 1000}]
        before = reconcile_runs(base)

        # An uncorrelated run (no run_id) is added to the SAME batch.
        after = reconcile_runs(base + [{"duration_ms": 9_999_999}])

        # THE property: metered seconds are unchanged; only the gap count grows.
        assert after.metered_browser_seconds == before.metered_browser_seconds
        assert after.unmetered_runs == before.unmetered_runs + 1
        assert after.metered_runs == before.metered_runs

    def test_metered_is_a_lower_bound_on_true_time(self):
        # Two real runs (3s total) + one uncorrelated run that truly took a long
        # time. The meter reports only the 3s it can prove — a strict lower bound.
        runs = [
            {"run_id": "a", "duration_ms": 1000},
            {"run_id": "b", "duration_ms": 2000},
            {"duration_ms": 60_000},  # uncorrelated: real, but unprovable
        ]
        rec = reconcile_runs(runs)
        assert rec.metered_browser_seconds == Decimal("3")
        assert rec.unmetered_runs == 1

    def test_garbage_record_is_counted_as_a_gap_never_raises(self):
        rec = reconcile_runs([{"run_id": "a", "duration_ms": None}, 12345, object()])
        assert rec.metered_runs == 0
        assert rec.unmetered_runs == 3
        assert rec.metered_browser_seconds == Decimal("0")


# ── build_cost_entries — append-only guards ────────────────────────────────
class TestBuildCostEntries:
    def test_builds_one_entry_per_unit(self):
        entries = build_cost_entries(
            tenant_id="t1", app_id="app1", cycle_id="c1",
            units={UNIT_BROWSER_SECONDS: Decimal("4.2"), UNIT_RUNNER_RUNS: 1},
            source_ref="run-9",
        )
        by_unit = {e["unit"]: e for e in entries}
        assert set(by_unit) == {UNIT_BROWSER_SECONDS, UNIT_RUNNER_RUNS}
        assert by_unit[UNIT_BROWSER_SECONDS]["quantity"] == Decimal("4.2")
        assert by_unit[UNIT_RUNNER_RUNS]["quantity"] == Decimal("1")
        assert all(e["tenant_id"] == "t1" and e["cycle_id"] == "c1" for e in entries)
        assert all(e["source_ref"] == "run-9" for e in entries)
        assert all(e["unit_cost_usd"] is None for e in entries)  # raw units first

    def test_prices_applied_when_supplied(self):
        entries = build_cost_entries(
            tenant_id="t1", units={UNIT_BROWSER_SECONDS: 10},
            unit_cost_usd={UNIT_BROWSER_SECONDS: Decimal("0.001")},
        )
        assert entries[0]["unit_cost_usd"] == Decimal("0.001")

    def test_unknown_unit_refused(self):
        with pytest.raises(ValueError, match="unknown cost unit"):
            build_cost_entries(tenant_id="t1", units={"gpu_hours": 3})

    def test_negative_quantity_refused(self):
        with pytest.raises(ValueError, match="negative"):
            build_cost_entries(tenant_id="t1", units={UNIT_BROWSER_SECONDS: Decimal("-1")})

    def test_bool_quantity_refused(self):
        with pytest.raises(ValueError):
            build_cost_entries(tenant_id="t1", units={UNIT_RUNNER_RUNS: True})

    def test_empty_tenant_refused(self):
        with pytest.raises(ValueError, match="tenant_id is required"):
            build_cost_entries(tenant_id="", units={UNIT_RUNNER_RUNS: 1})


# ── aggregate_cost ─────────────────────────────────────────────────────────
def _entry(unit, qty, *, cycle="c1", app="app1", usd=None, at=None):
    return {
        "tenant_id": "t1", "app_id": app, "cycle_id": cycle, "unit": unit,
        "quantity": Decimal(str(qty)), "unit_cost_usd": usd,
        "created_at": at or datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc),
    }


class TestAggregateCost:
    def test_sums_metered_units_per_cycle(self):
        entries = [
            _entry(UNIT_BROWSER_SECONDS, "4.2", cycle="c1"),
            _entry(UNIT_BROWSER_SECONDS, "1.8", cycle="c1"),
            _entry(UNIT_BROWSER_SECONDS, "5.0", cycle="c2"),
        ]
        agg = aggregate_cost(entries, group_by="cycle_id")
        assert agg["c1"].units[UNIT_BROWSER_SECONDS] == Decimal("6.0")
        assert agg["c2"].units[UNIT_BROWSER_SECONDS] == Decimal("5.0")

    def test_unmetered_runs_counted_separately_not_in_units(self):
        entries = [
            _entry(UNIT_BROWSER_SECONDS, "3.0", cycle="c1"),
            _entry(UNIT_UNMETERED_RUN, 1, cycle="c1"),
            _entry(UNIT_UNMETERED_RUN, 1, cycle="c1"),
        ]
        agg = aggregate_cost(entries, group_by="cycle_id")["c1"]
        assert agg.unmetered_runs == 2
        assert UNIT_UNMETERED_RUN not in agg.units
        assert agg.units[UNIT_BROWSER_SECONDS] == Decimal("3.0")

    def test_usd_only_when_fully_priced(self):
        priced = [
            _entry(UNIT_BROWSER_SECONDS, 10, usd=Decimal("0.001")),
            _entry(UNIT_LLM_TOKENS, 1000, usd=Decimal("0.000002")),
        ]
        agg = aggregate_cost(priced, group_by="cycle_id")["c1"]
        assert agg.usd == Decimal("10") * Decimal("0.001") + Decimal("1000") * Decimal("0.000002")

    def test_usd_none_when_any_entry_unpriced(self):
        mixed = [
            _entry(UNIT_BROWSER_SECONDS, 10, usd=Decimal("0.001")),
            _entry(UNIT_LLM_TOKENS, 1000, usd=None),  # unpriced ⇒ no invented dollars
        ]
        agg = aggregate_cost(mixed, group_by="cycle_id")["c1"]
        assert agg.usd is None

    def test_window_filters_by_created_at(self):
        old = datetime(2026, 7, 1, tzinfo=timezone.utc)
        new = datetime(2026, 7, 8, tzinfo=timezone.utc)
        entries = [
            _entry(UNIT_BROWSER_SECONDS, "1.0", at=old),
            _entry(UNIT_BROWSER_SECONDS, "2.0", at=new),
        ]
        since = new - timedelta(hours=1)
        agg = aggregate_cost(entries, group_by="cycle_id", window=(since, None))
        assert agg["c1"].units[UNIT_BROWSER_SECONDS] == Decimal("2.0")

    def test_group_by_none_rolls_into_all(self):
        entries = [_entry(UNIT_BROWSER_SECONDS, "1.0", cycle="c1"),
                   _entry(UNIT_BROWSER_SECONDS, "2.0", cycle="c2")]
        agg = aggregate_cost(entries, group_by="none")
        assert set(agg) == {"all"}
        assert agg["all"].units[UNIT_BROWSER_SECONDS] == Decimal("3.0")

    def test_invalid_group_by_raises(self):
        with pytest.raises(ValueError, match="group_by"):
            aggregate_cost([], group_by="banana")
