-- ─────────────────────────────────────────────────────────────────────────────
-- Phase 6 WS-C — FORCE Row-Level Security on the nexus-DB EVIDENCE tables.
--
-- The verdict ledger + governance evidence tables (verdict_events,
-- decision_dossiers, verification_waivers, finding_labels) are created OUT-OF-BAND
-- on the SDK Base (checkfirst create scripts), so the qec_001 RLS loop — which
-- covers only the qecentral tables — never reached them. Their isolation therefore
-- leaned on the application WHERE-clause alone. This script moves that isolation
-- into the database: ENABLE + FORCE ROW LEVEL SECURITY + a tenant_isolation policy
-- keyed on the same `nexus.current_tenant_id` GUC every writer already sets via
-- tenant_scoped_session.
--
-- Idempotent (safe to re-run) and reversible (see the downgrade at the bottom).
-- Additive: it touches NO frozen factory CODE — only applies RLS to existing tables.
--
-- Apply:    psql "$NEXUS_DATABASE_URL" -f scripts/apply_evidence_rls.sql
-- Verify:   the qe-central contract test tests/contract/test_rls_coverage_complete.py
--           style check, or:  SELECT relname, relrowsecurity, relforcerowsecurity
--           FROM pg_class WHERE relname = ANY(ARRAY['verdict_events', ...]);
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  t text;
  evidence_tables text[] := ARRAY[
    'verdict_events', 'decision_dossiers', 'verification_waivers', 'finding_labels'
  ];
BEGIN
  FOREACH t IN ARRAY evidence_tables LOOP
    -- Only touch tables that actually exist on this deployment (checkfirst posture).
    IF EXISTS (
      SELECT 1 FROM information_schema.tables
      WHERE table_schema = 'public' AND table_name = t
    ) THEN
      -- Confirm the table carries a tenant_id column before enforcing tenant RLS.
      IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = t AND column_name = 'tenant_id'
      ) THEN
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I', t);
        EXECUTE format(
          'CREATE POLICY tenant_isolation ON public.%I '
          'USING (tenant_id = current_setting(''nexus.current_tenant_id'', true)) '
          'WITH CHECK (tenant_id = current_setting(''nexus.current_tenant_id'', true))',
          t
        );
        RAISE NOTICE 'evidence_rls: enforced FORCE RLS + tenant_isolation on %', t;
      ELSE
        RAISE WARNING 'evidence_rls: % has no tenant_id column — SKIPPED (needs a bespoke policy)', t;
      END IF;
    ELSE
      RAISE NOTICE 'evidence_rls: % not present on this deployment — skipped', t;
    END IF;
  END LOOP;
END $$;

-- ── Downgrade (reversible) — run to remove the enforcement, e.g. for a rollback ──
-- DO $$
-- DECLARE t text;
-- BEGIN
--   FOREACH t IN ARRAY ARRAY['verdict_events','decision_dossiers','verification_waivers','finding_labels'] LOOP
--     IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=t) THEN
--       EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I', t);
--       EXECUTE format('ALTER TABLE public.%I NO FORCE ROW LEVEL SECURITY', t);
--       EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t);
--     END IF;
--   END LOOP;
-- END $$;
