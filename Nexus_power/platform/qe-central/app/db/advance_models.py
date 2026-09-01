"""ORM rows for the E2E advance-memory learning layer (alembic ``qec_004``).

``AdvanceMemoryRow`` is TENANT-PRIVATE (RLS-forced in the migration): one row
per PROVEN decision point — the value-free signature of the stuck page's
eligible controls → the normalized label a crawl demonstrated advances.  A
pick is only ever written here after the crawler observed a genuine advance
(real effect + new unseen state), harvested at completion-callback time from
the flow steps' advance evidence.  An LLM guess is not knowledge.

``AdvanceLabelPriorRow`` is CROSS-TENANT and VALUE-FREE by construction: the
normalized label pattern only (product UI text), a proof count, and
pseudonymous contributor hashes for the distinct-tenant count.  Contribution
is opt-in per tenant (``tenant_provisioning.share_advance_priors``); nothing
tenant-identifying is stored.

Schema is managed by Alembic — these classes exist for typed queries (and for
``QecBase.metadata.create_all`` in DB-backed tests).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .models import QecBase


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AdvanceMemoryRow(QecBase):
    """One proven advance decision, keyed (tenant_id, signature)."""

    __tablename__ = "advance_memory"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: sha256 hex over the eligible controls' normalized names + kinds + the
    #: page title's word shape — computed by
    #: :func:`app.services.advance_agent.compute_signature`. NO URL material.
    signature: Mapped[str] = mapped_column(String(64), primary_key=True)
    chosen_label_norm: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Provenance only — recall is tenant-wide (same shape ⇒ same answer).
    app_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    proof_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_proven_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)


class AdvanceLabelPriorRow(QecBase):
    """One pooled, value-free advance-label pattern (no tenant key BY DESIGN)."""

    __tablename__ = "advance_label_priors"

    label_norm: Mapped[str] = mapped_column(String(200), primary_key=True)
    proof_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    distinct_tenants: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Pseudonymous contributor markers (sha256(tenant_id) prefixes) — enough
    #: to count distinct tenants, never enough to name one.
    contributor_hashes: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list)
    last_proven_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)


class MechanicMemoryRow(QecBase):
    """R4: one proven control-interaction mechanic, keyed (tenant_id, control_sig).

    When the explorer verifies (R0) that a specific ladder rung operates a
    control, it writes the rung's variant name here. On the next crawl, the
    explorer tries the proven mechanic FIRST — no ladder walk, no medic.

    The key is the control's field-signature digest (value-free: name tokens +
    kind + input_type + option shape). One mechanic per control per tenant."""

    __tablename__ = "control_mechanics"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    control_sig: Mapped[str] = mapped_column(String(64), primary_key=True)
    mechanic: Mapped[str] = mapped_column(String(80), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    proof_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_proven_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)


class MechanicPriorRow(QecBase):
    """R4: pooled, value-free mechanic knowledge (no tenant key BY DESIGN).

    One row per (control_sig, mechanic) — the cross-tenant evidence that a
    particular control shape is best operated with a particular mechanic.
    Contribution is opt-in (consent-gated, OFF by default)."""

    __tablename__ = "mechanic_priors"

    control_sig: Mapped[str] = mapped_column(String(64), primary_key=True)
    mechanic: Mapped[str] = mapped_column(String(80), primary_key=True)
    proof_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    distinct_tenants: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    contributor_hashes: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list)
    last_proven_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)


__all__ = ["AdvanceMemoryRow", "AdvanceLabelPriorRow",
           "MechanicMemoryRow", "MechanicPriorRow"]
