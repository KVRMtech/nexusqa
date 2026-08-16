"""R2 — ANONYMOUS INTERNAL API.  R3 — CROSS-TENANT INTERNAL ACCESS.

R2 ATTACK
=========
``/internal/*`` is mounted OUTSIDE ``/api/*`` on purpose (the explorer holds no
JWT), and the JWT middleware only ever guarded ``/api/*``.  So the prefix had no
boundary authentication at all: it relied on each handler remembering to check
an HMAC, and the container published port 8093 on 0.0.0.0.  An attacker who can
reach the host POSTs straight at ``/internal/...``.

R3 ATTACK
=========
Every mid-crawl endpoint read ``tenant_id`` out of the request BODY.  A caller
who got past the signature could name ANY tenant and have the service query,
spend LLM budget against, and return data for it.

EXPECTED
========
R2: rejected at the middleware, before any handler runs.
R3: rejected — the tenant comes from a server-side crawl binding, and a body
that names a tenant which does not own the crawl resolves to nothing.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.clients.config import phase1_settings

ENDPOINTS = [
    "/internal/pick-advance",
    "/internal/operate-control",
    "/internal/vision-operate",
    "/internal/perceive-controls",
    "/internal/crawls/" + "a" * 32 + "/complete",
]


@pytest.fixture
def client():
    """The REAL application object, with its real middleware stack.

    No lifespan is entered, so nothing touches a database — which is itself the
    point of R2: a correct refusal happens before any handler, and therefore
    before any I/O."""
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


# ── R2: anonymous ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_r2_anonymous_internal_request_is_refused(client, endpoint):
    """No token at all — the plain drive-by."""
    r = client.post(endpoint, json={"tenant_id": "victim"})
    assert r.status_code == 401, f"{endpoint} answered {r.status_code} anonymously"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_r2_invalid_fleet_token_is_refused(client, endpoint):
    r = client.post(endpoint, json={"tenant_id": "victim"},
                    headers={"X-QEC-Token": "not-the-fleet-token"})
    assert r.status_code == 401


def test_r2_a_get_on_the_internal_prefix_is_refused(client):
    """Not just POST: the whole PREFIX is authenticated, method-independently."""
    assert client.get("/internal/pick-advance").status_code == 401
    assert client.get("/internal/").status_code == 401


def test_r2_refusal_happens_before_the_handler(client, monkeypatch):
    """Prove the boundary, not the handler: make the handler explode.

    If the request ever reached ``pick_advance`` this would surface as a 500."""
    from app.routers import internal

    async def _boom(*a, **k):  # pragma: no cover — must never be called
        raise AssertionError("handler reached on an unauthenticated request")

    monkeypatch.setattr(internal, "_authenticate_internal", _boom)
    assert client.post("/internal/pick-advance", json={}).status_code == 401


def test_r2_the_metrics_and_health_endpoints_stay_public(client):
    """The gate must be surgical — it covers /internal, nothing else."""
    assert client.get("/health").status_code in (200, 503)


def test_r2_a_valid_fleet_token_passes_the_boundary(client):
    """POSITIVE half: the real explorer gets through the middleware.

    It is then refused by the SECOND factor (an unsigned body), which is the
    correct layering — 401 from the handler's signature check, not from the
    boundary."""
    r = client.post("/internal/pick-advance", json={"tenant_id": "t"},
                    headers={"X-QEC-Token": phase1_settings.explorer_token})
    assert r.status_code == 401           # signature missing, not token missing
    assert "signature" in str(r.json().get("detail", "")).lower()


def test_r2_the_rotation_overlap_token_is_accepted_at_the_boundary(client, monkeypatch):
    """T-SEC-11: mid-rotation, the previous fleet token still reaches handlers."""
    import time

    monkeypatch.setattr(phase1_settings, "explorer_token_previous", "K1-old-fleet-token")
    monkeypatch.setattr(phase1_settings, "explorer_token_previous_expires_at",
                        time.time() + 600)
    r = client.post("/internal/pick-advance", json={"tenant_id": "t"},
                    headers={"X-QEC-Token": "K1-old-fleet-token"})
    assert r.status_code == 401
    assert "signature" in str(r.json().get("detail", "")).lower()

    # …and is refused again once the overlap has closed.
    monkeypatch.setattr(phase1_settings, "explorer_token_previous_expires_at",
                        time.time() - 1)
    r2 = client.post("/internal/pick-advance", json={"tenant_id": "t"},
                     headers={"X-QEC-Token": "K1-old-fleet-token"})
    assert r2.status_code == 401
    assert "token" in str(r2.json().get("detail", "")).lower()


# ── R3: cross-tenant ───────────────────────────────────────────────────────

@pytest.fixture
def bound(monkeypatch):
    """Fake the server-side crawl binding at its DB seam.

    ``_bind_crawl`` resolves under the CLAIMED tenant's RLS scope, so "not
    owned" is modelled exactly as the database models it: nothing comes back."""
    from app.routers import internal

    state = {"owner": "tenant-victim"}

    async def fake_bind(crawl_id, claimed_tenant):
        if claimed_tenant != state["owner"]:
            return None
        return internal.CrawlBinding(
            tenant_id=state["owner"], exploration_id="exp-victim",
            app_id="app-victim", status="dispatched")

    monkeypatch.setattr(internal, "_bind_crawl", fake_bind)
    monkeypatch.setattr(
        internal, "phase1_settings",
        type("S", (), {"verify_signature": staticmethod(
            lambda raw, sig, scope="": True)})())
    return state


def _router_client():
    from fastapi import FastAPI

    from app.routers import internal

    app = FastAPI()
    app.include_router(internal.router)
    return TestClient(app, raise_server_exceptions=False)


CRAWL = "b" * 32


@pytest.mark.parametrize("endpoint,payload", [
    ("/internal/pick-advance", {"controls": [{"name": "Next", "kind": "button"}]}),
    ("/internal/operate-control", {"control": {"name": "Next"}}),
    ("/internal/vision-operate", {"control": {"name": "Next"}, "screenshot_b64": "x"}),
    ("/internal/perceive-controls", {"screenshot_b64": "x"}),
])
def test_r3_naming_another_tenant_in_the_body_reaches_nothing(bound, endpoint, payload):
    """THE body-tenant escalation, on every mid-crawl endpoint."""
    c = _router_client()
    r = c.post(endpoint, json={"crawl_id": CRAWL, "tenant_id": "tenant-attacker",
                               **payload})
    assert r.status_code == 404, f"{endpoint} honoured a foreign tenant claim"


@pytest.mark.parametrize("endpoint", [
    "/internal/pick-advance", "/internal/operate-control",
    "/internal/vision-operate", "/internal/perceive-controls",
])
def test_r3_a_body_with_no_crawl_id_identifies_nothing(bound, endpoint):
    """Without a crawl id there is nothing the server can verify ownership of."""
    c = _router_client()
    r = c.post(endpoint, json={"tenant_id": "tenant-victim"})
    assert r.status_code == 400


def test_r3_the_refusal_is_not_an_existence_oracle(bound):
    """A crawl owned by someone else and a crawl that does not exist look the same.

    A 403-vs-404 distinction here would let an attacker enumerate other tenants'
    crawl ids one guess at a time."""
    c = _router_client()
    owned_by_other = c.post("/internal/pick-advance", json={
        "crawl_id": CRAWL, "tenant_id": "tenant-attacker",
        "controls": [{"name": "Next"}]})
    nonexistent = c.post("/internal/pick-advance", json={
        "crawl_id": "c" * 32, "tenant_id": "tenant-attacker",
        "controls": [{"name": "Next"}]})
    assert owned_by_other.status_code == nonexistent.status_code == 404
    assert owned_by_other.json() == nonexistent.json()


def test_r3_the_owning_tenant_still_works(bound, monkeypatch):
    """POSITIVE half: the legitimate crawl's own tenant is served."""
    from app.services import advance_agent

    seen = {}

    async def fake_pick(*, tenant_id, controls, page_title, page_url):
        seen["tenant_id"] = tenant_id
        return type("D", (), {"index": 0, "status": "picked", "signature": "s",
                              "usage": None})()

    monkeypatch.setattr(advance_agent, "pick_advance", fake_pick)
    c = _router_client()
    r = c.post("/internal/pick-advance", json={
        "crawl_id": CRAWL, "tenant_id": "tenant-victim",
        "controls": [{"name": "Next", "kind": "button"}]})
    assert r.status_code == 200
    assert seen["tenant_id"] == "tenant-victim"


def test_r3_the_service_receives_the_bound_tenant_not_the_body_one(bound, monkeypatch):
    """The precise property: what reaches the service is the ROW's tenant.

    Even in the case where the claim happens to be true, the value handed
    downstream must come from the binding — otherwise the next refactor
    reintroduces the hole."""
    from app.services import advance_agent

    seen = {}

    async def fake_pick(*, tenant_id, controls, page_title, page_url):
        seen["tenant_id"] = tenant_id
        return type("D", (), {"index": None, "status": "none", "signature": "",
                              "usage": None})()

    monkeypatch.setattr(advance_agent, "pick_advance", fake_pick)
    import inspect

    from app.routers import internal

    src = inspect.getsource(internal.pick_advance)
    assert "binding.tenant_id" in src
    assert 'body.get("tenant_id"' not in src

    c = _router_client()
    c.post("/internal/pick-advance", json={
        "crawl_id": CRAWL, "tenant_id": "tenant-victim",
        "controls": [{"name": "Next"}]})
    assert seen["tenant_id"] == "tenant-victim"


def test_r3_every_internal_handler_routes_through_the_one_authenticator():
    """Structural: a new endpoint cannot be added with a weaker check by accident."""
    import inspect

    from app.routers import internal

    for name in ("pick_advance", "operate_control", "vision_operate",
                 "perceive_controls_endpoint"):
        src = inspect.getsource(getattr(internal, name))
        assert "_authenticate_internal" in src, f"{name} bypasses the internal gate"
    complete = inspect.getsource(internal.complete_crawl)
    assert "_bind_crawl" in complete
    assert 'scope=f"complete:{crawl_id}"' in complete
