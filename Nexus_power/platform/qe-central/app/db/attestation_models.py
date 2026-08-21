"""A11 / T-WP-02 — ORM for the attestation issuer's persistent state.

Four tables, added by migration ``qec_023_attestation_issuer`` (the migration is
the source of truth; every column here mirrors it EXACTLY).  Read that module's
docstring for WHY each exists — in particular why
:class:`EnvProvisioningRecordRow` had to be a new table rather than a read of
``app_environments.env_attestation``.

Binds the QE-Central-private ``QecBase`` (NOT the SDK ``Base``) — engine-enforced
bounded context (R-7).  Timestamps are UTC timezone-aware.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .models import QecBase


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Key custody ─────────────────────────────────────────────────────────────

#: Signing key states.  Only ``ACTIVE`` signs; ``RETIRING`` keeps verifying
#: proofs already in flight; ``REVOKED`` must be pulled from every fleet trust
#: store immediately.
KEY_ACTIVE = "active"
KEY_RETIRING = "retiring"
KEY_REVOKED = "revoked"

#: The public keys an explorer fleet should currently trust: a key that can still
#: legitimately have unexpired proofs in the wild.  A REVOKED key is excluded by
#: construction — that is what revoking it means.
PUBLISHABLE_KEY_STATES = (KEY_ACTIVE, KEY_RETIRING)


class AttestationIssuerKeyRow(QecBase):
    """One Ed25519 issuer keypair, private half sealed under the KMS KEK.

    FLEET INFRASTRUCTURE — deliberately no ``tenant_id``.  The explorer's trust
    store is fleet-wide, and the proof binds ``tenant_id`` inside the signed
    claims, so per-tenant issuer keys would mean every explorer holding every
    tenant's public key: more key material, no more isolation.

    ``sealed_private_key`` is an ``EnvelopeBlob.to_bytes()``.  There is no
    accessor on this class that returns a plaintext private key; unsealing lives
    in :mod:`app.services.attestation_keys` and is scoped to a single sign.
    """

    __tablename__ = "attestation_issuer_keys"

    kid: Mapped[str] = mapped_column(String(32), primary_key=True)
    public_key: Mapped[str] = mapped_column(String(128), nullable=False)
    issuer: Mapped[str] = mapped_column(String(128), nullable=False)
    sealed_private_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kek_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    kek_id: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=KEY_ACTIVE)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_attestation_issuer_keys_status", "status"),
    )


# ── Authoritative provisioning truth ────────────────────────────────────────

PROVISIONING_ACTIVE = "active"
PROVISIONING_RETIRED = "retired"


class EnvProvisioningRecordRow(QecBase):
    """The platform's OWN statement that an environment is what it says it is.

    THE POINT OF THIS TABLE is that a tenant cannot write it.  Its one writer is
    guarded by :func:`app.fleet.rbac.require_platform_admin`, a claim a
    tenant-scoped token structurally cannot carry.  The issuer reads ``env_kind``
    from HERE and from nowhere else — see ``qec_023``'s docstring for the
    self-attestation attack this closes.
    """

    __tablename__ = "env_provisioning_records"

    provisioning_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    env_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_origin: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    reset_procedure: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    provisioned_by: Mapped[str] = mapped_column(String(200), nullable=False)
    provisioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_walk_mutations_per_step: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PROVISIONING_ACTIVE)
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    retired_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    __table_args__ = (
        CheckConstraint(
            "env_kind IN ('disposable','staging','uat','test','dev','prod')",
            name="ck_env_provisioning_records_env_kind"),
        CheckConstraint(
            "max_walk_mutations_per_step >= 0 AND max_walk_mutations_per_step <= 10",
            name="ck_env_provisioning_records_budget"),
        Index("ix_env_provisioning_records_lookup",
              "tenant_id", "app_id", "environment_id", "status"),
    )


# ── Revocation ──────────────────────────────────────────────────────────────

SUBJECT_PROOF = "proof"
SUBJECT_ENVIRONMENT = "environment"


class AttestationRevocationRow(QecBase):
    """One revoked subject.  INSERT-ONLY: a revocation is never edited or
    deleted, because a revocation an attacker can delete is not a revocation."""

    __tablename__ = "attestation_revocations"

    revocation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    revoked_by: Mapped[str] = mapped_column(String(200), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    prune_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("subject_type IN ('proof','environment')",
                        name="ck_attestation_revocations_subject_type"),
        UniqueConstraint("tenant_id", "subject_type", "subject_id",
                         name="uq_attestation_revocations_subject"),
        Index("ix_attestation_revocations_tenant", "tenant_id", "subject_type"),
    )


# ── Audit trail ─────────────────────────────────────────────────────────────


class AttestationIssuanceLogRow(QecBase):
    """One issued proof, recorded BEFORE it is handed to anybody.

    Also the operational index for revocation: an incident responder finds the
    proof ids to revoke by querying this table.  A proof whose issuance was never
    logged could only be revoked by burning its entire environment.
    """

    __tablename__ = "attestation_issuance_log"

    proof_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    crawl_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kid: Mapped[str] = mapped_column(String(32), nullable=False)
    claims_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    target_origin: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    issued_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_walk_mutations_per_step: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    issued_to: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    provisioning_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("ix_attestation_issuance_log_tenant",
              "tenant_id", "environment_id", "issued_at"),
        Index("ix_attestation_issuance_log_crawl", "tenant_id", "crawl_id"),
    )


__all__ = [
    "KEY_ACTIVE", "KEY_RETIRING", "KEY_REVOKED", "PUBLISHABLE_KEY_STATES",
    "PROVISIONING_ACTIVE", "PROVISIONING_RETIRED",
    "SUBJECT_PROOF", "SUBJECT_ENVIRONMENT",
    "AttestationIssuerKeyRow", "EnvProvisioningRecordRow",
    "AttestationRevocationRow", "AttestationIssuanceLogRow",
]
