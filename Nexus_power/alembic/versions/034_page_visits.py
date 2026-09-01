"""Page visits — URL-keyed chronological log of distinct pages the user visited.

Phase 2.0 — independent of scene boundaries.  The canonical pipeline's
``scene_grouper`` merges visually-similar consecutive frames (e.g. all
``/personal-information/*`` USAA form sub-pages get one scene because
the layout is identical).  For QA test generation we need every
distinct URL captured.  This table is the parallel axis.

Each row represents one contiguous visit to a single location
(URL for web, window_title for desktop, screen_name for mobile/native).
A user who visited ``/state`` → ``/gender`` → ``/state`` would produce
three rows because each is a separate contiguous visit.

The table is populated post-pipeline by
``platform/api/app/services/storyboard/page_visit_extractor.py``
running URL regex over every frame's ``extracted_text``.  When the
recording is not a web app (no URL detected) the extractor falls back
to ``visual_frames.screen_name`` or ``app_instances.window_title``,
recording the choice in ``source``.

The downstream ``page_action_extractor`` and
``form_snapshot_extractor`` services key off ``page_visit_id``.

Revision ID: 034_page_visits
Revises: 033_scene_actions_subaction
Create Date: 2026-05-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "034_page_visits"
down_revision: Union[str, None] = "033_scene_actions_subaction"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "page_visits"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("page_visit_id", sa.String(64), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False, server_default=""),

        # ── Chronological position within the artifact ──────────────
        # 0-based, monotonic.  Two visits to the same URL get different
        # sequence_index values (each contiguous visit is its own row).
        sa.Column("sequence_index", sa.Integer, nullable=False),

        # ── The "where was the user" payload ─────────────────────────
        # The full canonical location string.  For web pages this is
        # the URL; for desktop/mainframe/mobile it's the window title
        # or screen name.  Length-capped to fit deep paths.
        sa.Column("location", sa.String(2000), nullable=False, server_default=""),

        # Parsed URL components (populated when source=url_*; otherwise empty).
        # Stored separately so we can index/query by host without parsing.
        sa.Column("url_host", sa.String(500), nullable=False, server_default=""),
        sa.Column("url_path", sa.String(2000), nullable=False, server_default=""),
        sa.Column("url_query", sa.String(2000), nullable=False, server_default=""),

        # Canonicalised host.  Mirrors the URL canonicalisation already
        # applied to scene URLs (Msdd.com → usaa.com when the artifact's
        # dominant host is usaa.com).  Lets downstream consumers (test
        # exporters) emit clean URLs without re-parsing.
        sa.Column("canonical_host", sa.String(500), nullable=False, server_default=""),

        # ── Temporal extent within the recording ────────────────────
        sa.Column("first_seen_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_seen_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("frame_count", sa.Integer, nullable=False, server_default="0"),

        # ── How the location was discovered ─────────────────────────
        # Values:
        #   url_regex          — extracted via regex on visual_frames.extracted_text
        #   url_scene          — copied from visual_scenes.detected_url (when frame OCR was missing)
        #   window_title       — non-web; fell back to app_instances.window_title
        #   screen_name_ocr    — fell back to visual_frames.screen_name
        #   llm_inferred       — Sonnet reconciled when none of the above worked
        sa.Column("source", sa.String(40), nullable=False, server_default="url_regex"),

        # 0..1 confidence in the extraction (regex hits = 1.0;
        # screen_name fallback = ~0.6; LLM-inferred = ~0.85).
        sa.Column(
            "extraction_confidence",
            sa.Float,
            nullable=False,
            server_default="1.0",
        ),

        # ── Optional join to the canonical scene this visit fell within
        # (best-effort; null when the visit spans multiple scenes or no
        # scene boundary matched).  Useful for cross-referencing back to
        # the scene-keyed action_extractor output.
        sa.Column(
            "primary_scene_id",
            sa.String(64),
            sa.ForeignKey("visual_scenes.scene_id", ondelete="SET NULL"),
            nullable=True,
        ),

        # ── Form snapshot — populated by form_snapshot_extractor
        # (migration 036 adds default).  Shape:
        #   { "What state do you live in?": "Texas",
        #     "Gender": "Male", ... }
        # Empty {} when the extractor hasn't run or no form was visible.
        sa.Column(
            "form_snapshot",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Confidence/evidence for the form snapshot extraction itself.
        sa.Column(
            "form_snapshot_signals",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),

        # ── Provenance ──────────────────────────────────────────────
        sa.Column("extractor_version", sa.String(50), nullable=False, server_default="v1"),
        sa.Column(
            "form_snapshot_extractor_version",
            sa.String(50),
            nullable=False,
            server_default="",
        ),

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
            "artifact_id", "sequence_index", "extractor_version",
            name="uq_page_visits_artifact_sequence_version",
        ),
    )
    op.create_index(
        "ix_page_visits_artifact_sequence",
        _TABLE,
        ["artifact_id", "sequence_index"],
    )
    op.create_index(
        "ix_page_visits_tenant_session",
        _TABLE,
        ["tenant_id", "session_id"],
    )
    op.create_index(
        "ix_page_visits_canonical_host",
        _TABLE,
        ["tenant_id", "canonical_host"],
    )

    # ── Row-Level Security (mirrors migrations 030 / 031 / 033) ─────────
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
    op.drop_index("ix_page_visits_canonical_host", table_name=_TABLE)
    op.drop_index("ix_page_visits_tenant_session", table_name=_TABLE)
    op.drop_index("ix_page_visits_artifact_sequence", table_name=_TABLE)
    op.drop_table(_TABLE)
