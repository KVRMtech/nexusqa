-- surface_prefs — per-surface derivation preferences (cost control for the
-- visual-evidence layer). One row per (tenant_id, artifact_id); artifact_id = ''
-- is the per-tenant DEFAULT, any other value is a per-artifact override.
-- Mirrors migration 036 / script_versions conventions exactly: tenant_id column,
-- RLS ENABLE + FORCE + tenant_isolation policy on nexus.current_tenant_id.
-- Fully IDEMPOTENT (safe to re-run) and reversible (DROP TABLE surface_prefs;).
-- Additive: no existing table/column/data is touched. Default behavior is
-- unchanged — absence of a row means "all surfaces ON".

CREATE TABLE IF NOT EXISTS surface_prefs (
    tenant_id        VARCHAR(64)  NOT NULL,
    artifact_id      VARCHAR(64)  NOT NULL DEFAULT '',
    storyboard       BOOLEAN      NOT NULL DEFAULT TRUE,
    pages_forms      BOOLEAN      NOT NULL DEFAULT TRUE,
    three_d_journey  BOOLEAN      NOT NULL DEFAULT TRUE,
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, artifact_id)
);

-- RLS — byte-identical to migration 036's tenant_isolation block.
DO $$
BEGIN
    IF EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'surface_prefs'
    ) THEN
        ALTER TABLE surface_prefs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE surface_prefs FORCE ROW LEVEL SECURITY;

        DROP POLICY IF EXISTS tenant_isolation ON surface_prefs;
        CREATE POLICY tenant_isolation ON surface_prefs
            USING (tenant_id = current_setting('nexus.current_tenant_id', true))
            WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON surface_prefs TO nexus_app;
        END IF;
    END IF;
END$$;
