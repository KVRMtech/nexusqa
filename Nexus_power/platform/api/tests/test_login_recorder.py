"""Crawl-recorder core (Phase 3/5): observed login -> recipe + reuse key.

Pins that a single crawler observation deterministically yields a replayable recipe
(with optional verify-documents + a Home oracle) AND a login_type_key that MATCHES
the key computed from the same login form at onboarding-match time — the property
that makes 'record once, reuse fleet-wide' actually match. Pure — no DB.
"""
from app.services.test_factory import login_recorder as rec
from app.services.test_factory import login_fingerprint as fp

INTERPRETER_ACTIONS = {"goto", "fill", "click", "wait", "assert_home"}

OBS = {
    "domain": "usaa.com",
    "login_path": "/portal/login",
    "fields": [{"slot": "member_number", "label": "Member number", "type": "text"},
               {"slot": "password", "label": "Password", "type": "password"},
               {"slot": "pin", "label": "Security PIN", "type": "text"}],
    "submit": "Continue",
    "home": {"selector": "#dashboard"},
}


def test_observed_login_yields_a_replayable_recipe():
    out = rec.recipe_from_observed_login(OBS)
    actions = [s["action"] for s in out["steps"]]
    assert actions[0] == "goto"
    assert actions[-1] == "assert_home"          # Home oracle appended
    assert set(actions) <= INTERPRETER_ACTIONS   # only interpreter-handled actions
    assert {s["name"] for s in out["slots"]} == {"member_number", "password", "pin"}


def test_verify_documents_are_recorded_optional():
    obs = dict(OBS, verify_documents=[{"action": "click", "name": "Sign document"}])
    out = rec.recipe_from_observed_login(obs)
    vdoc = [s for s in out["steps"]
            if s.get("action") == "click" and s.get("name") == "Sign document"]
    assert vdoc and vdoc[0].get("optional") is True
    # order: the verify-doc click sits before the Home oracle
    actions = [s["action"] for s in out["steps"]]
    assert actions.index("assert_home") == len(actions) - 1


def test_recorded_key_matches_the_onboarding_match_key():
    # The KEY property: the key stamped when recording == the key computed from the
    # SAME login form when a second app is onboarded and matched.
    recorded = rec.recipe_from_observed_login(OBS)["login_type_key"]
    match_time = fp.login_type_key(
        domain="usaa.com", login_path="/portal/login",
        fields=[{"name": "member_number", "type": "text"},
                {"name": "password", "type": "password"},
                {"name": "pin", "type": "text"}],
        submit="Continue")
    assert recorded == match_time


def test_a_different_login_shape_gets_a_different_key():
    dotcom = {"domain": "usaa.com", "login_path": "/login",
              "fields": [{"slot": "email", "type": "email"},
                         {"slot": "password", "type": "password"}],
              "submit": "Log on"}
    assert (rec.recipe_from_observed_login(dotcom)["login_type_key"]
            != rec.recipe_from_observed_login(OBS)["login_type_key"])


def test_no_home_signal_leaves_a_plain_recipe():
    obs = {k: v for k, v in OBS.items() if k != "home"}
    out = rec.recipe_from_observed_login(obs)
    assert "assert_home" not in [s["action"] for s in out["steps"]]  # backward-compatible
    assert out["login_type_key"].startswith("lt_")
