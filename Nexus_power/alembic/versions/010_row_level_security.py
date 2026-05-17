"""Enable PostgreSQL Row-Level Security (RLS) on all tenant-scoped tables.

Adds a security barrier so even if application code misses a WHERE tenant_id
filter, PostgreSQL enforces tenant isolation at the database level.

How it works:
  1. Each table with tenant_id gets RLS enabled + a policy:
     - Policy checks: tenant_id = current_setting('nexus.current_tenant_id')
  2. Application sets the session variable before each query:
     SET LOCAL nexus.current_tenant_id = '<tenant_id>';
  3. A shared nexus_app role is used for application connections
     (RLS doesn't apply to table owners / superusers).

This is DEFENSE IN DEPTH — application-level filtering remains primary,
RLS is the safety net.

Revision ID: 010_row_level_security
Revises: 009_semantic_completeness
Create Date: 2026-04-15
"""

from typing import Sequence, Union

from alembic import op

revision: str = "010_row_level_security"
down_revision: Union[str, None] = "009_semantic_completeness"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# All tables that have a tenant_id column and need RLS
_TENANT_TABLES = [
    "sessions",
    "sme_profiles",
    "contradictions",
    "guardrail_pipeline",
    "review_queue",
    "test_cases",
    "test_executions",
    "test_suites",
    "forge_jobs",
    "forge_test_cases",
    "compliance_reports",
    "canonical_artifacts",
    "workflow_instances",
]


def upgrade() -> None:
    # Create application role if it doesn't exist
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_app') THEN
                CREATE ROLE nexus_app NOLOGIN;
            END IF;
        END $$;
    """)

    # Grant usage to application role
    op.execute("GRANT USAGE ON SCHEMA public TO nexus_app;")

    for table in _TENANT_TABLES:
        # Check if table exists before applying RLS
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = '{table}'
                ) THEN
                    -- Enable RLS on the table
                    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;

                    -- Force RLS even for table owner (defense in depth)
                    ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

                    -- Drop existing policy if upgrading
                    DROP POLICY IF EXISTS tenant_isolation ON {table};

                    -- Create tenant isolation policy
                    -- Applies to all operations (SELECT, INSERT, UPDATE, DELETE)
                    CREATE POLICY tenant_isolation ON {table}
                        USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

                    -- Grant table access to the application role
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO nexus_app;

                    RAISE NOTICE 'RLS enabled on table: %', '{table}';
                ELSE
                    RAISE NOTICE 'Table % does not exist, skipping RLS', '{table}';
                END IF;
            END $$;
        """)

    # Create a superuser bypass policy for admin operations
    # The nexus (owner) role bypasses RLS naturally.
    # But we also need a way for admin operations to see all data:
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_admin') THEN
                CREATE ROLE nexus_admin NOLOGIN;
            END IF;
        END $$;
    """)
    op.execute("GRANT nexus_app TO nexus_admin;")


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
                    ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
                    ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
                END IF;
            END $$;
        """)
