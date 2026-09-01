-- Phase 7 (record at onboarding): hold a RECORDED login on the app until the
-- first crawl mints an artifact.
--
-- Recipes are artifact-scoped and no artifact exists when an app is created, so a
-- login recorded on the Access page has nowhere to live yet. It waits here and is
-- materialised into tp_login_recipes on crawl completion.
--
-- Contains NO credential values — steps, slot NAMES and the reuse keys only. The
-- SESSION from the same recording travels separately in the encrypted creds_blob.
--
-- Additive + idempotent: an existing app simply has {} and nothing is materialised.
ALTER TABLE client_apps
    ADD COLUMN IF NOT EXISTS login_recording JSONB NOT NULL DEFAULT '{}'::jsonb;
