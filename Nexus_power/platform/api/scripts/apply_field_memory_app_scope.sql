-- T-FE-09 · FIELD LEARNING MUST SURVIVE MORE THAN ONE CRAWL.  Idempotent, additive.
--
-- THE DEFECT, exactly as it shipped.  `tp_field_memory` is keyed
-- (tenant_id, artifact_id, signature) and a re-crawl MINTS A NEW ARTIFACT.  So
-- crawl N wrote its answers under artifact N; crawl N+1 read artifact N (the
-- "latest completed" one) and wrote to artifact N+1; and crawl N+2 could no
-- longer see anything crawl N had learned.  Every crawl inherited exactly one
-- generation of memory and then dropped it — which reads, from the outside,
-- exactly like learning that works.
--
-- The identity had the same defect through the same key: `identity_seed` came
-- back as "tenant::artifact", so the synthetic applicant CHANGED between runs.
-- A rate quote that moves because the age moved is a false difference, and
-- after the fact there is no way to tell it from a real one.
--
-- Both are one mistake — a per-RUN key used for per-APPLICATION knowledge.
--
-- WHY A NEW COLUMN AND NOT A REWRITTEN ONE.  The ciphertext in `value_blob` is
-- bound to (tenant, artifact, signature) through its AAD.  Overwriting
-- `artifact_id` with an application id would make every existing blob
-- undecryptable — a silent, total loss of everything clients have already told
-- us, indistinguishable from "we never knew".  So `app_id` is ADDED,
-- `artifact_id` is KEPT, and the reader falls back to the artifact-keyed row
-- with its original AAD when no app-scoped row exists yet.  Nothing is
-- destroyed and nothing has to be re-wrapped in a batch job.
--
-- `scope_version` is the same discipline as `field_signature.SIGNATURE_VERSION`:
-- when the MEANING of a scope changes, old rows become identifiable rather than
-- silently mismatched.
--
--   psql -U nexus -d nexus -f apply_field_memory_app_scope.sql

BEGIN;

ALTER TABLE tp_field_memory
    ADD COLUMN IF NOT EXISTS app_id        VARCHAR(64)  NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS scope_version INTEGER      NOT NULL DEFAULT 1;

-- The app-scoped uniqueness that replaces the per-artifact one.  PARTIAL, so
-- legacy rows (app_id = '') are untouched and keep their old unique constraint;
-- only rows that have been given an application take the new key.
CREATE UNIQUE INDEX IF NOT EXISTS tp_field_memory_app_uq
    ON tp_field_memory (tenant_id, app_id, signature)
    WHERE app_id <> '';

CREATE INDEX IF NOT EXISTS tp_field_memory_app_lookup
    ON tp_field_memory (tenant_id, app_id)
    WHERE app_id <> '';

-- The RLS policy already scopes every read and write to `current_setting`'s
-- tenant, and it is written against `tenant_id`, which has not moved.  Adding a
-- column cannot widen it — but say so explicitly rather than leaving a reader to
-- work it out, because "the new column is not in the policy" is exactly the
-- shape of a cross-tenant leak.
COMMIT;
