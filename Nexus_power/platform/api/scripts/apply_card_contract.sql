-- Credential-card contract (F2/F3). Idempotent, additive.
--
-- Records WHICH login a card was provisioned against:
--   login_type_key  -- the login-form fingerprint (see login_fingerprint.py)
--   recipe_version  -- the recipe version whose slots the card was checked against
--
-- Without these, a re-recorded login that renames a slot leaves every card looking
-- healthy while none of them can authenticate: the run skips the login, executes
-- unauthenticated, and the failures are attributed to the application under test.
-- Both default to the empty/zero value, so existing cards read as "provisioned
-- before the contract existed" rather than as matching some recipe they were never
-- checked against.
--
--   psql -U nexus -d nexus -f apply_card_contract.sql

BEGIN;

ALTER TABLE tp_persona_credentials
    ADD COLUMN IF NOT EXISTS login_type_key VARCHAR(64) NOT NULL DEFAULT '';

ALTER TABLE tp_persona_credentials
    ADD COLUMN IF NOT EXISTS recipe_version INTEGER NOT NULL DEFAULT 0;

COMMIT;
