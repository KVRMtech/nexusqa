BEGIN;

UPDATE alembic_version SET version_num = '016_scene_state_summaries';

CREATE TABLE evidence_steps (
    step_id            VARCHAR(64) PRIMARY KEY,
    artifact_id        VARCHAR(64) NOT NULL REFERENCES canonical_artifacts(artifact_id) ON DELETE CASCADE,
    scene_id           VARCHAR(64) NOT NULL REFERENCES visual_scenes(scene_id) ON DELETE CASCADE,
    tenant_id          VARCHAR(64) NOT NULL,
    session_id         VARCHAR(64) NOT NULL,
    step_index         INTEGER NOT NULL DEFAULT 0,
    action_kind        VARCHAR(32) NOT NULL,
    target_label       VARCHAR(500) NOT NULL DEFAULT '',
    observed_value     VARCHAR(2000) NOT NULL DEFAULT '',
    trigger_control_id VARCHAR(64) REFERENCES evidence_controls(control_id) ON DELETE SET NULL,
    before_frame_id    VARCHAR(64) REFERENCES visual_frames(frame_id) ON DELETE SET NULL,
    after_frame_id     VARCHAR(64) REFERENCES visual_frames(frame_id) ON DELETE SET NULL,
    start_ms           INTEGER NOT NULL DEFAULT 0,
    end_ms             INTEGER NOT NULL DEFAULT 0,
    confidence         DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    agreement_score    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    evidence_signals   JSONB NOT NULL DEFAULT '[]'::jsonb,
    cursor_x           INTEGER,
    cursor_y           INTEGER,
    audio_intent_text  TEXT NOT NULL DEFAULT '',
    audio_intent_ts_ms INTEGER,
    metadata_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_evidence_steps_artifact ON evidence_steps(artifact_id);
CREATE INDEX ix_evidence_steps_scene_index ON evidence_steps(scene_id, step_index);
CREATE INDEX ix_evidence_steps_tenant_session ON evidence_steps(tenant_id, session_id);

CREATE TABLE cursor_events (
    event_id          VARCHAR(64) PRIMARY KEY,
    artifact_id       VARCHAR(64) NOT NULL REFERENCES canonical_artifacts(artifact_id) ON DELETE CASCADE,
    frame_id          VARCHAR(64) REFERENCES visual_frames(frame_id) ON DELETE SET NULL,
    tenant_id         VARCHAR(64) NOT NULL,
    session_id        VARCHAR(64) NOT NULL,
    frame_index       INTEGER NOT NULL DEFAULT 0,
    timestamp_ms      INTEGER NOT NULL DEFAULT 0,
    cursor_x          INTEGER NOT NULL,
    cursor_y          INTEGER NOT NULL,
    velocity          DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    is_click          BOOLEAN NOT NULL DEFAULT FALSE,
    is_drag           BOOLEAN NOT NULL DEFAULT FALSE,
    detection_method  VARCHAR(32) NOT NULL DEFAULT 'motion',
    confidence        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    metadata_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_cursor_events_artifact ON cursor_events(artifact_id);
CREATE INDEX ix_cursor_events_frame ON cursor_events(frame_id);
CREATE INDEX ix_cursor_events_tenant_session ON cursor_events(tenant_id, session_id);

UPDATE alembic_version SET version_num = '017_evidence_steps_cursor';

COMMIT;
