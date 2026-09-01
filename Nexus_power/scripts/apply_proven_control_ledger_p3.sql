-- Phase 3 — invalidation + provenance for proven_control_ledger.
-- A seed that fails its OWN first prove is "stale" (the app changed since the fix
-- was proven). We bump stale_count and, once it reaches the threshold, quarantine
-- the row (invalidated_at) so it stops being SEEDED until a fresh green re-proves it.
-- Invalidation only ever REMOVES a seed (the loop then heals from scratch), so it
-- can never green-wash. ADDITIVE + IDEMPOTENT + REVERSIBLE (DROP COLUMN ...).
-- No RLS/policy/grant change — the columns inherit the table's existing posture.

ALTER TABLE proven_control_ledger
    ADD COLUMN IF NOT EXISTS stale_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE proven_control_ledger
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ NULL;
ALTER TABLE proven_control_ledger
    ADD COLUMN IF NOT EXISTS invalidated_reason VARCHAR(200) NOT NULL DEFAULT '';

-- Seed-read fast path already filters (tenant_id, app_key, control_fp IN (...)) and
-- now also invalidated_at IS NULL; a partial index keeps the live (non-quarantined)
-- set cheap to scan as the ledger grows.
CREATE INDEX IF NOT EXISTS ix_proven_control_ledger_live
    ON proven_control_ledger (tenant_id, app_key)
    WHERE invalidated_at IS NULL;
