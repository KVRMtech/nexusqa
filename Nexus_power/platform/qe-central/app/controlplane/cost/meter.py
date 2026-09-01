"""QE-Central S5 — the append-only cost meter (design §3.5).

The control plane must be able to publish, per cycle and per app, exactly what
a regression run COST in raw units — so that "incremental is ≥10× cheaper than
a full re-crawl" is a MEASURED number, not a claim.  Two honesty invariants are
load-bearing and are enforced structurally here, not by convention:

  1. **The meter can only UNDER-count.**  ``browser_seconds`` are sourced from a
     run record's ``duration_ms`` (``GET /runs/{run_id}`` — NEVER a SQL query
     into the nexus DB).  A runner run that cannot be correlated to a real
     duration is recorded as an ``unmetered_run`` GAP FLAG — never as invented
     ``browser_seconds``.  So the published ``browser_seconds`` total is always a
     lower bound on the true browser time; it can never inflate a cost (or, read
     the other way, never make an expensive cycle look cheap by fabricating a
     low number, nor an app look busy with time it did not spend).

  2. **RAW UNITS first — dollars only when configured.**  ``unit_cost_usd`` is
     NULLABLE; an aggregate publishes a USD figure ONLY when every contributing
     entry is priced, else ``usd is None``.  The meter never invents dollars.

The pure primitives (:func:`browser_seconds_from_run`, :func:`reconcile_runs`,
:func:`build_cost_entries`, :func:`aggregate_cost`) carry all the accounting
logic and are unit-tested with no database.  The ``async`` helpers
(:func:`record_cost`, :func:`record_run_cost`) persist through the tenant-scoped
qecentral session and are exercised behind ``QEC_TEST_QEC_DATABASE_URL``.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import tenant_scoped_qec_session
from ...db.controlplane_models import CostLedgerRow

logger = logging.getLogger(__name__)


# ── Canonical cost units (cost_ledger.unit vocabulary, §3.5) ───────────────
UNIT_BROWSER_SECONDS = "browser_seconds"
UNIT_LLM_TOKENS = "llm_tokens"
UNIT_SUBSTRATE_ROWS = "substrate_rows"
UNIT_WALLCLOCK_SECONDS = "wallclock_seconds"
UNIT_RUNNER_RUNS = "runner_runs"

#: The real, spendable units the meter accumulates.
METERED_UNITS = frozenset({
    UNIT_BROWSER_SECONDS, UNIT_LLM_TOKENS, UNIT_SUBSTRATE_ROWS,
    UNIT_WALLCLOCK_SECONDS, UNIT_RUNNER_RUNS,
})

#: The gap flag recorded when a runner run cannot be correlated to a duration.
#: It is a COUNT (quantity=1 per uncorrelated run), never a fabricated second —
#: this is the visible face of the can-only-under-count invariant.
UNIT_UNMETERED_RUN = "unmetered_run"
GAP_UNITS = frozenset({UNIT_UNMETERED_RUN})

#: Everything :func:`record_cost` will accept; an unknown unit is refused
#: (never silently dropped — a dropped cost would be a green-wash of spend).
ALL_COST_UNITS = METERED_UNITS | GAP_UNITS

_MS_PER_SECOND = Decimal("1000")


# ── run-record accessor (dict OR object — the client returns a dict) ───────
_MISSING = object()


def _field(record: Any, name: str, default: Any = _MISSING) -> Any:
    """Read ``name`` from a dict-like OR attribute-bearing run record."""
    if record is None:
        return default
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _to_decimal(value: Any) -> Decimal:
    """Coerce ``value`` to :class:`Decimal`, raising ``ValueError`` on garbage.

    ``bool`` is rejected explicitly — ``Decimal(True)`` is ``1`` and would let a
    truthy flag masquerade as a quantity.
    """
    if isinstance(value, bool):
        raise ValueError(f"cost quantity must be numeric, not bool: {value!r}")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"cost quantity is not numeric: {value!r}") from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── browser_seconds derivation (the under-count seam) ──────────────────────
@dataclass(frozen=True)
class RunCostSample:
    """The metered cost of ONE runner run, derived from its run record.

    ``unmetered`` is True (and ``browser_seconds`` is None) when the run could
    not be correlated to a real duration — the meter then records an
    ``unmetered_run`` gap flag instead of inventing seconds.  ``reason`` names
    exactly why a run is unmetered so the gap is auditable, never mysterious.
    """

    run_id: str
    browser_seconds: Optional[Decimal]
    unmetered: bool
    reason: str = ""


def browser_seconds_from_run(run_record: Any) -> RunCostSample:
    """Derive ``browser_seconds`` from a run record's ``duration_ms``.

    Sourcing rule (design §3.5): ``browser_seconds = duration_ms / 1000`` read
    from the run record the caller already fetched via ``GET /runs/{run_id}`` —
    this function NEVER touches the nexus DB and NEVER invents a duration.

    A run is UNMETERED (``unmetered=True``, ``browser_seconds=None``) when:
      * ``run_record`` is ``None`` (``missing_run_record``);
      * it carries no ``run_id`` (``missing_run_id`` — cannot be correlated);
      * ``duration_ms`` is absent/``None`` (``missing_duration``);
      * ``duration_ms`` is non-numeric (``non_numeric_duration``);
      * ``duration_ms`` is negative (``negative_duration``).

    A present ``duration_ms == 0`` is a CORRELATED zero, not a gap: it meters
    ``0`` browser_seconds (an honest under-count of a run that measured no time),
    because we DO have the run record — we simply observed no billable duration.
    """
    if run_record is None:
        return RunCostSample("", None, True, "missing_run_record")

    run_id = str(_field(run_record, "run_id", "") or "").strip()
    if not run_id:
        return RunCostSample("", None, True, "missing_run_id")

    duration = _field(run_record, "duration_ms", _MISSING)
    if duration is _MISSING or duration is None:
        return RunCostSample(run_id, None, True, "missing_duration")

    try:
        ms = _to_decimal(duration)
    except ValueError:
        return RunCostSample(run_id, None, True, "non_numeric_duration")
    if ms < 0:
        return RunCostSample(run_id, None, True, "negative_duration")

    return RunCostSample(run_id, ms / _MS_PER_SECOND, False, "")


@dataclass(frozen=True)
class RunReconciliation:
    """The result of reconciling a batch of run records into metered cost.

    ``metered_browser_seconds`` is a GUARANTEED LOWER BOUND on the true browser
    time of the batch: it sums only correlated runs; every uncorrelated run
    contributes 0 seconds and +1 to ``unmetered_runs``.  This is the can-only-
    under-count property in one value.
    """

    metered_browser_seconds: Decimal
    metered_runs: int
    unmetered_runs: int
    samples: tuple[RunCostSample, ...]


def reconcile_runs(run_records: Iterable[Any]) -> RunReconciliation:
    """Split ``run_records`` into metered vs unmetered and sum the metered time.

    Pure + total: never raises on a malformed record (a garbage record simply
    lands in ``unmetered_runs``).  Adding an uncorrelated run can ONLY leave
    ``metered_browser_seconds`` unchanged and increment ``unmetered_runs``.
    """
    samples = tuple(browser_seconds_from_run(r) for r in run_records)
    metered = Decimal("0")
    metered_runs = 0
    unmetered_runs = 0
    for s in samples:
        if s.unmetered or s.browser_seconds is None:
            unmetered_runs += 1
        else:
            metered += s.browser_seconds
            metered_runs += 1
    return RunReconciliation(metered, metered_runs, unmetered_runs, samples)


# ── entry construction (pure — the shape record_cost persists) ─────────────
def build_cost_entries(
    *,
    tenant_id: str,
    app_id: str = "",
    cycle_id: str = "",
    units: Mapping[str, Any],
    source_ref: str = "",
    unit_cost_usd: Mapping[str, Any] | None = None,
    id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Build the append-only ledger entry dicts for ``units`` (one row per unit).

    Validation (fail LOUD — a silently dropped or negated cost is a green-wash):
      * ``tenant_id`` is required (RLS everywhere);
      * every unit MUST be in :data:`ALL_COST_UNITS` (unknown ⇒ ``ValueError``);
      * every quantity MUST be numeric and ``>= 0`` — a NEGATIVE entry is
        refused, because the ledger is append-only and can only accumulate; a
        negative would let the meter be walked BACKWARDS below true spend.

    ``unit_cost_usd`` (optional) maps unit → price; an unpriced unit gets a NULL
    ``unit_cost_usd`` (raw units published, no invented dollars).
    """
    tid = str(tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id is required to record cost")

    created_at = now or _utc_now()
    ref = str(source_ref or "")[:200]
    prices = dict(unit_cost_usd or {})
    entries: list[dict] = []
    for unit, raw_qty in units.items():
        if unit not in ALL_COST_UNITS:
            raise ValueError(
                f"unknown cost unit {unit!r}; expected one of "
                f"{sorted(ALL_COST_UNITS)}"
            )
        qty = _to_decimal(raw_qty)
        if qty < 0:
            raise ValueError(
                f"cost quantity for {unit!r} is negative ({qty}); the ledger is "
                "append-only and can only under-count, never below zero"
            )
        price = prices.get(unit)
        entries.append({
            "entry_id": id_factory(),
            "tenant_id": tid,
            "app_id": str(app_id or "")[:64],
            "cycle_id": str(cycle_id or "")[:64],
            "unit": unit,
            "quantity": qty,
            "source_ref": ref,
            "unit_cost_usd": None if price is None else _to_decimal(price),
            "created_at": created_at,
        })
    return entries


# ── aggregation (pure — window + group_by over ledger rows) ────────────────
@dataclass(frozen=True)
class CostAggregate:
    """One group's rolled-up cost (per cycle / app / unit / whole window).

    ``units`` sums only METERED units.  ``unmetered_runs`` is the count of
    ``unmetered_run`` gap flags — surfaced separately so a consumer can SEE the
    metering gap rather than have it hidden inside a total.  ``usd`` is populated
    ONLY when every metered entry in the group is priced; otherwise ``None``.
    """

    group_key: str
    units: dict[str, Decimal]
    unmetered_runs: int
    usd: Optional[Decimal]
    entry_count: int


def _coerce_dt(value: Any) -> Optional[datetime]:
    """Coerce a ledger row's ``created_at`` (datetime or ISO string) to datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _in_window(
    created_at: Any, window: Optional[tuple[Optional[datetime], Optional[datetime]]],
) -> bool:
    """True when ``created_at`` falls within an optional ``(start, end)`` window.

    A ``None`` bound is open on that side.  A row whose ``created_at`` cannot be
    parsed is INCLUDED only when the window is fully open — never silently
    dropped from an unbounded aggregate, never counted into a bounded one it
    cannot be placed in.
    """
    if window is None:
        return True
    start, end = window
    dt = _coerce_dt(created_at)
    if dt is None:
        return start is None and end is None
    if start is not None and dt < start:
        return False
    if end is not None and dt > end:
        return False
    return True


_GROUP_FIELDS = {"cycle_id", "app_id", "unit", "tenant_id"}


def aggregate_cost(
    entries: Iterable[Any],
    *,
    group_by: str = "cycle_id",
    window: Optional[tuple[Optional[datetime], Optional[datetime]]] = None,
) -> dict[str, CostAggregate]:
    """Aggregate ledger rows (dicts OR :class:`CostLedgerRow`) by ``group_by``.

    ``group_by`` ∈ ``cycle_id | app_id | unit | tenant_id | none``; ``none``
    rolls everything into a single ``"all"`` group.  ``window`` filters by
    ``created_at``.  Pure — no DB, no mutation of inputs.
    """
    gb = (group_by or "none").lower()
    if gb != "none" and gb not in _GROUP_FIELDS:
        raise ValueError(
            f"group_by must be one of {sorted(_GROUP_FIELDS) + ['none']}, got {group_by!r}"
        )

    # Per group: {unit -> summed qty}, unmetered count, running usd, whether the
    # usd figure is still fully priced, and an entry counter.
    acc: dict[str, dict] = {}
    for entry in entries:
        if not _in_window(_field(entry, "created_at", None), window):
            continue
        key = "all" if gb == "none" else str(_field(entry, gb, "") or "")
        unit = str(_field(entry, "unit", "") or "")
        qty = _to_decimal(_field(entry, "quantity", 0) or 0)

        bucket = acc.setdefault(
            key,
            {"units": {}, "unmetered_runs": 0, "usd": Decimal("0"),
             "usd_complete": True, "entry_count": 0},
        )
        bucket["entry_count"] += 1

        if unit in GAP_UNITS:
            bucket["unmetered_runs"] += int(qty)
            continue

        bucket["units"][unit] = bucket["units"].get(unit, Decimal("0")) + qty
        price = _field(entry, "unit_cost_usd", None)
        if price is None:
            bucket["usd_complete"] = False
        else:
            bucket["usd"] += qty * _to_decimal(price)

    out: dict[str, CostAggregate] = {}
    for key, b in acc.items():
        priced = b["usd_complete"] and bool(b["units"])
        out[key] = CostAggregate(
            group_key=key,
            units=dict(b["units"]),
            unmetered_runs=b["unmetered_runs"],
            usd=(b["usd"] if priced else None),
            entry_count=b["entry_count"],
        )
    return out


# ── async persistence (qecentral DB, tenant-scoped) ────────────────────────
async def record_cost(
    *,
    tenant_id: str,
    app_id: str = "",
    cycle_id: str = "",
    units: Mapping[str, Any],
    source_ref: str = "",
    unit_cost_usd: Mapping[str, Any] | None = None,
    session: Optional[AsyncSession] = None,
) -> list[CostLedgerRow]:
    """Append cost entries to ``cost_ledger`` (one immutable row per unit).

    Entry construction + validation happens BEFORE any DB write
    (:func:`build_cost_entries`), so a refused (unknown/negative) unit leaves no
    partial rows.  Pass an existing tenant-scoped ``session`` to fold the write
    into a cycle transaction, or omit it to open a fresh tenant-scoped session.

    Returns the inserted rows.  Errors propagate — a cost that appears recorded
    but did not persist would silently under-report spend.
    """
    entries = build_cost_entries(
        tenant_id=tenant_id, app_id=app_id, cycle_id=cycle_id,
        units=units, source_ref=source_ref, unit_cost_usd=unit_cost_usd,
    )
    if not entries:
        return []
    rows = [CostLedgerRow(**e) for e in entries]

    if session is not None:
        session.add_all(rows)
        await session.flush()
    else:
        async with tenant_scoped_qec_session(tenant_id) as s:
            s.add_all(rows)
            await s.flush()

    logger.info(
        "qec.cost.recorded",
        extra={"tenant_id": tenant_id, "app_id": app_id, "cycle_id": cycle_id,
               "units": {e["unit"]: str(e["quantity"]) for e in entries},
               "source_ref": source_ref[:120]},
    )
    return rows


async def _run_already_metered(
    session: AsyncSession, *, tenant_id: str, cycle_id: str, run_id: str,
) -> bool:
    """True when this run_id was already metered for this cycle (idempotency).

    Guards against DOUBLE-counting a run on a retry — which would OVER-count and
    break the can-only-under-count invariant from the other direction.
    """
    result = await session.execute(
        select(CostLedgerRow.entry_id)
        .where(
            CostLedgerRow.tenant_id == tenant_id,
            CostLedgerRow.cycle_id == cycle_id,
            CostLedgerRow.source_ref == run_id,
            CostLedgerRow.unit.in_([UNIT_BROWSER_SECONDS, UNIT_UNMETERED_RUN]),
        )
        .limit(1)
    )
    return result.first() is not None


async def record_run_cost(
    *,
    tenant_id: str,
    app_id: str = "",
    cycle_id: str = "",
    run_record: Any,
    idempotent: bool = True,
    session: Optional[AsyncSession] = None,
) -> RunCostSample:
    """Meter ONE runner run from its run record (the primary browser-seconds seam).

    Correlated run  → ``browser_seconds`` (from ``duration_ms``) + one
    ``runner_runs``.  Uncorrelated run → ONE ``unmetered_run`` gap flag, never
    invented seconds.  With ``idempotent=True`` (default) a run already metered
    for this cycle is a no-op — a retried cycle can never double-bill.

    Returns the :class:`RunCostSample` describing what was (or was not) metered.
    """
    sample = browser_seconds_from_run(run_record)
    ref = sample.run_id or "unknown"

    async def _do(s: AsyncSession) -> None:
        if idempotent and sample.run_id and await _run_already_metered(
            s, tenant_id=tenant_id, cycle_id=cycle_id, run_id=sample.run_id,
        ):
            logger.info(
                "qec.cost.run_already_metered",
                extra={"tenant_id": tenant_id, "cycle_id": cycle_id, "run_id": sample.run_id},
            )
            return
        if sample.unmetered:
            await record_cost(
                tenant_id=tenant_id, app_id=app_id, cycle_id=cycle_id,
                units={UNIT_UNMETERED_RUN: 1}, source_ref=ref, session=s,
            )
            logger.warning(
                "qec.cost.unmetered_run",
                extra={"tenant_id": tenant_id, "cycle_id": cycle_id,
                       "run_id": sample.run_id, "reason": sample.reason},
            )
        else:
            await record_cost(
                tenant_id=tenant_id, app_id=app_id, cycle_id=cycle_id,
                units={UNIT_BROWSER_SECONDS: sample.browser_seconds, UNIT_RUNNER_RUNS: 1},
                source_ref=ref, session=s,
            )

    if session is not None:
        await _do(session)
    else:
        async with tenant_scoped_qec_session(tenant_id) as s:
            await _do(s)
    return sample


async def load_cycle_ledger(
    *, tenant_id: str, cycle_id: str, session: Optional[AsyncSession] = None,
) -> list[CostLedgerRow]:
    """Load every ledger row for one cycle (newest-first) — the cost snapshot.

    Used by the cycle-status endpoint and the budget gate.  Tenant-scoped (RLS);
    opens its own session when one is not supplied.
    """
    async def _query(s: AsyncSession) -> list[CostLedgerRow]:
        result = await s.execute(
            select(CostLedgerRow)
            .where(
                CostLedgerRow.tenant_id == tenant_id,
                CostLedgerRow.cycle_id == cycle_id,
            )
            .order_by(CostLedgerRow.created_at.desc())
        )
        return list(result.scalars().all())

    if session is not None:
        return await _query(session)
    async with tenant_scoped_qec_session(tenant_id) as s:
        return await _query(s)


def rows_to_entry_dicts(rows: Sequence[CostLedgerRow]) -> list[dict]:
    """Project ledger rows onto the plain dicts :func:`aggregate_cost` consumes.

    Lets a DB read feed the pure aggregator (and the budget gate) unchanged.
    """
    return [
        {
            "entry_id": r.entry_id,
            "tenant_id": r.tenant_id,
            "app_id": r.app_id,
            "cycle_id": r.cycle_id,
            "unit": r.unit,
            "quantity": r.quantity,
            "source_ref": r.source_ref,
            "unit_cost_usd": r.unit_cost_usd,
            "created_at": r.created_at,
        }
        for r in rows
    ]


__all__ = [
    "UNIT_BROWSER_SECONDS",
    "UNIT_LLM_TOKENS",
    "UNIT_SUBSTRATE_ROWS",
    "UNIT_WALLCLOCK_SECONDS",
    "UNIT_RUNNER_RUNS",
    "UNIT_UNMETERED_RUN",
    "METERED_UNITS",
    "GAP_UNITS",
    "ALL_COST_UNITS",
    "RunCostSample",
    "RunReconciliation",
    "CostAggregate",
    "browser_seconds_from_run",
    "reconcile_runs",
    "build_cost_entries",
    "aggregate_cost",
    "record_cost",
    "record_run_cost",
    "load_cycle_ledger",
    "rows_to_entry_dicts",
]
