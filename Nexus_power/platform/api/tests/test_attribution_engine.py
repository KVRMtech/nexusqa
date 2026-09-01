"""P1.4 regression — Attribution Engine v1: the deterministic blame ladder.

Doctrine pinned by these tests:
  * blame requires POSITIVE evidence (every verdict carries verbatim quotes);
  * a product limitation is NEVER classified as an application defect;
  * an application defect is claimed only on grounded evidence (5xx, a PROVEN
    oracle breaking);
  * anything unprovable is UNKNOWN or None — honest silence, no guessing.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_attribution_engine.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

_SVC = os.path.join(os.path.dirname(__file__), "..", "app", "services")

# Load failure_attribution first under the package alias the engine imports.
_pkg = types.ModuleType("svc_tf")
_pkg.__path__ = [os.path.join(_SVC, "test_factory")]
sys.modules.setdefault("svc_tf", _pkg)
_fa_spec = importlib.util.spec_from_file_location(
    "svc_tf.failure_attribution",
    os.path.join(_SVC, "test_factory", "failure_attribution.py"))
_fa = importlib.util.module_from_spec(_fa_spec)
sys.modules["svc_tf.failure_attribution"] = _fa
_fa_spec.loader.exec_module(_fa)
_ae_spec = importlib.util.spec_from_file_location(
    "svc_tf.attribution_engine",
    os.path.join(_SVC, "test_factory", "attribution_engine.py"))
ae = importlib.util.module_from_spec(_ae_spec)
sys.modules["svc_tf.attribution_engine"] = ae
_ae_spec.loader.exec_module(ae)


def _v(err, **kw):
    return ae.attribute_failure(err, **kw)


# ── Rung 1: URL-as-text oracle (the escaped client incident) ─────────────────

def test_rung1_url_text_oracle_is_confirmed_product():
    err = ("Error: Timed out 15000ms waiting for expect(locator).toBeVisible()\n"
           "Locator: getByText(/https/i).first()\nReceived: <element(s) not found>")
    v = _v(err)
    assert v["category"] == ae.CATEGORY_PRODUCT and v["tier"] == ae.TIER_CONFIRMED
    assert v["blame"] == "product" and v["evidence"]


# ── Rung 2: environment ──────────────────────────────────────────────────────

def test_rung2_unreachable_target_is_environment_not_app():
    for frag in ("net::ERR_CONNECTION_REFUSED at https://uat.example/",
                 "getaddrinfo ENOTFOUND uat.example",
                 "net::ERR_CERT_DATE_INVALID"):
        v = _v(f"page.goto: {frag}")
        assert v["category"] == ae.CATEGORY_ENVIRONMENT, frag
        assert v["tier"] == ae.TIER_CONFIRMED
        assert v["evidence"] and frag.split()[0].split(":")[0] in v["evidence"][0] or v["evidence"]


# ── Rung 3: strict-mode = our ambiguous locator ──────────────────────────────

def test_rung3_strict_mode_violation_is_product():
    err = ("Error: strict mode violation: getByRole('button', { name: 'Add' }) "
           "resolved to 6 elements")
    v = _v(err)
    assert v["category"] == ae.CATEGORY_PRODUCT and v["tier"] == ae.TIER_CONFIRMED
    assert v["cause"] == "ambiguous_locator"


# ── Rung 4: 5xx = application (candidate, evidence-quoted) ───────────────────

def test_rung4_server_error_is_application_candidate():
    err = "expect(response).toBeOK() failed: 503 Service Unavailable from /api/quote"
    v = _v(err)
    assert v["category"] == ae.CATEGORY_APPLICATION and v["tier"] == ae.TIER_CANDIDATE
    assert v["evidence"]


# ── Rung 5: auth wall = configuration ────────────────────────────────────────

def test_rung5_auth_wall_is_configuration():
    err = ("Error: expect(page).toHaveURL(/\\/portal\\/apply/) failed\n"
           'Received string: "https://app.example/login?next=%2Fportal%2Fapply"')
    v = _v(err)
    assert v["category"] == ae.CATEGORY_CONFIG
    assert v["cause"] == "auth_wall"


# ── Rung 6: best-effort text oracle after grounded oracles passed ────────────

def test_rung6_best_effort_text_oracle_with_grounded_nav_is_product_candidate():
    err = ("Error: Timed out 15000ms waiting for expect(locator).toBeVisible()\n"
           "Locator: getByText(/summary/i).first()\nReceived: <element(s) not found>")
    step = {"observed": {"navigation_grounded": True,
                         "next_url": "https://app.example/portal/apply"}}
    v = _v(err, step_def=step)
    assert v["category"] == ae.CATEGORY_PRODUCT and v["tier"] == ae.TIER_CANDIDATE
    assert v["cause"] == "best_effort_text_oracle"


def test_rung6_without_grounding_context_stays_silent():
    """Same error but NO step evidence that grounded oracles preceded it —
    no claim (could be a real missing region on the app)."""
    err = ("Error: Timed out 15000ms waiting for expect(locator).toBeVisible()\n"
           "Locator: getByText(/summary/i).first()\nReceived: <element(s) not found>")
    assert _v(err) is None


# ── Rung 7: PROVEN oracles breaking = the app signal we sell ─────────────────

def test_rung7_grounded_navigation_break_is_application_candidate():
    err = ("Error: expect(page).toHaveURL(/\\/portal\\/apply/) failed\n"
           'Expected pattern: /\\/portal\\/apply/\n'
           'Received string: "https://app.example/portal/error"')
    step = {"observed": {"navigation_grounded": True,
                         "next_url": "https://app.example/portal/apply"}}
    v = _v(err, step_def=step)
    assert v["category"] == ae.CATEGORY_APPLICATION
    assert v["cause"] == "grounded_navigation_broken"


def test_rung7_demonstrated_value_lost_is_application_candidate():
    err = ("Error: expect(locator).toHaveValue(expected) failed\n"
           "Expected string: \"Venkata\"\nReceived string: \"\"")
    v = _v(err, step_def={"provenance": "demonstrated", "observed": {}})
    assert v["category"] == ae.CATEGORY_APPLICATION
    assert v["cause"] == "demonstrated_value_lost"


def test_ungrounded_tohaveurl_gets_no_application_claim():
    """A toHaveURL failure WITHOUT navigation_grounded evidence must not blame
    the app (mis-attributed navs exist — never green-wash in reverse)."""
    err = "Error: expect(page).toHaveURL(/checkout/) failed"
    assert _v(err, step_def={"observed": {}}) is None


# ── Rung 8: action timeouts route to heal, honestly open ─────────────────────

def test_rung8_action_timeout_is_unknown_routed_to_heal():
    err = ("locator.click: Timeout 30000ms exceeded.\n"
           "waiting for getByRole('button', { name: 'Continue' })")
    v = _v(err)
    assert v["category"] == ae.CATEGORY_UNKNOWN
    assert v["cause"] == "action_locator_timeout"


# ── Rung 9: certification failures are quarantined but never guessed ─────────

def test_rung9_certification_unknown_is_honest_unknown():
    err = "Error: something novel the ladder has never seen"
    v = _v(err, is_certification=True)
    assert v["category"] == ae.CATEGORY_UNKNOWN
    assert v["cause"] == "failed_on_attested_baseline"
    assert "NOT painted red" in v["detail"]


def test_non_certification_unknown_returns_none():
    assert _v("Error: something novel the ladder has never seen") is None
    assert _v("") is None and _v(None) is None


# ── Back-compat: F4 consumers keep working ───────────────────────────────────

def test_back_compat_f4_keys_present_on_every_verdict():
    errs = [
        ("Locator: getByText(/https/i) expect( toBeVisible", {}),
        ("page.goto: net::ERR_CONNECTION_REFUSED", {}),
        ("strict mode violation: x resolved to 3 elements", {}),
    ]
    for err, kw in errs:
        v = _v(err, **kw)
        assert v is not None
        for key in ("attribution", "blame", "category", "tier", "cause",
                    "detail", "evidence", "engine"):
            assert key in v, key


# ── Escape registry (P1.6) sanity: entries reference real guard tests ────────

def test_escape_registry_shape():
    assert "url_as_text_oracle" in ae.ESCAPED_DEFECT_REGISTRY
    for cls, entry in ae.ESCAPED_DEFECT_REGISTRY.items():
        assert entry.get("guards") and entry.get("tests"), cls
