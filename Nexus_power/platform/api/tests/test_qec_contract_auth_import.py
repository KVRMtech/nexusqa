"""QE-Central VKPower pin suite — extension E3 (crawler storageState import).

Pins the §4-E3 contract on
``POST /api/v1/test-factory/{artifact_id}/playwright/auth/import``:

* T13 — flag unset (default) → 403 and save_profile never invoked: every
  existing deployment is byte-identical.
* T14 — envelope service None → 503 and NO row is written (the platform never
  stores a session in plaintext; the refusal happens before any DB work).
* T15 — with a working envelope: the route delegates to
  ``auth_profiles.save_profile`` (the ONE encrypt path), the persisted blob is
  real envelope ciphertext (plaintext absent from the bytes, AAD=artifact_id),
  and it round-trips through ``auth_profiles.get_storage_state`` — including
  the negative pin that a blob bound to another artifact decrypts to None.

No DB / no KMS: the tenant-scoped session is an in-memory fake capturing the
exact statements the route would execute, and the envelope is the REAL
``EnvelopeService`` running over an in-memory test KEK provider, so the
encrypt/decrypt path under test is the production code. Run from
Nexus_power/platform/api:
    python -m pytest tests/test_qec_contract_auth_import.py -q
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SDK_DIR = os.path.abspath(os.path.join(_API_DIR, "..", "..", "sdk", "nexus-sdk"))
for _p in (_API_DIR, _SDK_DIR):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.sql import Select  # noqa: E402

from nexus_sdk.security.envelope import EnvelopeBlob, EnvelopeService  # noqa: E402

from app.services.test_factory import auth_profiles  # noqa: E402


def _import_test_factory():
    """Import the router module; in the deployed image this succeeds directly.

    The LOCAL working tree has two PRE-EXISTING import blockers that predate
    and are unrelated to extension E3 (``routers/integrations`` declares a
    204-with-return-annotation route that FastAPI >= 0.112 rejects at import,
    and ``services/diff_and_heal/heal_slo`` exists only on the VM — the known
    repo↔VM divergence). When — and only when — the plain import fails, those
    two modules are replaced with inert placeholders so the REAL E3 route is
    still exercised locally. Neither placeholder is touched by the endpoints
    under test.
    """
    try:
        from app.routers import test_factory as tf
        return tf, None
    except Exception as first_err:  # pragma: no cover - environment dependent
        try:
            sys.modules.pop("app.routers.test_factory", None)
            import app.services.diff_and_heal as _dah
            if "app.services.diff_and_heal.heal_slo" not in sys.modules:
                _hs = types.ModuleType("app.services.diff_and_heal.heal_slo")
                sys.modules["app.services.diff_and_heal.heal_slo"] = _hs
                _dah.heal_slo = _hs
            if "app.routers.integrations" not in sys.modules:
                _ig = types.ModuleType("app.routers.integrations")
                _ig.integration_installations = None
                sys.modules["app.routers.integrations"] = _ig
            from app.routers import test_factory as tf
            return tf, None
        except Exception as second_err:
            return None, f"first: {first_err!r}; with fallback: {second_err!r}"


tf, _TF_IMPORT_ERROR = _import_test_factory()

_FLAG = "NEXUS_QEC_AUTH_IMPORT_ENABLED"
_ARTIFACT = "art-e3"
_TENANT = "t-contract"
_ROUTE = f"/api/v1/test-factory/{_ARTIFACT}/playwright/auth/import"

_ADMIN = {"sub": "u-admin", "user_id": "u-admin", "tenant_id": _TENANT,
          "email": "admin@contract.test", "role": "admin"}

_STORAGE_STATE = {
    "cookies": [{"name": "session", "value": "s3cr3t-cookie-value",
                 "domain": "portal.example", "path": "/"}],
    "origins": [],
}


# ── Real EnvelopeService over an in-memory test KEK (no KMS, real crypto) ────


class _TestKekProvider:
    """In-memory KEK: AES-256-GCM wrap of the DEK under a per-instance master
    key, AAD-bound to the tenant. Same contract as the KMS providers."""

    provider_id = "qec-test-kek"

    def __init__(self):
        self._master = AESGCM(secrets.token_bytes(32))

    async def kek_id(self, tenant_id: str) -> str:
        return f"qec-test-kek/{tenant_id}"

    async def wrap(self, tenant_id: str, dek: bytes) -> tuple[str, bytes]:
        nonce = secrets.token_bytes(12)
        return await self.kek_id(tenant_id), nonce + self._master.encrypt(
            nonce, dek, tenant_id.encode("utf-8"))

    async def unwrap(self, tenant_id: str, kek_id: str, wrapped_dek: bytes) -> bytes:
        nonce, ct = wrapped_dek[:12], wrapped_dek[12:]
        return self._master.decrypt(nonce, ct, tenant_id.encode("utf-8"))


def _make_envelope() -> EnvelopeService:
    return EnvelopeService(_TestKekProvider())


# ── In-memory session double ─────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    """SELECTs resolve the artifact row; every non-SELECT statement (the
    auth-profile upsert) is captured for byte-level inspection."""

    def __init__(self, artifact_row=None, select_row=None):
        self.artifact_row = artifact_row
        self.select_row = select_row  # row returned to get_storage_state
        self.writes = []
        self.added = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        if isinstance(stmt, Select):
            return _FakeResult(self.select_row if self.select_row is not None
                               else self.artifact_row)
        self.writes.append(stmt)
        return _FakeResult(None)

    def add(self, row):  # audit-log rows from the router's RBAC gate
        self.added.append(row)

    async def commit(self):
        self.commits += 1


def _make_client(monkeypatch: pytest.MonkeyPatch, *, envelope) -> tuple[TestClient, _FakeSession]:
    if tf is None:
        pytest.skip(f"app.routers.test_factory not importable here: {_TF_IMPORT_ERROR}")
    artifact = types.SimpleNamespace(artifact_id=_ARTIFACT, tenant_id=_TENANT, session_id="s-e3")
    fake = _FakeSession(artifact_row=artifact)

    @contextlib.asynccontextmanager
    async def _fake_scoped(tenant_id: str):
        assert tenant_id == _TENANT, "route must scope by the JWT tenant"
        yield fake

    monkeypatch.setattr(tf, "tenant_scoped_session", _fake_scoped)
    app = FastAPI()
    app.include_router(tf.router)
    app.state.envelope_service = envelope
    app.dependency_overrides[tf.get_current_user] = lambda: _ADMIN
    return TestClient(app), fake


def _upsert_blob_bytes(stmt) -> bytes:
    """Extract the encrypted blob from the captured pg_insert upsert."""
    params = stmt.compile(dialect=postgresql.dialect()).params
    return bytes(params["blob"])


# ── T13: flag unset (default) → 403, save_profile never invoked ──────────────


def test_t13_flag_unset_403_and_no_save(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    calls = []
    real_save = auth_profiles.save_profile

    async def _spy(*a, **kw):
        calls.append(kw)
        return await real_save(*a, **kw)

    monkeypatch.setattr(auth_profiles, "save_profile", _spy)
    client, fake = _make_client(monkeypatch, envelope=_make_envelope())
    resp = client.post(_ROUTE, json={"storage_state": _STORAGE_STATE})
    assert resp.status_code == 403, resp.text
    assert "disabled" in resp.json()["detail"].lower()
    assert not calls, "save_profile must not run while the flag is unset"
    assert not fake.writes, "no row may be written while the flag is unset"


@pytest.mark.parametrize("flag_value", ["0", "false", "off", ""])
def test_t13_flag_falsy_403(monkeypatch, flag_value):
    monkeypatch.setenv(_FLAG, flag_value)
    client, fake = _make_client(monkeypatch, envelope=_make_envelope())
    resp = client.post(_ROUTE, json={"storage_state": _STORAGE_STATE})
    assert resp.status_code == 403, resp.text
    assert not fake.writes


# ── T14: envelope None → 503, and NO row written ─────────────────────────────


def test_t14_envelope_none_503_and_no_row(monkeypatch):
    monkeypatch.setenv(_FLAG, "1")
    calls = []
    real_save = auth_profiles.save_profile

    async def _spy(*a, **kw):
        calls.append(kw)
        return await real_save(*a, **kw)

    monkeypatch.setattr(auth_profiles, "save_profile", _spy)
    client, fake = _make_client(monkeypatch, envelope=None)
    resp = client.post(_ROUTE, json={"storage_state": _STORAGE_STATE})
    assert resp.status_code == 503, resp.text
    assert "encryption" in resp.json()["detail"].lower()
    assert not calls, "save_profile must not run without encryption"
    assert not fake.writes, "refusal must leave zero rows (never plaintext)"


def test_t14_service_refuses_plaintext_directly():
    """Service-level pin: save_profile itself refuses when envelope is None."""
    fake = _FakeSession()
    with pytest.raises(RuntimeError):
        asyncio.run(auth_profiles.save_profile(
            fake, envelope=None, tenant_id=_TENANT, artifact_id=_ARTIFACT,
            storage_state_json=json.dumps(_STORAGE_STATE),
        ))
    assert not fake.writes


# ── T15: working envelope → encrypt path + get_storage_state round-trip ──────


def test_t15_route_delegates_to_save_profile_encrypt_path(monkeypatch):
    monkeypatch.setenv(_FLAG, "1")
    envelope = _make_envelope()
    calls = []
    real_save = auth_profiles.save_profile

    async def _spy(*a, **kw):
        calls.append((a, kw))
        return await real_save(*a, **kw)

    monkeypatch.setattr(auth_profiles, "save_profile", _spy)
    client, fake = _make_client(monkeypatch, envelope=envelope)
    resp = client.post(_ROUTE, json={"storage_state": _STORAGE_STATE, "label": "crawl c7f2"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "saved"}

    # Delegation: exactly one save_profile call, with the shared envelope and
    # the artifact-scoped identifiers (the ONE encrypt path — E3 never crypts
    # on its own).
    assert len(calls) == 1
    _, kw = calls[0]
    assert kw["envelope"] is envelope
    assert kw["tenant_id"] == _TENANT and kw["artifact_id"] == _ARTIFACT
    assert json.loads(kw["storage_state_json"]) == _STORAGE_STATE
    assert kw["label"] == "crawl c7f2"

    # The captured upsert stores REAL ciphertext: the cookie secret is absent
    # from the raw bytes, and the blob decrypts only under AAD=artifact_id.
    assert len(fake.writes) == 1 and fake.commits >= 1
    raw = _upsert_blob_bytes(fake.writes[0])
    assert b"s3cr3t-cookie-value" not in raw, "session must never be plaintext at rest"
    blob = EnvelopeBlob.from_bytes(raw)
    assert blob.aad == _ARTIFACT.encode("utf-8")
    plaintext = asyncio.run(envelope.decrypt(
        _TENANT, blob, expected_aad=_ARTIFACT.encode("utf-8")))
    assert json.loads(plaintext.decode("utf-8")) == _STORAGE_STATE


def test_t15_round_trip_via_get_storage_state():
    """save_profile → stored blob → get_storage_state returns the same JSON;
    the same blob bound to another artifact (AAD swap) yields None."""
    envelope = _make_envelope()
    state_json = json.dumps(_STORAGE_STATE)
    writer = _FakeSession()
    asyncio.run(auth_profiles.save_profile(
        writer, envelope=envelope, tenant_id=_TENANT, artifact_id=_ARTIFACT,
        storage_state_json=state_json, label="round-trip",
    ))
    assert len(writer.writes) == 1
    raw = _upsert_blob_bytes(writer.writes[0])

    row = auth_profiles.E2EAuthProfileRow(
        artifact_id=_ARTIFACT, tenant_id=_TENANT, blob=raw, label="round-trip",
    )
    reader = _FakeSession(select_row=row)
    out = asyncio.run(auth_profiles.get_storage_state(
        reader, envelope=envelope, tenant_id=_TENANT, artifact_id=_ARTIFACT))
    assert out is not None and json.loads(out) == _STORAGE_STATE

    # AAD binding: the identical blob presented for a DIFFERENT artifact must
    # fail closed (None → the run proceeds unauthenticated, never mis-keyed).
    out_other = asyncio.run(auth_profiles.get_storage_state(
        reader, envelope=envelope, tenant_id=_TENANT, artifact_id="art-other"))
    assert out_other is None


def test_t15_empty_and_oversize_sessions_rejected_422(monkeypatch):
    monkeypatch.setenv(_FLAG, "1")
    client, fake = _make_client(monkeypatch, envelope=_make_envelope())
    resp = client.post(_ROUTE, json={"storage_state": {}})
    assert resp.status_code == 422, resp.text
    assert not fake.writes

    huge = {"cookies": [{"name": "c", "value": "x" * (auth_profiles.MAX_STORAGE_STATE_BYTES + 64)}]}
    resp = client.post(_ROUTE, json={"storage_state": huge})
    assert resp.status_code == 422, resp.text
    assert "large" in resp.json()["detail"].lower()
    assert not fake.writes


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
