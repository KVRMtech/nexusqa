"""Page actions — semantic user actions keyed by page_visit instead of scene.

Phase 2.1 — mirror of ``scene_actions`` but with ``page_visit_id`` as
the parent rather than ``scene_id``.  Solves the merged-scene problem
where 9 distinct URL visits got collapsed into one scene and the
action_extractor returned a single action.

Each row represents one user action that occurred on a specific URL
(page visit).  When the user filled 5 form fields on a single URL,
this table will have 5 rows for that page_visit_id — one per field
change — distinguished by ``subaction_index``.

Schema parity with ``scene_actions`` is intentional so downstream
consumers (test exporters, reconciliation, UI rendering) only need a
trivial adapter to switch sources.

Revision ID: 035_page_actions
Revises: 034_page_visits
Create Date: 2026-05-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "035_page_actions"
down_revision: Union[str, None] = "034_page_visits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "page_actions"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("page_action_id", sa.String(64), primary_key=True),
        sa.Column(
            "page_visit_id",
            sa.String(64),
            sa.ForeignKey("page_visits.page_visit_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),

        # Ordinal within (page_visit_id, extractor_version).  0 for
        # single-action page visits; 0,1,2,... when the user did
        # multiple things on the same URL.
        sa.Column("subaction_index", sa.Integer, nullable=False, server_default="0"),

        # ── Structured action shape (mirrors SceneAction Pydantic) ──
        # Values: click | type | select | scroll | navigate | submit | hover | none
        sa.Column("verb", sa.String(20), nullable=False, server_default="none"),
        sa.Column("target_label", sa.String(500), nullable=False, server_default=""),
        # button | link | dropdown | text_field | checkbox | radio | menu_item | tab | other
        sa.Column("target_kind", sa.String(20), nullable=False, server_default="other"),
        sa.Column("value", sa.Text, nullable=True),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0.0"),
        sa.Column(
            "automation_ready",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("reasoning", sa.String(500), nullable=False, server_default=""),

        # Reconciliation evidence (mirrors scene_actions.evidence_signals).
        sa.Column(
            "evidence_signals",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),

        # ── Provenance ──────────────────────────────────────────────
        sa.Column("extractor_model", sa.String(100), nullable=False, server_default=""),
        sa.Column("extractor_version", sa.String(50), nullable=False, server_default="v1"),
        sa.Column("generation_latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),

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

        sa.UniqueConstraint(
            "page_visit_id", "extractor_version", "subaction_index",
            name="uq_page_actions_visit_version_subaction",
        ),
    )
    op.create_index(
        "ix_page_actions_artifact",
        _TABLE,
        ["artifact_id"],
    )
    op.create_index(
        "ix_page_actions_tenant",
        _TABLE,
        ["tenant_id"],
    )
    op.create_index(
        "ix_page_actions_visit_sub",
        _TABLE,
        ["page_visit_id", "subaction_index"],
    )
    op.create_index(
        "ix_page_actions_verb_kind",
        _TABLE,
        ["tenant_id", "verb", "target_kind"],
    )

    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = '{_TABLE}'
            ) THEN
                ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;
                ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;

                DROP POLICY IF EXISTS tenant_isolation ON {_TABLE};
                CREATE POLICY tenant_isolation ON {_TABLE}
                    USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                    WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

                IF EXISTS (
                    SELECT FROM pg_roles WHERE rolname = 'nexus_app'
                ) THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO nexus_app;
                END IF;
            END IF;
        END$$;
    """)


def downgrade() -> None:
    op.drop_index("ix_page_actions_verb_kind", table_name=_TABLE)
    op.drop_index("ix_page_actions_visit_sub", table_name=_TABLE)
    op.drop_index("ix_page_actions_tenant", table_name=_TABLE)
    op.drop_index("ix_page_actions_artifact", table_name=_TABLE)
    op.drop_table(_TABLE)
