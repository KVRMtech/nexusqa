-- Part-11 evidence chain table (heal_events) — idempotent, safe to re-run.
--
-- WHY THIS EXISTS: app/services/diff_and_heal/heal_evidence.py implements the
-- append-only, hash-chained audit ledger that the Execution Evidence Report
-- writes to (evidence_exported / review_disposition events, spec §2.17). The
-- table was never created in some deployments, so every audit write silently
-- degraded to a WARNING and the "immutable audit trail" had nothing behind it.
-- Discovered 2026-07-25 while verifying Phase R3 against the live system.
--
-- IMMUTABILITY IS ENFORCED AT THE GRANT, not by convention: the application
-- role gets SELECT + INSERT only. A tamper attempt through the app's own
-- credentials fails at the database, and the hash chain makes any out-of-band
-- edit detectable.
--
--   psql -U nexus -d nexus -f apply_heal_events.sql

BEGIN;

CREATE TABLE IF NOT EXISTS heal_events (
    heal_event_id      VARCHAR(64)  PRIMARY KEY,
    tenant_id          VARCHAR(64)  NOT NULL,
    artifact_id        VARCHAR(64)  NOT NULL,
    scenario_id        VARCHAR(100) NOT NULL DEFAULT '',
    step_number        INTEGER      NOT NULL DEFAULT 0,
    event_type         VARCHAR(30)  NOT NULL,
    actor              VARCHAR(200) NOT NULL DEFAULT '',
    fix_kind           VARCHAR(40)  NOT NULL DEFAULT '',
    before_locator     TEXT         NOT NULL DEFAULT '',
    after_locator      TEXT         NOT NULL DEFAULT '',
    engine_verdict     VARCHAR(40)  NOT NULL DEFAULT '',
    verified_green     BOOLEAN      NOT NULL DEFAULT FALSE,
    version_no         INTEGER      NOT NULL DEFAULT 0,
    run_id             VARCHAR(64)  NOT NULL DEFAULT '',
    reason_for_change  TEXT         NOT NULL DEFAULT '',
    details            JSON         NOT NULL DEFAULT '{}'::json,
    prev_hash          VARCHAR(64)  NOT NULL DEFAULT '',
    row_hash           VARCHAR(64)  NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Optional detached signature over row_hash (heal_evidence.sign_row_hash).
-- Added separately so the column can land on an existing table.
ALTER TABLE heal_events ADD COLUMN IF NOT EXISTS signature VARCHAR(128) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS ix_heal_events_tenant_artifact ON heal_events (tenant_id, artifact_id);
CREATE INDEX IF NOT EXISTS ix_heal_events_run            ON heal_events (run_id);
CREATE INDEX IF NOT EXISTS ix_heal_events_created        ON heal_events (created_at);
CREATE INDEX IF NOT EXISTS ix_heal_events_tenant         ON heal_events (tenant_id);
CREATE INDEX IF NOT EXISTS ix_heal_events_artifact       ON heal_events (artifact_id);

-- ── Row-level security: a tenant sees only its own chain ────────────────────
ALTER TABLE heal_events ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'heal_events' AND policyname = 'heal_events_tenant_isolation'
    ) THEN
        CREATE POLICY heal_events_tenant_isolation ON heal_events
            USING (tenant_id = current_setting('nexus.current_tenant_id', true))
            WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));
    END IF;
END $$;

-- ── Immutability at the grant: append-only for the application role ─────────
-- No UPDATE, no DELETE. The ledger can be written and read, never rewritten.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus') THEN
        EXECUTE 'REVOKE ALL ON heal_events FROM nexus';
        EXECUTE 'GRANT SELECT, INSERT ON heal_events TO nexus';
    END IF;
END $$;

COMMIT;
