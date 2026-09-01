-- Persona × Environment Matrix — foundation schema (P0). Idempotent, RLS,
-- grant-scoped. Mirrors apply_auth_profiles.sql / apply_heal_events.sql.
--
-- RUN = Suite × Environment × Persona. This lands the WHOLE model so no later
-- phase forces a migration. Secrets (persona_credentials.blob) are
-- envelope-encrypted in app code; the table only ever holds ciphertext.
--
--   psql -U nexus -d nexus -f apply_persona_env.sql

BEGIN;

-- 1.1 login recipes — the choreography, per artifact, VERSIONED
CREATE TABLE IF NOT EXISTS tp_login_recipes (
    recipe_id     VARCHAR(64)  PRIMARY KEY,
    tenant_id     VARCHAR(64)  NOT NULL,
    artifact_id   VARCHAR(64)  NOT NULL,
    app_id        VARCHAR(64)  NOT NULL DEFAULT '',
    version       INTEGER      NOT NULL DEFAULT 1,
    steps         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    slots         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    source        VARCHAR(24)  NOT NULL DEFAULT 'crawl_demonstration',
    status        VARCHAR(16)  NOT NULL DEFAULT 'active',
    verified_at   TIMESTAMPTZ,
    verified_env  VARCHAR(64)  NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, artifact_id, version)
);

-- 1.2 personas — logical identities
CREATE TABLE IF NOT EXISTS tp_personas (
    persona_id            VARCHAR(64)  PRIMARY KEY,
    tenant_id             VARCHAR(64)  NOT NULL,
    artifact_id           VARCHAR(64)  NOT NULL,
    app_id                VARCHAR(64)  NOT NULL DEFAULT '',
    name                  VARCHAR(120) NOT NULL,
    description           TEXT         NOT NULL DEFAULT '',
    traits                JSONB        NOT NULL DEFAULT '[]'::jsonb,
    behavior_class        VARCHAR(64)  NOT NULL DEFAULT '',
    status                VARCHAR(16)  NOT NULL DEFAULT 'active',
    is_recording_baseline BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, artifact_id, name)
);

-- 1.3 persona credentials — the ingredient cards, per (persona, environment).
-- blob = EnvelopeBlob.to_bytes() of {slot_name: secret_value}. CIPHERTEXT ONLY.
CREATE TABLE IF NOT EXISTS tp_persona_credentials (
    persona_id       VARCHAR(64)  NOT NULL,
    environment_id   VARCHAR(64)  NOT NULL,
    tenant_id        VARCHAR(64)  NOT NULL,
    blob             BYTEA        NOT NULL,
    slot_names       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    last_verified_at TIMESTAMPTZ,
    verify_status    VARCHAR(16)  NOT NULL DEFAULT 'unverified',
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (persona_id, environment_id, tenant_id)
);

-- 1.4 persona expected values — the answer sheets
CREATE TABLE IF NOT EXISTS tp_persona_expected_values (
    persona_id      VARCHAR(64)  NOT NULL,
    environment_id  VARCHAR(64)  NOT NULL,
    tenant_id       VARCHAR(64)  NOT NULL,
    value_key       VARCHAR(200) NOT NULL,
    expected_value  TEXT         NOT NULL DEFAULT '',
    source          VARCHAR(24)  NOT NULL DEFAULT 'crawl_observed',
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (persona_id, environment_id, tenant_id, value_key)
);

-- 1.5 value classifications — member_derived | app_constant | volatile |
-- structural | unknown, with evidence
CREATE TABLE IF NOT EXISTS tp_value_classifications (
    artifact_id  VARCHAR(64)  NOT NULL,
    tenant_id    VARCHAR(64)  NOT NULL,
    value_key    VARCHAR(200) NOT NULL,
    class        VARCHAR(20)  NOT NULL DEFAULT 'unknown',
    evidence     VARCHAR(24)  NOT NULL DEFAULT 'unclassified',
    scenario_id  VARCHAR(64)  NOT NULL DEFAULT '',
    step_number  INTEGER      NOT NULL DEFAULT 0,
    detail       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (artifact_id, tenant_id, value_key)
);

-- 1.6 persona reservations — one live hold per (persona, environment)
CREATE TABLE IF NOT EXISTS tp_persona_reservations (
    reservation_id  VARCHAR(64)  PRIMARY KEY,
    persona_id      VARCHAR(64)  NOT NULL,
    environment_id  VARCHAR(64)  NOT NULL,
    tenant_id       VARCHAR(64)  NOT NULL,
    run_id          VARCHAR(64)  NOT NULL DEFAULT '',
    acquired_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ  NOT NULL,
    released_at     TIMESTAMPTZ
);
-- exactly one live (unreleased) hold per persona+env+tenant
CREATE UNIQUE INDEX IF NOT EXISTS ux_persona_reservation_live
    ON tp_persona_reservations (persona_id, environment_id, tenant_id)
    WHERE released_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_login_recipes_artifact ON tp_login_recipes (tenant_id, artifact_id, status);
CREATE INDEX IF NOT EXISTS ix_personas_artifact      ON tp_personas (tenant_id, artifact_id, status);
CREATE INDEX IF NOT EXISTS ix_persona_cred_env       ON tp_persona_credentials (tenant_id, environment_id);
CREATE INDEX IF NOT EXISTS ix_persona_exp_key        ON tp_persona_expected_values (tenant_id, persona_id, environment_id);
CREATE INDEX IF NOT EXISTS ix_value_class_artifact   ON tp_value_classifications (tenant_id, artifact_id);
CREATE INDEX IF NOT EXISTS ix_persona_resv_expires   ON tp_persona_reservations (expires_at) WHERE released_at IS NULL;

-- ── RLS: a tenant sees only its own rows ────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['tp_login_recipes','tp_personas','tp_persona_credentials',
                           'tp_persona_expected_values','tp_value_classifications',
                           'tp_persona_reservations'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    -- FORCE so RLS applies even to the table OWNER (the app connects as the
    -- owning role `nexus`). Without this, RLS is bypassed for the owner and
    -- tenant isolation is a no-op — matches e2e_auth_profiles, which holds
    -- secrets exactly as tp_persona_credentials does.
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

-- ── Grants: app may read/write/update; NEVER delete (retire via status) ──────
DO $$
DECLARE t TEXT;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus') THEN
    FOREACH t IN ARRAY ARRAY['tp_login_recipes','tp_personas','tp_persona_credentials',
                             'tp_persona_expected_values','tp_value_classifications',
                             'tp_persona_reservations'] LOOP
      EXECUTE format('REVOKE ALL ON %I FROM nexus', t);
      EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO nexus', t);
    END LOOP;
  END IF;
END $$;

COMMIT;
