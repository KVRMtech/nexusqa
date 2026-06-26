-- Phase 4 — app-scoped cross-recording reuse index (perf only, optional).
-- get_proven_fixes_by_app() filters (tenant_id, app_fingerprint, control_fp IN (...)).
-- This composite index keeps that lookup cheap as the ledger grows across recordings.
-- ADDITIVE + IDEMPOTENT + REVERSIBLE (DROP INDEX). No schema/column change: the
-- app_fingerprint column already exists (Phase 0); Phase 4 simply populates it (on each
-- green re-prove) and reads by it. RLS/policy unchanged. The by-app read works WITHOUT
-- this index (identical scan shape to get_proven_fixes); ship it after the code.

CREATE INDEX IF NOT EXISTS ix_pcl_tenant_appfp_fp
    ON proven_control_ledger (tenant_id, app_fingerprint, control_fp)
    WHERE invalidated_at IS NULL;
