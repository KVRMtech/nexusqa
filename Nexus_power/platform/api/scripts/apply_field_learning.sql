-- FIELD LEARNING (P1 + P4). Idempotent, additive.
--
-- Two tables, and the difference between them is the whole safety design.
--
--   tp_field_memory  — TENANT-PRIVATE. Holds the actual value a client supplied,
--                      envelope-encrypted exactly like a credential card. Scoped by
--                      tenant_id and never readable across tenants. This is what
--                      stops a client being asked the same question twice.
--
--   field_priors     — CROSS-TENANT and PHYSICALLY VALUE-FREE. It has no column a
--                      value could be written to. It records only that a field with
--                      a given signature turned out to be a given SEMANTIC TYPE, and
--                      how often that held. This is what makes the hundred-and-first
--                      client start smarter than the first.
--
-- The absence of a value column on field_priors is not an oversight to be corrected
-- later. It is the enforcement: a future writer cannot leak a value across tenants
-- through a column that does not exist.
--
--   psql -U nexus -d nexus -f apply_field_learning.sql

BEGIN;

-- ── P1 · tenant-private field memory ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tp_field_memory (
    memory_id       VARCHAR(64)  PRIMARY KEY,
    tenant_id       VARCHAR(64)  NOT NULL,
    artifact_id     VARCHAR(64)  NOT NULL,
    -- the value-free fingerprint of the field (see field_signature.py)
    signature       VARCHAR(64)  NOT NULL,
    signature_version INTEGER    NOT NULL DEFAULT 1,
    -- what the field is FOR, from the closed vocabulary in field_semantics.py
    semantic_type   VARCHAR(48)  NOT NULL DEFAULT 'unknown',
    -- the human-readable label, kept so an operator can recognise what they are
    -- being asked about. Never used as a key.
    field_label     VARCHAR(300) NOT NULL DEFAULT '',
    -- ENVELOPE-ENCRYPTED value. Never plaintext, exactly like a credential card.
    value_blob      BYTEA        NOT NULL,
    -- how the value got here: 'provided' (the client typed it) is the only kind
    -- worth remembering; the others are regenerated, not recalled.
    provenance      VARCHAR(24)  NOT NULL DEFAULT 'provided',
    -- P5 · whether the application ACCEPTED this value the last time it was used.
    -- A remembered value that the app rejects is worse than no value, because it
    -- looks like an answer.
    accept_count    INTEGER      NOT NULL DEFAULT 0,
    reject_count    INTEGER      NOT NULL DEFAULT 0,
    last_outcome    VARCHAR(16)  NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT tp_field_memory_uq UNIQUE (tenant_id, artifact_id, signature)
);

CREATE INDEX IF NOT EXISTS tp_field_memory_lookup
    ON tp_field_memory (tenant_id, artifact_id);

-- Row-level security: a tenant may only ever see its own remembered values.
ALTER TABLE tp_field_memory ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'tp_field_memory' AND policyname = 'tp_field_memory_tenant'
    ) THEN
        CREATE POLICY tp_field_memory_tenant ON tp_field_memory
            USING (tenant_id = current_setting('app.tenant_id', TRUE))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', TRUE));
    END IF;
END $$;

-- ── P4 · cross-tenant priors — SHAPES ONLY, NEVER VALUES ────────────────────
-- There is deliberately no value column, no tenant column and no artifact column.
-- Nothing here can identify a client or carry their data.
CREATE TABLE IF NOT EXISTS field_priors (
    signature       VARCHAR(64)  NOT NULL,
    signature_version INTEGER    NOT NULL DEFAULT 1,
    semantic_type   VARCHAR(48)  NOT NULL,
    -- how many DISTINCT tenants independently confirmed this reading. A prior
    -- confirmed by one tenant is an anecdote; by twenty, it is a fact — and the
    -- count is what lets a reader tell those apart.
    tenant_count    INTEGER      NOT NULL DEFAULT 0,
    observations    INTEGER      NOT NULL DEFAULT 0,
    accepted        INTEGER      NOT NULL DEFAULT 0,
    rejected        INTEGER      NOT NULL DEFAULT 0,
    first_seen      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (signature, semantic_type)
);

CREATE INDEX IF NOT EXISTS field_priors_signature ON field_priors (signature);

-- Which tenants have contributed to a signature, WITHOUT storing what they said.
-- Needed only to keep tenant_count honest (the same client crawling fifty times
-- is still one confirmation). Holds no value and no semantic type.
CREATE TABLE IF NOT EXISTS field_prior_contributors (
    signature       VARCHAR(64)  NOT NULL,
    tenant_hash     VARCHAR(64)  NOT NULL,   -- salted hash, never the tenant id
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (signature, tenant_hash)
);

-- ── consent · cross-tenant contribution is OPT-IN, per tenant ───────────────
-- Default is NOT to contribute. A regulated client must be able to use the product
-- without their field shapes ever reaching a shared table, and the safe default is
-- the one that applies when nobody has decided yet.
CREATE TABLE IF NOT EXISTS tenant_learning_consent (
    tenant_id       VARCHAR(64)  PRIMARY KEY,
    contribute      BOOLEAN      NOT NULL DEFAULT FALSE,
    consume         BOOLEAN      NOT NULL DEFAULT TRUE,
    decided_by      VARCHAR(200) NOT NULL DEFAULT '',
    decided_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMIT;
