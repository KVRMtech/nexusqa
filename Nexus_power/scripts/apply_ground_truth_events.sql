-- Ground-truth capture sidecar (Road B — the Tier-0 overlay).
-- Idempotent + tenant-RLS. Apply with a superuser/owner connection:
--   psql "$DATABASE_URL" -f scripts/apply_ground_truth_events.sql
-- Until applied, the GROUND_TRUTH overlay is simply inert (no rows) and the
-- video pipeline is byte-identical to today (fail-open). Generic across every
-- capture modality — web CDP, Windows UIA, macOS AX, mainframe HLLAPI, Appium —
-- all post the SAME shape, so a new modality needs no schema change.

CREATE TABLE IF NOT EXISTS ground_truth_events (
    event_id          VARCHAR(64)  PRIMARY KEY,
    artifact_id       VARCHAR(64)  NOT NULL
                        REFERENCES canonical_artifacts(artifact_id) ON DELETE CASCADE,
    tenant_id         VARCHAR(64)  NOT NULL,
    session_id        VARCHAR(64)  NOT NULL DEFAULT '',
    sequence_index    INTEGER      NOT NULL DEFAULT 0,
    timestamp_ms      INTEGER      NOT NULL DEFAULT 0,
    kind              VARCHAR(40)  NOT NULL DEFAULT 'navigate',
    url               VARCHAR(2000) NOT NULL DEFAULT '',
    url_host          VARCHAR(500) NOT NULL DEFAULT '',
    url_path          VARCHAR(2000) NOT NULL DEFAULT '',
    url_query         VARCHAR(2000) NOT NULL DEFAULT '',
    target_label      VARCHAR(500) NOT NULL DEFAULT '',
    value             VARCHAR(1000) NOT NULL DEFAULT '',
    target_kind       VARCHAR(40)  NOT NULL DEFAULT '',
    modality          VARCHAR(40)  NOT NULL DEFAULT 'web_cdp',
    recorder_version  VARCHAR(50)  NOT NULL DEFAULT 'v1',
    signals           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ground_truth_events_artifact_seq
    ON ground_truth_events (artifact_id, sequence_index);
CREATE INDEX IF NOT EXISTS ix_ground_truth_events_tenant_session
    ON ground_truth_events (tenant_id, session_id);

-- Tenant isolation (mirrors the other Phase-2 tables): rows are visible only to
-- their tenant, enforced by the per-transaction `nexus.current_tenant_id` GUC.
ALTER TABLE ground_truth_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE ground_truth_events FORCE ROW LEVEL SECURITY;

-- Policy name is `tenant_isolation` (NOT a table-prefixed name): every Phase-2
-- substrate table names its RLS policy `tenant_isolation`, and the substrate
-- contract test (test_substrate_contract.py::test_unique_constraints_policies_
-- and_force_rls) asserts (table, 'tenant_isolation') is present. A prefixed
-- name is a policy the test cannot see -> "ground_truth_events missing policy".
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'ground_truth_events'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON ground_truth_events
            USING (tenant_id = current_setting('nexus.current_tenant_id', true))
            WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));
    END IF;
    -- Idempotent cleanup: drop the legacy table-prefixed policy if a prior
    -- apply created it, so both names never coexist.
    IF EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'ground_truth_events'
          AND policyname = 'ground_truth_events_tenant_isolation'
    ) THEN
        DROP POLICY ground_truth_events_tenant_isolation ON ground_truth_events;
    END IF;
END $$;
