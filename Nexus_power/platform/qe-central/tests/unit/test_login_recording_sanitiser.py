"""A held login recording must never be able to carry a secret.

Phase 7: a login recorded on the Access page waits on the app row until the first
crawl mints an artifact. That column is JSONB and is NOT encrypted — unlike
creds_blob — so what may be stored there is an ALLOWLIST, not a denylist. Anything
unexpected is dropped by default rather than caught by name.

Pure — no DB.
"""
from app.routers.apps import _sanitised_login_recording as sanitise


def _recording(**extra):
    base = {
        "steps": [{"action": "goto", "path": "/login"},
                  {"action": "fill", "slot": "email", "label": "Email"},
                  {"action": "click", "name": "Sign in"}],
        "slots": [{"name": "email", "type": "secret"},
                  {"name": "password", "type": "secret"}],
        "login_type_key": "lt_abc123",
        "domain": "example.com",
        "login_path": "/login",
        "home_path": "/portal/dashboard",
        "federated": False,
        "slot_names": ["email", "password"],
    }
    base.update(extra)
    return base


def test_the_choreography_is_kept():
    out = sanitise(_recording())
    assert [s["action"] for s in out["steps"]] == ["goto", "fill", "click"]
    assert [s["name"] for s in out["slots"]] == ["email", "password"]
    assert out["login_type_key"] == "lt_abc123"
    assert out["domain"] == "example.com"
    assert out["home_path"] == "/portal/dashboard"


def test_a_session_can_never_be_smuggled_in():
    """The session belongs in the ENCRYPTED creds_blob, never here."""
    out = sanitise(_recording(session={"cookies": [{"name": "sl_session", "value": "LIVE"}]}))
    assert "session" not in out
    assert "LIVE" not in repr(out)


def test_a_value_on_a_step_is_stripped():
    """Even a hand-crafted payload cannot park a secret in a plaintext column."""
    out = sanitise({"steps": [
        {"action": "fill", "slot": "password", "value": "SUPERSECRET"},
        {"action": "fill", "slot": "email", "secret": "also-secret"},
        {"action": "fill", "slot": "pin", "password": "1234"},
    ]})
    blob = repr(out)
    for leaked in ("SUPERSECRET", "also-secret", "1234"):
        assert leaked not in blob
    assert [s["slot"] for s in out["steps"]] == ["password", "email", "pin"]


def test_unknown_keys_are_dropped_by_default():
    out = sanitise(_recording(credentials={"u": "p"}, storage_state={"cookies": []},
                              token="t", anything_else="x"))
    assert set(out.keys()) <= {"steps", "slots", "login_type_key", "domain",
                               "login_path", "home_path", "federated", "slot_names"}


def test_nothing_without_steps_is_stored():
    """No choreography means no recipe — an empty dict, not a partial row."""
    for raw in (None, {}, {"slots": [{"name": "email"}]}, {"steps": []},
                "not-a-dict", 42):
        assert sanitise(raw) == {}


def test_malformed_steps_are_survivable():
    out = sanitise({"steps": ["not-a-dict", None, {"action": "goto", "path": "/x"}]})
    assert out["steps"] == [{"action": "goto", "path": "/x"}]
