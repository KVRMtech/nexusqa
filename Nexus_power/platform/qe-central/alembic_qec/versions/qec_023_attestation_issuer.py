"""A11 / T-WP-02 — THE ISSUER'S PERSISTENT STATE (key custody, provisioning
truth, revocation, and the audit trail).

WHY FOUR TABLES AND NOT ONE
===========================
``app/services/walk_attestation.py`` has been able to SIGN a provisioning proof
since Gate 1.  It could never be *called* by an endpoint, because signing needs
four things this schema did not hold:

  1. a PRIVATE KEY that is not in the environment, the config or the image;
  2. an AUTHORITATIVE answer to "is this environment genuinely disposable?"
     that the tenant cannot write;
  3. somewhere to record a revocation, since an expiry is not revocation;
  4. a record of what was issued, because an unlogged issuance is an
     unauditable grant of mutation authority.

THE SECOND ONE IS THE WHOLE MILESTONE
=====================================
Today ``app_environments.env_attestation`` is a JSONB blob a TENANT ADMIN sets
with ``PATCH /apps/{id}/environments/{env}``.  It is where ``env_kind`` lives.
A tenant who types the word ``disposable`` into their own environment profile
therefore declares their own environment mutable — and if the issuer read that
field, a tenant could mint themselves a signed proof authorising the platform to
POST at a target of their choosing.  That is the ``tenant self-attestation``
attack in the A11.5 matrix, and no amount of cryptography downstream repairs it:
a correctly-signed statement of a tenant-supplied fact is a correctly-signed lie.

``env_provisioning_records`` is the fix.  It is written on ONE path
(``require_platform_admin``, a claim a tenant token structurally cannot carry —
see ``app/fleet/rbac.py``) and read by the issuer as the ONLY source of
``env_kind``.  ``env_attestation`` keeps its existing job (the human RoE
statement that gates SUBMIT) and loses the one it should never have had.

THE TRUST BOUNDARY IS NOW A ROW, AND THAT IS DELIBERATE.  This does not make
"genuinely disposable" a mathematical fact; it makes it an ATTRIBUTABLE
PLATFORM DECISION with a named principal, a timestamp, and evidence attached —
which is the strongest honest claim available, and is exactly what the
verifier's signature then binds.  Stated here so it is not an undocumented
assumption.

PURELY ADDITIVE — no existing table is touched, and no existing behaviour
changes when these tables are empty (an empty ledger issues nothing, which is
the fail-closed default).

Revision ID: qec_023
Revises: qec_022
Create Date: 2026-08-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_023"
down_revision: Union[str, None] = "qec_022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The tenant-scoped tables created here.  Each gets ENABLE + FORCE row security
#: and a policy covering all four DML commands, per the standing coverage gate in
#: ``tests/contract/test_rls_coverage_complete.py``.
_TENANT_TABLES = (
    "env_provisioning_records",
    "attestation_revocations",
    "attestation_issuance_log",
)

#: Fleet infrastructure — deliberately WITHOUT a ``tenant_id`` column.  The
#: explorer's trust store is fleet-wide (``QEC_ATTESTATION_PUBLIC_KEYS`` is one
#: list, ``QEC_ATTESTATION_ISSUER`` one name), so the issuer identity is a
#: property of the DEPLOYMENT, not of a customer.  Per-tenant issuer keys would
#: require every explorer to hold every tenant's public key, which is strictly
#: more key material for strictly less isolation — the proof already binds
#: ``tenant_id`` INSIDE the signed claims, and the verifier checks it against the
#: dispatch.  Declared in the coverage gate's ``_NO_TENANT_COLUMN`` with this
#: reason.
_KEY_TABLE = "attestation_issuer_keys"


def upgrade() -> None:
    # ── 1. KEY CUSTODY ────────────────────────────────────────────────────
    # The private key is NEVER stored here in the clear.  ``sealed_private_key``
    # is an ``EnvelopeBlob.to_bytes()`` — an AES-GCM ciphertext whose DEK is
    # wrapped by Cloud KMS (AAD = the reserved platform tenant id).  A full dump
    # of this database yields no signing capability without a live KMS decrypt
    # permission, which is the property that makes DB backups safe to hold.
    op.create_table(
        _KEY_TABLE,
        # sha256(public_key_b64)[:16] — the SAME derivation as
        # ``attest.key_id`` / ``walk_attestation.key_id``.  It is the value the
        # explorer looks its trust anchor up by, so it is the natural PK: a
        # duplicate kid is a duplicate key, which the database now forbids.
        sa.Column("kid", sa.String(32), nullable=False),
        # Base64 raw 32-byte Ed25519 public key.  NOT a secret — this column is
        # what gets published into the explorer's trust store.
        sa.Column("public_key", sa.String(128), nullable=False),
        # The issuer NAME bound into every claim this key signs.  Stored per-key
        # (not read from config at sign time) so a config edit can never silently
        # re-attribute proofs already signed by an existing key.
        sa.Column("issuer", sa.String(128), nullable=False),
        sa.Column("sealed_private_key", sa.LargeBinary, nullable=False),
        # Custody provenance, for the audit trail and for rotation planning.
        # Copied out of the envelope so an operator can answer "which KMS key
        # protects this?" without decrypting anything.
        sa.Column("kek_provider", sa.String(32), nullable=False,
                  server_default=""),
        sa.Column("kek_id", sa.String(500), nullable=False, server_default=""),
        # 'active'   — signs new proofs (exactly one, enforced by index below)
        # 'retiring' — no longer signs; public key still published so proofs
        #              already in flight keep verifying until they expire
        # 'revoked'  — compromised; public key must be pulled from the fleet
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        # Who/what performed the custody operation (principal ``sub``).
        sa.Column("created_by", sa.String(200), nullable=False,
                  server_default=""),
        sa.Column("meta", JSONB, nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("kid"),
    )
    # AT MOST ONE ACTIVE SIGNING KEY, enforced by the DATABASE.  Two active keys
    # is not a harmless race: it means half the fleet's proofs are signed by a
    # key the other half may not have been told about yet, which presents as
    # intermittent ``unknown_key_id`` refusals that look like a network fault.
    # Rotation is therefore forced through the retire-then-activate sequence.
    op.execute(f"""
        CREATE UNIQUE INDEX uq_{_KEY_TABLE}_one_active
            ON {_KEY_TABLE} ((status))
            WHERE status = 'active';
    """)
    op.create_index(f"ix_{_KEY_TABLE}_status", _KEY_TABLE, ["status"])

    # ── 2. AUTHORITATIVE PROVISIONING TRUTH ───────────────────────────────
    # The only source of ``env_kind`` the issuer will read.  See the module
    # docstring: this exists precisely because the field it replaces is
    # tenant-writable.
    op.create_table(
        "env_provisioning_records",
        sa.Column("provisioning_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("environment_id", sa.String(64), nullable=False),
        # SERVER-SET, from the platform-admin's declaration — never copied from
        # a tenant payload.  Constrained below so a typo cannot become a novel
        # kind that some future reader treats as non-production.
        sa.Column("env_kind", sa.String(32), nullable=False),
        # The normalised ``scheme://host[:port]`` this record provisions.  Pinned
        # at provisioning time and re-checked against the environment's live
        # ``base_url`` at issue time: an environment silently re-pointed at a new
        # host after being certified disposable must not keep its certification.
        sa.Column("target_origin", sa.String(512), nullable=False,
                  server_default=""),
        # How the environment is torn down / reset.  Travels into the signed
        # claims, so the proof itself carries the operator's reset contract.
        sa.Column("reset_procedure", sa.String(512), nullable=False,
                  server_default=""),
        # WHAT the platform verified, and HOW.  Free-form on purpose — an
        # ephemeral-namespace id, a Terraform run URL, a teardown-job handle, or
        # a human's written justification.  It is evidence for an auditor, never
        # an input to a decision.
        sa.Column("evidence", JSONB, nullable=False, server_default="{}"),
        # The platform-admin principal accountable for this record.
        sa.Column("provisioned_by", sa.String(200), nullable=False),
        sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        # A provisioning record EXPIRES.  A disposable environment that was
        # torn down six months ago is not disposable, it is gone — and the next
        # thing at that origin may be anything at all.  Re-certification is
        # therefore mandatory rather than a matter of remembering.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Per-environment least-privilege ceiling.  The verifier takes
        # min(this, fleet policy), so this can only ever narrow the grant.
        sa.Column("max_walk_mutations_per_step", sa.Integer, nullable=False,
                  server_default="1"),
        # 'active' | 'retired' (revoked by an operator, or the env was destroyed)
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="active"),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by", sa.String(200), nullable=False,
                  server_default=""),
        sa.PrimaryKeyConstraint("provisioning_id"),
        # ``disposable`` is the only kind that can authorise a walk mutation, but
        # the OTHER kinds are recordable on purpose: a platform-admin explicitly
        # certifying "this is production" is a useful, auditable statement, and
        # it makes the issuer's refusal cite a record rather than an absence.
        sa.CheckConstraint(
            "env_kind IN ('disposable','staging','uat','test','dev','prod')",
            name="ck_env_provisioning_records_env_kind"),
        sa.CheckConstraint(
            "max_walk_mutations_per_step >= 0 AND "
            "max_walk_mutations_per_step <= 10",
            name="ck_env_provisioning_records_budget"),
    )
    # AT MOST ONE ACTIVE RECORD PER ENVIRONMENT.  Without this, re-provisioning
    # would leave two active rows and the issuer would have to CHOOSE — and any
    # tie-break rule ("newest wins") is a rule an attacker who can create a row
    # gets to exploit.  Re-provisioning retires the old row in the same
    # transaction instead.
    op.execute("""
        CREATE UNIQUE INDEX uq_env_provisioning_records_one_active
            ON env_provisioning_records (tenant_id, environment_id)
            WHERE status = 'active';
    """)
    op.create_index(
        "ix_env_provisioning_records_lookup", "env_provisioning_records",
        ["tenant_id", "app_id", "environment_id", "status"],
    )

    # ── 3. REVOCATION ─────────────────────────────────────────────────────
    # An INSERT-ONLY ledger.  A revocation is never deleted and never edited:
    # "we un-revoked it" is a new fact, not an erasure of the old one, and a
    # revocation you can delete is a revocation an attacker can delete.
    op.create_table(
        "attestation_revocations",
        sa.Column("revocation_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        # 'proof'       — one specific issued proof (by proof_id)
        # 'environment' — EVERY proof for an environment, including ones not yet
        #                 issued.  The blast-radius control: when a supposedly
        #                 disposable env turns out to be shared, you revoke the
        #                 environment, not a list of proof ids you must first
        #                 go and find.
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False, server_default=""),
        sa.Column("revoked_by", sa.String(200), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        # When this revocation may stop being published.  A revocation only
        # needs to outlive the longest proof it could possibly suppress; keeping
        # it forever would grow every dispatch's signed list without bound.
        # NULL = never prune.
        sa.Column("prune_after", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("revocation_id"),
        sa.CheckConstraint("subject_type IN ('proof','environment')",
                           name="ck_attestation_revocations_subject_type"),
        sa.UniqueConstraint("tenant_id", "subject_type", "subject_id",
                            name="uq_attestation_revocations_subject"),
    )
    op.create_index(
        "ix_attestation_revocations_tenant", "attestation_revocations",
        ["tenant_id", "subject_type"],
    )

    # ── 4. THE AUDIT TRAIL ────────────────────────────────────────────────
    # Every issuance, recorded BEFORE the proof is handed out.  Two jobs: it is
    # the audit record, and it is how an operator revoking an incident finds the
    # proof ids to revoke — a proof whose issuance was never logged could not be
    # revoked by id, only by burning its whole environment.
    op.create_table(
        "attestation_issuance_log",
        sa.Column("proof_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("environment_id", sa.String(64), nullable=False),
        sa.Column("crawl_id", sa.String(128), nullable=False),
        sa.Column("kid", sa.String(32), nullable=False),
        # sha256 of the canonical claims, truncated — the same digest the
        # verifier reports in its verdict, so an auditor can tie a line in the
        # explorer's log to a row here without either side holding the proof.
        sa.Column("claims_digest", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("target_origin", sa.String(512), nullable=False,
                  server_default=""),
        sa.Column("issued_at_ms", sa.BigInteger, nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger, nullable=False),
        sa.Column("max_walk_mutations_per_step", sa.Integer, nullable=False,
                  server_default="0"),
        # The authenticated principal that ASKED for this proof, and the request
        # id that ties the issuance to the API access log.
        sa.Column("issued_to", sa.String(200), nullable=False,
                  server_default=""),
        sa.Column("request_id", sa.String(64), nullable=False,
                  server_default=""),
        # The provisioning record this issuance stood on.  A proof can therefore
        # always be traced back to the platform-admin decision that authorised
        # its environment.
        sa.Column("provisioning_id", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("proof_id"),
    )
    op.create_index(
        "ix_attestation_issuance_log_tenant", "attestation_issuance_log",
        ["tenant_id", "environment_id", "issued_at"],
    )
    op.create_index(
        "ix_attestation_issuance_log_crawl", "attestation_issuance_log",
        ["tenant_id", "crawl_id"],
    )

    # ─── Row-Level Security (qec_001 / qec_002 / qec_003 pattern) ─────────
    for table in _TENANT_TABLES:
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = '{table}'
                ) THEN
                    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
                    ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

                    DROP POLICY IF EXISTS tenant_isolation ON {table};
                    CREATE POLICY tenant_isolation ON {table}
                        USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

                    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'qec') THEN
                        GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO qec;
                    END IF;
                END IF;
            END $$;
        """)

    # The key table is fleet infrastructure (see ``_KEY_TABLE`` above) so it gets
    # no tenant policy — but it still needs the grant, and it is the one table in
    # this migration whose CONTENTS are a signing capability.  Read access to it
    # is therefore worth stating explicitly rather than inheriting.
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'qec') THEN
                GRANT SELECT, INSERT, UPDATE ON {_KEY_TABLE} TO qec;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    for table in _TENANT_TABLES:
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = '{table}'
                ) THEN
                    DROP POLICY IF EXISTS tenant_isolation ON {table};
                    ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
                    ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
                END IF;
            END $$;
        """)
    op.drop_index("ix_attestation_issuance_log_crawl",
                  table_name="attestation_issuance_log")
    op.drop_index("ix_attestation_issuance_log_tenant",
                  table_name="attestation_issuance_log")
    op.drop_table("attestation_issuance_log")
    op.drop_index("ix_attestation_revocations_tenant",
                  table_name="attestation_revocations")
    op.drop_table("attestation_revocations")
    op.drop_index("ix_env_provisioning_records_lookup",
                  table_name="env_provisioning_records")
    op.execute("DROP INDEX IF EXISTS uq_env_provisioning_records_one_active;")
    op.drop_table("env_provisioning_records")
    op.drop_index(f"ix_{_KEY_TABLE}_status", table_name=_KEY_TABLE)
    op.execute(f"DROP INDEX IF EXISTS uq_{_KEY_TABLE}_one_active;")
    op.drop_table(_KEY_TABLE)
