#!/usr/bin/env python3
"""QE-Central S5 — CI budget gate (design §3.5 / §7).

Exits NON-ZERO when the metered cost-per-cycle exceeds a configured budget, so a
control-plane change that quietly makes cycles more expensive fails the build
instead of shipping.  It reads the append-only ``cost_ledger`` (the SAME rows
the cost meter writes), aggregates by cycle over an optional time window, and
compares each budgeted unit against the aggregate.

Two layers, deliberately separated:

  * :func:`evaluate_budget` + :func:`parse_budget_mapping` + :func:`load_budgets`
    — the PURE core.  No DB, no network, no clock; unit-tested directly.  A
    breach is any cycle whose metered ``unit`` quantity strictly exceeds its
    budget.  ``unmetered_run`` may itself be budgeted, turning a metering-gap
    (runs the meter could not correlate) into a hard CI failure — so the
    can-only-under-count meter cannot hide spend by dropping runs.

  * :func:`main` — the thin DB shell.  Loads the ledger through the tenant-scoped
    qecentral session, calls the pure core, prints a report, returns the exit
    code.

Budgets are CONFIG-DRIVEN (never hardcoded): a JSON file (``--budget-file`` /
``QEC_BUDGET_FILE``) mapping unit → per-cycle max, or individual
``QEC_BUDGET_<UNIT>`` environment variables (e.g. ``QEC_BUDGET_BROWSER_SECONDS``).

Exit codes:  0 = within budget (or nothing to enforce);  1 = a budget breach;
2 = a usage/config/ledger error (never confused with a clean pass).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
from typing import Any, Optional, Sequence


# ── Make the qe-central ``app`` package importable (single source of truth for
#    the cost units + aggregator) ─────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
_QEC_ROOT = os.path.join(_REPO_ROOT, "platform", "qe-central")
_SDK_PATH = os.path.join(_REPO_ROOT, "sdk", "nexus-sdk")
for _p in (_QEC_ROOT, _SDK_PATH):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Importing the meter pulls in ``app.db`` (async engines are created LAZILY — no
# connection at import), so the pure core below is importable without a live DB.
from app.controlplane.cost.meter import (  # noqa: E402
    ALL_COST_UNITS,
    UNIT_UNMETERED_RUN,
    aggregate_cost,
)

GATE_NAME = "qec-budget-gate"
BUDGET_ENV_PREFIX = "QEC_BUDGET_"

EXIT_OK = 0
EXIT_BREACH = 1
EXIT_CONFIG_ERROR = 2


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"budget must be numeric, not bool: {value!r}")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"budget is not numeric: {value!r}") from exc


# ── Budget parsing (pure) ──────────────────────────────────────────────────
def parse_budget_mapping(raw: Mapping[str, Any]) -> dict[str, Decimal]:
    """Validate + normalise a ``{unit: max}`` mapping into ``{unit: Decimal}``.

    Every key MUST be a known cost unit (:data:`ALL_COST_UNITS`) — an unknown
    unit is a configuration error (a typo would silently disable a limit).
    Every value MUST be numeric and ``>= 0``.
    """
    budgets: dict[str, Decimal] = {}
    for unit, limit in raw.items():
        key = str(unit)
        if key not in ALL_COST_UNITS:
            raise ValueError(
                f"unknown budget unit {key!r}; expected one of {sorted(ALL_COST_UNITS)}"
            )
        value = _to_decimal(limit)
        if value < 0:
            raise ValueError(f"budget for {key!r} is negative ({value})")
        budgets[key] = value
    return budgets


def load_budgets(
    budget_file: Optional[str], env: Mapping[str, str],
) -> dict[str, Decimal]:
    """Load budgets from a JSON file if given, else from ``QEC_BUDGET_<UNIT>`` env.

    File precedence: an explicit/existing ``budget_file`` wins; otherwise every
    ``QEC_BUDGET_<UNIT>`` variable (case-insensitive unit) is read.  Returns an
    empty mapping when nothing is configured — the gate then enforces nothing and
    passes (honestly reported, never a silent skip of a real limit).
    """
    if budget_file:
        with open(budget_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("budget file must be a JSON object of {unit: max}")
        return parse_budget_mapping(data)

    raw: dict[str, Any] = {}
    for name, value in env.items():
        if not name.startswith(BUDGET_ENV_PREFIX):
            continue
        unit = name[len(BUDGET_ENV_PREFIX):].lower()
        if unit in ("file", "tenant", "window_hours"):  # reserved control vars
            continue
        raw[unit] = value
    return parse_budget_mapping(raw)


# ── Budget evaluation (pure) ───────────────────────────────────────────────
@dataclass(frozen=True)
class BudgetBreach:
    """One (cycle, unit) that exceeded its per-cycle budget."""

    cycle_id: str
    unit: str
    quantity: Decimal
    budget: Decimal


@dataclass(frozen=True)
class BudgetVerdict:
    """The gate verdict.  ``ok`` is True iff no cycle breached any budget."""

    ok: bool
    breaches: list[BudgetBreach]
    checked_cycles: int
    checked_units: tuple[str, ...]


def _aggregate_quantity(aggregate: Any, unit: str) -> Decimal:
    """Read one unit's quantity from a :class:`CostAggregate` (duck-typed).

    ``unmetered_run`` is the count of metering-gap flags (``.unmetered_runs``);
    every other unit is a metered sum (``.units[unit]``).
    """
    if unit == UNIT_UNMETERED_RUN:
        return _to_decimal(getattr(aggregate, "unmetered_runs", 0) or 0)
    units = getattr(aggregate, "units", {}) or {}
    return _to_decimal(units.get(unit, 0) or 0)


def evaluate_budget(
    aggregates: Mapping[str, Any], budgets: Mapping[str, Decimal],
) -> BudgetVerdict:
    """Compare each cycle aggregate against ``budgets`` (strictly-greater = breach).

    Pure: ``aggregates`` maps cycle_id → an object exposing ``units`` (dict) and
    ``unmetered_runs`` (int) — i.e. :class:`app.controlplane.cost.meter.CostAggregate`.
    Deterministic ordering (cycle, then unit) makes CI output stable.
    """
    breaches: list[BudgetBreach] = []
    for cycle_id in sorted(aggregates):
        aggregate = aggregates[cycle_id]
        for unit in sorted(budgets):
            limit = budgets[unit]
            quantity = _aggregate_quantity(aggregate, unit)
            if quantity > limit:
                breaches.append(BudgetBreach(str(cycle_id), unit, quantity, limit))
    return BudgetVerdict(
        ok=not breaches,
        breaches=breaches,
        checked_cycles=len(aggregates),
        checked_units=tuple(sorted(budgets)),
    )


def format_verdict(verdict: BudgetVerdict, budgets: Mapping[str, Decimal]) -> str:
    """Render a human + CI-log friendly report of the verdict."""
    lines = [
        f"{GATE_NAME}: checked {verdict.checked_cycles} cycle(s) against "
        f"{len(budgets)} budget(s) {sorted(budgets)}"
    ]
    if verdict.ok:
        lines.append(f"{GATE_NAME}: OK — every cycle is within budget")
    else:
        lines.append(f"{GATE_NAME}: FAIL — {len(verdict.breaches)} breach(es):")
        for b in verdict.breaches:
            lines.append(
                f"  cycle={b.cycle_id} unit={b.unit} "
                f"metered={b.quantity} > budget={b.budget}"
            )
    return "\n".join(lines)


# ── DB shell (impure — only :func:`main` touches it) ───────────────────────
def _window_start(window_hours: Optional[float]) -> Optional[datetime]:
    if window_hours is None or window_hours <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(hours=float(window_hours))


async def _load_ledger_entries(tenant_id: str, since: Optional[datetime]) -> list[dict]:
    """Load this tenant's ledger rows (tenant-scoped, RLS) as aggregator dicts."""
    from sqlalchemy import select

    from app.controlplane.cost.meter import rows_to_entry_dicts
    from app.db import tenant_scoped_qec_session
    from app.db.controlplane_models import CostLedgerRow

    async with tenant_scoped_qec_session(tenant_id) as session:
        query = select(CostLedgerRow).where(CostLedgerRow.tenant_id == tenant_id)
        if since is not None:
            query = query.where(CostLedgerRow.created_at >= since)
        rows = (await session.execute(query)).scalars().all()
    return rows_to_entry_dicts(list(rows))


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=GATE_NAME,
        description="Fail CI when metered cost-per-cycle exceeds the configured budget.",
    )
    parser.add_argument(
        "--budget-file", default=os.getenv("QEC_BUDGET_FILE", ""),
        help="JSON file mapping cost unit -> per-cycle max (else QEC_BUDGET_* env vars).",
    )
    parser.add_argument(
        "--tenant", default=os.getenv("QEC_BUDGET_TENANT", ""),
        help="Tenant whose ledger is checked (RLS-scoped). Required for the DB read.",
    )
    parser.add_argument(
        "--window-hours", type=float,
        default=float(os.getenv("QEC_BUDGET_WINDOW_HOURS", "0") or 0) or None,
        help="Only consider ledger entries newer than this many hours (default: all).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.  Returns the process exit code (never raises to the shell)."""
    args = _parse_args(argv)

    try:
        budgets = load_budgets(args.budget_file or None, os.environ)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"{GATE_NAME}: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if not budgets:
        print(
            f"{GATE_NAME}: no budgets configured (set QEC_BUDGET_* or --budget-file); "
            "nothing to enforce",
        )
        return EXIT_OK

    tenant = (args.tenant or "").strip()
    if not tenant:
        print(
            f"{GATE_NAME}: --tenant (or QEC_BUDGET_TENANT) is required to read the ledger",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    import asyncio

    since = _window_start(args.window_hours)
    try:
        entries = asyncio.run(_load_ledger_entries(tenant, since))
    except Exception as exc:  # DB unreachable / schema mismatch — a config error
        print(f"{GATE_NAME}: could not read the cost ledger: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    aggregates = aggregate_cost(entries, group_by="cycle_id", window=(since, None))
    verdict = evaluate_budget(aggregates, budgets)
    print(format_verdict(verdict, budgets))
    return EXIT_OK if verdict.ok else EXIT_BREACH


if __name__ == "__main__":
    raise SystemExit(main())
