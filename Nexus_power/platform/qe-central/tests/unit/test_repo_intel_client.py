"""qe-central → repo-intel typed client (CODE P0.2) — shaping + fail-open."""
from __future__ import annotations

import asyncio

from app.clients import repo_intel


def test_create_connection_shapes_request(monkeypatch):
    captured: dict = {}

    async def fake(method, path, *, json_body=None, params=None):
        captured.update(method=method, path=path, json_body=json_body, params=params)
        return {"connection_id": "conn-1", "provider": "github", "project_path": "o/r"}

    monkeypatch.setattr(repo_intel, "_request", fake)
    conn = asyncio.run(repo_intel.create_connection(
        tenant_id="t", provider="github", base_url="https://github.com",
        project_path="o/r", token="tok-123", app_id="app-1",
    ))
    assert conn.connection_id == "conn-1"
    assert captured["path"].endswith("/connections") and captured["method"] == "POST"
    assert captured["json_body"]["token"] == "tok-123"
    assert captured["json_body"]["tenant_id"] == "t"


def test_get_diff_fail_safe_returns_none(monkeypatch):
    async def boom(*a, **k):
        raise repo_intel.RepoIntelError(500, "boom")

    monkeypatch.setattr(repo_intel, "_request", boom)
    out = asyncio.run(repo_intel.get_diff(
        tenant_id="t", app_id="a", connection_id="c", old_sha="1", new_sha="2"))
    assert out is None  # never a false 'no change' — caller runs full


def test_get_diff_parses_result(monkeypatch):
    async def fake(*a, **k):
        return {"app_id": "a", "changed_files": ["x.py"], "mapped_atoms": [{"k": 1}],
                "stack_supported": True, "reason": ""}

    monkeypatch.setattr(repo_intel, "_request", fake)
    out = asyncio.run(repo_intel.get_diff(
        tenant_id="t", app_id="a", connection_id="c", old_sha="1", new_sha="2"))
    assert out is not None and out.stack_supported is True and out.changed_files == ["x.py"]


def test_revoke_is_best_effort(monkeypatch):
    async def boom(*a, **k):
        raise repo_intel.RepoIntelError(0, "down")

    monkeypatch.setattr(repo_intel, "_request", boom)
    # Must not raise — a repo-intel outage never blocks a delete.
    asyncio.run(repo_intel.revoke_connection(tenant_id="t", connection_id="c"))
