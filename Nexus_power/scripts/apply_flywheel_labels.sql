-- flywheel_labels — additive, de-identified correction ledger (Phase 2 foundation).
-- The substrate of the consented failure→fix flywheel. Mirrors apply_script_versions.sql
-- EXACTLY: tenant_id + RLS ENABLE+FORCE+tenant_isolation policy. IDEMPOTENT + reversible
-- (DROP TABLE flywheel_labels;). EVERY column is an enum/bool/bucket/hash — NEVER raw
-- text, values, selectors, or accessible names (de-id is enforced by the schema itself).
-- Apply on prod out-of-band (alembic does not run at startup):
--   sudo docker cp scripts/apply_flywheel_labels.sql nexus-postgres:/tmp/x.sql
--   sudo docker exec nexus-postgres psql -U nexus -d nexus -v ON_ERROR_STOP=1 -f /tmp/x.sql

CREATE TABLE IF NOT EXISTS flywheel_labels (
    flywheel_label_id    VARCHAR(64) PRIMARY KEY,
    tenant_id            VARCHAR(64) NOT NULL,                  -- the RLS key
    artifact_id          VARCHAR(64) NOT NULL DEFAULT '',
    test_case_id         VARCHAR(64) NOT NULL DEFAULT '',
    scenario_id          VARCHAR(64) NOT NULL DEFAULT '',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    generator_version    VARCHAR(64) NOT NULL DEFAULT '',       -- pin example → code (provenance)
    git_commit           VARCHAR(64) NOT NULL DEFAULT '',
    consented            BOOLEAN     NOT NULL DEFAULT false,    -- export gate, stamped at write
    vertical_tag         VARCHAR(40) NOT NULL DEFAULT 'unspecified',
    decision_point       VARCHAR(32) NOT NULL,                  -- script_edit|heal_approve|
                         -- triage_feedback|heal_outcome|value_conflict|control_kind_fix|
                         -- reanchor|scenario_lifecycle
    -- de-identified FEATURES (model input) — all coarse
    verb_enum            VARCHAR(24) NOT NULL DEFAULT '',
    emitted_method_enum  VARCHAR(24) NOT NULL DEFAULT '',       -- fill|selectOption|check|click
    observed_kind_enum   VARCHAR(24) NOT NULL DEFAULT '',
    has_grounded_select  BOOLEAN     NOT NULL DEFAULT false,
    options_count_bucket VARCHAR(8)  NOT NULL DEFAULT '',       -- 0|1|2+
    value_shape_sig      VARCHAR(24) NOT NULL DEFAULT '',       -- empty|numeric|masked|date|free-text
    selector_drifted     BOOLEAN     NOT NULL DEFAULT false,
    bbox_drifted         BOOLEAN     NOT NULL DEFAULT false,
    is_flaky             BOOLEAN     NOT NULL DEFAULT false,
    error_pattern_class  VARCHAR(40) NOT NULL DEFAULT '',       -- regex-bucket id, NOT the text
    similarity_bucket    VARCHAR(8)  NOT NULL DEFAULT '',
    ambiguity_bucket     VARCHAR(8)  NOT NULL DEFAULT '',
    label_token_hash     VARCHAR(64) NOT NULL DEFAULT '',       -- per-tenant HMAC; opaque across
    diff_shape_enum      VARCHAR(32) NOT NULL DEFAULT '',
    -- the engine's call + the human/outcome CORRECTION (the label)
    engine_verdict_enum  VARCHAR(24) NOT NULL DEFAULT '',
    engine_confidence    REAL        NOT NULL DEFAULT 0,
    human_decision_enum  VARCHAR(32) NOT NULL DEFAULT '',       -- approved|declined|left_pending|
                         -- overridden_to:<v>|chosen_typed|chosen_committed|chosen_other
    verified_green       BOOLEAN,                               -- from evaluate_heal (nullable)
    later_contradicted   BOOLEAN                                -- approved_then_contradicted proxy
);

CREATE INDEX IF NOT EXISTS ix_flywheel_labels_tenant ON flywheel_labels (tenant_id);
CREATE INDEX IF NOT EXISTS ix_flywheel_labels_export
    ON flywheel_labels (consented, vertical_tag, decision_point);

-- RLS — byte-identical to apply_script_versions.sql (tenant isolation, ENABLE+FORCE).
DO $$
BEGIN
  IF EXISTS (SELECT FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'flywheel_labels') THEN
    ALTER TABLE flywheel_labels ENABLE ROW LEVEL SECURITY;
    ALTER TABLE flywheel_labels FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON flywheel_labels;
    CREATE POLICY tenant_isolation ON flywheel_labels
      USING (tenant_id = current_setting('nexus.current_tenant_id', true))
      WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_app') THEN
      GRANT SELECT, INSERT, UPDATE, DELETE ON flywheel_labels TO nexus_app;
    END IF;
  END IF;
END$$;
