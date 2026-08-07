"""Re-recording a login must REFRESH the session, never destroy the credentials.

Sessions expire on the application's own schedule, so every app eventually needs
its login re-recorded. The dangerous edge is the credential blob: ``PATCH /apps``
sets ``credentials`` wholesale, so reusing that path here would silently wipe a
stored username/password every time somebody refreshed a session. These pin the
merge — and the fail-closed behaviour when the existing blob cannot be read.

Pure — the envelope service and the app row are stubs, no DB.
"""
import json

import pytest
from fastapi import HTTPException

from app.routers import apps as apps_router


class _FakeEnvelopeBlob:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def to_bytes(self) -> bytes:
        return self.payload


class _FakeEnvelope:
    """Round-trips plaintext; ``readable=False`` simulates a KEK that cannot open
    an existing blob (a rotated/restored key)."""

    def __init__(self, *, readable: bool = True) -> None:
        self.readable = readable

    async def encrypt(self, tenant_id, plaintext, aad):
        return _FakeEnvelopeBlob(plaintext)

    async def decrypt(self, tenant_id, blob, expected_aad):
        if not self.readable:
            raise ValueError("cannot decrypt with this key")
        return blob.payload


class _FakeRow:
    def __init__(self, creds: dict | None) -> None:
        self.app_id = "app-1"
        self.creds_blob = (
            json.dumps(creds, sort_keys=True).encode("utf-8") if creds is not None else None
        )


class _FakeRequest:
    def __init__(self, envelope) -> None:
        self.app = type("A", (), {"state": type("S", (), {"envelope_service": envelope})()})()


@pytest.fixture(autouse=True)
def _stub_envelope_blob(monkeypatch):
    """`_decrypt_credentials_for_merge` imports EnvelopeBlob lazily from the SDK."""
    import sys
    import types

    mod = types.ModuleType("nexus_sdk.security.envelope")
    mod.EnvelopeBlob = type("EnvelopeBlob", (), {
        "from_bytes": staticmethod(lambda b: _FakeEnvelopeBlob(b)),
    })
    monkeypatch.setitem(sys.modules, "nexus_sdk.security.envelope", mod)
    return mod


async def _merge(row, envelope):
    return await apps_router._decrypt_credentials_for_merge(
        _FakeRequest(envelope), "t1", row,
    )


@pytest.mark.asyncio
async def test_a_username_and_password_survive_a_session_refresh():
    """The whole point: refreshing an expired session must not cost the operator
    the credentials they registered."""
    row = _FakeRow({"username": "u@x.com", "password": "pw", "session": {"cookies": ["old"]}})
    creds = await _merge(row, _FakeEnvelope())
    creds["session"] = {"cookies": ["new"]}
    assert creds["username"] == "u@x.com"
    assert creds["password"] == "pw"
    assert creds["session"] == {"cookies": ["new"]}


@pytest.mark.asyncio
async def test_an_app_with_no_credentials_yet_merges_onto_nothing():
    assert await _merge(_FakeRow(None), _FakeEnvelope()) == {}


@pytest.mark.asyncio
async def test_an_unreadable_blob_refuses_rather_than_overwriting():
    """Returning {} here would re-encrypt the app's credentials as "just the new
    session" and destroy a working login. Fail closed instead."""
    with pytest.raises(HTTPException) as exc:
        await _merge(_FakeRow({"username": "u", "password": "p"}), _FakeEnvelope(readable=False))
    assert exc.value.status_code == 503
    assert "refusing to overwrite" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_no_encryption_service_refuses_rather_than_overwriting():
    with pytest.raises(HTTPException) as exc:
        await _merge(_FakeRow({"username": "u"}), None)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_a_non_dict_blob_is_not_propagated():
    row = _FakeRow(None)
    row.creds_blob = json.dumps(["not", "a", "dict"]).encode("utf-8")
    assert await _merge(row, _FakeEnvelope()) == {}
