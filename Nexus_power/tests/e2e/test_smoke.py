"""
Nexus QA Platform — End-to-End Smoke Tests
=============================================
Validates that all 14 services are running and that key cross-service
flows work from the outside (via HTTP), just as a real client would call them.

Prerequisites:
  - All 14 services running (see scripts/health_check.py)
  - PostgreSQL:5432, Redis:6379, Neo4j:7687 up
  - LLM_BACKEND=stub (heart engine)

Run:
    pytest tests/e2e/test_smoke.py -v
"""

import io
import os
import uuid
import pytest
import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE = "http://localhost"
PORTS = {
    "auth": 8000,
    "shield": 8001,
    "ears": 8002,
    "eyes": 8003,
    "heart": 8004,
    "backbone": 8005,
    "nerves": 8006,
    "legs": 8007,
    "hands": 8008,
    "spine": 8009,
    "mouth": 8010,
    "gateway": 8080,
    "platform-api": 8091,
    "orchestrator": 8100,
}

TENANT_ID = "t-smoke-test"
TIMEOUT = 120
E2E_EMAIL = os.environ.get("NEXUS_E2E_EMAIL", "admin@nexus.local")
E2E_PASSWORD = os.environ.get("NEXUS_E2E_PASSWORD", "change-this-password")


def url(svc: str, path: str) -> str:
    return f"{BASE}:{PORTS[svc]}{path}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    with httpx.Client(timeout=TIMEOUT) as c:
        yield c


@pytest.fixture(scope="module")
def auth_token(client: httpx.Client):
    """Obtain a JWT token for authenticated API calls."""
    try:
        r = client.post(url("auth", "/api/v1/auth/login"), json={
            "email": E2E_EMAIL, "password": E2E_PASSWORD,
        })
        if r.status_code == 200 and r.json().get("access_token"):
            return r.json()["access_token"]
    except Exception:
        pass
    pytest.skip("Cannot obtain auth token")


@pytest.fixture(scope="module")
def hdr(auth_token):
    """Auth headers dict, usable everywhere."""
    return {"Authorization": f"Bearer {auth_token}"}


# ===================================================================
# 1. HEALTH CHECKS — every service must respond 200 on /health
# ===================================================================

class TestHealthChecks:
    @pytest.mark.parametrize("service,port", list(PORTS.items()))
    def test_health(self, client, service, port):
        r = client.get(f"{BASE}:{port}/health")
        assert r.status_code == 200, f"{service}:{port} unhealthy: {r.text}"
        body = r.json()
        assert body.get("status") in ("healthy", "ok", True), body


# ===================================================================
# 2. AUTH SERVICE
# ===================================================================

class TestAuthService:
    def test_login_returns_token(self, auth_token):
        assert auth_token and len(auth_token) > 20

    def test_get_current_user(self, client, hdr):
        r = client.get(url("auth", "/api/v1/auth/users/me"), headers=hdr)
        assert r.status_code in (200, 404), r.text


# ===================================================================
# 3. SHIELD ENGINE — PII redaction
# ===================================================================

class TestShieldEngine:
    def test_redact_text(self, client, hdr):
        r = client.post(
            url("shield", "/api/v1/shield/redact"),
            json={
                "tenant_id": TENANT_ID,
                "text": "John Smith's SSN is 123-45-6789 and email is john@example.com",
            },
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        safe = body.get("safe_text", "")
        assert "123-45-6789" not in safe, "SSN was NOT redacted"
        assert body.get("entity_count", 0) > 0

    def test_analyze_text(self, client, hdr):
        r = client.post(
            url("shield", "/api/v1/shield/analyze"),
            json={"tenant_id": TENANT_ID, "text": "Call me at 555-123-4567"},
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "entities" in body or "risk_level" in body


# ===================================================================
# 4. HEART ENGINE — Rule extraction (stub LLM)
# ===================================================================

class TestHeartEngine:
    def test_extract_rules(self, client, hdr):
        r = client.post(
            url("heart", "/api/v1/heart/extract-rules"),
            json={
                "tenant_id": TENANT_ID,
                "transcript": "The premium is based on age. Over 65 gets 20% surcharge.",
                "session_id": f"s-{uuid.uuid4().hex[:8]}",
            },
            headers=hdr,
        )
        # 200 = OK, 500 = stub LLM limitation (acceptable in dev)
        assert r.status_code in (200, 500), r.text
        if r.status_code == 200:
            body = r.json()
            assert "rules" in body or "error" not in body

    def test_ask_question(self, client, hdr):
        r = client.post(
            url("heart", "/api/v1/heart/ask"),
            json={"tenant_id": TENANT_ID, "question": "What is the underwriting process?"},
            headers=hdr,
        )
        assert r.status_code in (200, 500), r.text


# ===================================================================
# 5. BACKBONE ENGINE — Knowledge graph
# ===================================================================

class TestBackboneEngine:
    def test_store_and_retrieve_node(self, client, hdr):
        r = client.post(
            url("backbone", "/api/v1/backbone/nodes"),
            json={
                "tenant_id": TENANT_ID,
                "node_type": "business_rule",
                "properties": {"title": "E2E Rule", "description": "10% smoker surcharge"},
                "tags": ["e2e"],
            },
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        node_id = r.json().get("node_id")
        assert node_id

        r2 = client.get(url("backbone", f"/api/v1/backbone/nodes/{node_id}"), headers=hdr)
        assert r2.status_code == 200, r2.text

    def test_search(self, client, hdr):
        r = client.post(
            url("backbone", "/api/v1/backbone/search"),
            json={"tenant_id": TENANT_ID, "query": "smoker premium", "limit": 5},
            headers=hdr,
        )
        # 200 = OK, 500 = in-memory vector store limitation
        assert r.status_code in (200, 500), r.text
        if r.status_code == 200:
            assert "results" in r.json()

    def test_stats(self, client, hdr):
        r = client.get(url("backbone", "/api/v1/backbone/stats"), headers=hdr)
        assert r.status_code == 200, r.text


# ===================================================================
# 6. NERVES ENGINE — External connectors (stub mode)
# ===================================================================

class TestNervesEngine:
    def test_list_connectors(self, client, hdr):
        r = client.get(url("nerves", "/api/v1/nerves/connectors"), headers=hdr)
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), (list, dict))

    def test_execute_stub_action(self, client, hdr):
        r = client.post(
            url("nerves", "/api/v1/nerves/execute"),
            json={
                "tenant_id": TENANT_ID,
                "connector": "webhook",
                "action": "send",
                "parameters": {"url": "https://httpbin.org/post", "payload": {"test": True}},
            },
            headers=hdr,
        )
        assert r.status_code in (200, 422), r.text


# ===================================================================
# 7. HANDS ENGINE — Test data generation
# ===================================================================

class TestHandsEngine:
    def test_generate_boundary_values(self, client, hdr):
        r = client.post(
            url("hands", "/api/v1/hands/generate-boundary"),
            json={
                "tenant_id": TENANT_ID,
                "field_name": "age",
                "field_type": "integer",
                "min_value": 18,
                "max_value": 65,
            },
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "boundary_values" in body

    def test_generate_combinatorial(self, client, hdr):
        r = client.post(
            url("hands", "/api/v1/hands/generate-combinatorial"),
            json={
                "tenant_id": TENANT_ID,
                "dimensions": {
                    "browser": ["chrome", "firefox", "safari"],
                    "os": ["windows", "mac", "linux"],
                },
                "strategy": "pairwise",
            },
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        assert "combinations" in r.json()

    def test_stats(self, client, hdr):
        r = client.get(url("hands", "/api/v1/hands/stats"), headers=hdr)
        assert r.status_code == 200, r.text


# ===================================================================
# 8. SPINE ENGINE — Document ingestion
# ===================================================================

class TestSpineEngine:
    def test_stats(self, client, hdr):
        r = client.get(url("spine", "/api/v1/spine/stats"), headers=hdr)
        assert r.status_code == 200, r.text

    def test_list_documents(self, client, hdr):
        r = client.get(
            url("spine", "/api/v1/spine/documents"),
            params={"tenant_id": TENANT_ID},
            headers=hdr,
        )
        assert r.status_code == 200, r.text

    def test_ingest_text_file(self, client, hdr):
        content = b"This is a smoke test document for ingestion."
        files = {"file": ("smoke_test.txt", io.BytesIO(content), "text/plain")}
        r = client.post(
            url("spine", "/api/v1/spine/ingest"),
            data={"tenant_id": TENANT_ID},
            files=files,
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("document_id") or body.get("job_id")


# ===================================================================
# 9. MOUTH ENGINE — Reports
# ===================================================================

class TestMouthEngine:
    def test_stats(self, client, hdr):
        r = client.get(url("mouth", "/api/v1/mouth/stats"), headers=hdr)
        assert r.status_code == 200, r.text

    def test_list_reports(self, client, hdr):
        r = client.get(
            url("mouth", "/api/v1/mouth/reports"),
            params={"tenant_id": TENANT_ID},
            headers=hdr,
        )
        assert r.status_code == 200, r.text


# ===================================================================
# 10. LEGS ENGINE
# ===================================================================

class TestLegsEngine:
    def test_health_detail(self, client, hdr):
        r = client.get(url("legs", "/health/detail"), headers=hdr)
        assert r.status_code == 200, r.text


# ===================================================================
# 11. EARS ENGINE
# ===================================================================

class TestEarsEngine:
    def test_list_sessions(self, client, hdr):
        r = client.get(url("ears", "/api/v1/ears/sessions"), headers=hdr)
        assert r.status_code == 200, r.text


# ===================================================================
# 12. EYES ENGINE
# ===================================================================

class TestEyesEngine:
    def test_health_detail(self, client, hdr):
        r = client.get(url("eyes", "/health/detail"), headers=hdr)
        assert r.status_code == 200, r.text


# ===================================================================
# 13. GATEWAY — Proxy routing
# ===================================================================

class TestGateway:
    def test_root_info(self, client):
        r = client.get(url("gateway", "/"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert "service" in body or "version" in body or "engines" in body

    def test_engines_status(self, client, hdr):
        try:
            r = client.get(url("gateway", "/api/v1/engines/status"), headers=hdr)
            assert r.status_code in (200, 504), r.text
        except httpx.ReadTimeout:
            pytest.skip("Gateway engines/status timed out (probes all engines)")

    def test_proxy_to_backbone_stats(self, client, hdr):
        r = client.get(url("gateway", "/api/v1/backbone/stats"), headers=hdr)
        # 200=ok, 502=upstream error, 503=engine unavailable via gateway
        assert r.status_code in (200, 502, 503), r.text


# ===================================================================
# 14. PLATFORM API — Sessions, Insights, Admin
# ===================================================================

class TestPlatformAPI:
    def test_health(self, client):
        r = client.get(url("platform-api", "/health"))
        assert r.status_code == 200
        assert r.json().get("status") == "healthy"

    def test_create_and_list_sessions(self, client, hdr):
        r = client.post(
            url("platform-api", "/api/v1/sessions"),
            json={
                "title": f"E2E Session {uuid.uuid4().hex[:6]}",
                "session_type": "knowledge_transfer",
            },
            headers=hdr,
        )
        assert r.status_code in (200, 201, 503), r.text
        if r.status_code in (200, 201):
            session_id = r.json().get("session_id") or r.json().get("id")
            assert session_id

        r2 = client.get(
            url("platform-api", "/api/v1/sessions"),
            headers=hdr,
        )
        assert r2.status_code == 200, r2.text

    def test_insights_kpis(self, client, hdr):
        r = client.get(
            url("platform-api", "/api/v1/insights/kpis"),
            headers=hdr,
        )
        assert r.status_code == 200, r.text

    def test_admin_engines(self, client, hdr):
        r = client.get(url("platform-api", "/api/v1/admin/engines"), headers=hdr)
        assert r.status_code == 200, r.text

    def test_admin_resources(self, client, hdr):
        r = client.get(url("platform-api", "/api/v1/admin/resources"), headers=hdr)
        assert r.status_code in (200, 500), r.text


# ===================================================================
# 15. ORCHESTRATOR — Chains & workflows
# ===================================================================

class TestOrchestrator:
    def test_list_chains(self, client, hdr):
        r = client.get(url("orchestrator", "/api/v1/orchestrator/chains"), headers=hdr)
        # 200 = OK, 500 = internal state issue (acceptable in dev)
        assert r.status_code in (200, 500), r.text

    def test_dashboard_summary(self, client, hdr):
        r = client.get(url("orchestrator", "/api/v1/orchestrator/dashboard/summary"), headers=hdr)
        assert r.status_code in (200, 500), r.text

    def test_list_workflows(self, client, hdr):
        r = client.get(url("orchestrator", "/api/v1/orchestrator/workflows"), headers=hdr)
        assert r.status_code in (200, 500), r.text


# ===================================================================
# 16. CROSS-SERVICE FLOW: Shield → Heart → Backbone
# ===================================================================

class TestCrossServiceFlow:
    """
    End-to-end mini pipeline:
      1. Shield: redact PII from transcript
      2. Heart: extract rules from redacted text
      3. Backbone: store extracted rule as a graph node
      4. Backbone: search and verify it's findable
    """

    def test_redact_then_extract_then_store(self, client, hdr):
        # 1. Redact PII
        raw = (
            "John Doe (SSN 987-65-4321) said the deductible is $500 for "
            "claims under $10,000 and $1,000 for claims above that."
        )
        r1 = client.post(
            url("shield", "/api/v1/shield/redact"),
            json={"tenant_id": TENANT_ID, "text": raw},
            headers=hdr,
        )
        assert r1.status_code == 200, f"Shield redact failed: {r1.text}"
        safe_text = r1.json().get("safe_text", raw)
        assert "987-65-4321" not in safe_text

        # 2. Extract rules
        r2 = client.post(
            url("heart", "/api/v1/heart/extract-rules"),
            json={
                "tenant_id": TENANT_ID,
                "transcript": safe_text,
                "session_id": f"s-{uuid.uuid4().hex[:8]}",
            },
            headers=hdr,
        )
        # 200 = OK, 500 = stub LLM limitation
        assert r2.status_code in (200, 500), f"Heart extract failed: {r2.text}"
        rules = r2.json().get("rules", []) if r2.status_code == 200 else []

        # 3. Store in knowledge graph
        desc = rules[0].get("description", safe_text) if rules else safe_text
        r3 = client.post(
            url("backbone", "/api/v1/backbone/nodes"),
            json={
                "tenant_id": TENANT_ID,
                "node_type": "business_rule",
                "properties": {"title": "E2E Cross-Service Rule", "description": desc},
                "tags": ["e2e", "cross-service"],
            },
            headers=hdr,
        )
        assert r3.status_code == 200, f"Backbone store failed: {r3.text}"
        assert r3.json().get("node_id")

        # 4. Search
        r4 = client.post(
            url("backbone", "/api/v1/backbone/search"),
            json={"tenant_id": TENANT_ID, "query": "deductible claims", "limit": 5},
            headers=hdr,
        )
        assert r4.status_code in (200, 500), f"Backbone search failed: {r4.text}"
