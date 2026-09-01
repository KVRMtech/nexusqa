-- Phase 8 (reuse prompt at onboarding): record which DOMAIN a login recipe was
-- recorded against, so a new app on a known host can be offered the existing
-- recipe BEFORE anyone records anything.
--
-- The login_type_key is a hash of domain + path + form shape, so it cannot be
-- reversed to a domain: at onboarding only the base URL is known and no form has
-- been seen yet, which is exactly when the proposal is worth making.
--
-- Additive + idempotent: an existing recipe simply has an empty domain until it is
-- next recorded, and an empty domain never matches anything.
ALTER TABLE tp_login_recipes
    ADD COLUMN IF NOT EXISTS login_domain VARCHAR(253) NOT NULL DEFAULT '';

-- Domain lookup is per-tenant (see persona_store.find_recipes_by_domain).
CREATE INDEX IF NOT EXISTS ix_login_recipes_domain
    ON tp_login_recipes (tenant_id, login_domain);
