"""Draft recording — capture a login before the application exists (Phase 7).

Recording belongs at the start of onboarding, but a recipe is stored against an
artifact and an artifact is only minted by the first crawl. So a draft is derived
and handed back, persisting nothing, and becomes a real recipe later.

The properties that matter:
  * one pass yields BOTH the login and the environment, because the operator does
    both in one sitting;
  * a session cookie never rides along — a draft describes how to log in, it does
    not carry a login;
  * a public flow with no login is a legitimate outcome, not an error.

Pure — no runner, no DB.
"""
from app.services.test_factory import draft_recording as dr


def _snap(events, start="https://app.example.com/login", current=""):
    return {"observer_version": "v1", "start_url": start,
            "current_url": current or start, "truncated": False, "events": events}


def _fill(i, name, type_="text"):
    return {"kind": "fill", "sequence_index": i, "name": name, "id": "",
            "label": name, "type": type_, "autocomplete": "", "filled": True}


def _click(i, text):
    return {"kind": "click", "sequence_index": i, "text": text, "role": "button",
            "name": "", "id": "", "type": "submit"}


_LOGIN = [_fill(0, "member_number"), _fill(1, "password", "password"), _click(2, "Sign in")]


# ── one pass, two things ─────────────────────────────────────────────────────

def test_a_recording_yields_both_a_login_and_an_environment():
    draft = dr.derive_draft(
        observation_snapshot=_snap(_LOGIN, current="https://app.example.com/portal"),
        storage_state={"cookies": [{"name": "route", "value": "B",
                                    "domain": "app.example.com", "path": "/"}]})

    assert draft["usable"] is True
    assert draft["login"]["slot_names"] == ["member_number", "password"]
    assert draft["login"]["login_path"] == "/login"
    assert draft["login"]["home_path"] == "/portal"
    assert draft["login"]["domain"] == "example.com"
    assert draft["environment"] == {
        "base_url": "https://app.example.com",
        "cookies": [{"name": "route", "value": "B",
                     "domain": "app.example.com", "path": "/"}]}


def test_the_environment_is_where_the_browser_ENDED_UP():
    """A routed estate lands somewhere other than where you started."""
    draft = dr.derive_draft(
        observation_snapshot=_snap(_LOGIN, start="https://app.example.com/login",
                                   current="https://box-786.example.com/portal"),
        storage_state=None)
    assert draft["environment"]["base_url"] == "https://box-786.example.com"


# ── a draft must never carry a session ───────────────────────────────────────

def test_session_cookies_are_dropped_and_routing_cookies_are_kept():
    state = {"cookies": [
        {"name": "route", "value": "B", "domain": "d", "path": "/"},
        {"name": "gloo-lane", "value": "uat", "domain": "d", "path": "/"},
        {"name": "SESSIONID", "value": "secret", "domain": "d", "path": "/"},
        {"name": "auth_token", "value": "secret", "domain": "d", "path": "/"},
        {"name": "csrf", "value": "secret", "domain": "d", "path": "/"},
        {"name": "JSESSIONID", "value": "secret", "domain": "d", "path": "/"},
    ]}
    kept = [c["name"] for c in dr.routing_cookies(state)]
    assert kept == ["route", "gloo-lane"]
    assert "secret" not in repr(dr.routing_cookies(state))


def test_no_credential_value_can_reach_the_draft():
    secret = "50000005"
    events = [dict(_fill(0, "member_number"), value=secret),
              _fill(1, "password", "password"), _click(2, "Sign in")]
    draft = dr.derive_draft(observation_snapshot=_snap(events),
                            storage_state={"cookies": []})
    assert secret not in repr(draft)


# ── honest outcomes ──────────────────────────────────────────────────────────

def test_a_public_flow_records_its_environment_and_says_there_was_no_login():
    draft = dr.derive_draft(
        observation_snapshot=_snap([], current="https://app.example.com/quote"),
        storage_state={"cookies": [{"name": "route", "value": "A"}]})
    assert draft["usable"] is False
    assert draft["login"] is None
    assert draft["reason"] == "no_credential_fields_observed"
    assert draft["environment"]["base_url"] == "https://app.example.com"
    assert draft["environment"]["cookies"][0]["name"] == "route"


def test_a_login_that_was_never_submitted_is_refused():
    draft = dr.derive_draft(
        observation_snapshot=_snap([_fill(0, "member_number")]), storage_state=None)
    assert draft["usable"] is False
    assert draft["reason"] == "no_submit_control_observed"


def test_nothing_recorded_at_all_is_survivable():
    for snap in (None, {}, {"events": None}):
        draft = dr.derive_draft(observation_snapshot=snap, storage_state=None)
        assert draft["usable"] is False and draft["login"] is None


def test_a_federated_login_is_flagged_and_keys_on_the_application():
    events = [
        {"kind": "navigate", "sequence_index": 0, "url": "https://app.example.com/"},
        _click(1, "Sign in with SSO"),
        {"kind": "navigate", "sequence_index": 2, "url": "https://idp-co.com/authorize"},
        _fill(3, "username"), _fill(4, "password", "password"), _click(5, "Log in"),
        {"kind": "navigate", "sequence_index": 6, "url": "https://app.example.com/portal"},
    ]
    draft = dr.derive_draft(
        observation_snapshot=_snap(events, start="https://app.example.com/",
                                   current="https://app.example.com/portal"),
        storage_state=None)
    assert draft["usable"] is True
    assert draft["login"]["federated"] is True
    assert draft["login"]["domain"] == "example.com"      # not idp-co.com


def test_the_draft_carries_the_reuse_key_so_it_can_be_matched_later():
    draft = dr.derive_draft(observation_snapshot=_snap(_LOGIN), storage_state=None)
    assert draft["login"]["login_type_key"].startswith("lt_")
