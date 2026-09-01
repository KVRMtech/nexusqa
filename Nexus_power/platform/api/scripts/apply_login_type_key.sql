-- Phase 5 (record-once / reuse): key each login recipe by its login TYPE
-- (domain + login-page + login-FORM fingerprint, computed by login_fingerprint)
-- so the fleet can propose an already-recorded recipe instead of re-recording.
-- Additive + idempotent: an existing recipe simply has an empty key until re-keyed.
ALTER TABLE tp_login_recipes
    ADD COLUMN IF NOT EXISTS login_type_key VARCHAR(64) NOT NULL DEFAULT '';

-- Reuse lookup is per-tenant by key (see persona_store.find_recipes_by_login_type).
CREATE INDEX IF NOT EXISTS ix_login_recipes_type_key
    ON tp_login_recipes (tenant_id, login_type_key);
