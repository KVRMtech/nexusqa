-- e2e_runner_jobs — additive durable registry for live run / heal / auto-heal
-- jobs, so a job survives a platform-api restart or a second worker (the status
-- poll + heal verification no longer orphan). Mirrors the script_versions RLS
-- conventions exactly: tenant_id column, RLS ENABLE + FORCE + tenant_isolation on
-- nexus.current_tenant_id. Fully IDEMPOTENT (safe to re-run) and reversible
-- (DROP TABLE e2e_runner_jobs;). Additive: no existing table/column/data touched.

CREATE TABLE IF NOT EXISTS e2e_runner_jobs (
    run_id       VARCHAR(64)  PRIMARY KEY,
    tenant_id    VARCHAR(64)  NOT NULL,
    artifact_id  VARCHAR(64)  NOT NULL DEFAULT '',
    kind         VARCHAR(32)  NOT NULL DEFAULT 'run',
    status       VARCHAR(32)  NOT NULL DEFAULT 'running',
    job_json     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_e2e_runner_jobs_tenant_artifact
    ON e2e_runner_jobs (tenant_id, artifact_id);

DO $$
BEGIN
    IF EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'e2e_runner_jobs'
    ) THEN
        ALTER TABLE e2e_runner_jobs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE e2e_runner_jobs FORCE ROW LEVEL SECURITY;

        DROP POLICY IF EXISTS tenant_isolation ON e2e_runner_jobs;
        CREATE POLICY tenant_isolation ON e2e_runner_jobs
            USING (tenant_id = current_setting('nexus.current_tenant_id', true))
            WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON e2e_runner_jobs TO nexus_app;
        END IF;
    END IF;
END$$;
