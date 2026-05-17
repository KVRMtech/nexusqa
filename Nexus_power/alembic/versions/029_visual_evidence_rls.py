"""Extend Row-Level Security to visual-evidence tables.

Migration 010 enabled RLS on transactional / business tables (sessions,
canonical_artifacts, workflow_instances, etc.) but the visual-evidence
tables added in 011–018 were never covered.  An audit before the
multi-tenant rollout flagged this as the highest-priority gap: today
tenant isolation on visual_frames / visual_scenes / evidence_controls
relies only on WHERE clauses in application code.  One missed predicate
leaks cross-tenant evidence.

This migration applies the same pattern used by 010:

  * ENABLE + FORCE ROW LEVEL SECURITY on every visual-evidence table.
  * CREATE POLICY tenant_isolation
      USING (tenant_id = current_setting('nexus.current_tenant_id', true))
      WITH CHECK (...)
  * GRANT SELECT/INSERT/UPDATE/DELETE on each table to the existing
    ``nexus_app`` role (created in 010).

The session variable name is identical to migration 010
(``nexus.current_tenant_id``) so the application only needs to set it
once per DB session.

Also adds a missing composite index ``(artifact_id, scene_index)`` on
``visual_scenes`` — the audit found scenes are routinely fetched in
scene_index order for one artifact and the existing index covers only
artifact_id, forcing a sort.

Revision ID: 029_visual_evidence_rls
Revises: 028_workflow_state
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op

revision: str = "029_visual_evidence_rls"
down_revision: Union[str, None] = "028_workflow_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Visual-evidence + media tables that carry tenant_id but were never
# enrolled in RLS by migration 010.
_VISUAL_EVIDENCE_TABLES = [
    "video_files",
    "visual_frames",
    "visual_scenes",
    "app_instances",
    "evidence_controls",
    "evidence_steps",
    "cursor_events",
    "visual_flow_edges",
    "visual_flows",
    "ui_dictionary_entries",
]


def upgrade() -> None:
    # ── RLS on each visual-evidence table ───────────────────────
    # The DO-block guards each statement so a missing table (e.g. on
    # a partial-deploy env) is a NOTICE not a failure.
    for table in _VISUAL_EVIDENCE_TABLES:
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

                    GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO nexus_app;

                    RAISE NOTICE 'RLS enabled on visual-evidence table: %', '{table}';
                ELSE
                    RAISE NOTICE 'Table % does not exist, skipping RLS', '{table}';
                END IF;
            END $$;
        """)

    # ── Index for scene_index traversal on a single artifact ────
    # Audit Phase 0 — replaces a sort that previously hit
    # ix_visual_scenes_artifact (artifact_id only).
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'visual_scenes'
            ) AND NOT EXISTS (
                SELECT FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexname = 'ix_visual_scenes_artifact_scene_index'
            ) THEN
                CREATE INDEX ix_visual_scenes_artifact_scene_index
                    ON visual_scenes (artifact_id, scene_index);
            END IF;
        END $$;
    """)


def downgrade() -> None:
    for table in _VISUAL_EVIDENCE_TABLES:
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = '{table}'
                ) THEN
                    DROP POLICY IF EXISTS tenant_isolation ON {table};
                    ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
                    ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
                END IF;
            END $$;
        """)

    op.execute("""
        DROP INDEX IF EXISTS ix_visual_scenes_artifact_scene_index;
    """)
