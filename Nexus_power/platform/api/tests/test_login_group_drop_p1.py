"""P1 (generator layer) — a leading LOGIN page is dropped from the URL-anchored
flow so the demonstrated test STARTS at the first post-login page and the run
supplies auth via a captured session (the auth precondition says so). The signal is
the OBSERVED password input type (form_snapshot_signals[label].type == 'password'),
so it is PRECISE: a non-login form (search-by-email, newsletter) is never dropped —
no green-wash. Loaded via importlib exactly like the navigation-grounding suite so
the app import chain is not dragged in."""
from __future__ import annotations

import importlib.util
import os
import sys
import types

# Prefer the real SDK; fall back to a minimal stub so the generator's pure logic
# loads standalone (same approach as test_generator_navigation_grounding).
try:
    from nexus_sdk.models import Precondition, ProductionTestCase, ProductionTestStep  # noqa: F401
except Exception:
    class _Base:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class Precondition(_Base):
        pass

    class ProductionTestStep(_Base):
        def __init__(self, **kw):
            self.observed = {}
            self.provenance = ""
            self.screenshot = ""
            self.data_ref = None
            super().__init__(**kw)

    class ProductionTestCase(_Base):
        pass

    _mod = types.ModuleType("nexus_sdk")
    _models = types.ModuleType("nexus_sdk.models")
    _models.Precondition = Precondition
    _models.ProductionTestStep = ProductionTestStep
    _models.ProductionTestCase = ProductionTestCase
    _mod.models = _models
    sys.modules["nexus_sdk"] = _mod
    sys.modules["nexus_sdk.models"] = _models

_GEN_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "services", "test_factory", "generator.py"
)
_spec = importlib.util.spec_from_file_location("nexus_generator_login_drop", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
sys.modules["nexus_generator_login_drop"] = gen
_spec.loader.exec_module(gen)

PV, PA = gen.PageVisitInput, gen.PageActionInput


def _pv(vid, seq, loc, path, *, signals=None, snap=None):
    return PV(page_visit_id=vid, sequence_index=seq, location=loc,
              url_host="saucedemo.com", url_path=path, url_query="",
              canonical_host="saucedemo.com", source="url_regex",
              form_snapshot=snap or {}, form_snapshot_signals=signals or {})


def _login_flow():
    visits = [
        _pv("v1", 0, "Login", "/",
            snap={"Username": "standard_user", "Password": ""},
            signals={"Username": {"type": "text"}, "Password": {"type": "password"}}),
        _pv("v2", 1, "Inventory", "/inventory.html"),
        _pv("v3", 2, "Item", "/inventory-item.html"),
    ]
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="type", target_label="Username",
           target_kind="text_field", value="standard_user"),
        PA(page_visit_id="v1", subaction_index=1, verb="click", target_label="Login",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
        PA(page_visit_id="v2", subaction_index=0, verb="click", target_label="Sauce Labs Backpack",
           target_kind="link", value=None, after_outcome="navigation", navigated=True),
    ]
    return visits, actions


def test_leading_login_group_is_dropped_entry_is_post_login():
    visits, actions = _login_flow()
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions)
    assert res.test_cases
    tc = res.test_cases[0]
    entry = tc.steps[0]
    entry_txt = ((entry.observed or {}).get("url", "") + " " + (entry.action or "")).lower()
    assert "inventory" in entry_txt          # starts post-login…
    assert entry_txt.strip().rstrip("/").endswith("inventory.html") or "inventory" in entry_txt
    # …and the credential replay is gone entirely (no username/password step)
    joined = " ".join((s.action or "") for s in tc.steps).lower()
    assert "password" not in joined
    assert not any((s.observed or {}).get("value") == "standard_user" for s in tc.steps)


def test_dropped_login_adds_the_auth_precondition():
    visits, actions = _login_flow()
    tc = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions).test_cases[0]
    assert any(
        "authentic" in (p.description or "").lower()
        for p in tc.preconditions
    ), "expected the 'apply an authentication profile' precondition"


def test_non_login_search_by_email_is_NOT_dropped():
    # A search-by-email form: field type 'email', NOT 'password' → must never be
    # misclassified as login (this is the green-wash the run-compile approach hit).
    visits = [
        _pv("v1", 0, "Search", "/search",
            snap={"Search by email": ""},
            signals={"Search by email": {"type": "email"}}),
        _pv("v2", 1, "Results", "/results"),
    ]
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="type",
           target_label="Search by email", target_kind="text_field", value="a@b.com"),
        PA(page_visit_id="v1", subaction_index=1, verb="click", target_label="Search",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
    ]
    tc = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions).test_cases[0]
    # entry stays at /search — the flow is intact, the fill + assertions survive.
    entry_txt = ((tc.steps[0].observed or {}).get("url", "") + " " + (tc.steps[0].action or "")).lower()
    assert "/search" in entry_txt
    assert any((s.observed or {}).get("value") == "a@b.com" for s in tc.steps)


def _no_auth_precondition(tc):
    return not any("authentic" in (p.description or "").lower() for p in tc.preconditions)


def test_signup_registration_is_NOT_dropped():
    # Password + Confirm Password + a 'Create account' commit — a signup, NOT a login.
    # The create-account flow is the test's subject; it must survive (no green-wash).
    visits = [
        _pv("v1", 0, "Register", "/register",
            snap={"Email": "", "Password": "", "Confirm Password": ""},
            signals={"Email": {"type": "email"}, "Password": {"type": "password"},
                     "Confirm Password": {"type": "password"}}),
        _pv("v2", 1, "Welcome", "/welcome"),
        _pv("v3", 2, "Dashboard", "/dashboard"),
    ]
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="type", target_label="Email",
           target_kind="text_field", value="a@b.com"),
        PA(page_visit_id="v1", subaction_index=1, verb="click", target_label="Create account",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
        PA(page_visit_id="v2", subaction_index=0, verb="click", target_label="Get started",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
    ]
    tc = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions).test_cases[0]
    entry_txt = ((tc.steps[0].observed or {}).get("url", "") + " " + (tc.steps[0].action or "")).lower()
    assert "/register" in entry_txt                          # NOT dropped
    assert any((s.observed or {}).get("value") == "a@b.com" for s in tc.steps)
    assert _no_auth_precondition(tc)                          # no spurious auth precondition


def test_ssn_masked_field_wizard_is_NOT_dropped():
    # Insurance beachhead: an SSN rendered as <input type=password> is a MASKED field,
    # not a login. The wizard's opening page + SSN step must survive.
    visits = [
        _pv("v1", 0, "Apply", "/apply",
            snap={"Social Security Number": ""},
            signals={"Social Security Number": {"type": "password"}}),
        _pv("v2", 1, "Coverage", "/apply/coverage"),
        _pv("v3", 2, "Review", "/apply/review"),
    ]
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="type",
           target_label="Social Security Number", target_kind="text_field", value="123456789"),
        PA(page_visit_id="v1", subaction_index=1, verb="click", target_label="Continue",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
        PA(page_visit_id="v2", subaction_index=0, verb="click", target_label="Next",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
    ]
    tc = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions).test_cases[0]
    entry_txt = ((tc.steps[0].observed or {}).get("url", "") + " " + (tc.steps[0].action or "")).lower()
    assert "/apply" in entry_txt                              # NOT dropped
    assert _no_auth_precondition(tc)


def test_change_password_page_is_NOT_dropped():
    # New Password + 'Update password' — a change-password flow, not a sign-in.
    visits = [
        _pv("v1", 0, "Reset", "/account/password",
            snap={"New Password": "", "Confirm New Password": ""},
            signals={"New Password": {"type": "password"}, "Confirm New Password": {"type": "password"}}),
        _pv("v2", 1, "Settings", "/account/settings"),
        _pv("v3", 2, "Home", "/home"),
    ]
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="type", target_label="New Password",
           target_kind="text_field", value="x"),
        PA(page_visit_id="v1", subaction_index=1, verb="click", target_label="Update password",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
        PA(page_visit_id="v2", subaction_index=0, verb="click", target_label="Done",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
    ]
    tc = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions).test_cases[0]
    entry_txt = ((tc.steps[0].observed or {}).get("url", "") + " " + (tc.steps[0].action or "")).lower()
    assert "/account/password" in entry_txt                   # NOT dropped
    assert _no_auth_precondition(tc)


def test_set_new_password_with_signin_button_is_NOT_dropped():
    # Round-3 finding: a set-new-password / reset-confirmation page whose commit reads
    # 'Sign in' (post-reset auto-login) has TWO password fields (New + Confirm), so the
    # exactly-one-password rule keeps it — its create-a-password flow is the subject.
    visits = [
        _pv("v1", 0, "Set password", "/reset/set",
            snap={"New Password": "", "Confirm Password": ""},
            signals={"New Password": {"type": "password"}, "Confirm Password": {"type": "password"}}),
        _pv("v2", 1, "Confirmed", "/reset/done"),
        _pv("v3", 2, "Dashboard", "/dashboard"),
    ]
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="type", target_label="New Password",
           target_kind="text_field", value="x"),
        PA(page_visit_id="v1", subaction_index=1, verb="click", target_label="Sign in",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
        PA(page_visit_id="v2", subaction_index=0, verb="click", target_label="Go to dashboard",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
    ]
    tc = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions).test_cases[0]
    entry_txt = ((tc.steps[0].observed or {}).get("url", "") + " " + (tc.steps[0].action or "")).lower()
    assert "/reset/set" in entry_txt                          # NOT dropped
    assert _no_auth_precondition(tc)


def test_combined_auth_signup_with_signin_affordance_is_NOT_dropped():
    # Round-3 finding: a combined login/signup page where the recording captured a
    # 'Sign in' tab/link click IN ADDITION to 'Create account'. The signup form has TWO
    # passwords, so exactly-one-password keeps it — the any('Sign in') disjunction can
    # no longer flip a two-password page to 'login'.
    visits = [
        _pv("v1", 0, "Auth", "/auth",
            snap={"Email": "", "Password": "", "Confirm Password": ""},
            signals={"Email": {"type": "email"}, "Password": {"type": "password"},
                     "Confirm Password": {"type": "password"}}),
        _pv("v2", 1, "Verify email", "/verify"),
        _pv("v3", 2, "Dashboard", "/dashboard"),
    ]
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="type", target_label="Email",
           target_kind="text_field", value="a@b.com"),
        PA(page_visit_id="v1", subaction_index=1, verb="click", target_label="Sign in",
           target_kind="link", value=None),   # the incidental 'Already have an account? Sign in'
        PA(page_visit_id="v1", subaction_index=2, verb="click", target_label="Create account",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
        PA(page_visit_id="v2", subaction_index=0, verb="click", target_label="Continue",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
    ]
    tc = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions).test_cases[0]
    entry_txt = ((tc.steps[0].observed or {}).get("url", "") + " " + (tc.steps[0].action or "")).lower()
    assert "/auth" in entry_txt                               # NOT dropped
    assert any((s.observed or {}).get("value") == "a@b.com" for s in tc.steps)
    assert _no_auth_precondition(tc)


def test_login_kept_when_too_few_milestones_would_survive():
    # login + dashboard = 2 groups; dropping login leaves 1 (not a valid E2E), so the
    # login stays rather than fabricating a one-page test.
    visits = [
        _pv("v1", 0, "Login", "/",
            snap={"Password": ""}, signals={"Password": {"type": "password"}}),
        _pv("v2", 1, "Dashboard", "/dashboard"),
    ]
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="click", target_label="Login",
           target_kind="button", value=None, after_outcome="navigation", navigated=True),
    ]
    tc = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=visits, page_actions=actions).test_cases[0]
    entry_txt = ((tc.steps[0].observed or {}).get("url", "") + " " + (tc.steps[0].action or "")).lower()
    assert entry_txt.strip().rstrip("/").endswith("saucedemo.com") or entry_txt.rstrip().endswith("/")
