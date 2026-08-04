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


__all__ = ["AdvanceMemoryRow", "AdvanceLabelPriorRow"]
