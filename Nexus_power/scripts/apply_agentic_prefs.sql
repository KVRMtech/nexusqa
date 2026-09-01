-- Agentic-QE per-agent on/off preferences. Additive, idempotent, tenant-RLS.
-- Mirrors surface_prefs / proven_control_ledger. Safe to run repeatedly.
-- Defaults mirror the Governor: $0 deterministic agents ON, LLM agents OFF.

CREATE TABLE IF NOT EXISTS agentic_prefs (
    tenant_id   varchar(64) PRIMARY KEY,
    sentinel    boolean NOT NULL DEFAULT true,
    context     boolean NOT NULL DEFAULT false,
    triage      boolean NOT NULL DEFAULT true,
    verdict     boolean NOT NULL DEFAULT true,
    intent      boolean NOT NULL DEFAULT false,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE agentic_prefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agentic_prefs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agentic_prefs_tenant_isolation ON agentic_prefs;
CREATE POLICY agentic_prefs_tenant_isolation ON agentic_prefs
    USING (tenant_id = current_setting('nexus.current_tenant_id', true))
    WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));
