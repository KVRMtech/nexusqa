"""U2 — /internal/perceive-controls endpoint: HMAC + vision-flag gate + wiring.
complete_vision is faked; HMAC is bypassed via monkeypatch.

M0.5 updated the internal-seam contract (T-SEC-06/T-SEC-07): the signature is
scope-bound to the crawl id, and the tenant is resolved SERVER-SIDE from that
crawl id rather than read out of the body.  The fakes below mirror both, and two
new cases pin the properties the change exists for.
"""
from __future__ import annotations

import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

TENANT = "t"
CRAWL = "a" * 32


def _client(monkeypatch, *, flag: bool, vision_text: str, sig_ok: bool = True,
            bound_tenant: str | None = TENANT):
    from app.clients import platform_api
    from app.routers import internal

    # phase1_settings is a pydantic model (can't setattr a method) — replace the
    # module-level reference the endpoint reads instead. The signature now takes
    # a `scope` keyword, so the fake must accept it.
    monkeypatch.setattr(
        internal, "phase1_settings",
        types.SimpleNamespace(verify_signature=lambda raw, sig, scope="": sig_ok))
    monkeypatch.setattr(internal, "settings",
                        types.SimpleNamespace(crawl_vision_enabled=flag))

    # The server-side crawl→tenant binding, faked at the DB seam. Returning None
    # models "this crawl is not owned by the claimed tenant".
    async def fake_bind(crawl_id, claimed_tenant):
        if bound_tenant is None or claimed_tenant != bound_tenant:
            return None
        return internal.CrawlBinding(
            tenant_id=bound_tenant, exploration_id="exp-1", app_id="app-1",
            status="dispatched")

    monkeypatch.setattr(internal, "_bind_crawl", fake_bind)

    async def fake_vision(**kw):
        return types.SimpleNamespace(ok=True, text=vision_text, detail="")

    monkeypatch.setattr(platform_api, "complete_vision", fake_vision)

    app = FastAPI()
    app.include_router(internal.router)
    return TestClient(app, raise_server_exceptions=False)


def test_perceive_endpoint_returns_controls_when_enabled(monkeypatch):
    text = ('{"controls":[{"label":"Pay","role":"button","bbox":[1,1,10,10]}],'
            '"displayed_values":[{"label":"Total","text":"$5"}]}')
    c = _client(monkeypatch, flag=True, vision_text=text)
    r = c.post("/internal/perceive-controls",
               json={"crawl_id": CRAWL, "tenant_id": TENANT, "screenshot_b64": "abc"})
    assert r.status_code == 200
    body = r.json()
    assert body["controls"][0]["label"] == "Pay"
    assert body["displayed_values"] == [{"label": "Total", "text": "$5"}]


def test_perceive_endpoint_flag_off_returns_empty(monkeypatch):
    c = _client(monkeypatch, flag=False, vision_text="{}")
    r = c.post("/internal/perceive-controls",
               json={"crawl_id": CRAWL, "tenant_id": TENANT, "screenshot_b64": "abc"})
    assert r.status_code == 200
    assert r.json()["controls"] == []
    assert r.json().get("reason") == "vision disabled"


def test_perceive_endpoint_bad_signature_is_401(monkeypatch):
    c = _client(monkeypatch, flag=True, vision_text="{}", sig_ok=False)
    r = c.post("/internal/perceive-controls",
               json={"crawl_id": CRAWL, "tenant_id": TENANT})
    assert r.status_code == 401


def test_perceive_endpoint_requires_a_crawl_id(monkeypatch):
    """T-SEC-07: a body naming only a tenant no longer identifies anything.

    The crawl id is what the server can VERIFY ownership of; without it there is
    nothing to bind the request to and it is refused before any LLM spend."""
    c = _client(monkeypatch, flag=True, vision_text="{}")
    r = c.post("/internal/perceive-controls", json={"tenant_id": TENANT})
    assert r.status_code == 400


def test_perceive_endpoint_refuses_a_crawl_owned_by_another_tenant(monkeypatch):
    """T-SEC-07: naming a different tenant in the body cannot reach their crawl.

    The binding is resolved server-side under the CLAIMED tenant's scope, so a
    mismatched claim resolves to nothing — and the refusal is a plain 404, not a
    403, so it cannot be used to enumerate which crawl ids exist elsewhere."""
    c = _client(monkeypatch, flag=True, vision_text="{}", bound_tenant="victim")
    r = c.post("/internal/perceive-controls",
               json={"crawl_id": CRAWL, "tenant_id": "attacker",
                     "screenshot_b64": "abc"})
    assert r.status_code == 404
