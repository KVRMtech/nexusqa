-- CODE P0.4 — webhook app resolver (fixes the always-401 RLS bug).
--
-- The GitLab/GitHub webhook carries no JWT, so the handler must resolve an app
-- by app_id WITHOUT a tenant GUC. client_apps has FORCE ROW LEVEL SECURITY, so a
-- direct read from the service role (qec) returns NOTHING → the handler
-- fail-closes 401 on EVERY delivery (documented open-decision #7).
--
-- Fix: a NARROW, read-only SECURITY DEFINER function owned by a BYPASSRLS role
-- (nexus). SECURITY DEFINER runs with the owner's privileges, so it can read the
-- single row for one app_id and return only the three fields the handler needs
-- (tenant_id, repo_binding, status) — never the whole table, never a wrong-tenant
-- write. EXECUTE is granted only to the service role. Idempotent.

CREATE OR REPLACE FUNCTION qec_resolve_webhook_app(p_app_id text)
RETURNS TABLE (tenant_id text, repo_binding jsonb, status text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT ca.tenant_id, ca.repo_binding, ca.status
    FROM client_apps ca
    WHERE ca.app_id = p_app_id
    LIMIT 1;
$$;

-- Owner MUST be a BYPASSRLS role for SECURITY DEFINER to see past FORCE RLS.
ALTER FUNCTION qec_resolve_webhook_app(text) OWNER TO nexus;

-- Least privilege: revoke from everyone, grant EXECUTE only to the service roles.
REVOKE ALL ON FUNCTION qec_resolve_webhook_app(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION qec_resolve_webhook_app(text) TO qec;
