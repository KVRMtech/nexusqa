"""QE-Central S5 — budget-gate pure-logic tests (no DB, no network).

Pins the CI budget gate (``scripts/qec_budget_gate.py``, design §7):

  * :func:`parse_budget_mapping` refuses unknown / negative budgets;
  * :func:`load_budgets` reads a JSON file OR ``QEC_BUDGET_*`` env vars;
  * :func:`evaluate_budget` flags a cycle whose metered unit exceeds its budget;
  * ``main`` returns a NON-ZERO exit code on a drift, and ``0`` within budget —
    driven with a monkeypatched ledger loader so no database is required.

An ``unmetered_run`` budget is also exercised: the meter can only under-count,
so budgeting the gap flag turns a metering hole into a hard CI failure.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from app.controlplane.cost.meter import (
    UNIT_BROWSER_SECONDS,
    UNIT_UNMETERED_RUN,
    aggregate_cost,
)

# ── Load the repo-root gate script by absolute path (not on sys.path) ──────
_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TEST_DIR, "..", "..", "..", ".."))
_GATE_PATH = os.path.join(_REPO_ROOT, "scripts", "qec_budget_gate.py")

_spec = importlib.util.spec_from_file_location("qec_budget_gate", _GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
sys.modules["qec_budget_gate"] = gate
_spec.loader.exec_module(gate)


def _entry(unit, qty, *, cycle="c1"):
    from datetime import datetime, timezone
    from decimal import Decimal

    return {
        "tenant_id": "t1", "app_id": "app1", "cycle_id": cycle, "unit": unit,
        "quantity": Decimal(str(qty)), "unit_cost_usd": None,
        "created_at": datetime(2026, 7, 8, tzinfo=timezone.utc),
    }


# ── parse_budget_mapping ───────────────────────────────────────────────────
class TestParseBudgetMapping:
    def test_valid_units_parsed(self):
        from decimal import Decimal

        budgets = gate.parse_budget_mapping({UNIT_BROWSER_SECONDS: "600", "runner_runs": 5})
        assert budgets[UNIT_BROWSER_SECONDS] == Decimal("600")
        assert budgets["runner_runs"] == Decimal("5")

    def test_unknown_unit_refused(self):
        with pytest.raises(ValueError, match="unknown budget unit"):
            gate.parse_budget_mapping({"gpu_hours": 10})

    def test_negative_budget_refused(self):
        with pytest.raises(ValueError, match="negative"):
            gate.parse_budget_mapping({UNIT_BROWSER_SECONDS: -1})


# ── load_budgets ───────────────────────────────────────────────────────────
class TestLoadBudgets:
    def test_from_env_vars(self):
        env = {"QEC_BUDGET_BROWSER_SECONDS": "600", "QEC_BUDGET_RUNNER_RUNS": "3",
               "PATH": "irrelevant"}
        budgets = gate.load_budgets(None, env)
        assert set(budgets) == {UNIT_BROWSER_SECONDS, "runner_runs"}

    def test_reserved_env_control_vars_ignored(self):
        env = {"QEC_BUDGET_FILE": "x", "QEC_BUDGET_TENANT": "t1",
               "QEC_BUDGET_WINDOW_HOURS": "24", "QEC_BUDGET_BROWSER_SECONDS": "600"}
        budgets = gate.load_budgets(None, env)
        assert set(budgets) == {UNIT_BROWSER_SECONDS}

    def test_from_json_file_takes_precedence(self, tmp_path):
        import json
        from decimal import Decimal

        path = tmp_path / "budget.json"
        path.write_text(json.dumps({UNIT_BROWSER_SECONDS: 100}))
        # File wins even though the env also sets a (different) budget.
        budgets = gate.load_budgets(str(path), {"QEC_BUDGET_BROWSER_SECONDS": "999"})
        assert budgets[UNIT_BROWSER_SECONDS] == Decimal("100")

    def test_nothing_configured_is_empty(self):
        assert gate.load_budgets(None, {"PATH": "x"}) == {}


# ── evaluate_budget ────────────────────────────────────────────────────────
class TestEvaluateBudget:
    def test_within_budget_is_ok(self):
        from decimal import Decimal

        aggregates = aggregate_cost(
            [_entry(UNIT_BROWSER_SECONDS, "500", cycle="c1")], group_by="cycle_id",
        )
        verdict = gate.evaluate_budget(aggregates, {UNIT_BROWSER_SECONDS: Decimal("600")})
        assert verdict.ok is True
        assert verdict.breaches == []
        assert verdict.checked_cycles == 1

    def test_over_budget_is_a_breach(self):
        from decimal import Decimal

        aggregates = aggregate_cost(
            [_entry(UNIT_BROWSER_SECONDS, "400", cycle="c1"),
             _entry(UNIT_BROWSER_SECONDS, "350", cycle="c1")],
            group_by="cycle_id",
        )
        verdict = gate.evaluate_budget(aggregates, {UNIT_BROWSER_SECONDS: Decimal("600")})
        assert verdict.ok is False
        assert len(verdict.breaches) == 1
        b = verdict.breaches[0]
        assert b.cycle_id == "c1" and b.unit == UNIT_BROWSER_SECONDS
        assert b.quantity == Decimal("750") and b.budget == Decimal("600")

    def test_equal_to_budget_is_not_a_breach(self):
        from decimal import Decimal

        aggregates = aggregate_cost(
            [_entry(UNIT_BROWSER_SECONDS, "600", cycle="c1")], group_by="cycle_id",
        )
        verdict = gate.evaluate_budget(aggregates, {UNIT_BROWSER_SECONDS: Decimal("600")})
        assert verdict.ok is True

    def test_unmetered_run_budget_catches_metering_gap(self):
        from decimal import Decimal

        aggregates = aggregate_cost(
            [_entry(UNIT_UNMETERED_RUN, 1, cycle="c1"),
             _entry(UNIT_UNMETERED_RUN, 1, cycle="c1"),
             _entry(UNIT_UNMETERED_RUN, 1, cycle="c1")],
            group_by="cycle_id",
        )
        verdict = gate.evaluate_budget(aggregates, {UNIT_UNMETERED_RUN: Decimal("0")})
        assert verdict.ok is False
        assert verdict.breaches[0].quantity == Decimal("3")


# ── main() exit codes (monkeypatched ledger loader — no DB) ─────────────────
class TestMainExitCodes:
    @staticmethod
    def _clean_env(monkeypatch):
        for var in ("QEC_BUDGET_FILE", "QEC_BUDGET_TENANT", "QEC_BUDGET_WINDOW_HOURS"):
            monkeypatch.delenv(var, raising=False)

    def _patch_ledger(self, monkeypatch, entries):
        async def _fake_loader(tenant_id, since):
            return entries
        monkeypatch.setattr(gate, "_load_ledger_entries", _fake_loader)

    def test_exit_nonzero_on_drift(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("QEC_BUDGET_BROWSER_SECONDS", "600")
        self._patch_ledger(monkeypatch, [
            _entry(UNIT_BROWSER_SECONDS, "400", cycle="c1"),
            _entry(UNIT_BROWSER_SECONDS, "350", cycle="c1"),
        ])
        assert gate.main(["--tenant", "t1"]) == gate.EXIT_BREACH

    def test_exit_zero_within_budget(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("QEC_BUDGET_BROWSER_SECONDS", "600")
        self._patch_ledger(monkeypatch, [_entry(UNIT_BROWSER_SECONDS, "500", cycle="c1")])
        assert gate.main(["--tenant", "t1"]) == gate.EXIT_OK

    def test_no_budgets_configured_passes(self, monkeypatch):
        self._clean_env(monkeypatch)
        for var in list(os.environ):
            if var.startswith("QEC_BUDGET_"):
                monkeypatch.delenv(var, raising=False)
        assert gate.main(["--tenant", "t1"]) == gate.EXIT_OK

    def test_missing_tenant_is_config_error(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("QEC_BUDGET_BROWSER_SECONDS", "600")
        assert gate.main([]) == gate.EXIT_CONFIG_ERROR

    def test_unknown_budget_unit_is_config_error(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("QEC_BUDGET_GPU_HOURS", "10")
        assert gate.main(["--tenant", "t1"]) == gate.EXIT_CONFIG_ERROR
