-- script_versions — additive editor store for Phase C (per-test Script/Data editor).
-- Mirrors factory_test_cases (migration 036) conventions exactly: tenant_id column,
-- RLS ENABLE + FORCE + tenant_isolation policy on nexus.current_tenant_id.
-- Fully IDEMPOTENT (safe to re-run) and reversible (DROP TABLE script_versions;).
-- Additive: no existing table/column/data is touched.

CREATE TABLE IF NOT EXISTS script_versions (
    script_version_id  VARCHAR(64)  PRIMARY KEY,
    test_case_id       VARCHAR(64)  NOT NULL
        REFERENCES factory_test_cases(test_case_id) ON DELETE CASCADE,
    artifact_id        VARCHAR(64)  NOT NULL
        REFERENCES canonical_artifacts(artifact_id) ON DELETE CASCADE,
    tenant_id          VARCHAR(64)  NOT NULL,
    session_id         VARCHAR(64)  NOT NULL DEFAULT '',
    spec_path          VARCHAR(500) NOT NULL DEFAULT '',
    version_no         INTEGER      NOT NULL,
    script_source      TEXT         NOT NULL DEFAULT '',
    data_json          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    author             VARCHAR(200) NOT NULL DEFAULT '',
    note               VARCHAR(500) NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_script_versions_test_version
    ON script_versions (test_case_id, version_no);
CREATE INDEX IF NOT EXISTS ix_script_versions_artifact_version
    ON script_versions (artifact_id, version_no);
CREATE INDEX IF NOT EXISTS ix_script_versions_tenant_test
    ON script_versions (tenant_id, test_case_id);

-- RLS — byte-identical to migration 036's tenant_isolation block.
DO $$
BEGIN
    IF EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'script_versions'
    ) THEN
        ALTER TABLE script_versions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE script_versions FORCE ROW LEVEL SECURITY;

        DROP POLICY IF EXISTS tenant_isolation ON script_versions;
        CREATE POLICY tenant_isolation ON script_versions
            USING (tenant_id = current_setting('nexus.current_tenant_id', true))
            WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON script_versions TO nexus_app;
        END IF;
    END IF;
END$$;
