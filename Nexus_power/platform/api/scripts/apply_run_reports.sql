-- Frozen Execution Evidence Report snapshots (e2e_run_reports) — idempotent.
--
-- WHY: the report is assembled on demand from live rows, so re-rendering an old
-- run after cases were regenerated does not necessarily reproduce the account
-- that described it at the time. A snapshot freezes the report when the run
-- lands (spec AC-1) and carries its own SHA-256 chain root, so a stored report
-- can itself be checked for tampering.
--
-- Snapshots are APPEND/UPSERT only for the app role — never deletable by it, so
-- the frozen record of a run cannot be quietly dropped.
--
--   psql -U nexus -d nexus -f apply_run_reports.sql

BEGIN;

CREATE TABLE IF NOT EXISTS e2e_run_reports (
    run_id       VARCHAR(64)  NOT NULL,
    tenant_id    VARCHAR(64)  NOT NULL,
    artifact_id  VARCHAR(64)  NOT NULL,
    environment  VARCHAR(64)  NOT NULL DEFAULT '',
    report_json  TEXT         NOT NULL,
    chain_root   VARCHAR(64)  NOT NULL DEFAULT '',
    byte_size    INTEGER      NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS ix_e2e_run_reports_artifact
    ON e2e_run_reports (artifact_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_e2e_run_reports_tenant
    ON e2e_run_reports (tenant_id);

ALTER TABLE e2e_run_reports ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'e2e_run_reports'
          AND policyname = 'e2e_run_reports_tenant_isolation'
    ) THEN
        CREATE POLICY e2e_run_reports_tenant_isolation ON e2e_run_reports
            USING (tenant_id = current_setting('nexus.current_tenant_id', true))
            WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));
    END IF;
END $$;

-- The app may write and refresh a snapshot, but never DELETE one: a frozen
-- record of a run must not be removable through the application.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus') THEN
        EXECUTE 'REVOKE ALL ON e2e_run_reports FROM nexus';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON e2e_run_reports TO nexus';
    END IF;
END $$;

COMMIT;
