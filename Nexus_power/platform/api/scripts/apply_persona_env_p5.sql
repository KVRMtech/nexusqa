-- Persona × Environment Matrix — scale (P5). Idempotent, additive.
--
-- Adds the epoch a credential card was last verified against, so staleness can
-- be judged when an environment's data snapshot rolls (persona_scale.card_staleness).
-- ADD COLUMN IF NOT EXISTS is additive and safe on a live table.
--
--   psql -U nexus -d nexus -f apply_persona_env_p5.sql

BEGIN;

ALTER TABLE tp_persona_credentials
    ADD COLUMN IF NOT EXISTS verified_epoch VARCHAR(64) NOT NULL DEFAULT '';

COMMIT;
