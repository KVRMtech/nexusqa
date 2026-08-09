"""U2 — /internal/perceive-controls endpoint: HMAC + vision-flag gate + wiring.
complete_vision is faked; HMAC is bypassed via monkeypatch."""
from __future__ import annotations

import types

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, *, flag: bool, vision_text: str, sig_ok: bool = True):
    from app.clients import platform_api
    from app.routers import internal

    # phase1_settings is a pydantic model (can't setattr a method) — replace the
    # module-level reference the endpoint reads instead.
    monkeypatch.setattr(internal, "phase1_settings",
                        types.SimpleNamespace(verify_signature=lambda raw, sig: sig_ok))
    monkeypatch.setattr(internal, "settings",
                        types.SimpleNamespace(crawl_vision_enabled=flag))

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
               json={"tenant_id": "t", "screenshot_b64": "abc"})
    assert r.status_code == 200
    body = r.json()
    assert body["controls"][0]["label"] == "Pay"
    assert body["displayed_values"] == [{"label": "Total", "text": "$5"}]


def test_perceive_endpoint_flag_off_returns_empty(monkeypatch):
    c = _client(monkeypatch, flag=False, vision_text="{}")
    r = c.post("/internal/perceive-controls",
               json={"tenant_id": "t", "screenshot_b64": "abc"})
    assert r.status_code == 200
    assert r.json()["controls"] == []
    assert r.json().get("reason") == "vision disabled"


def test_perceive_endpoint_bad_signature_is_401(monkeypatch):
    c = _client(monkeypatch, flag=True, vision_text="{}", sig_ok=False)
    r = c.post("/internal/perceive-controls", json={"tenant_id": "t"})
    assert r.status_code == 401
