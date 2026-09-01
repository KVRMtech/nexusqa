BEGIN;

CREATE TABLE ui_dictionary_entries (
    entry_id                  VARCHAR(64) PRIMARY KEY,
    tenant_id                 VARCHAR(64) NOT NULL,
    element_signature         VARCHAR(64) NOT NULL,
    page_key                  VARCHAR(200) NOT NULL DEFAULT '',
    domain                    VARCHAR(200) NOT NULL DEFAULT '',
    element_type              VARCHAR(50) NOT NULL,
    label_text                VARCHAR(500) NOT NULL DEFAULT '',
    display_label             VARCHAR(600) NOT NULL DEFAULT '',
    action_kind               VARCHAR(32) NOT NULL DEFAULT '',
    preferred_selector        VARCHAR(2000) NOT NULL DEFAULT '',
    selector_confidence       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    selector_source           VARCHAR(20) NOT NULL DEFAULT 'unknown',
    recognition_count         INTEGER NOT NULL DEFAULT 0,
    automation_success_count  INTEGER NOT NULL DEFAULT 0,
    automation_failure_count  INTEGER NOT NULL DEFAULT 0,
    bbox_centre_x             INTEGER,
    bbox_centre_y             INTEGER,
    metadata_json             JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_ui_dictionary_tenant_signature UNIQUE (tenant_id, element_signature)
);
CREATE INDEX ix_ui_dictionary_tenant_page ON ui_dictionary_entries(tenant_id, page_key);
CREATE INDEX ix_ui_dictionary_tenant_domain ON ui_dictionary_entries(tenant_id, domain);

UPDATE alembic_version SET version_num = '018_ui_dictionary';

COMMIT;
