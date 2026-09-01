"""Phase-1 Test Studio bridge — reverse-proxy to the factory with a single portal
login. Pins: tenant-scoped service token, RBAC on mutations, surface limited to the
four Studio roots, and the client's own Authorization is never forwarded upstream."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import require_auth
from app.routers.factory_proxy import router as bridge_router

ADMIN = {"sub": "u", "tenant_id": "t1", "email": "a@x", "role": "admin"}
MANAGER = {"sub": "u", "tenant_id": "t2", "email": "m@x", "role": "manager"}
VIEWER = {"sub": "u", "tenant_id": "t1", "email": "v@x", "role": "viewer"}


class _FakeResp:
    def __init__(self, status=200, content=b'{"ok":true}', headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {"content-type": "application/json"}


class _FakeClient:
    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, params=None, content=None, headers=None):
        _FakeClient.captured = {
            "method": method, "url": url, "headers": headers,
            "params": params, "content": content,
        }
        return _FakeResp()


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr("app.routers.factory_proxy.httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr("app.routers.factory_proxy.mint_service_jwt", lambda t, **k: f"svc-{t}")
    monkeypatch.setattr(
        "app.routers.factory_proxy.settings",
        type("S", (), {"platform_api_url": "http://factory"})(),
    )
    _FakeClient.captured = {}


def _client(user):
    app = FastAPI()
    app.include_router(bridge_router)
    app.dependency_overrides[require_auth] = lambda: user
    return TestClient(app)


def test_get_forwards_with_tenant_scoped_service_token():
    r = _client(ADMIN).get("/api/v1/test-factory/art1/test-cases?limit=50")
    assert r.status_code == 200
    cap = _FakeClient.captured
    assert cap["url"] == "http://factory/api/v1/test-factory/art1/test-cases"
    assert cap["headers"]["Authorization"] == "Bearer svc-t1"  # minted for THIS tenant
    assert ("limit", "50") in cap["params"]


def test_tenant_isolation_service_token_matches_caller_tenant():
    _client(MANAGER).get("/api/v1/test-factory/art9/test-cases")
    assert _FakeClient.captured["headers"]["Authorization"] == "Bearer svc-t2"


def test_viewer_may_read():
    assert _client(VIEWER).get("/api/v1/test-factory/art1/test-cases").status_code == 200


def test_viewer_may_not_mutate_and_it_is_never_forwarded():
    r = _client(VIEWER).post("/api/v1/test-factory/art1/generate", json={})
    assert r.status_code == 403
    assert _FakeClient.captured == {}  # refused before any factory call


def test_admin_may_mutate_and_it_forwards():
    r = _client(ADMIN).post("/api/v1/test-factory/art1/playwright/run", json={"test_ids": []})
    assert r.status_code == 200
    assert _FakeClient.captured["method"] == "POST"
    assert _FakeClient.captured["url"].endswith("/api/v1/test-factory/art1/playwright/run")


def test_manager_may_mutate():
    assert _client(MANAGER).post("/api/v1/test-factory/a/scripts/regenerate-all", json={}).status_code == 200


def test_only_the_studio_roots_are_bridged():
    c = _client(ADMIN)
    assert c.get("/api/v1/artifacts/art1/x").status_code == 200
    assert c.get("/api/v1/test-runs/run1").status_code == 200
    assert c.get("/api/v1/eyes/art1/extract").status_code == 200
    assert c.get("/api/v1/agentic/config").status_code == 200
    # NOT an open proxy — an unlisted path has no route here at all.
    _FakeClient.captured = {}
    assert c.get("/api/v1/secrets/leak").status_code == 404
    assert _FakeClient.captured == {}


def test_client_authorization_header_is_replaced_never_forwarded():
    _client(ADMIN).get(
        "/api/v1/test-factory/art1/test-cases",
        headers={"Authorization": "Bearer attacker-token"},
    )
    assert _FakeClient.captured["headers"]["Authorization"] == "Bearer svc-t1"
