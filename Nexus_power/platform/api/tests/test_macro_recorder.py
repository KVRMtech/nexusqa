"""U4 — record-once generalized to any widget/flow (recipe_from_observed_macro).
Pure — reuses the login recipe's generic sequence builder, without the login tail."""
from __future__ import annotations

from app.services.test_factory.login_recorder import (
    recipe_from_observed_login,
    recipe_from_observed_macro,
)


def test_macro_replays_the_observed_sequence_generically():
    obs = {
        "start_path": "/apply/signature",
        "name": "Sign document",
        "sequence": [
            {"action": "click", "name": "Adopt signature", "role": "button"},
            {"action": "fill", "slot": "full_name", "label": "Full name"},
            {"action": "click", "name": "Apply"},
        ],
    }
    r = recipe_from_observed_macro(obs)
    assert r["steps"][0] == {"action": "goto", "path": "/apply/signature"}
    assert [s["action"] for s in r["steps"]] == ["goto", "click", "fill", "click", "wait"]
    assert {"name": "full_name", "type": "secret"} in r["slots"]
    assert r["macro_key"].startswith("macro:")


def test_macro_key_is_stable_and_shape_sensitive():
    obs = {"start_path": "/x", "name": "m", "sequence": [{"action": "click", "name": "Go"}]}
    assert recipe_from_observed_macro(obs)["macro_key"] == recipe_from_observed_macro(obs)["macro_key"]
    obs2 = {"start_path": "/x", "name": "m", "sequence": [{"action": "click", "name": "Stop"}]}
    assert recipe_from_observed_macro(obs2)["macro_key"] != recipe_from_observed_macro(obs)["macro_key"]


def test_macro_has_no_login_tail():
    r = recipe_from_observed_macro({"start_path": "/x", "sequence": []})
    assert "login_type_key" not in r
    assert r["steps"] == [{"action": "goto", "path": "/x"},
                          {"action": "wait", "state": "networkidle"}]


def test_login_recipe_still_works_unchanged():
    r = recipe_from_observed_login({"login_path": "/login", "sequence": [
        {"action": "fill", "slot": "username", "label": "User"},
        {"action": "click", "name": "Sign in"}]})
    assert "login_type_key" in r and r["steps"][0]["action"] == "goto"
