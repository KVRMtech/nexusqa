"""Recovery Agent v2 — persistence + human-gated decisions for proposals.

v1 (recovery_agent.py) classifies failures and PRODUCES proposal bundles on
demand. v2 adds the durable, auditable gate the requirement calls for:

    persist a scan's PRODUCT-CAPABILITY-GAP proposals  ->  a human APPROVES or
    REJECTS each (attributed + timestamped)  ->  the decision is recorded.

DOCTRINE (unchanged, enforced): the agent NEVER applies a proposal, never edits
code, never re-runs anything itself. Approval records INTENT + attribution; the
code change and the verifying re-run remain human actions (the failing case is
the repro — red now, green after the human's fix). This keeps the loop inside
the platform's own auditability principle.

The table binds the SDK ``Base`` (create_all-only, like surface_prefs) so it
needs no alembic migration; RLS is applied operator-side. Every read/write is
tenant-scoped.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base

STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_RESOLVED = "resolved"   # a later green run proved the gap closed
_TERMINAL = frozenset({STATUS_APPROVED, STATUS_REJECTED, STATUS_RESOLVED})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _proposal_id(tenant_id: str, artifact_id: str, scenario_id: str, cause: str) -> str:
    """Stable id per (tenant, artifact, scenario, cause) — re-persisting the same
    gap UPSERTs rather than piling up duplicate proposals across runs."""
    h = hashlib.sha256(f"{tenant_id}|{artifact_id}|{scenario_id}|{cause}".encode()).hexdigest()
    return h[:40]


class RecoveryProposalRow(Base):
    """One human-gated capability-gap proposal from the Recovery Agent."""

    __tablename__ = "recovery_proposals"

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    step_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    classification: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    cause: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    suggested_strategy: Mapped[str] = mapped_column(Text, nullable=False, default="")
    bundle_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_PROPOSED)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    decision_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False)

    def as_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "step_number": int(self.step_number or 0),
            "classification": self.classification,
            "cause": self.cause,
            "suggested_strategy": self.suggested_strategy,
            "bundle": json.loads(self.bundle_json or "{}"),
            "status": self.status,
            "decided_by": self.decided_by,
            "decision_note": self.decision_note,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


async def persist_scan(
    session: AsyncSession, *, tenant_id: str, artifact_id: str, run_id: str,
    proposals: list[dict],
) -> int:
    """UPSERT each capability-gap proposal from a scan. A proposal already in a
    TERMINAL state (approved/rejected/resolved) is NOT reopened by a repeat scan
    — the human decision stands until a green run resolves it. Returns the count
    newly created or refreshed. BEST-EFFORT / fail-open (table absent -> 0)."""
    if not tenant_id or not proposals:
        return 0
    n = 0
    try:
        for p in proposals:
            cause = str(p.get("cause") or "")
            sid = str(p.get("scenario_id") or "")
            pid = _proposal_id(tenant_id, artifact_id, sid, cause)
            row = (await session.execute(
                select(RecoveryProposalRow).where(
                    RecoveryProposalRow.proposal_id == pid,
                    RecoveryProposalRow.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            if row is None:
                session.add(RecoveryProposalRow(
                    proposal_id=pid, tenant_id=tenant_id, artifact_id=artifact_id,
                    run_id=run_id, scenario_id=sid, step_number=int(p.get("step_number") or 0),
                    classification=str(p.get("kind") or "capability_gap_proposal"),
                    cause=cause, suggested_strategy=str(p.get("suggested_strategy") or ""),
                    bundle_json=json.dumps(p, default=str)[:20000]))
                n += 1
            elif row.status not in _TERMINAL:
                row.run_id = run_id
                row.step_number = int(p.get("step_number") or 0)
                row.suggested_strategy = str(p.get("suggested_strategy") or "")
                row.bundle_json = json.dumps(p, default=str)[:20000]
                row.updated_at = _utc_now()
                n += 1
        await session.flush()
    except Exception:
        return 0
    return n


async def list_proposals(
    session: AsyncSession, *, tenant_id: str, artifact_id: str = "",
    status: str = "",
) -> list[dict]:
    """Persisted proposals for a tenant (optionally one artifact / one status),
    newest first. Read-only, fail-open."""
    if not tenant_id:
        return []
    try:
        q = select(RecoveryProposalRow).where(RecoveryProposalRow.tenant_id == tenant_id)
        if artifact_id:
            q = q.where(RecoveryProposalRow.artifact_id == artifact_id)
        if status:
            q = q.where(RecoveryProposalRow.status == status)
        q = q.order_by(RecoveryProposalRow.updated_at.desc()).limit(200)
        rows = (await session.execute(q)).scalars().all()
    except Exception:
        return []
    return [r.as_dict() for r in rows]


async def record_decision(
    session: AsyncSession, *, tenant_id: str, proposal_id: str, decision: str,
    decided_by: str, note: str = "",
) -> dict | None:
    """Record a human APPROVE / REJECT on a proposal (attributed + timestamped).
    The agent still applies nothing — this captures INTENT + accountability. A
    later green run of the repro case flips it to RESOLVED via
    :func:`resolve_if_passing` (never auto-closed here). Returns the updated row
    dict, or None if not found / bad decision."""
    decision = (decision or "").strip().lower()
    if decision not in ("approve", "reject") or not (proposal_id and decided_by):
        return None
    try:
        row = (await session.execute(
            select(RecoveryProposalRow).where(
                RecoveryProposalRow.proposal_id == proposal_id,
                RecoveryProposalRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if row is None:
            return None
        row.status = STATUS_APPROVED if decision == "approve" else STATUS_REJECTED
        row.decided_by = str(decided_by)[:128]
        row.decision_note = str(note or "")[:2000]
        row.decided_at = _utc_now()
        row.updated_at = _utc_now()
        await session.flush()
        return row.as_dict()
    except Exception:
        return None


async def resolve_if_passing(
    session: AsyncSession, *, tenant_id: str, artifact_id: str,
    passing_scenario_ids: set[str],
) -> int:
    """Flip APPROVED proposals whose repro scenario now PASSES to RESOLVED — the
    honest 'the gap is closed' signal, driven by a real green run (never by the
    agent asserting it). Returns the count resolved."""
    if not (tenant_id and passing_scenario_ids):
        return 0
    try:
        rows = (await session.execute(
            select(RecoveryProposalRow).where(
                RecoveryProposalRow.tenant_id == tenant_id,
                RecoveryProposalRow.artifact_id == artifact_id,
                RecoveryProposalRow.status == STATUS_APPROVED,
            )
        )).scalars().all()
        n = 0
        for row in rows:
            if row.scenario_id in passing_scenario_ids:
                row.status = STATUS_RESOLVED
                row.updated_at = _utc_now()
                n += 1
        await session.flush()
        return n
    except Exception:
        return 0
