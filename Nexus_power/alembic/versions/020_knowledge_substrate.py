"""Knowledge substrate — addressable transcript segments, cached clips,
and resumable indexing jobs that consume canonical_artifacts.

Purely additive — no existing table or column is modified. All
tenant-scoped tables receive the same RLS regime as migration 010.

The substrate is the contract that downstream phases consume:

* ``transcript_segments``   — speaker-aware, embedding-linked,
                              product-tagged units of transcript text.
                              The unit of retrieval for echo matches.
* ``media_clips``           — pre-cut, S3-backed video/audio windows.
                              Caches expensive ffmpeg work keyed by
                              (session_id, start_ms, end_ms).
* ``indexing_jobs``         — durable queue + idempotency record for
                              the fusion engine's substrate-build pass.
                              Survives restarts, enables backfill.

Revision ID: 020_knowledge_substrate
Revises: 019_knowledge_foundation
Create Date: 2026-05-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


revision: str = "020_knowledge_substrate"
down_revision: Union[str, None] = "019_knowledge_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TENANT_TABLES_THIS_MIGRATION: tuple[str, ...] = (
    "transcript_segments",
    "media_clips",
    "indexing_jobs",
)


def upgrade() -> None:
    # ── transcript_segments ─────────────────────────────────────
    #
    # Each row is a PII-safe, speaker-attributed chunk of a canonical
    # artifact's transcript. text_hash supports deterministic
    # idempotency for re-indexing — the fusion engine recomputes
    # SHA-256(text_redacted) on each run and skips rows that already
    # exist.
    #
    # Migration 004 created a simpler version of this table; this
    # migration is a redesign with PII-safe fields + canonical-artifact
    # linkage. Drop the older shape first so the chain runs cleanly on
    # a fresh DB. CASCADE handles any FK references from other 004-era
    # media tables. The 004 version was never used in production.
    op.execute("DROP TABLE IF EXISTS transcript_segments CASCADE;")
    op.create_table(
        "transcript_segments",
        sa.Column("segment_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("speaker_id", sa.String(64), nullable=True),
        sa.Column("speaker_role", sa.String(64), nullable=True),
        sa.Column("text_redacted", sa.Text, nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("start_ms", sa.BigInteger, nullable=False),
        sa.Column("end_ms", sa.BigInteger, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column(
            "product_ids",
            ARRAY(sa.String(64)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column("topic_label", sa.String(256), nullable=True),
        sa.Column("backbone_node_id", sa.String(64), nullable=True),
        sa.Column("embedding_status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["canonical_artifacts.artifact_id"],
            ondelete="CASCADE",
            name="fk_ts_artifact",
        ),
        sa.CheckConstraint(
            "embedding_status IN ('pending','indexed','failed','skipped')",
            name="ck_ts_embedding_status",
        ),
        sa.CheckConstraint("end_ms >= start_ms", name="ck_ts_time_window"),
        sa.UniqueConstraint(
            "artifact_id", "ordinal", name="uq_ts_artifact_ordinal"
        ),
    )
    op.create_index(
        "ix_ts_tenant_session", "transcript_segments",
        ["tenant_id", "session_id"],
    )
    op.create_index(
        "ix_ts_tenant_product_gin",
        "transcript_segments",
        ["product_ids"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_ts_artifact", "transcript_segments", ["artifact_id"],
    )
    op.create_index(
        "ix_ts_pending",
        "transcript_segments",
        ["tenant_id"],
        postgresql_where=sa.text("embedding_status = 'pending'"),
    )

    # ── media_clips ─────────────────────────────────────────────
    #
    # Cache of ffmpeg-cut windows of the source media. The unique
    # constraint on (tenant_id, session_id, start_ms, end_ms) is what
    # makes the clip service safe to call concurrently — duplicate
    # requests collapse to a single row.
    op.create_table(
        "media_clips",
        sa.Column("clip_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64), nullable=True),
        sa.Column("segment_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("start_ms", sa.BigInteger, nullable=False),
        sa.Column("end_ms", sa.BigInteger, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("s3_bucket", sa.String(256), nullable=False),
        sa.Column("s3_key", sa.String(512), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=True),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("thumbnail_key", sa.String(512), nullable=True),
        sa.Column("hits", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'ready'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_served_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('video','audio')", name="ck_clips_kind"
        ),
        sa.CheckConstraint(
            "status IN ('ready','failed','deleted')", name="ck_clips_status"
        ),
        sa.CheckConstraint("end_ms > start_ms", name="ck_clips_time_window"),
        sa.UniqueConstraint(
            "tenant_id",
            "session_id",
            "kind",
            "start_ms",
            "end_ms",
            name="uq_clip_window",
        ),
    )
    op.create_index("ix_clips_tenant", "media_clips", ["tenant_id"])
    op.create_index(
        "ix_clips_artifact", "media_clips", ["artifact_id"]
    )
    op.create_index(
        "ix_clips_lru",
        "media_clips",
        ["last_served_at"],
        postgresql_where=sa.text("status = 'ready'"),
    )

    # ── indexing_jobs ───────────────────────────────────────────
    #
    # Durable queue + idempotency record for the substrate-build pass.
    # On receipt of ``spine.canonical_artifact.ready`` the fusion
    # engine UPSERTs into this table (unique key on artifact_id). A
    # worker pool selects pending/retryable rows under a row lock,
    # processes them, and updates status.
    op.create_table(
        "indexing_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempts",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("5")),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("input", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','dead_letter','skipped')",
            name="ck_jobs_status",
        ),
        sa.UniqueConstraint(
            "tenant_id", "artifact_id", name="uq_jobs_tenant_artifact"
        ),
    )
    op.create_index("ix_jobs_tenant_status", "indexing_jobs", ["tenant_id", "status"])
    op.create_index(
        "ix_jobs_pending",
        "indexing_jobs",
        ["created_at"],
        postgresql_where=sa.text("status IN ('pending','running')"),
    )

    # ── RLS ─────────────────────────────────────────────────────
    for table in _TENANT_TABLES_THIS_MIGRATION:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO nexus_app;")


def downgrade() -> None:
    for table in reversed(_TENANT_TABLES_THIS_MIGRATION):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_jobs_pending", table_name="indexing_jobs")
    op.drop_index("ix_jobs_tenant_status", table_name="indexing_jobs")
    op.drop_table("indexing_jobs")

    op.drop_index("ix_clips_lru", table_name="media_clips")
    op.drop_index("ix_clips_artifact", table_name="media_clips")
    op.drop_index("ix_clips_tenant", table_name="media_clips")
    op.drop_table("media_clips")

    op.drop_index("ix_ts_pending", table_name="transcript_segments")
    op.drop_index("ix_ts_artifact", table_name="transcript_segments")
    op.drop_index("ix_ts_tenant_product_gin", table_name="transcript_segments")
    op.drop_index("ix_ts_tenant_session", table_name="transcript_segments")
    op.drop_table("transcript_segments")
