"""The card contract is enforced by the ENDPOINT, over HTTP — not just present in a file.

F2. `test_card_contract.py` pins the rule; this pins that the rule is actually
REACHED and that nothing is written when it fires. Grepping a guard out of a source
file proves it exists, not that a request runs through it — the same class of gap
that shipped an invisible button and a stale deployed module.

Three things are proven here, and each is a distinct way the defect could survive a
"fixed" claim:

  1. a non-covering card gets 422 AND ``save_persona_credential`` is never called —
     a refusal that still writes is not a refusal;
  2. the refusal names the missing and unexpected slots, so an operator can act on it;
  3. a covering card is stored, stamped with the recipe version and login type it was
     checked against — and the response still carries slot NAMES only.

No DB, no KMS: the tenant-scoped session is a fake and the envelope is a stub that
records what it was handed. Run from Nexus_power/platform/api:
    python -m pytest tests/test_card_contract_endpoint.py -q
"""
from __future__ import annotations

import contextlib
import os
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

from sqlalchemy.sql import Select  # noqa: E402

from app.services.test_factory import persona_store  # noqa: E402


def _import_test_factory():
    """Same local-import shim the other contract suites use — the working tree has
    two pre-existing import blockers unrelated to this feature."""
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

_ARTIFACT = "art-f2"
_TENANT = "t-f2"
_PERSONA = "p-member-a"
_ENV = "uat"
_CARD = f"/api/v1/test-factory/{_ARTIFACT}/personas/{_PERSONA}/credentials/{_ENV}"
_CONTRACT = f"/api/v1/test-factory/{_ARTIFACT}/login-contract"

_ADMIN = {"sub": "u-a", "user_id": "u-a", "tenant_id": _TENANT,
          "email": "admin@f2.test", "role": "admin"}

# What the recorder produced for THIS app. Deliberately not 'member_number,
# password' — the vocabulary the old form defaulted to.
_RECIPE = {
    "recipe_id": "r-1", "version": 3, "status": "active",
    "login_type_key": "lk-abc123", "login_domain": "portal.example",
    "steps": [{"action": "goto", "path": "/signin"},
              {"action": "fill", "slot": "policy_no", "label": "Policy #"},
              {"action": "fill", "slot": "pin", "label": "PIN"},
              {"action": "click", "name": "Continue"}],
    "slots": [{"name": "policy_no", "type": "secret"}, {"name": "pin", "type": "secret"}],
}


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, artifact_row):
        self.artifact_row = artifact_row
        self.writes: list = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        if isinstance(stmt, Select):
            return _FakeResult(self.artifact_row)
        self.writes.append(stmt)
        return _FakeResult(None)

    def add(self, row):
        pass

    async def commit(self):
        self.commits += 1

    def card_writes(self) -> list:
        """Statements that touch the credential table. The router carries a
        router-level RBAC audit dependency that writes and commits on EVERY
        request, so a bare commit count says nothing about whether the card was
        stored — the table is what matters."""
        return [s for s in self.writes if "tp_persona_credentials" in str(s)]


class _StubEnvelope:
    """Records what it was asked to encrypt; returns a blob-shaped object."""

    def __init__(self):
        self.encrypted: list = []

    async def encrypt(self, tenant_id, payload, aad=b""):
        self.encrypted.append(payload)
        return types.SimpleNamespace(to_bytes=lambda: b"ciphertext-stand-in")


@pytest.fixture
def harness(monkeypatch):
    if tf is None:
        pytest.skip(f"app.routers.test_factory not importable here: {_TF_IMPORT_ERROR}")
    artifact = types.SimpleNamespace(artifact_id=_ARTIFACT, tenant_id=_TENANT, session_id="s-f2")
    fake = _FakeSession(artifact)

    @contextlib.asynccontextmanager
    async def _scoped(tenant_id: str):
        assert tenant_id == _TENANT, "the route must scope by the JWT tenant"
        yield fake

    monkeypatch.setattr(tf, "tenant_scoped_session", _scoped)

    saves: list = []
    real_save = persona_store.save_persona_credential

    async def _spy(*a, **kw):
        saves.append(kw)
        return await real_save(*a, **kw)

    monkeypatch.setattr(persona_store, "save_persona_credential", _spy)

    state = {"recipe": dict(_RECIPE)}

    async def _get_recipe(session, *, tenant_id, artifact_id, version=None):
        return state["recipe"]

    monkeypatch.setattr(persona_store, "get_recipe", _get_recipe)

    # The audit sink writes over the network in production; irrelevant here.
    async def _no_audit(**kw):
        return None

    monkeypatch.setattr(tf, "_persona_audit", _no_audit)

    app = FastAPI()
    app.include_router(tf.router)
    app.state.envelope_service = _StubEnvelope()
    app.dependency_overrides[tf.get_current_user] = lambda: _ADMIN
    return types.SimpleNamespace(client=TestClient(app), saves=saves,
                                 session=fake, state=state,
                                 envelope=app.state.envelope_service)


# ── the refusal actually runs, and nothing is written ────────────────────────

def test_a_card_that_cannot_fill_the_login_is_422_and_is_NOT_stored(harness):
    """The old form's default vocabulary against a policy_no/pin app. A refusal
    that still wrote the row would leave exactly the state we are preventing."""
    r = harness.client.put(_CARD, json={"slot_values": {"member_number": "8891234",
                                                       "password": "s3cret"}})
    assert r.status_code == 422, r.text
    assert not harness.saves, "the card must not be written when the contract refuses"
    assert not harness.envelope.encrypted, "nothing should even reach encryption"
    assert not harness.session.card_writes(), "a refused card must not reach the table"


def test_the_refusal_names_what_is_missing_and_what_does_not_belong(harness):
    r = harness.client.put(_CARD, json={"slot_values": {"policy_no": "P-1", "pincode": "1234"}})
    detail = r.json()["detail"]
    assert detail["reason"] == "slots_do_not_match_recipe"
    assert detail["required"] == ["policy_no", "pin"]
    assert detail["missing"] == ["pin"]
    assert detail["unexpected"] == ["pincode"]


def test_a_card_before_any_login_is_recorded_is_refused(harness):
    harness.state["recipe"] = None
    r = harness.client.put(_CARD, json={"slot_values": {"policy_no": "P-1", "pin": "1"}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["reason"] == "no_recipe"
    assert not harness.saves


def test_an_empty_value_is_refused_at_the_endpoint(harness):
    r = harness.client.put(_CARD, json={"slot_values": {"policy_no": "P-1", "pin": "   "}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["missing"] == ["pin"]
    assert not harness.saves


# ── a covering card is stored, and stamped with what it was checked against ──

def test_a_covering_card_is_stored_and_records_the_login_it_matches(harness):
    r = harness.client.put(_CARD, json={"slot_values": {"policy_no": "P-1", "pin": "4321"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "saved"
    assert body["slot_names"] == ["pin", "policy_no"]      # sorted, names only
    # stamped with WHICH login it was checked against — a later re-record that
    # renames a slot is then a detectable break, not a card that silently stops
    # authenticating.
    assert body["login_type_key"] == "lk-abc123"
    assert body["recipe_version"] == 3
    assert harness.saves and harness.saves[0]["login_type_key"] == "lk-abc123"
    assert harness.saves[0]["recipe_version"] == 3
    assert len(harness.session.card_writes()) == 1


def test_the_response_never_carries_a_secret_value(harness):
    r = harness.client.put(_CARD, json={"slot_values": {"policy_no": "P-1",
                                                       "pin": "SUPERSECRET"}})
    assert r.status_code == 200, r.text
    assert "SUPERSECRET" not in r.text


# ── the form the operator sees comes from the same place ─────────────────────

def test_the_login_contract_endpoint_serves_the_apps_own_fields(harness):
    r = harness.client.get(_CONTRACT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_recipe"] is True
    assert body["version"] == 3
    assert body["fields"] == [
        {"name": "policy_no", "label": "Policy #", "type": "secret"},
        {"name": "pin", "label": "PIN", "type": "secret"},
    ]


def test_the_contract_endpoint_states_the_prerequisite_when_nothing_is_recorded(harness):
    harness.state["recipe"] = None
    body = harness.client.get(_CONTRACT).json()
    assert body["has_recipe"] is False
    assert body["fields"] == []
    assert body["reason"] == "no_recipe"
    assert body["note"], "the panel renders this — it must say what to do next"


def test_the_form_and_the_refusal_cannot_drift(harness):
    """Whatever the contract endpoint offers must be exactly what the write accepts.
    If these two ever derive slots differently, the panel starts producing cards the
    API rejects — or worse, cards it accepts that cannot log in."""
    fields = harness.client.get(_CONTRACT).json()["fields"]
    r = harness.client.put(_CARD, json={"slot_values": {f["name"]: "v" for f in fields}})
    assert r.status_code == 200, r.text
