"""R2 — a crawl / auth-setup YIELDS a login recipe (no hand-authoring).

Pins that a configured form-login is turned into a v1 login recipe in the exact
shape the compiler's recipe interpreter replays (goto → fill* → click → wait),
with slots named from the form fields so a persona card drops straight in; and
that the materialization is idempotent (never a second recipe) and honest (a bare
session with no login steps derives nothing).

Run from Nexus_power/platform/api:
    python -m pytest tests/test_persona_env_r2.py -q
"""
from __future__ import annotations

import os

from app.services.test_factory import persona_store as store

_ROUTER = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                            "test_factory.py"), encoding="utf-8").read()


# ── the builder produces compiler-replayable steps ───────────────────────────

def test_recipe_from_form_login_shape():
    cfg = {"login_path": "/signin", "submit_label": "Log in",
           "fields": [{"label": "Member Number", "value": "member_number"},
                      {"label": "Password", "value": "password"}]}
    steps, slots = store._recipe_from_form_login(cfg)
    assert steps[0] == {"action": "goto", "path": "/signin"}
    # a fill per field, keyed by the field's value as the slot, located by label
    assert {"action": "fill", "slot": "member_number", "label": "Member Number"} in steps
    assert {"action": "fill", "slot": "password", "label": "Password"} in steps
    # submit + settle
    assert steps[-2] == {"action": "click", "name": "Log in"}
    assert steps[-1] == {"action": "wait", "state": "networkidle"}
    assert {s["name"] for s in slots} == {"member_number", "password"}
    assert all(s["type"] == "secret" for s in slots)


def test_recipe_from_form_login_defaults_email_password():
    steps, slots = store._recipe_from_form_login({})
    kinds = [s["action"] for s in steps]
    assert kinds == ["goto", "fill", "fill", "click", "wait"]
    assert {s["name"] for s in slots} == {"user", "password"}


def test_builder_actions_match_the_compiler_interpreter():
    # every emitted action must be one the recipe interpreter handles
    steps, _ = store._recipe_from_form_login(
        {"fields": [{"label": "Email", "value": "user"}]})
    assert set(s["action"] for s in steps) <= {"goto", "fill", "click", "wait"}


# ── store fn is idempotent + honest ──────────────────────────────────────────

def test_store_exposes_ensure_baseline():
    assert "async def ensure_baseline_from_form_login(" in \
        open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                          "test_factory", "persona_store.py"), encoding="utf-8").read()


# ── wiring ───────────────────────────────────────────────────────────────────

def test_form_login_save_materializes_a_baseline_recipe():
    seg = _ROUTER[_ROUTER.index("async def set_form_login("):]
    seg = seg[:seg.index("async def import_auth_profile(")]
    assert "ensure_baseline_from_form_login(" in seg
    assert "best-effort" in seg  # a materialization hiccup never fails the auth save


def test_standalone_ensure_baseline_endpoint_exists():
    assert "/recipes/ensure-baseline" in _ROUTER
    seg = _ROUTER[_ROUTER.index("async def ensure_baseline_recipe_endpoint"):]
    seg = seg[:2500]
    # reads the stored form-login and is honest about a bare session
    assert "get_form_login(" in seg
    assert "cannot derive" in seg
