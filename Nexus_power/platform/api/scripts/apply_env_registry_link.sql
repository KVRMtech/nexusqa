-- One environment registry, seen from the runner side (F6). Idempotent, additive.
--
-- Onboarding (qe-central, app_environments) collects the RICH profile: base_url,
-- routing cookies/headers, data overrides, fences and an env assertion. Studio's
-- governance registry (tp_environments) held only posture/production/base_url/epoch.
-- Two lists, nothing linking them, and an operator reasonably believes they are one.
--
-- The concrete harm was not cosmetic: environment_routing copies cookies, headers and
-- env_assertion out of the environment row into the run's context, and the row had
-- nowhere to hold them — so a cookie-selected lane on a shared host silently landed on
-- the host's default, which for these estates is production. The copy could never fire.
--
-- These columns give the runner-side row somewhere to keep the routing fields it must
-- APPLY. Ownership does not move: qe-central still owns the profile and pushes the
-- non-secret routing fields here. `source` records which registry a row came from, and
-- `app_env_id` links it back to the profile it mirrors, so the panel can say plainly
-- what it is showing instead of implying one list.
--
-- SECRETS ARE NOT MIRRORED. The profile's creds_blob stays sealed in qe-central; only
-- non-secret routing travels.
--
--   psql -U nexus -d nexus -f apply_env_registry_link.sql

BEGIN;

ALTER TABLE tp_environments
    ADD COLUMN IF NOT EXISTS cookies JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE tp_environments
    ADD COLUMN IF NOT EXISTS headers JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE tp_environments
    ADD COLUMN IF NOT EXISTS env_assertion JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE tp_environments
    ADD COLUMN IF NOT EXISTS source VARCHAR(24) NOT NULL DEFAULT 'studio';

ALTER TABLE tp_environments
    ADD COLUMN IF NOT EXISTS app_env_id VARCHAR(64) NOT NULL DEFAULT '';

COMMIT;
