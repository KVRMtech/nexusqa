"""Route-level HTTP tests for the P2/P3/P6 catalog + persona routes: auth is
required, the path/tenant/body flow to the service, response shapes are right, and
the not-found path returns 404. The service layer is monkeypatched — its DB paths
are proven in test_catalog_store / test_persona_journeys against real Postgres."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import require_auth
from app.routers import journeys
from app.services import catalog_store, persona_journeys

_USER = {"sub": "u-1", "tenant_id": "t-1", "email": "e@x.test", "role": "manager"}


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(journeys.router)
    app.dependency_overrides[require_auth] = lambda: _USER
    return TestClient(app, raise_server_exceptions=False)


def test_get_master_catalog_passes_tenant_and_app_and_returns_shape(monkeypatch):
    seen = {}

    async def fake(tenant_id, app_id, include_retired=False):
        seen["args"] = (tenant_id, app_id)
        seen["include_retired"] = include_retired
        return {"questions": [{"question_id": "q_1", "name": "Q"}],
                "summary": {"question_count": 1}}

    monkeypatch.setattr(catalog_store, "build_app_master_catalog", fake)
    r = _client().get("/api/v1/qec/apps/app-9/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["app_id"] == "app-9"
    assert body["questions"][0]["question_id"] == "q_1"
    assert seen["args"] == ("t-1", "app-9")     # tenant from auth, app from path
    # M2.3 — THE DEFAULT IS THE ACTIVE CATALOGUE. Pinned here rather than left
    # implicit: this route feeds planning, and a default that silently flipped to
    # the audit view would hand a client questions the application has stopped
    # asking, with nothing in the response shape to reveal it.
    assert seen["include_retired"] is False


def test_get_master_catalog_forwards_the_audit_view_flag(monkeypatch):
    """M2.3 — ``include_retired=true`` asks for the history, not the plan."""
    seen = {}

    async def fake(tenant_id, app_id, include_retired=False):
        seen["include_retired"] = include_retired
        return {"questions": [], "summary": {"question_count": 0}}

    monkeypatch.setattr(catalog_store, "build_app_master_catalog", fake)
    r = _client().get("/api/v1/qec/apps/app-9/catalog?include_retired=true")
    assert r.status_code == 200
    assert seen["include_retired"] is True


def test_get_retired_questions_route(monkeypatch):
    """M2.3 — the audit record of what the application stopped asking."""
    seen = {}

    async def fake(tenant_id, app_id):
        seen["args"] = (tenant_id, app_id)
        return [{"question_id": "q_gone", "name": "Primary beneficiary",
                 "lifecycle": "retired", "retired_at": "2026-08-19T00:00:00+00:00",
                 "retired_in_crawl": "crawl-2"}]

    monkeypatch.setattr(catalog_store, "load_retired_questions", fake)
    r = _client().get("/api/v1/qec/apps/app-9/catalog/retired")
    assert r.status_code == 200
    body = r.json()
    assert seen["args"] == ("t-1", "app-9")
    assert body["app_id"] == "app-9" and body["count"] == 1
    entry = body["retired"][0]
    assert entry["question_id"] == "q_gone"
    assert entry["retired_at"] and entry["retired_in_crawl"] == "crawl-2"


def test_project_route_forwards_answers_body(monkeypatch):
    seen = {}

    async def fake(tenant_id, app_id, answers):
        seen["call"] = (tenant_id, app_id, dict(answers))
        return {"app_id": app_id, "counts": {"activated": 1}, "answered": 1}

    monkeypatch.setattr(persona_journeys, "project_app_journey", fake)
    r = _client().post("/api/v1/qec/apps/app-9/catalog/project",
                       json={"answers": {"tobacco use": "yes"}})
    assert r.status_code == 200
    assert r.json()["counts"]["activated"] == 1
    assert seen["call"] == ("t-1", "app-9", {"tobacco use": "yes"})


def test_catalog_diff_route(monkeypatch):
    async def fake(tenant_id, app_id):
        return {"from": None, "to": None, "diff": None, "reason": "need two"}

    monkeypatch.setattr(catalog_store, "diff_latest_versions", fake)
    r = _client().get("/api/v1/qec/apps/app-9/catalog/diff")
    assert r.status_code == 200
    assert r.json()["reason"] == "need two"


def test_register_persona_route(monkeypatch):
    seen = {}

    async def fake(*, tenant_id, app_id, name, answers):
        seen["call"] = (tenant_id, app_id, name, dict(answers))
        return {"persona_id": "p-abc", "name": name}

    monkeypatch.setattr(persona_journeys, "register_persona", fake)
    r = _client().post("/api/v1/qec/apps/app-9/personas",
                       json={"name": "Tobacco", "answers": {"tobacco use": "yes"}})
    assert r.status_code == 200
    assert r.json()["persona_id"] == "p-abc"
    assert seen["call"] == ("t-1", "app-9", "Tobacco", {"tobacco use": "yes"})


def test_generate_all_personas_route(monkeypatch):
    async def fake(*, tenant_id, app_id):
        return {"generated": 3, "question_count": 40, "personas": []}

    monkeypatch.setattr(persona_journeys, "generate_all_journeys", fake)
    r = _client().post("/api/v1/qec/apps/app-9/personas/generate")
    assert r.status_code == 200
    assert r.json()["generated"] == 3


def test_generate_one_persona_missing_returns_404(monkeypatch):
    async def fake(*, tenant_id, app_id, persona_id):
        raise ValueError(f"persona {persona_id} not found")

    monkeypatch.setattr(persona_journeys, "generate_persona_journey", fake)
    r = _client().post("/api/v1/qec/apps/app-9/personas/nope/journey")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_routes_require_auth():
    # No dependency override → the real require_auth rejects an unauthenticated
    # request (never 200). Proves the Depends(require_auth) guard is present.
    app = FastAPI()
    app.include_router(journeys.router)
    r = TestClient(app, raise_server_exceptions=False).get(
        "/api/v1/qec/apps/app-9/catalog")
    assert r.status_code in (401, 403)
