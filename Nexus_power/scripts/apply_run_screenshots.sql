-- e2e_run_screenshots — additive store for per-step ACTUAL run screenshots
-- (the 'This run' side of the baseline-vs-actual two-up; also feeds the drift /
-- visual-change verdicts). Mirrors the script_versions RLS conventions exactly:
-- tenant_id column, RLS ENABLE + FORCE + tenant_isolation on
-- nexus.current_tenant_id. Fully IDEMPOTENT (safe to re-run) and reversible
-- (DROP TABLE e2e_run_screenshots;). Additive: no existing table/column/data is
-- touched. run_id is a plain column (NOT an FK) so a screenshot can be uploaded
-- before the run row is ingested.

CREATE TABLE IF NOT EXISTS e2e_run_screenshots (
    screenshot_id  VARCHAR(64)  PRIMARY KEY,
    run_id         VARCHAR(64)  NOT NULL DEFAULT '',
    artifact_id    VARCHAR(64)  NOT NULL DEFAULT '',
    tenant_id      VARCHAR(64)  NOT NULL,
    scenario_id    VARCHAR(64)  NOT NULL DEFAULT '',
    step_number    INTEGER      NOT NULL DEFAULT 0,
    content_type   VARCHAR(64)  NOT NULL DEFAULT 'image/png',
    byte_size      INTEGER      NOT NULL DEFAULT 0,
    image          BYTEA        NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_e2e_run_screenshots_run
    ON e2e_run_screenshots (run_id);
CREATE INDEX IF NOT EXISTS ix_e2e_run_screenshots_tenant_artifact
    ON e2e_run_screenshots (tenant_id, artifact_id);

-- RLS — byte-identical to script_versions' tenant_isolation block.
DO $$
BEGIN
    IF EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'e2e_run_screenshots'
    ) THEN
        ALTER TABLE e2e_run_screenshots ENABLE ROW LEVEL SECURITY;
        ALTER TABLE e2e_run_screenshots FORCE ROW LEVEL SECURITY;

        DROP POLICY IF EXISTS tenant_isolation ON e2e_run_screenshots;
        CREATE POLICY tenant_isolation ON e2e_run_screenshots
            USING (tenant_id = current_setting('nexus.current_tenant_id', true))
            WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON e2e_run_screenshots TO nexus_app;
        END IF;
    END IF;
END$$;
