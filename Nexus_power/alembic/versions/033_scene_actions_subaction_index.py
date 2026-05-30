"""Scene actions — add subaction_index for multi-action-per-scene.

Phase 1.6 — supports multiple ``scene_actions`` rows per scene at the
same ``extractor_version``, so long-form-fill scenes (Personal
Information page with 5-7 fields) can emit one row PER captured action
instead of being squashed into one.

What changes
============
* New column ``subaction_index`` (default 0) on ``scene_actions``.
* Drop the unique constraint ``(scene_id, extractor_version)``.
* Add a new unique constraint ``(scene_id, extractor_version,
  subaction_index)`` so re-runs at the same version are still
  idempotent per ordinal.

Existing rows (v1 - v7) retain ``subaction_index = 0`` — they were
already 1-per-scene under the old constraint, so back-filling 0 keeps
them addressable by the same (scene, version, 0) tuple.

The deterministic ``action_id`` derivation (in
``action_extractor._action_id``) is updated separately to include the
subaction_index in the uuid5 input, so re-runs are still idempotent.

Revision ID: 033_scene_actions_subaction_index
Revises: 031_scene_actions
Create Date: 2026-05-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "033_scene_actions_subaction"
down_revision: Union[str, None] = "031_scene_actions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "scene_actions"


def upgrade() -> None:
    # Add the new column with a server default so the existing rows
    # automatically populate as subaction_index=0.  We then KEEP the
    # server default so future INSERTs that forget the column still
    # round-trip safely.
    op.add_column(
        _TABLE,
        sa.Column(
            "subaction_index",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )

    # Drop the old unique constraint (one row per scene+version).
    # Wrap in DO-block so a missing constraint (e.g. partial deploy
    # state) is a NOTICE, not a hard failure.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_scene_actions_scene_version'
            ) THEN
                ALTER TABLE scene_actions DROP CONSTRAINT uq_scene_actions_scene_version;
            END IF;
        END$$;
    """)

    # New unique constraint includes subaction_index so re-runs at the
    # same version still UPSERT cleanly per ordinal.
    op.create_unique_constraint(
        "uq_scene_actions_scene_version_subaction",
        _TABLE,
        ["scene_id", "extractor_version", "subaction_index"],
    )

    # Index for the common rendering query ("all actions for this
    # scene, in order").  The unique constraint above already creates
    # a btree over (scene_id, extractor_version, subaction_index) but
    # this dedicated index drops the version filter for endpoints that
    # want every row regardless of version.
    op.create_index(
        "ix_scene_actions_scene_subaction",
        _TABLE,
        ["scene_id", "subaction_index"],
    )


def downgrade() -> None:
    op.drop_index("ix_scene_actions_scene_subaction", table_name=_TABLE)
    op.drop_constraint(
        "uq_scene_actions_scene_version_subaction", _TABLE, type_="unique",
    )
    # Recreate the old unique constraint — note: this will fail if
    # multi-action rows exist at the same (scene_id, version).
    # Caller must DELETE those rows first.
    op.create_unique_constraint(
        "uq_scene_actions_scene_version",
        _TABLE,
        ["scene_id", "extractor_version"],
    )
    op.drop_column(_TABLE, "subaction_index")
