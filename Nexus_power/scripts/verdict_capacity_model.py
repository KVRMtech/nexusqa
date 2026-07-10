#!/usr/bin/env python3
"""VKPower Verdict — fleet capacity & cost model (Phase 7, pure calc).

WHY
===
Running 20+ clients × thousands of apps is only sustainable if the *cost of a
day of regression* is a MEASURED, predictable function of a few fleet inputs —
not a surprise on the invoice.  This module turns a :class:`FleetSpec`
(clients, apps, change-rate, per-cycle unit costs) into a :class:`CapacityPlan`:
cycles/day, browser-seconds/day, evidence-substrate DB growth, an estimated USD
spend, and the concurrency / replica / admission-cap sizing that supports it.

THE FLYWHEEL THIS MODEL PROVES
==============================
VKPower Verdict is *change-triggered incremental regression*: a cycle fires when
an app's repo/UI actually changes, and it re-verifies only the affected slice —
so fleet cost scales with **change**, not with **app count**.  The model makes
that claim falsifiable: it computes the incremental daily browser-seconds *and*
the full-re-crawl baseline for the same fleet and reports the ``savings_ratio``.
Adding apps that do not change adds ~0 incremental cost (only the cheap
scheduled "full floor" safety re-crawl), which is the whole economic thesis.

GROUNDING — the numbers bind to real product concepts
=====================================================
  * ``browser_seconds`` is the primary metered cost unit
    (``app.controlplane.cost.meter.UNIT_BROWSER_SECONDS``) — the same unit the
    live cost ledger accumulates, so a modeled number and a measured number are
    directly comparable.
  * concurrency sizing feeds ``QEC_MAX_GLOBAL_CYCLES`` /
    ``QEC_MAX_PER_TENANT_CYCLES`` (the admission caps in
    ``app.controlplane.scheduling.admission``) and the replica count for the
    distributed limiter (``QEC_ADMISSION_BACKEND=redis``).
  * DB growth binds to ``substrate_rows`` × bytes/row so Postgres sizing for the
    evidence system-of-record is a planned number, not a guess.

PURITY / SAFETY
===============
Standard-library only (no third-party imports), so it ``py_compile``s and runs
in a bare checkout and inside CI with no dependencies.  Every calculation is a
pure function of its inputs — no I/O, no clock, no environment — so ``--selftest``
is deterministic.  Defaults are conservative, documented, and OVERRIDABLE via
CLI flags; they are illustrative planning values, never product guarantees.

USAGE
=====
    python scripts/verdict_capacity_model.py --selftest
    python scripts/verdict_capacity_model.py --clients 20 --apps-per-client 100 \
        --changes-per-app-per-day 0.5 --json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

SECONDS_PER_DAY = 86_400
BYTES_PER_MIB = 1024 * 1024
BYTES_PER_GIB = 1024 * 1024 * 1024


# ─────────────────────────────────────────────────────────────────────────────
# Inputs
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FleetSpec:
    """The planning inputs for one fleet.

    All rates are per-DAY unless named otherwise.  ``changes_per_app_per_day``
    is the expected number of *change-triggered* incremental cycles per app per
    day (a deploy/PR/probe-drift event); ``full_floor_recrawls_per_app_per_day``
    is the scheduled safety re-crawl cadence (e.g. weekly ⇒ ``1/7`` ≈ 0.143).

    The two ``*_per_cycle`` cost figures separate the CHEAP incremental cycle
    (re-verifies only the changed slice) from the EXPENSIVE full re-crawl used as
    the flywheel baseline.  ``unit_cost_usd_*`` are NULLABLE-in-spirit: pass 0 to
    publish raw units only (mirrors the cost meter's "no invented dollars" rule).
    """

    clients: int = 20
    apps_per_client: float = 100.0

    # Change / cadence drivers ------------------------------------------------
    changes_per_app_per_day: float = 0.5
    full_floor_recrawls_per_app_per_day: float = 1.0 / 7.0  # weekly safety floor

    # Per-cycle resource cost (incremental = changed-slice only) -------------
    browser_seconds_per_incremental_cycle: float = 90.0
    browser_seconds_per_full_cycle: float = 900.0
    substrate_rows_per_incremental_cycle: float = 120.0
    substrate_rows_per_full_cycle: float = 1500.0
    llm_tokens_per_incremental_cycle: float = 0.0  # scripts are compiled, ~0 LLM
    bytes_per_substrate_row: float = 2_048.0

    # Wall-clock & concurrency shaping ---------------------------------------
    avg_cycle_wallclock_seconds: float = 240.0
    peak_to_average_ratio: float = 4.0  # diurnal + deploy-storm burstiness
    # How many cycles ONE qe-central replica hosts concurrently (bounded by
    # headless-browser memory / CPU per pod). Replicas are sized so the fleet's
    # PEAK concurrent cycles fit within replicas x this number.
    concurrent_cycles_per_replica: float = 4.0

    # Pricing (0 ⇒ raw units only, no USD) -----------------------------------
    unit_cost_usd_per_browser_second: float = 0.0
    unit_cost_usd_per_llm_1k_tokens: float = 0.0
    unit_cost_usd_per_gib_month: float = 0.0

    def validate(self) -> None:
        """Raise ``ValueError`` on any nonsensical input (fail loud, never fudge)."""
        checks: list[tuple[str, float]] = [
            ("clients", self.clients),
            ("apps_per_client", self.apps_per_client),
            ("changes_per_app_per_day", self.changes_per_app_per_day),
            ("full_floor_recrawls_per_app_per_day", self.full_floor_recrawls_per_app_per_day),
            ("browser_seconds_per_incremental_cycle", self.browser_seconds_per_incremental_cycle),
            ("browser_seconds_per_full_cycle", self.browser_seconds_per_full_cycle),
            ("substrate_rows_per_incremental_cycle", self.substrate_rows_per_incremental_cycle),
            ("substrate_rows_per_full_cycle", self.substrate_rows_per_full_cycle),
            ("llm_tokens_per_incremental_cycle", self.llm_tokens_per_incremental_cycle),
            ("bytes_per_substrate_row", self.bytes_per_substrate_row),
            ("avg_cycle_wallclock_seconds", self.avg_cycle_wallclock_seconds),
            ("peak_to_average_ratio", self.peak_to_average_ratio),
            ("concurrent_cycles_per_replica", self.concurrent_cycles_per_replica),
            ("unit_cost_usd_per_browser_second", self.unit_cost_usd_per_browser_second),
            ("unit_cost_usd_per_llm_1k_tokens", self.unit_cost_usd_per_llm_1k_tokens),
            ("unit_cost_usd_per_gib_month", self.unit_cost_usd_per_gib_month),
        ]
        for name, value in checks:
            if value is None or float(value) < 0:
                raise ValueError(f"{name} must be a non-negative number, got {value!r}")
        if self.peak_to_average_ratio < 1.0:
            raise ValueError("peak_to_average_ratio must be >= 1.0 (peak is never below average)")
        if self.concurrent_cycles_per_replica <= 0:
            raise ValueError("concurrent_cycles_per_replica must be > 0")
        if self.avg_cycle_wallclock_seconds <= 0:
            raise ValueError("avg_cycle_wallclock_seconds must be > 0")

    @property
    def total_apps(self) -> float:
        return float(self.clients) * float(self.apps_per_client)


# ─────────────────────────────────────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CapacityPlan:
    """The derived daily capacity / cost / sizing plan for a :class:`FleetSpec`."""

    total_apps: float

    # Cycles/day --------------------------------------------------------------
    incremental_cycles_per_day: float
    full_floor_cycles_per_day: float
    cycles_per_day: float
    full_recrawl_baseline_cycles_per_day: float

    # Browser-seconds/day -----------------------------------------------------
    incremental_browser_seconds_per_day: float
    full_floor_browser_seconds_per_day: float
    browser_seconds_per_day: float
    browser_hours_per_day: float
    full_recrawl_baseline_browser_seconds_per_day: float
    savings_ratio: float

    # LLM tokens/day ----------------------------------------------------------
    llm_tokens_per_day: float

    # Evidence-substrate DB growth -------------------------------------------
    substrate_rows_per_day: float
    db_growth_bytes_per_day: float
    db_growth_gib_per_day: float
    db_growth_gib_per_month: float

    # Concurrency / replica / admission sizing -------------------------------
    average_concurrent_cycles: float
    peak_concurrent_cycles: float
    recommended_max_global_cycles: int
    recommended_replicas: int

    # Estimated cost (USD; 0 when unpriced) ----------------------------------
    est_usd_per_day: float
    est_usd_per_month: float
    priced: bool


def compute_plan(spec: FleetSpec) -> CapacityPlan:
    """Compute the :class:`CapacityPlan` for ``spec`` (pure; validates first).

    Model
    -----
    * **Incremental cycles/day** = ``total_apps × changes_per_app_per_day`` — the
      change-triggered flywheel: an app that does not change fires no incremental
      cycle, so cost tracks CHANGE, not app count.
    * **Full-floor cycles/day** = ``total_apps × full_floor_recrawls_per_app_per_day``
      — the cheap scheduled safety re-crawl (still an incremental-shaped run).
    * **Full-re-crawl baseline** = ``total_apps × 1/day`` at the EXPENSIVE
      full-cycle cost — the "re-crawl everything nightly" strawman the flywheel
      beats.  ``savings_ratio`` = baseline_browser_seconds / actual_browser_seconds.
    * **Concurrency** = ``cycles_per_day × avg_cycle_wallclock / 86400`` (Little's
      law), scaled by ``peak_to_average_ratio`` for the peak.  The admission cap
      and replica count are sized to the PEAK (never the average) so a deploy
      storm degrades to queueing, never to dropping a customer's regression.
    """
    spec.validate()
    apps = spec.total_apps

    # ── cycles/day ──────────────────────────────────────────────────────────
    incr_cycles = apps * spec.changes_per_app_per_day
    floor_cycles = apps * spec.full_floor_recrawls_per_app_per_day
    cycles = incr_cycles + floor_cycles
    baseline_cycles = apps * 1.0  # nightly full re-crawl strawman

    # ── browser-seconds/day ─────────────────────────────────────────────────
    incr_bs = incr_cycles * spec.browser_seconds_per_incremental_cycle
    floor_bs = floor_cycles * spec.browser_seconds_per_full_cycle
    total_bs = incr_bs + floor_bs
    baseline_bs = baseline_cycles * spec.browser_seconds_per_full_cycle
    savings = (baseline_bs / total_bs) if total_bs > 0 else 1.0

    # ── LLM tokens/day ──────────────────────────────────────────────────────
    llm_tokens = (incr_cycles + floor_cycles) * spec.llm_tokens_per_incremental_cycle

    # ── substrate DB growth ─────────────────────────────────────────────────
    substrate_rows = (
        incr_cycles * spec.substrate_rows_per_incremental_cycle
        + floor_cycles * spec.substrate_rows_per_full_cycle
    )
    db_bytes = substrate_rows * spec.bytes_per_substrate_row
    db_gib_day = db_bytes / BYTES_PER_GIB
    db_gib_month = db_gib_day * 30.0

    # ── concurrency / replica / admission sizing (Little's law) ─────────────
    avg_concurrency = cycles * spec.avg_cycle_wallclock_seconds / SECONDS_PER_DAY
    peak_concurrency = avg_concurrency * spec.peak_to_average_ratio
    # Admission cap >= peak, min 1; round up so the cap never throttles the plan.
    rec_max_global = max(1, int(math.ceil(peak_concurrency)))
    # Replica count sized so the fleet's PEAK concurrent cycles (never the daily
    # mean) fit within replicas x concurrent_cycles_per_replica, so a deploy storm
    # queues at the shared admission gate instead of bursting a customer's app.
    rec_replicas = max(1, int(math.ceil(peak_concurrency / spec.concurrent_cycles_per_replica)))

    # ── estimated USD ───────────────────────────────────────────────────────
    usd_browser = total_bs * spec.unit_cost_usd_per_browser_second
    usd_llm = (llm_tokens / 1000.0) * spec.unit_cost_usd_per_llm_1k_tokens
    usd_storage_day = db_gib_month * spec.unit_cost_usd_per_gib_month / 30.0
    usd_day = usd_browser + usd_llm + usd_storage_day
    # "priced" only when EVERY non-zero cost driver has a price — mirrors the cost
    # meter's rule that a USD figure is published only when fully priced.
    priced = _fully_priced(spec, total_bs, llm_tokens, db_gib_month)

    return CapacityPlan(
        total_apps=apps,
        incremental_cycles_per_day=incr_cycles,
        full_floor_cycles_per_day=floor_cycles,
        cycles_per_day=cycles,
        full_recrawl_baseline_cycles_per_day=baseline_cycles,
        incremental_browser_seconds_per_day=incr_bs,
        full_floor_browser_seconds_per_day=floor_bs,
        browser_seconds_per_day=total_bs,
        browser_hours_per_day=total_bs / 3600.0,
        full_recrawl_baseline_browser_seconds_per_day=baseline_bs,
        savings_ratio=savings,
        llm_tokens_per_day=llm_tokens,
        substrate_rows_per_day=substrate_rows,
        db_growth_bytes_per_day=db_bytes,
        db_growth_gib_per_day=db_gib_day,
        db_growth_gib_per_month=db_gib_month,
        average_concurrent_cycles=avg_concurrency,
        peak_concurrent_cycles=peak_concurrency,
        recommended_max_global_cycles=rec_max_global,
        recommended_replicas=rec_replicas,
        est_usd_per_day=usd_day,
        est_usd_per_month=usd_day * 30.0,
        priced=priced,
    )


def _fully_priced(
    spec: FleetSpec, total_bs: float, llm_tokens: float, db_gib_month: float,
) -> bool:
    """True only when every cost driver that is actually present has a price.

    A driver with a zero quantity does not require a price (there is nothing to
    charge); a driver with a positive quantity but no price makes the whole USD
    figure UNPRICED (``priced=False``) — the model never invents dollars for an
    unpriced-but-present driver.
    """
    if total_bs > 0 and spec.unit_cost_usd_per_browser_second <= 0:
        return False
    if llm_tokens > 0 and spec.unit_cost_usd_per_llm_1k_tokens <= 0:
        return False
    if db_gib_month > 0 and spec.unit_cost_usd_per_gib_month <= 0:
        return False
    # At least one priced, present driver ⇒ a meaningful USD figure.
    return (
        (total_bs > 0 and spec.unit_cost_usd_per_browser_second > 0)
        or (llm_tokens > 0 and spec.unit_cost_usd_per_llm_1k_tokens > 0)
        or (db_gib_month > 0 and spec.unit_cost_usd_per_gib_month > 0)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────
def render_report(spec: FleetSpec, plan: CapacityPlan) -> str:
    """Human-readable capacity report (deterministic; no I/O)."""
    usd_day = f"${plan.est_usd_per_day:,.2f}" if plan.priced else "UNPRICED (raw units only)"
    usd_month = f"${plan.est_usd_per_month:,.2f}" if plan.priced else "UNPRICED"
    # ASCII-only so the report prints on any console (incl. Windows cp1252) and
    # in log files without an encoding declaration.
    lines = [
        "VKPower Verdict - Fleet Capacity & Cost Plan",
        "=" * 60,
        f"Fleet: {spec.clients} clients x {spec.apps_per_client:g} apps "
        f"= {plan.total_apps:,.0f} apps",
        f"Change rate: {spec.changes_per_app_per_day:g} change-cycles/app/day, "
        f"floor {spec.full_floor_recrawls_per_app_per_day:.3f} re-crawls/app/day",
        "",
        "-- Cycles / day -----------------------------------------",
        f"  incremental (change-triggered) : {plan.incremental_cycles_per_day:,.1f}",
        f"  full-floor (scheduled safety)  : {plan.full_floor_cycles_per_day:,.1f}",
        f"  TOTAL cycles/day               : {plan.cycles_per_day:,.1f}",
        "",
        "-- Browser-seconds / day (the metered cost unit) --------",
        f"  actual (incremental flywheel)  : {plan.browser_seconds_per_day:,.0f} s "
        f"({plan.browser_hours_per_day:,.1f} browser-hours)",
        f"  full-re-crawl baseline         : "
        f"{plan.full_recrawl_baseline_browser_seconds_per_day:,.0f} s",
        f"  >>> FLYWHEEL SAVINGS RATIO     : {plan.savings_ratio:,.1f}x cheaper "
        f"than re-crawling everything nightly",
        "",
        "-- Evidence-substrate DB growth -------------------------",
        f"  substrate rows/day             : {plan.substrate_rows_per_day:,.0f}",
        f"  DB growth                      : {plan.db_growth_gib_per_day:,.2f} GiB/day "
        f"({plan.db_growth_gib_per_month:,.1f} GiB/month)",
        "",
        "-- Concurrency / replica / admission sizing -------------",
        f"  average concurrent cycles      : {plan.average_concurrent_cycles:,.2f}",
        f"  peak concurrent cycles         : {plan.peak_concurrent_cycles:,.2f}",
        f"  -> QEC_MAX_GLOBAL_CYCLES       : {plan.recommended_max_global_cycles}",
        f"  -> recommended qe-central replicas: {plan.recommended_replicas} "
        f"(activate QEC_ADMISSION_BACKEND=redis + leader election)",
        "",
        "-- Estimated cost ---------------------------------------",
        f"  per day                        : {usd_day}",
        f"  per month                      : {usd_month}",
        f"  LLM tokens/day                 : {plan.llm_tokens_per_day:,.0f} "
        f"(compiler-not-chatbot: ~0 by design)",
    ]
    return "\n".join(lines)


def plan_to_dict(spec: FleetSpec, plan: CapacityPlan) -> dict[str, Any]:
    """Combined ``{spec, plan}`` dict for ``--json`` output / programmatic use."""
    return {"spec": asdict(spec), "plan": asdict(plan)}


# ─────────────────────────────────────────────────────────────────────────────
# Self-test (deterministic invariant checks — the honesty gate)
# ─────────────────────────────────────────────────────────────────────────────
def selftest() -> None:
    """Assert the model's load-bearing invariants; raise ``AssertionError`` on any.

    These are the properties that make the flywheel claim and the sizing outputs
    trustworthy — a regression in the arithmetic trips here, not on a customer's
    invoice.
    """
    base = FleetSpec()
    plan = compute_plan(base)

    # 1. Linearity in fleet size: doubling clients doubles apps, cycles, cost.
    dbl = compute_plan(FleetSpec(clients=base.clients * 2))
    assert math.isclose(dbl.total_apps, base.total_apps * 2), "apps must scale linearly with clients"
    assert math.isclose(dbl.cycles_per_day, plan.cycles_per_day * 2), "cycles must scale linearly with clients"
    assert math.isclose(dbl.browser_seconds_per_day, plan.browser_seconds_per_day * 2), \
        "browser-seconds must scale linearly with clients"

    # 2. Flywheel: incremental+floor is never MORE expensive than nightly full re-crawl.
    assert plan.savings_ratio >= 1.0, "incremental must never cost more than the full-re-crawl baseline"
    assert plan.browser_seconds_per_day <= plan.full_recrawl_baseline_browser_seconds_per_day + 1e-6, \
        "actual browser-seconds must be <= full-re-crawl baseline"

    # 3. Cost tracks CHANGE, not app count: apps that never change add only the
    #    cheap floor, so a fleet with 0 change has 0 incremental cycles.
    no_change = compute_plan(FleetSpec(changes_per_app_per_day=0.0))
    assert no_change.incremental_cycles_per_day == 0.0, "zero change must yield zero incremental cycles"
    assert no_change.incremental_browser_seconds_per_day == 0.0, "zero change must yield zero incremental browser-seconds"

    # 3b. Total-zero fleet: no change AND no floor ⇒ everything zero, savings defined as 1.0.
    zero = compute_plan(FleetSpec(changes_per_app_per_day=0.0, full_floor_recrawls_per_app_per_day=0.0))
    assert zero.cycles_per_day == 0.0 and zero.browser_seconds_per_day == 0.0, "empty fleet must be zero-cost"
    assert zero.savings_ratio == 1.0, "savings ratio must be defined (1.0) for a zero-work fleet"
    assert zero.db_growth_bytes_per_day == 0.0, "empty fleet must grow the DB by zero"

    # 4. Monotonicity: more change ⇒ strictly more cycles & browser-seconds & cost.
    more = compute_plan(FleetSpec(changes_per_app_per_day=base.changes_per_app_per_day * 2))
    assert more.incremental_cycles_per_day > plan.incremental_cycles_per_day, "more change must mean more cycles"
    assert more.browser_seconds_per_day > plan.browser_seconds_per_day, "more change must mean more browser-seconds"

    # 4b. As change-rate rises toward a full re-crawl, savings shrink toward ~1×.
    heavy = compute_plan(FleetSpec(
        changes_per_app_per_day=1.0,
        browser_seconds_per_incremental_cycle=base.browser_seconds_per_full_cycle,
        full_floor_recrawls_per_app_per_day=0.0,
    ))
    assert math.isclose(heavy.savings_ratio, 1.0, rel_tol=1e-6), \
        "when every app changes at full cost, incremental == full (savings ~1x)"

    # 5. Sizing sanity: caps/replicas are >= 1 and cover the PEAK, not the average.
    assert plan.recommended_max_global_cycles >= 1, "admission cap must be at least 1"
    assert plan.recommended_replicas >= 1, "replica count must be at least 1"
    assert plan.peak_concurrent_cycles >= plan.average_concurrent_cycles, "peak must be >= average"
    assert plan.recommended_max_global_cycles >= plan.peak_concurrent_cycles - 1e-9, \
        "admission cap must cover the peak concurrency"
    assert plan.recommended_replicas * base.concurrent_cycles_per_replica >= plan.peak_concurrent_cycles - 1e-9, \
        "replica count x per-replica concurrency must cover the peak concurrency"

    # 6. Non-negativity across the board (a negative cost/size is a bug, not a plan).
    for name, value in asdict(plan).items():
        if isinstance(value, (int, float)):
            assert value >= 0, f"plan.{name} must be non-negative, got {value}"

    # 7. Pricing honesty: unpriced fleet publishes no USD; fully-priced does.
    assert plan.priced is False and plan.est_usd_per_day == 0.0, \
        "default (unpriced) spec must publish no USD figure"
    priced = compute_plan(FleetSpec(unit_cost_usd_per_browser_second=0.0001, unit_cost_usd_per_gib_month=0.10))
    assert priced.priced is True and priced.est_usd_per_day > 0, "a priced spec must publish a positive USD figure"

    # 8. Validation rejects garbage inputs (fail loud).
    for bad in (
        FleetSpec(clients=-1),
        FleetSpec(peak_to_average_ratio=0.5),
        FleetSpec(avg_cycle_wallclock_seconds=0.0),
    ):
        try:
            compute_plan(bad)
        except ValueError:
            pass
        else:  # pragma: no cover - only reached if validation regresses
            raise AssertionError(f"expected ValueError for invalid spec {bad!r}")

    print("verdict_capacity_model selftest: OK (8 invariant groups passed)")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verdict_capacity_model",
        description="VKPower Verdict fleet capacity & cost model (pure calc).",
    )
    p.add_argument("--selftest", action="store_true", help="run invariant self-tests and exit")
    p.add_argument("--json", action="store_true", help="emit the plan as JSON")
    p.add_argument("--clients", type=int, default=FleetSpec.clients)
    p.add_argument("--apps-per-client", type=float, default=FleetSpec.apps_per_client)
    p.add_argument("--changes-per-app-per-day", type=float, default=FleetSpec.changes_per_app_per_day)
    p.add_argument(
        "--full-floor-recrawls-per-app-per-day", type=float,
        default=FleetSpec.full_floor_recrawls_per_app_per_day,
    )
    p.add_argument("--browser-seconds-per-incremental-cycle", type=float,
                   default=FleetSpec.browser_seconds_per_incremental_cycle)
    p.add_argument("--browser-seconds-per-full-cycle", type=float,
                   default=FleetSpec.browser_seconds_per_full_cycle)
    p.add_argument("--avg-cycle-wallclock-seconds", type=float,
                   default=FleetSpec.avg_cycle_wallclock_seconds)
    p.add_argument("--usd-per-browser-second", type=float,
                   default=FleetSpec.unit_cost_usd_per_browser_second)
    p.add_argument("--usd-per-gib-month", type=float,
                   default=FleetSpec.unit_cost_usd_per_gib_month)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.selftest:
        selftest()
        return 0

    spec = FleetSpec(
        clients=args.clients,
        apps_per_client=args.apps_per_client,
        changes_per_app_per_day=args.changes_per_app_per_day,
        full_floor_recrawls_per_app_per_day=args.full_floor_recrawls_per_app_per_day,
        browser_seconds_per_incremental_cycle=args.browser_seconds_per_incremental_cycle,
        browser_seconds_per_full_cycle=args.browser_seconds_per_full_cycle,
        avg_cycle_wallclock_seconds=args.avg_cycle_wallclock_seconds,
        unit_cost_usd_per_browser_second=args.usd_per_browser_second,
        unit_cost_usd_per_gib_month=args.usd_per_gib_month,
    )
    try:
        plan = compute_plan(spec)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(plan_to_dict(spec, plan), indent=2))
    else:
        print(render_report(spec, plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
