"""P1 — Login Recipe: the multi-step replay interpreter + verify probe.

Pins:
  * the compiled globalSetup interprets strategy:'recipe' (multi-page login)
    while leaving 'form' and 'none' untouched;
  * a recipe step that will not replay is RECIPE DRIFT (named step), never a
    fabricated session or a product-blame failure;
  * TOTP is computed in-bundle from a seed (no dependency) for a 'totp_seed'
    slot; 'fixed_code' passes through literally (the client's MFA rung);
  * the shared resolver prefers a recipe, falls back to legacy form-login.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_persona_env_p1.py -q
"""
from __future__ import annotations

import os

_COMPILER = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                              "script_factory", "compiler.py"), encoding="utf-8").read()
_ROUTER = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                            "test_factory.py"), encoding="utf-8").read()


# ── interpreter ──────────────────────────────────────────────────────────────

def test_globalsetup_interprets_recipe_and_preserves_form_and_none():
    assert "strategy !== 'form' && strategy !== 'recipe'" in _COMPILER
    assert "if (strategy === 'recipe')" in _COMPILER
    # the original single-page form path is still present
    assert "form login OK -- wrote ./vkpower.auth.json" in _COMPILER


def test_recipe_supports_multi_step_actions():
    seg = _COMPILER[_COMPILER.index("if (strategy === 'recipe')"):]
    for action in ("goto", "fill", "click", "wait"):
        assert f"action === '{action}'" in seg


def test_recipe_drift_is_named_not_a_product_failure():
    assert "recipe drift at step" in _COMPILER
    # a drift closes the browser and returns WITHOUT writing a session
    seg = _COMPILER[_COMPILER.index("recipe drift at step"):]
    assert "await browser.close(); return;" in seg
    assert "not a product" in _COMPILER.lower()


def test_missing_slot_value_skips_rather_than_fabricates():
    seg = _COMPILER[_COMPILER.index("if (strategy === 'recipe')"):]
    assert "credential env not set for slots" in seg
    assert "NOT a fabricated session" in seg or "not a fabricated" in seg.lower()


def test_totp_is_computed_in_bundle_without_a_dependency():
    assert "function __nxTotp(" in _COMPILER
    assert "createHmac('sha1'" in _COMPILER
    # only node builtins
    assert "import * as crypto from 'crypto'" in _COMPILER
    assert "otplib" not in _COMPILER and "speakeasy" not in _COMPILER


def test_fixed_code_slot_passes_through_literally():
    seg = _COMPILER[_COMPILER.index("function __nxSlotValue"):]
    assert "'totp_seed'" in seg
    assert "pass through literally" in seg


def test_secrets_are_read_from_env_not_the_bundle():
    seg = _COMPILER[_COMPILER.index("function __nxSlotValue"):]
    assert "process.env[slot.env" in seg


# ── resolver + probe ─────────────────────────────────────────────────────────

def test_shared_resolver_prefers_recipe_then_legacy_form_login():
    assert "async def _persona_auth_bundle(" in _ROUTER
    assert "persona_store.get_recipe(" in _ROUTER
    assert "persona_store.build_persona_bundle(recipe, card)" in _ROUTER
    # legacy fallback for persona-0
    assert "auth_profiles.build_form_login_bundle(card)" in _ROUTER
    # A persona with neither cannot log in, and the resolver says so instead of
    # producing a bundle that would run the suite logged out. Pinned as BEHAVIOUR
    # (the honest empty return) rather than as a comment string, which moved when
    # F4 split the resolver in two and told us nothing when it did.
    seg = _ROUTER[_ROUTER.index("async def _persona_auth_resolve("):]
    seg = seg[:seg.index("async def _persona_auth_bundle(")]
    assert '"auth_config": None' in seg
    assert "build_persona_bundle" in seg and "build_form_login_bundle" in seg
    # …and the bundle wrapper is exactly that resolver, so a probe and a run can
    # never authenticate differently.
    wrap = _ROUTER[_ROUTER.index("async def _persona_auth_bundle("):]
    assert "_persona_auth_resolve(" in wrap[:1200]


def test_verify_probe_reports_drift_distinctly_from_login_ok():
    assert '/recipes/verify' in _ROUTER
    assert '"recipe_drift": drift' in _ROUTER
    # drift note says, in contiguous fragments, that it is NOT an app failure
    assert "This is NOT " in _ROUTER and "an application failure" in _ROUTER
    # The drift parser moved OUT of the router into login_probe, where the run gate,
    # the verify probe and the environment health probe all read the same signal.
    # Three private copies of one regex is how they drifted apart in the first place.
    assert "login_probe.read_outcome(" in _ROUTER
    probe = open("app/services/test_factory/login_probe.py", encoding="utf-8").read()
    assert r"recipe drift at step (\d+) \(([^)]*)\)" in probe
    assert "_RX_DRIFT" in probe


def test_probe_stamps_verified_only_on_success():
    """F3 tightened this: the gate used to be `if ok:`, where `ok` was a substring
    match that is ALSO printed when the login steps merely replayed and when a form
    login was submitted with no assertion at all. Now only a probe that reached the
    recorded landing page may stamp — `proven`, not `ok`."""
    seg = _ROUTER[_ROUTER.index("async def verify_recipe_endpoint"):]
    assert "stamp_recipe_verified" in seg
    assert 'if v["proven"]:' in seg
    assert seg.index('if v["proven"]:') < seg.index("stamp_recipe_verified")
    # and the weaker signal must NOT be what gates it any more
    assert "if ok:" not in seg[:seg.index("stamp_recipe_verified")]
