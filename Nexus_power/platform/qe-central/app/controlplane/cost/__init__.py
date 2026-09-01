"""QE-Central S5 — the append-only cost meter (design §3.5).

Raw units first, dollars only when a rate card is configured, and a HARD
honesty invariant: the meter can only UNDER-count.  An uncorrelated runner run
becomes an ``unmetered_run`` gap flag — never invented ``browser_seconds``.
"""
from .meter import (  # noqa: F401
    ALL_COST_UNITS,
    GAP_UNITS,
    METERED_UNITS,
    UNIT_BROWSER_SECONDS,
    UNIT_LLM_TOKENS,
    UNIT_RUNNER_RUNS,
    UNIT_SUBSTRATE_ROWS,
    UNIT_UNMETERED_RUN,
    UNIT_WALLCLOCK_SECONDS,
    CostAggregate,
    RunCostSample,
    RunReconciliation,
    aggregate_cost,
    browser_seconds_from_run,
    build_cost_entries,
    load_cycle_ledger,
    reconcile_runs,
    record_cost,
    record_run_cost,
    rows_to_entry_dicts,
)

__all__ = [
    "ALL_COST_UNITS",
    "GAP_UNITS",
    "METERED_UNITS",
    "UNIT_BROWSER_SECONDS",
    "UNIT_LLM_TOKENS",
    "UNIT_RUNNER_RUNS",
    "UNIT_SUBSTRATE_ROWS",
    "UNIT_UNMETERED_RUN",
    "UNIT_WALLCLOCK_SECONDS",
    "CostAggregate",
    "RunCostSample",
    "RunReconciliation",
    "aggregate_cost",
    "browser_seconds_from_run",
    "build_cost_entries",
    "load_cycle_ledger",
    "reconcile_runs",
    "record_cost",
    "record_run_cost",
    "rows_to_entry_dicts",
]
