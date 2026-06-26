-- proven_control_ledger — app-level memo of oracle-VERIFIED heals so a fix proven
-- once (a recipe / re-anchor / wait / control-kind correction) is reused across
-- every scenario AND recording that touches the same control, instead of re-healing
-- from scratch. Reuse is always re-gated by the step's own oracle, so a stale entry
-- fails RED and is re-healed — the ledger can never green-wash.
--
-- Mirrors the script_versions / e2e_run_screenshots RLS conventions EXACTLY:
-- tenant_id column, RLS ENABLE + FORCE + tenant_isolation on
-- nexus.current_tenant_id, GRANT to nexus_app. Fully IDEMPOTENT (safe to re-run)
-- and reversible (DROP TABLE proven_control_ledger;). Additive: no existing
-- table/column/data is touched. control_fp is a stable app-level fingerprint
-- (page-path + normalized accessible name + control kind); app_key is the reuse
-- scope (an artifact today, the target app for cross-recording reuse later).

CREATE TABLE IF NOT EXISTS proven_control_ledger (
    ledger_id        VARCHAR(64)  PRIMARY KEY,
    tenant_id        VARCHAR(64)  NOT NULL,
    app_key          VARCHAR(200) NOT NULL DEFAULT '',
    control_fp       VARCHAR(64)  NOT NULL DEFAULT '',
    fix_kind         VARCHAR(32)  NOT NULL DEFAULT '',
    payload          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    label            VARCHAR(512) NOT NULL DEFAULT '',
    page_path        VARCHAR(512) NOT NULL DEFAULT '',
    proven_by_run    VARCHAR(64)  NOT NULL DEFAULT '',
    app_fingerprint  VARCHAR(128) NOT NULL DEFAULT '',
    confirmed_count  INTEGER      NOT NULL DEFAULT 1,
    proven_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- One proven fix per control per channel (the upsert target).
CREATE UNIQUE INDEX IF NOT EXISTS uq_proven_control_ledger_control
    ON proven_control_ledger (tenant_id, app_key, control_fp, fix_kind);

-- Seed-path lookup: every proven fix for an app, tenant-scoped.
CREATE INDEX IF NOT EXISTS ix_proven_control_ledger_scope
    ON proven_control_ledger (tenant_id, app_key);

-- RLS — byte-identical to script_versions / e2e_run_screenshots tenant_isolation.
DO $$
BEGIN
    IF EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'proven_control_ledger'
    ) THEN
        ALTER TABLE proven_control_ledger ENABLE ROW LEVEL SECURITY;
        ALTER TABLE proven_control_ledger FORCE ROW LEVEL SECURITY;

        DROP POLICY IF EXISTS tenant_isolation ON proven_control_ledger;
        CREATE POLICY tenant_isolation ON proven_control_ledger
            USING (tenant_id = current_setting('nexus.current_tenant_id', true))
            WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON proven_control_ledger TO nexus_app;
        END IF;
    END IF;
END$$;
