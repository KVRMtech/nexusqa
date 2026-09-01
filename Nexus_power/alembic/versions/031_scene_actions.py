"""Scene actions — multimodal LLM-extracted semantic user actions.

Phase 1 of the action-capture redesign.  Adds ONE additive derivation
table on top of the frozen canonical pipeline.  None of the pipeline
tables are modified — this table is populated post-pipeline by
``platform/api/app/services/storyboard/action_extractor.py`` and
consumed by the new ``scene_actions`` array in the visual-flow API
response and the 3D Journey bottom detail panel.

scene_actions
    One row per scene with a structured representation of the single
    most significant user action that happened in that scene.  The
    extractor calls a multimodal LLM with before/after frame
    screenshots, OCR text, URL, and any existing cursor/control
    signals, and parses the response through a Pydantic schema:

        {
            verb: click | type | select | scroll | navigate
                | submit | hover | none,
            target_label: "What state do you live in?",
            target_kind: button | link | dropdown | text_field |
                checkbox | radio | menu_item | tab | other,
            value: "TX",            # null when no value is involved
            confidence: 0.95,        # 0.0 - 1.0
            automation_ready: true,  # has enough info to emit
                                     # a playwright step
            reasoning: "Dropdown showed 'TX' in After frame after
                        being blank in Before frame.",
        }

    The pipeline's frame_actions extractor (OCR-text-diff) cannot
    produce these semantic actions — it can only detect that *some*
    text changed.  The LLM does the semantic interpretation that
    pixel-level extraction fundamentally cannot.

    Diagnosed against artifact 72be675e on 2026-05-25: the existing
    frame_actions JSON contains 80 entries across 17 scenes but 75%
    are empty placeholders and the rest are OCR-noise text fragments
    mislabeled as actions.  This table replaces that as the source of
    truth for "what did the user do".

The table follows the storyboard layer conventions established in
migration 030:
  * tenant_id on every row + RLS policy
  * Deterministic uuid5 primary keys → idempotent UPSERT semantics
  * Version column (``extractor_version``) — bump
    ``STORYBOARD_ACTION_EXTRACTOR_VERSION`` to force re-derivation
  * ON DELETE CASCADE from canonical_artifacts → tenant offboarding
    is clean
  * created_at + updated_at timestamps
  * Unique constraint on (scene_id, extractor_version) so re-runs
    of the same version overwrite rather than duplicate

Revision ID: 031_scene_actions
Revises: 030_storyboard_phase1
Create Date: 2026-05-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "031_scene_actions"
down_revision: Union[str, None] = "030_storyboard_phase1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "scene_actions"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("action_id", sa.String(64), primary_key=True),
        sa.Column(
            "scene_id",
            sa.String(64),
            sa.ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        # ── Structured action shape (mirrors the SceneAction Pydantic schema)
        # The verb the LLM inferred.  Enum-of-strings, normalised by the
        # extractor; rows with unknown verbs are rejected upstream.
        # Values: click | type | select | scroll | navigate | submit |
        #         hover | none
        sa.Column("verb", sa.String(20), nullable=False, server_default="none"),
        # The element label the LLM identified as the action target.
        # Human-readable phrase like "What state do you live in?" or
        # "Get Policy Quote".  Empty when no clear target.
        sa.Column("target_label", sa.String(500), nullable=False, server_default=""),
        # UI element kind.  Values: button | link | dropdown | text_field |
        #         checkbox | radio | menu_item | tab | other
        sa.Column(
            "target_kind", sa.String(20), nullable=False, server_default="other",
        ),
        # The value the user entered or selected (e.g. "TX", "john@x.com",
        # "1985").  Null when verb is click / submit / hover / etc. where
        # no value is involved.  Capped at 1000 chars — anything longer
        # is truncated before insert.
        sa.Column("value", sa.Text, nullable=True),
        # 0..1 confidence from the LLM, normalised to fit the existing
        # storyboard confidence-chip rendering.
        sa.Column(
            "confidence", sa.Float, nullable=False, server_default="0.0",
        ),
        # True when the extractor + reconciliation produced an action
        # with enough info for a downstream test exporter to emit a real
        # Playwright/Cypress/Gherkin step.  False when value is missing
        # but verb is type/select (so we know SOMETHING was entered but
        # not what).
        sa.Column(
            "automation_ready",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        # One-sentence LLM-supplied justification.  Helps QA engineers
        # validate the extraction without re-watching the video.  E.g.
        # "Dropdown showed 'TX' in After frame after being blank in
        # Before frame."  Capped at 500 chars.
        sa.Column("reasoning", sa.String(500), nullable=False, server_default=""),
        # ── Reconciliation evidence
        # Snapshot of which existing pipeline signals agreed with the
        # LLM's call.  Keys (all bool unless noted):
        #   ocr_text_match     — LLM's value matches OCR text in
        #                        After frame
        #   control_match      — evidence_controls.observed_value for
        #                        same scene matches LLM's value
        #   cursor_event_match — cursor_events with same scene has a
        #                        click within 200px of inferred target
        #   url_changed        — URL differs between Before/After
        #                        (corroborates verb=navigate/submit)
        #   audio_intent_match — transcript mentions a verb+target
        #                        matching the LLM's call
        #   sources (list)     — signals consumed by the extractor
        sa.Column(
            "evidence_signals",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # ── Provenance
        # Fully-qualified model identifier (e.g. ``anthropic/claude-sonnet-4-6``).
        # Stamped at extraction time so future backfills can target
        # specific outdated rows.
        sa.Column(
            "extractor_model", sa.String(100), nullable=False, server_default="",
        ),
        # Bump STORYBOARD_ACTION_EXTRACTOR_VERSION env var to force
        # re-extraction of all scenes.  Used when the prompt template
        # changes or when reconciliation logic is updated.
        sa.Column(
            "extractor_version", sa.String(50), nullable=False, server_default="v1",
        ),
        # Observability: how long the LLM call took.  Captured as
        # integer milliseconds so we can SUM/AVG cheaply.
        sa.Column(
            "generation_latency_ms",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        # Token costs for cost-tracking dashboards.  Zero for rows that
        # fell back to deterministic / no-LLM behaviour.
        sa.Column(
            "prompt_tokens", sa.Integer, nullable=False, server_default="0",
        ),
        sa.Column(
            "completion_tokens", sa.Integer, nullable=False, server_default="0",
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
            "scene_id", "extractor_version",
            name="uq_scene_actions_scene_version",
        ),
    )
    op.create_index(
        "ix_scene_actions_artifact", _TABLE, ["artifact_id"],
    )
    op.create_index(
        "ix_scene_actions_tenant", _TABLE, ["tenant_id"],
    )
    op.create_index(
        "ix_scene_actions_verb_kind", _TABLE, ["tenant_id", "verb", "target_kind"],
    )

    # ── Row-Level Security (mirrors migration 030) ────────────────────────────
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
    op.drop_index("ix_scene_actions_verb_kind", table_name=_TABLE)
    op.drop_index("ix_scene_actions_tenant", table_name=_TABLE)
    op.drop_index("ix_scene_actions_artifact", table_name=_TABLE)
    op.drop_table(_TABLE)
