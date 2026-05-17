"""Per-tenant UI dictionary — knowledge accumulation across recordings.

Each row encodes one recognised UI control with its preferred selector and
running confidence.  When the canonical pipeline processes a new artifact,
the orchestration looks up entries for the tenant + page and reuses prior
selectors instead of re-deriving them from scratch.  Successful selector
matches at automation time bump ``selector_confidence``; failures decrement
it.  Over many recordings the tenant's library converges on stable
selectors that beat any single-shot extraction.

Identity model:
  ``element_signature`` is a deterministic hash of (page_key, normalised
  element_type, normalised label).  Two captures of the same control on the
  same page produce the same signature, which is the primary key.

Revision ID: 018_ui_dictionary
Revises: 017_evidence_steps_cursor
Create Date: 2026-05-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "018_ui_dictionary"
down_revision: Union[str, None] = "017_evidence_steps_cursor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ui_dictionary_entries",
        sa.Column("entry_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        # element_signature is a deterministic uuid5(page_key, element_type, label)
        # — two captures of the same control on the same page collide here so
        # the upsert path always finds the existing row.
        sa.Column("element_signature", sa.String(64), nullable=False),
        # Page key from scene_state_summary (e.g. usaa.insurance.life.estimate).
        # Empty string when the source scene had no resolvable page_key.
        sa.Column("page_key", sa.String(200), nullable=False, server_default=""),
        # Domain extracted from the source URL — quick filter for cross-app
        # disambiguation when the same label exists on multiple sites.
        sa.Column("domain", sa.String(200), nullable=False, server_default=""),
        sa.Column("element_type", sa.String(50), nullable=False),
        sa.Column("label_text", sa.String(500), nullable=False, server_default=""),
        sa.Column("display_label", sa.String(600), nullable=False, server_default=""),
        # Canonical action verb the control was last classified as.
        sa.Column("action_kind", sa.String(32), nullable=False, server_default=""),
        # Best Playwright selector seen so far + the confidence we have in it.
        sa.Column("preferred_selector", sa.String(2000), nullable=False, server_default=""),
        sa.Column("selector_confidence", sa.Float, nullable=False, server_default="0.0"),
        # Provenance — was the selector OCR-grounded, vision-only, hybrid?
        sa.Column("selector_source", sa.String(20), nullable=False, server_default="unknown"),
        # Counters.  recognition_count = how many recordings hit this entry.
        # automation_success_count + _failure_count fed by the closed-loop
        # test runner once that exists; harmless when 0.
        sa.Column("recognition_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("automation_success_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("automation_failure_count", sa.Integer, nullable=False, server_default="0"),
        # Last-known bounding box centre — useful when cursor coords need to
        # be matched against the most recently seen geometry.
        sa.Column("bbox_centre_x", sa.Integer, nullable=True),
        sa.Column("bbox_centre_y", sa.Integer, nullable=True),
        # Aggregated metadata — kept in JSONB so we can roll new signal
        # shapes into the dictionary without an alembic migration each time.
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "tenant_id", "element_signature",
            name="uq_ui_dictionary_tenant_signature",
        ),
    )
    op.create_index(
        "ix_ui_dictionary_tenant_page",
        "ui_dictionary_entries",
        ["tenant_id", "page_key"],
    )
    op.create_index(
        "ix_ui_dictionary_tenant_domain",
        "ui_dictionary_entries",
        ["tenant_id", "domain"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ui_dictionary_tenant_domain", table_name="ui_dictionary_entries",
    )
    op.drop_index(
        "ix_ui_dictionary_tenant_page", table_name="ui_dictionary_entries",
    )
    op.drop_table("ui_dictionary_entries")
