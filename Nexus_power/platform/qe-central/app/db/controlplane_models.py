"""QE-Central S5 — ORM for the four control-plane qecentral-DB tables.

These bind the SAME QE-Central-private ``QecBase`` declared in
``app.db.models`` (NOT the SDK ``Base``) so the qecentral logical database
stays a single bounded context (R-7).  Every column here mirrors
``alembic_qec/versions/qec_001_initial.py`` EXACTLY — the migration is the
source of truth (no migration is written for S5; the tables already exist):

  * ``app_cycles``      → :class:`AppCycleRow`   (one-ACTIVE-cycle-per-app
                          partial unique index reproduced in metadata)
  * ``change_events``   → :class:`ChangeEventRow` (UNIQUE(app_id, dedupe_key))
  * ``app_fingerprints``→ :class:`AppFingerprintRow`
  * ``cost_ledger``     → :class:`CostLedgerRow`  (append-only; ``quantity`` and
                          ``unit_cost_usd`` are exact-precision ``Numeric``)

Every table carries ``tenant_id`` and is protected by ENABLE+FORCE RLS with
the standard ``tenant_isolation`` policy (see qec_001_initial).  The ``Index``
declarations reproduce the migration's index NAMES so ORM metadata is a
faithful description of the deployed schema; schema itself is owned by
Alembic — nothing here runs ``create_all`` against a real database.

The state / trigger / source vocabularies are exported as ``frozenset``
constants so the cycle driver and change producers share ONE definition of
the state machine (and the terminal set that drives the partial unique index).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .models import QecBase


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── app_cycles state machine (§3.5) ────────────────────────────────────────
# The FULL ordered life-cycle plus the three HONEST terminal states.  A cycle
# in any NON-terminal state (including ``blackout_deferred``) still owns its
# app's one-active-cycle slot — see :data:`TERMINAL_CYCLE_STATES`.
CYCLE_STATE_PENDING = "pending"
CYCLE_STATE_PROBING = "probing"
CYCLE_STATE_SELECTING = "selecting"
CYCLE_STATE_CRAWLING = "crawling"
CYCLE_STATE_GENERATING = "generating"
CYCLE_STATE_RUNNING = "running"
CYCLE_STATE_HEALING = "healing"
CYCLE_STATE_VERIFYING = "verifying"
CYCLE_STATE_DONE = "done"
CYCLE_STATE_FAILED = "failed"
CYCLE_STATE_BUDGET_STOPPED = "budget_stopped"
CYCLE_STATE_BLACKOUT_DEFERRED = "blackout_deferred"

CYCLE_STATES = frozenset({
    CYCLE_STATE_PENDING, CYCLE_STATE_PROBING, CYCLE_STATE_SELECTING,
    CYCLE_STATE_CRAWLING, CYCLE_STATE_GENERATING, CYCLE_STATE_RUNNING,
    CYCLE_STATE_HEALING, CYCLE_STATE_VERIFYING, CYCLE_STATE_DONE,
    CYCLE_STATE_FAILED, CYCLE_STATE_BUDGET_STOPPED, CYCLE_STATE_BLACKOUT_DEFERRED,
})

# Terminal states — a cycle in one of these has released its app slot.  This set
# MUST stay byte-identical to the migration's ``_TERMINAL_CYCLE_STATES`` literal
# ``('done', 'failed', 'budget_stopped')`` that keys the partial unique index;
# a divergence would let two active cycles coexist (or wedge every new cycle).
TERMINAL_CYCLE_STATES = frozenset({
    CYCLE_STATE_DONE, CYCLE_STATE_FAILED, CYCLE_STATE_BUDGET_STOPPED,
})
# The exact WHERE predicate of ``uq_app_cycles_one_active_per_app`` — kept as a
# single string so the ORM's partial-index metadata is provably the same
# predicate the migration installed.
_ACTIVE_CYCLE_INDEX_PREDICATE = "state NOT IN ('done', 'failed', 'budget_stopped')"


def is_terminal_cycle_state(state: str) -> bool:
    """True when ``state`` releases the app's one-active-cycle slot."""
    return str(state or "") in TERMINAL_CYCLE_STATES


# ── app_cycles.trigger vocabulary (§3.5) ───────────────────────────────────
CYCLE_TRIGGER_WEBHOOK_REPO = "webhook_repo"
CYCLE_TRIGGER_SCHEDULE = "schedule"
CYCLE_TRIGGER_PROBE_DRIFT = "probe_drift"
CYCLE_TRIGGER_MANUAL = "manual"
CYCLE_TRIGGER_FULL_FLOOR = "full_floor"
CYCLE_TRIGGERS = frozenset({
    CYCLE_TRIGGER_WEBHOOK_REPO, CYCLE_TRIGGER_SCHEDULE, CYCLE_TRIGGER_PROBE_DRIFT,
    CYCLE_TRIGGER_MANUAL, CYCLE_TRIGGER_FULL_FLOOR,
})

# ── change_events.source vocabulary (§3.5) ─────────────────────────────────
CHANGE_SOURCE_REPO_SHA = "repo_sha"
CHANGE_SOURCE_PROBE = "probe"
CHANGE_SOURCE_MANUAL = "manual"
CHANGE_SOURCE_SCHEDULE_FLOOR = "schedule_floor"
CHANGE_SOURCES = frozenset({
    CHANGE_SOURCE_REPO_SHA, CHANGE_SOURCE_PROBE, CHANGE_SOURCE_MANUAL,
    CHANGE_SOURCE_SCHEDULE_FLOOR,
})


# ── app_cycles ─────────────────────────────────────────────────────────────
class AppCycleRow(QecBase):
    """One regression cycle for one app (design §3.5 ``app_cycles``).

    ``state`` walks ``pending → probing → selecting → crawling? → generating →
    running → healing → verifying → done`` with three HONEST terminal exits
    (``failed``, ``budget_stopped``, ``done``) and one non-terminal pause
    (``blackout_deferred``).  ``budget_stopped`` is a first-class terminal
    state — a budget breach is NEVER reported as a partial ``done``.

    At most ONE non-terminal cycle may exist per app: the partial unique index
    ``uq_app_cycles_one_active_per_app`` (reproduced below) enforces it in the
    engine, so the driver is restart-safe without advisory locks.
    """

    __tablename__ = "app_cycles"

    cycle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # webhook_repo | schedule | probe_drift | manual | full_floor
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=CYCLE_STATE_PENDING)
    # {mode, changed_atoms, selected_test_ids, carried_forward, selection_reason}
    selected_scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {uncomputable_pages_treated_changed, vanished_pages_possible_deletion}
    honest_gaps: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now,
    )

    __table_args__ = (
        Index("ix_app_cycles_tenant_app_created", "tenant_id", "app_id", "created_at"),
        # ONE ACTIVE cycle per app — restart-safe without advisory locks (§3.5).
        # The predicate is byte-identical to the migration's install.
        Index(
            "uq_app_cycles_one_active_per_app",
            "app_id",
            unique=True,
            postgresql_where=text(_ACTIVE_CYCLE_INDEX_PREDICATE),
        ),
    )


# ── change_events ──────────────────────────────────────────────────────────
class ChangeEventRow(QecBase):
    """One change signal awaiting (or bound to) a cycle (design §3.5).

    Producers (webhook, probe, manual, schedule floor) write; the driver
    consumes.  ``UNIQUE(app_id, dedupe_key)`` coalesces duplicate signals so a
    replayed GitLab webhook or a re-fired probe never spawns a second cycle;
    ``processed_cycle_id`` is set when the driver drains the event into a cycle.
    """

    __tablename__ = "change_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # repo_sha | probe | manual | schedule_floor
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    processed_cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )

    __table_args__ = (
        UniqueConstraint("app_id", "dedupe_key", name="uq_change_events_app_dedupe"),
        Index("ix_change_events_tenant_app", "tenant_id", "app_id"),
    )


# ── app_fingerprints ───────────────────────────────────────────────────────
class AppFingerprintRow(QecBase):
    """One baseline structural fingerprint of an app (design §3.5).

    ``page_fingerprints`` = ``{page_key → {structural_hash, control_fps,
    last_verified_at}}``; ``graph_hash`` summarises the whole journey graph.
    Seeded from ``GET /test-factory/{artifact_id}/journey-graph`` — but the
    change detector is FAIL-SAFE-TO-CHANGED, so an unavailable seed never
    manufactures a false "no change".  ``source_artifact_id`` records which
    artifact's journey graph produced this baseline.
    """

    __tablename__ = "app_fingerprints"

    fingerprint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    page_fingerprints: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )

    __table_args__ = (
        Index("ix_app_fingerprints_tenant_app_created", "tenant_id", "app_id", "created_at"),
    )


# ── cost_ledger ────────────────────────────────────────────────────────────
class CostLedgerRow(QecBase):
    """One append-only cost entry (design §3.5 ``cost_ledger``).

    RAW UNITS first: ``quantity`` counts one ``unit`` (browser_seconds,
    llm_tokens, substrate_rows, wallclock_seconds, runner_runs) — or the
    ``unmetered_run`` gap flag when a runner run could not be correlated to a
    real duration.  ``unit_cost_usd`` is NULLABLE by design — dollars are only
    published when a rate card is configured; the meter NEVER invents them.

    ``quantity`` and ``unit_cost_usd`` are exact-precision ``Numeric`` (mapped
    to :class:`decimal.Decimal`) to match the migration and to keep aggregation
    penny-exact — floats would silently drift a published cost total.
    """

    __tablename__ = "cost_ledger"

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    cycle_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, default=Decimal("0"),
    )
    source_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    unit_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )

    __table_args__ = (
        Index("ix_cost_ledger_tenant_app_created", "tenant_id", "app_id", "created_at"),
        Index("ix_cost_ledger_tenant_cycle", "tenant_id", "cycle_id"),
    )


__all__ = [
    "AppCycleRow",
    "ChangeEventRow",
    "AppFingerprintRow",
    "CostLedgerRow",
    "CYCLE_STATES",
    "TERMINAL_CYCLE_STATES",
    "is_terminal_cycle_state",
    "CYCLE_STATE_PENDING",
    "CYCLE_STATE_PROBING",
    "CYCLE_STATE_SELECTING",
    "CYCLE_STATE_CRAWLING",
    "CYCLE_STATE_GENERATING",
    "CYCLE_STATE_RUNNING",
    "CYCLE_STATE_HEALING",
    "CYCLE_STATE_VERIFYING",
    "CYCLE_STATE_DONE",
    "CYCLE_STATE_FAILED",
    "CYCLE_STATE_BUDGET_STOPPED",
    "CYCLE_STATE_BLACKOUT_DEFERRED",
    "CYCLE_TRIGGERS",
    "CYCLE_TRIGGER_WEBHOOK_REPO",
    "CYCLE_TRIGGER_SCHEDULE",
    "CYCLE_TRIGGER_PROBE_DRIFT",
    "CYCLE_TRIGGER_MANUAL",
    "CYCLE_TRIGGER_FULL_FLOOR",
    "CHANGE_SOURCES",
    "CHANGE_SOURCE_REPO_SHA",
    "CHANGE_SOURCE_PROBE",
    "CHANGE_SOURCE_MANUAL",
    "CHANGE_SOURCE_SCHEDULE_FLOOR",
]
