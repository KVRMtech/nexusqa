"""Knowledge Echo MVP — dispatch + feedback + dedup tables.

Adds the persistence the echo product service needs to:

* Audit every echo decision (shadow / DM / live / suppressed).
* Capture user feedback (thumbs up / down / ask-SME / opened-clip).
* Deduplicate near-identical questions inside a configurable window.

All tables are tenant-scoped and receive the same RLS policy as
migration 010 / 019 / 020.

Revision ID: 021_echo_mvp
Revises: 020_knowledge_substrate
Create Date: 2026-05-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "021_echo_mvp"
down_revision: Union[str, None] = "020_knowledge_substrate"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TENANT_TABLES_THIS_MIGRATION: tuple[str, ...] = (
    "echo_dispatches",
    "echo_feedback",
    "echo_dedup",
)


def upgrade() -> None:
    # ── echo_dispatches ─────────────────────────────────────────
    #
    # One row per evaluated trigger. ``decision`` records what the
    # orchestrator did:
    #   * 'posted_channel'  — message posted to the surface channel
    #   * 'posted_dm'       — DMed the asker (DM-only mode, or medium conf)
    #   * 'shadow_logged'   — full pipeline executed; nothing sent
    #   * 'suppressed_*'    — duplicate, low confidence, circuit open, etc.
    op.create_table(
        "echo_dispatches",
        sa.Column("dispatch_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("trigger_surface", sa.String(32), nullable=False),
        sa.Column("trigger_plugin_event_id", sa.String(128)),
        sa.Column("trigger_user_id_ext", sa.String(128)),
        sa.Column("trigger_channel_ext", sa.String(128)),
        sa.Column("trigger_text_hash", sa.String(64), nullable=False),
        sa.Column("classifier_output", JSONB),
        sa.Column("match_candidates", JSONB),
        sa.Column("top_similarity", sa.Float),
        sa.Column("confidence_band", sa.String(16)),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("decision_reason", sa.String(256)),
        sa.Column("effective_mode", sa.String(32)),
        sa.Column("rendered_payload_hash", sa.String(64)),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("posted_message_ref", sa.String(256)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "trigger_surface IN ('slack','teams','email','webhook','web')",
            name="ck_ed_surface",
        ),
        sa.CheckConstraint(
            "decision IN ("
            "'posted_channel','posted_dm','shadow_logged',"
            "'suppressed_dup','suppressed_low_conf','suppressed_circuit',"
            "'suppressed_disabled','suppressed_classifier','suppressed_error'"
            ")",
            name="ck_ed_decision",
        ),
        sa.CheckConstraint(
            "confidence_band IN ('high','medium','low','none') OR confidence_band IS NULL",
            name="ck_ed_confidence_band",
        ),
    )
    op.create_index(
        "ix_ed_tenant_time",
        "echo_dispatches",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_ed_trace", "echo_dispatches", ["trace_id"])
    op.create_index(
        "ix_ed_tenant_decision",
        "echo_dispatches",
        ["tenant_id", "decision"],
    )

    # ── echo_feedback ───────────────────────────────────────────
    op.create_table(
        "echo_feedback",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("dispatch_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id_ext", sa.String(128)),
        sa.Column("signal", sa.String(32), nullable=False),
        sa.Column("metadata_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["echo_dispatches.dispatch_id"],
            ondelete="CASCADE",
            name="fk_ef_dispatch",
        ),
        sa.CheckConstraint(
            "signal IN ('thumbs_up','thumbs_down','asked_sme','opened_clip','ran_script','dismissed')",
            name="ck_ef_signal",
        ),
    )
    op.create_index("ix_ef_dispatch", "echo_feedback", ["dispatch_id"])
    op.create_index(
        "ix_ef_tenant_signal",
        "echo_feedback",
        ["tenant_id", "signal"],
    )
    # Idempotency: each (dispatch_id, user_id_ext, signal) is a singleton.
    op.create_index(
        "ix_ef_dedup",
        "echo_feedback",
        ["dispatch_id", "user_id_ext", "signal"],
        unique=True,
        postgresql_where=sa.text("user_id_ext IS NOT NULL"),
    )

    # ── echo_dedup ──────────────────────────────────────────────
    #
    # Suppresses near-identical questions inside a tenant-configurable
    # window. Key is a hash of (channel, sanitised text). expires_at
    # is checked at lookup time; a background sweep (or trigger) can
    # remove expired rows but lookup-time check is authoritative.
    op.create_table(
        "echo_dedup",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("dedup_key", sa.String(128), primary_key=True),
        sa.Column("dispatch_id", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_dedup_expiry", "echo_dedup", ["expires_at"])

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

    op.drop_index("ix_dedup_expiry", table_name="echo_dedup")
    op.drop_table("echo_dedup")

    op.drop_index("ix_ef_dedup", table_name="echo_feedback")
    op.drop_index("ix_ef_tenant_signal", table_name="echo_feedback")
    op.drop_index("ix_ef_dispatch", table_name="echo_feedback")
    op.drop_table("echo_feedback")

    op.drop_index("ix_ed_tenant_decision", table_name="echo_dispatches")
    op.drop_index("ix_ed_trace", table_name="echo_dispatches")
    op.drop_index("ix_ed_tenant_time", table_name="echo_dispatches")
    op.drop_table("echo_dispatches")
