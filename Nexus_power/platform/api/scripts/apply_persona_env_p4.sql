-- Persona × Environment Matrix — environment governance (P4). Idempotent, RLS,
-- grant-scoped. Additive to apply_persona_env.sql (P0).
--
-- Two registries:
--   tp_environments  — per (artifact, environment) POSTURE + production flag +
--                      health. A RUN against an environment inherits its posture
--                      and, for production, is default-deny unless authorized.
--   tp_case_scopes   — per (artifact, scenario) behavior-class binding. A case
--                      that a structure fork proved belongs to one behavior class
--                      REFUSES an out-of-class persona (honestly, never a false
--                      red). Populated from the P3 structure diff or by an editor.
--
--   psql -U nexus -d nexus -f apply_persona_env_p4.sql

BEGIN;

-- 4.1 environments — the governed target. posture + production default-deny.
CREATE TABLE IF NOT EXISTS tp_environments (
    environment_id   VARCHAR(64)  NOT NULL,
    artifact_id      VARCHAR(64)  NOT NULL,
    tenant_id        VARCHAR(64)  NOT NULL,
    app_id           VARCHAR(64)  NOT NULL DEFAULT '',
    label            VARCHAR(120) NOT NULL DEFAULT '',
    -- read_write | read_only | no_submit  (governance posture)
    posture          VARCHAR(16)  NOT NULL DEFAULT 'read_write',
    is_production    BOOLEAN      NOT NULL DEFAULT FALSE,
    -- explicit, revocable authorization to WRITE to a production environment.
    -- Without it, a production run is refused (default-deny) — never assumed.
    write_authorized BOOLEAN      NOT NULL DEFAULT FALSE,
    base_url         TEXT         NOT NULL DEFAULT '',
    -- opaque marker of the underlying data snapshot; drives staleness (P5).
    data_epoch       VARCHAR(64)  NOT NULL DEFAULT '',
    -- unknown | healthy | unreachable | login_failed | recipe_drift
    health_status    VARCHAR(16)  NOT NULL DEFAULT 'unknown',
    health_detail    TEXT         NOT NULL DEFAULT '',
    last_health_at   TIMESTAMPTZ,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (environment_id, artifact_id, tenant_id)
);

-- 4.2 case scopes — a case bound to the behavior class a structure fork proved.
CREATE TABLE IF NOT EXISTS tp_case_scopes (
    artifact_id             VARCHAR(64)  NOT NULL,
    tenant_id               VARCHAR(64)  NOT NULL,
    scenario_id             VARCHAR(64)  NOT NULL,
    required_behavior_class VARCHAR(64)  NOT NULL DEFAULT '',
    evidence                VARCHAR(24)  NOT NULL DEFAULT 'structure_fork',
    detail                  JSONB        NOT NULL DEFAULT '{}'::jsonb,
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (artifact_id, tenant_id, scenario_id)
);

CREATE INDEX IF NOT EXISTS ix_environments_artifact ON tp_environments (tenant_id, artifact_id);
CREATE INDEX IF NOT EXISTS ix_case_scopes_artifact  ON tp_case_scopes (tenant_id, artifact_id);

-- ── RLS ─────────────────────────────────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['tp_environments','tp_case_scopes'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t
                   AND policyname = t || '_tenant_isolation') THEN
      EXECUTE format($f$CREATE POLICY %I ON %I
        USING (tenant_id = current_setting('nexus.current_tenant_id', true))
        WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true))$f$,
        t || '_tenant_isolation', t);
    END IF;
  END LOOP;
END $$;

-- ── Grants: read/write/update; NEVER delete ─────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus') THEN
    FOREACH t IN ARRAY ARRAY['tp_environments','tp_case_scopes'] LOOP
      EXECUTE format('REVOKE ALL ON %I FROM nexus', t);
      EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO nexus', t);
    END LOOP;
  END IF;
END $$;

COMMIT;
