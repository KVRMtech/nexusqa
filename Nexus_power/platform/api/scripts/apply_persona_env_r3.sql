-- Persona × Environment Matrix — scoped certification (R3). Idempotent, additive.
--
-- "Cert on env A does not grant trust on env B (or behavior class B)." A baseline
-- certification proves the suite on the recording baseline only. This ledger
-- records a certification per (scenario, environment, behavior_class) cell, so the
-- Trust Block can show the (env × class) proof matrix and an environment can OPT
-- IN to require a scoped certification before a member runs there.
--
-- All additive: a new ledger table + one nullable-defaulted column. Default
-- behavior is unchanged (require_scoped_certification defaults FALSE).
--
--   psql -U nexus -d nexus -f apply_persona_env_r3.sql

BEGIN;

-- opt-in: when TRUE, a persona run against this environment needs a scoped
-- certification for its (environment, behavior_class) cell.
ALTER TABLE tp_environments
    ADD COLUMN IF NOT EXISTS require_scoped_certification BOOLEAN NOT NULL DEFAULT FALSE;

-- one certification outcome per (scenario, environment, behavior_class).
CREATE TABLE IF NOT EXISTS tp_certification_ledger (
    artifact_id     VARCHAR(64)  NOT NULL,
    tenant_id       VARCHAR(64)  NOT NULL,
    scenario_id     VARCHAR(64)  NOT NULL,
    environment_id  VARCHAR(64)  NOT NULL,
    behavior_class  VARCHAR(64)  NOT NULL DEFAULT '',
    status          VARCHAR(16)  NOT NULL DEFAULT 'certified',   -- certified | failed
    run_id          VARCHAR(64)  NOT NULL DEFAULT '',
    detail          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (artifact_id, tenant_id, scenario_id, environment_id, behavior_class)
);

CREATE INDEX IF NOT EXISTS ix_cert_ledger_cell
    ON tp_certification_ledger (tenant_id, artifact_id, environment_id, behavior_class);

-- ── RLS + grants ─────────────────────────────────────────────────────────────
DO $$
BEGIN
  EXECUTE 'ALTER TABLE tp_certification_ledger ENABLE ROW LEVEL SECURITY';
  EXECUTE 'ALTER TABLE tp_certification_ledger FORCE ROW LEVEL SECURITY';
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'tp_certification_ledger'
                 AND policyname = 'tp_certification_ledger_tenant_isolation') THEN
    EXECUTE $f$CREATE POLICY tp_certification_ledger_tenant_isolation ON tp_certification_ledger
      USING (tenant_id = current_setting('nexus.current_tenant_id', true))
      WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true))$f$;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus') THEN
    EXECUTE 'REVOKE ALL ON tp_certification_ledger FROM nexus';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON tp_certification_ledger TO nexus';
  END IF;
END $$;

COMMIT;
