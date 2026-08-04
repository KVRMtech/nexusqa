"""QE-Central Phase-7 — ORM for the ``tenant_provisioning`` control record.

ONE table, added by migration ``qec_002_tenant_provisioning`` (the migration is
the source of truth; every column here mirrors it EXACTLY).  It is QE-Central's
OWN mutable, RLS-scoped, per-tenant lifecycle + quota control record — kept in
the qecentral bounded context (role ``qec``, full CRUD) rather than in the
VKPower ``nexus.tenants`` registry, which QE-Central may only SELECT/INSERT (the
``qec_substrate`` grant has NO UPDATE) and which VKPower alone owns (R-7).

Why a table (and not a JSONB column on an existing one): the lifecycle state
must be MUTABLE at runtime (suspend/resume/offboard), SHARED across replicas
(Phase-7 fleet scale), and RLS-isolated per tenant — and no existing qecentral
table is keyed purely on ``tenant_id``.  Shipping it OUTSIDE the alembic chain
would repeat the ``ground_truth_events`` no-migration defect, so it rides
``qec_002``.

BACKWARD-COMPAT: a tenant with NO row here is treated as ACTIVE / UNLIMITED by
the lifecycle gate + plan resolver, so every existing tenant behaves unchanged.

Binds the QE-Central-private ``QecBase`` (NOT the SDK ``Base``) — engine-enforced
bounded context (R-7).  Timestamps are UTC timezone-aware.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .models import QecBase


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TenantProvisioningRow(QecBase):
    """One tenant's lifecycle + quota control record (design Phase-7).

    ``status`` walks ``active -> suspended -> active`` / ``* -> offboarding ->
    deleted`` (see :mod:`app.fleet.lifecycle`).  ``plan`` names the quota envelope
    resolved via :func:`app.fleet.quota.resolve_plan` (the stored plan is fed as
    that resolver's ``plan_name`` hint); ``quota_overrides`` is a reserved
    per-tenant override store.
    ``retention_until`` + ``purge_requested`` govern offboarding data retention —
    evidence is NEVER hard-deleted here (a retention job is a separate seam); this
    row only RECORDS the policy.  ``tokens_revoked_at`` marks when a tenant's
    principal tokens were revoked (offboarding); the lifecycle gate refuses all
    crawl/cycle work regardless, so a still-valid JWT is inert operationally.
    """

    __tablename__ = "tenant_provisioning"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="starter")
    # active | suspended | offboarding | deleted (lifecycle.STATUS_*).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    admin_email: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # Reserved per-tenant quota-override store (the active envelope is resolved
    # by app.fleet.quota.resolve_plan from the ``plan`` column).
    quota_overrides: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    provisioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    offboarding_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Explicit purge request (offboard --purge); evidence deletion is a separate
    # gated seam — this flag NEVER triggers a delete on its own.
    purge_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # OPT-IN consent: contribute this tenant's PROVEN advance-label patterns
    # (value-free product UI text only) to the pooled cross-tenant priors.
    # OFF by default — recall of the pool is open, contribution is gated.
    share_advance_priors: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    # Journey Graph autonomy (Release C4/C5) — BOTH OFF by default, and each
    # also requires its env-level switch (fail-closed): planned branch walks,
    # and the autowalk loop that schedules them after each fold.
    branch_walks_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    journey_autowalk: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now,
    )

    __table_args__ = (
        Index("ix_tenant_provisioning_status", "status"),
    )


__all__ = ["TenantProvisioningRow"]
