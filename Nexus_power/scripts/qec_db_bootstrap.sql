-- ═══════════════════════════════════════════════════════════════════════
-- QE-Central database + role bootstrap (design R-7).
--
-- Creates:
--   * logical database `qecentral` (owner: role `qec`) on the SAME
--     postgres:16 instance as `nexus` — one PITR setup covers both;
--   * role `qec`           — full owner of qecentral, ZERO grants on nexus;
--   * role `qec_substrate` — least-privilege writer into the nexus DB:
--     INSERT/SELECT on the seven substrate tables (+ tenants bootstrap,
--     + UPDATE on surface_prefs for the Belt-1 upsert) and SELECT on
--     audit_log. Nothing else.
--
-- Run AS SUPERUSER (the compose `nexus` user), with psql, AFTER the nexus
-- alembic chain is at head (the substrate tables must exist):
--
--   psql -h <host> -p 5433 -U nexus -d nexus \
--        -v qec_password='<strong-password>' \
--        -v qec_substrate_password='<strong-password>' \
--        -f scripts/qec_db_bootstrap.sql
--
-- Idempotent: safe to re-run (roles/database are create-if-missing; grants
-- and revokes re-apply cleanly). Then run the qecentral migrations:
--
--   QEC_DATABASE_URL=postgresql+asyncpg://qec:...@<host>:5432/qecentral \
--     alembic -c platform/qe-central/alembic_qec/alembic.ini upgrade head
-- ═══════════════════════════════════════════════════════════════════════

\set ON_ERROR_STOP on

-- ── Guard: both passwords MUST be supplied (an undefined psql variable
--    makes this SELECT a syntax error, halting under ON_ERROR_STOP; an
--    empty value trips the \if below). ─────────────────────────────────
SELECT (length(:'qec_password') > 0 AND length(:'qec_substrate_password') > 0) AS pw_ok \gset
\if :pw_ok
\else
    \echo 'FATAL: pass -v qec_password=... -v qec_substrate_password=... (non-empty)'
    -- Force a non-zero exit code under ON_ERROR_STOP:
    SELECT 1/0;
\endif

-- ── Roles (create-if-missing; password always [re]set) ────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'qec') THEN
        CREATE ROLE qec LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'qec_substrate') THEN
        CREATE ROLE qec_substrate LOGIN;
    END IF;
END $$;

ALTER ROLE qec WITH LOGIN PASSWORD :'qec_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER ROLE qec_substrate WITH LOGIN PASSWORD :'qec_substrate_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- ── Database (create-if-missing, owned by qec) ─────────────────────────
SELECT 'CREATE DATABASE qecentral OWNER qec'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'qecentral') \gexec

-- ── Database-level connect fences ──────────────────────────────────────
-- R-7: role qec has ZERO grants on the nexus DB — including the implicit
-- PUBLIC CONNECT, which we revoke and re-grant explicitly. All VKPower
-- services connect as the `nexus` superuser/owner and are unaffected.
REVOKE CONNECT ON DATABASE nexus FROM PUBLIC;
REVOKE ALL ON DATABASE nexus FROM qec;
GRANT CONNECT ON DATABASE nexus TO qec_substrate;

REVOKE CONNECT ON DATABASE qecentral FROM PUBLIC;
REVOKE ALL ON DATABASE qecentral FROM qec_substrate;
GRANT CONNECT, TEMPORARY ON DATABASE qecentral TO qec;

-- ═══ qecentral: schema privileges for the owning app role ══════════════
\connect qecentral

-- qec owns the database; make schema rights explicit anyway so the
-- negative-grant test has a stable baseline.
GRANT USAGE, CREATE ON SCHEMA public TO qec;
REVOKE ALL ON SCHEMA public FROM qec_substrate;

-- ═══ nexus: least-privilege substrate grants for qec_substrate ═════════
\connect nexus

GRANT USAGE ON SCHEMA public TO qec_substrate;
REVOKE CREATE ON SCHEMA public FROM qec_substrate;

-- The seven substrate tables (§2.1 write order). All carry FORCE RLS +
-- the tenant_isolation policy, so qec_substrate additionally needs the
-- nexus.current_tenant_id GUC set inside every transaction — which
-- tenant_scoped_substrate_session guarantees.
GRANT SELECT, INSERT ON sessions             TO qec_substrate;
GRANT SELECT, INSERT ON canonical_artifacts  TO qec_substrate;
GRANT SELECT, INSERT ON page_visits          TO qec_substrate;
GRANT SELECT, INSERT ON page_actions         TO qec_substrate;
GRANT SELECT, INSERT ON ground_truth_events  TO qec_substrate;
GRANT SELECT, INSERT ON visual_frames        TO qec_substrate;
-- Belt-1 anti-clobber is an UPSERT (INSERT ... ON CONFLICT DO UPDATE),
-- so surface_prefs alone also needs UPDATE.
GRANT SELECT, INSERT, UPDATE ON surface_prefs TO qec_substrate;

-- Tenant bootstrap (§2.1 "tenant bootstrap → sessions → ..."): the
-- canonical_artifacts.tenant_id FK requires the tenant row to exist first
-- (spine-engine _ensure_tenant_exists mirror), so the writer may INSERT a
-- missing tenant. No UPDATE/DELETE — existing tenants are never touched.
GRANT SELECT, INSERT ON tenants TO qec_substrate;

-- Touch-meter ingest (S4) reads the audit trail — read-only.
GRANT SELECT ON audit_log TO qec_substrate;

-- Explicit negative fences (defense in depth; new tables are NOT granted
-- by default, this just hard-documents the posture on the crown jewels).
-- Existence-guarded: e2e_auth_profiles ships via an out-of-band script and
-- may be absent on a fresh dev database.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['factory_test_cases', 'e2e_auth_profiles', 'users']
    LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('REVOKE ALL ON %I FROM qec_substrate', t);
        END IF;
    END LOOP;
END $$;

-- ── Verification queries (informational output for the operator) ───────
\echo '── qec grants on nexus DB (MUST be empty): ──'
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'qec'
ORDER BY table_name, privilege_type;

\echo '── qec_substrate grants on nexus DB (expected: the substrate set): ──'
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'qec_substrate'
ORDER BY table_name, privilege_type;
