"""QE-Central VKPower pin suite — extension E1 (ground-truth ingest parity).

Pins the §4-E1 contract on BOTH client-writable ground-truth ingest routes
(``POST /api/v1/artifacts/{id}/ground-truth`` and
``POST /api/v1/artifacts/{id}/ground-truth/events``):

* T5 — flag ON + admin: an SSN-bearing VALUE is stored REDACTED on both
  routes (the module-scope ``_redact_value`` is exercised directly AND through
  the route wiring), and the redaction fails OPEN when the detector is
  unavailable (source-redacted value stands, never a 500).
* T6 — flag unset/false: BOTH ingest routes 403 and write nothing (default
  deployments are byte-identical: instrumented capture stays off).
* T7 — viewer role with the flag ON: BOTH ingest routes 403 and write nothing.

No DB / no LLM: the tenant-scoped session is replaced with an in-memory fake
that captures the ORM rows the routes would persist, so the assertions run on
exactly what would hit Postgres. Run from Nexus_power/platform/api:
    python -m pytest tests/test_qec_contract_gt_ingest.py -q
"""
from __future__ import annotations

import contextlib
import os
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Resolve the app package + the SDK source tree whether or not nexus_sdk is
# pip-installed (mirrors the file-path robustness of the sibling tests).
_API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SDK_DIR = os.path.abspath(os.path.join(_API_DIR, "..", "..", "sdk", "nexus-sdk"))
for _p in (_API_DIR, _SDK_DIR):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from app.routers import storyboard as sb  # noqa: E402

_FLAG = "NEXUS_GROUND_TRUTH_INGEST_ENABLED"
_SSN_RAW = "applicant ssn 123-45-6789 on file"
_SSN_REDACTED = "applicant ssn ***-**-#### on file"

_ADMIN = {"sub": "u-admin", "user_id": "u-admin", "tenant_id": "t-contract",
          "email": "admin@contract.test", "role": "admin"}
_MANAGER = {**_ADMIN, "sub": "u-mgr", "role": "manager"}
_VIEWER = {**_ADMIN, "sub": "u-view", "role": "viewer"}

_ROUTE_SIDECAR = "/api/v1/artifacts/art-e1/ground-truth"
_ROUTE_EVENTS = "/api/v1/artifacts/art-e1/ground-truth/events"


# ── In-memory session double (captures exactly what would hit Postgres) ──────


class _FakeResult:
    """Duck-typed SQLAlchemy result: scalar() + scalars().all()."""

    def __init__(self, scalar_value, scalars_list):
        self._scalar = scalar_value
        self._list = scalars_list

    def scalar(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._list)


class _FakeSession:
    """Captures added/merged ORM rows; answers the routes' lookup queries."""

    def __init__(self):
        self.added = []      # session.add(...)  (sidecar route)
        self.merged = []     # await session.merge(...)  (events route)
        self.statements = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        # Both artifact lookups (.scalar()) get a truthy id; the post-insert
        # count query (.scalars().all()) sees the rows merged so far.
        return _FakeResult("art-e1", [getattr(r, "event_id", "") for r in self.merged])

    def add(self, row):
        self.added.append(row)

    async def merge(self, row):
        self.merged.append(row)
        return row

    async def commit(self):
        self.commits += 1


def _make_client(monkeypatch: pytest.MonkeyPatch, user: dict) -> tuple[TestClient, _FakeSession]:
    fake = _FakeSession()

    @contextlib.asynccontextmanager
    async def _fake_scoped(tenant_id: str):
        assert tenant_id == user["tenant_id"], "route must scope by the JWT tenant"
        yield fake

    monkeypatch.setattr(sb, "tenant_scoped_session", _fake_scoped)
    app = FastAPI()
    app.include_router(sb.router)
    app.dependency_overrides[sb.get_current_user] = lambda: user
    return TestClient(app), fake


def _sidecar_payload(value: str) -> dict:
    return {
        "session_id": "s-e1",
        "recorder_version": "qec_test_v1",
        "events": [{
            "timestamp_ms": 5, "kind": "type", "url": "https://portal.example/apply",
            "url_host": "portal.example", "url_path": "/apply", "url_query": "",
            "target_label": "SSN", "value": value, "target_kind": "text",
            "modality": "web_cdp",
        }],
    }


def _events_payload(value: str) -> dict:
    return {
        "events": [{
            "sequence_index": 0, "timestamp_ms": 10, "kind": "type",
            "url": "https://portal.example/apply", "target_label": "SSN",
            "value": value, "target_kind": "text", "modality": "dom",
            "recorder_version": "qec_test_v1", "signals": {},
        }],
    }


# ── T5: flag ON + admin → SSN stored redacted (function + both routes) ──────


def test_t5_redact_value_is_module_scope_and_redacts_ssn():
    """The hoisted module-scope function redacts PII and passes clean text."""
    assert callable(getattr(sb, "_redact_value", None)), \
        "_redact_value must be hoisted to module scope (E1)"
    assert sb._redact_value(_SSN_RAW) == _SSN_REDACTED
    assert "123-45-6789" not in sb._redact_value(_SSN_RAW)
    assert sb._redact_value("") == ""
    assert sb._redact_value("Savings account nickname") == "Savings account nickname"


def test_t5_redact_value_fails_open_when_detector_unavailable(monkeypatch):
    """Detector import/raise failure ⇒ the source value stands, never raises."""
    broken = types.ModuleType("nexus_sdk.evidence.pii_detector")

    def _boom(*_a, **_kw):
        raise RuntimeError("detector unavailable")

    broken.detect_pii = _boom
    broken.redact = _boom
    monkeypatch.setitem(sys.modules, "nexus_sdk.evidence.pii_detector", broken)
    assert sb._redact_value(_SSN_RAW) == _SSN_RAW  # fail-open, no exception


def test_t5_events_route_stores_redacted_value(monkeypatch):
    monkeypatch.setenv(_FLAG, "1")
    client, fake = _make_client(monkeypatch, _ADMIN)
    resp = client.post(_ROUTE_EVENTS, json=_events_payload(_SSN_RAW))
    assert resp.status_code == 200, resp.text
    assert resp.json()["ingested"] == 1
    assert len(fake.merged) == 1
    stored = fake.merged[0]
    assert stored.value == _SSN_REDACTED
    assert "123-45-6789" not in (stored.value or "")
    assert stored.tenant_id == _ADMIN["tenant_id"]
    assert fake.commits >= 1


def test_t5_sidecar_route_still_redacts_after_hoist(monkeypatch):
    """Regression pin for the hoist: the sibling route's behavior is unchanged."""
    monkeypatch.setenv(_FLAG, "1")
    client, fake = _make_client(monkeypatch, _MANAGER)
    resp = client.post(_ROUTE_SIDECAR, json=_sidecar_payload(_SSN_RAW))
    assert resp.status_code == 200, resp.text
    assert resp.json()["ingested"] == 1
    assert len(fake.added) == 1
    assert fake.added[0].value == _SSN_REDACTED
    assert "123-45-6789" not in (fake.added[0].value or "")


# ── T6: flag unset/false → BOTH ingest routes 403, nothing written ───────────


@pytest.mark.parametrize("route,payload", [
    (_ROUTE_SIDECAR, _sidecar_payload("x")),
    (_ROUTE_EVENTS, _events_payload("x")),
])
def test_t6_flag_unset_both_routes_403(monkeypatch, route, payload):
    monkeypatch.delenv(_FLAG, raising=False)
    client, fake = _make_client(monkeypatch, _ADMIN)
    resp = client.post(route, json=payload)
    assert resp.status_code == 403, resp.text
    assert "disabled" in resp.json()["detail"].lower()
    assert not fake.added and not fake.merged, "gated route must write nothing"


@pytest.mark.parametrize("flag_value", ["0", "false", "off", ""])
@pytest.mark.parametrize("route,payload", [
    (_ROUTE_SIDECAR, _sidecar_payload("x")),
    (_ROUTE_EVENTS, _events_payload("x")),
])
def test_t6_flag_falsy_both_routes_403(monkeypatch, flag_value, route, payload):
    monkeypatch.setenv(_FLAG, flag_value)
    client, fake = _make_client(monkeypatch, _ADMIN)
    resp = client.post(route, json=payload)
    assert resp.status_code == 403, resp.text
    assert not fake.added and not fake.merged


# ── T7: viewer role (flag ON) → BOTH ingest routes 403, nothing written ──────


@pytest.mark.parametrize("route,payload", [
    (_ROUTE_SIDECAR, _sidecar_payload("x")),
    (_ROUTE_EVENTS, _events_payload("x")),
])
def test_t7_viewer_role_403_even_with_flag_on(monkeypatch, route, payload):
    monkeypatch.setenv(_FLAG, "1")
    client, fake = _make_client(monkeypatch, _VIEWER)
    resp = client.post(route, json=payload)
    assert resp.status_code == 403, resp.text
    assert "role" in resp.json()["detail"].lower()
    assert not fake.added and not fake.merged, "viewer must never write ground truth"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
