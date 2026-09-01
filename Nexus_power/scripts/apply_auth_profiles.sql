-- e2e_auth_profiles — additive store for per-artifact authentication profiles
-- (an ENCRYPTED Playwright storageState session, so a generated test can run from
-- a cold session). The session JSON is encrypted at rest via the per-tenant
-- EnvelopeService and stored here as EnvelopeBlob.to_bytes() in a BYTEA column —
-- NEVER plaintext. Mirrors the script_versions RLS conventions exactly: tenant_id
-- column, RLS ENABLE + FORCE + tenant_isolation on nexus.current_tenant_id. Fully
-- IDEMPOTENT + reversible (DROP TABLE e2e_auth_profiles;). Additive: touches no
-- existing table/column/data.

CREATE TABLE IF NOT EXISTS e2e_auth_profiles (
    artifact_id  VARCHAR(64)  NOT NULL,
    tenant_id    VARCHAR(64)  NOT NULL,
    blob         BYTEA        NOT NULL,
    label        VARCHAR(200) NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (artifact_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS ix_e2e_auth_profiles_tenant
    ON e2e_auth_profiles (tenant_id);

DO $$
BEGIN
    IF EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'e2e_auth_profiles'
    ) THEN
        ALTER TABLE e2e_auth_profiles ENABLE ROW LEVEL SECURITY;
        ALTER TABLE e2e_auth_profiles FORCE ROW LEVEL SECURITY;

        DROP POLICY IF EXISTS tenant_isolation ON e2e_auth_profiles;
        CREATE POLICY tenant_isolation ON e2e_auth_profiles
            USING (tenant_id = current_setting('nexus.current_tenant_id', true))
            WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON e2e_auth_profiles TO nexus_app;
        END IF;
    END IF;
END$$;
