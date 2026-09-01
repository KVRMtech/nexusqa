"""P3 — honest per-persona oracles: the two-layer diff classifier.

Pins the doctrine that changing the member changes the ORACLES:
  * two personas' differing value ⇒ member_derived (proven), identical ⇒
    app_constant; time-shaped difference ⇒ volatile, never member-derived;
  * a value echoing the logged-in member's own identity is member_derived from
    a SINGLE crawl (identity_echo);
  * structure differences (page forks, repeat cardinality) are their own layer;
  * the per-persona split NEVER asserts persona A's value against persona B —
    an unset member-derived value is UNVERIFIED, not a fabricated pass.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_persona_env_p3.py -q
"""
from __future__ import annotations

import os

from app.services.test_factory import persona_diff as pd

_ROUTER = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                            "test_factory.py"), encoding="utf-8").read()
_REPORT = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                            "test_factory", "evidence_report.py"), encoding="utf-8").read()


# ── value diff ───────────────────────────────────────────────────────────────

def test_differing_value_is_member_derived_identical_is_app_constant():
    a = {"header.name": "Maria Young", "page.title": "Apply", "policy.no": "POL-A"}
    b = {"header.name": "John Senior", "page.title": "Apply", "policy.no": "POL-B"}
    out = pd.diff_two_personas(a, b)
    assert out["header.name"]["class"] == pd.CLASS_MEMBER_DERIVED
    assert out["header.name"]["evidence"] == pd.EV_DIFF_PROVEN
    assert out["policy.no"]["class"] == pd.CLASS_MEMBER_DERIVED
    assert out["page.title"]["class"] == pd.CLASS_APP_CONSTANT
    assert out["page.title"]["evidence"] == pd.EV_STABLE


def test_time_shaped_difference_is_volatile_not_member_derived():
    a = {"footer.date": "Last updated 2026-07-25", "x": "same"}
    b = {"footer.date": "Last updated 2026-07-26", "x": "same"}
    out = pd.diff_two_personas(a, b)
    assert out["footer.date"]["class"] == pd.CLASS_VOLATILE
    assert out["x"]["class"] == pd.CLASS_APP_CONSTANT


def test_control_run_isolates_a_members_own_time_drift():
    a = {"balance.asof": "as of 10:00:01"}
    b = {"balance.asof": "as of 10:05:42"}
    control_a = {"balance.asof": "as of 09:59:58"}   # same persona, later run
    out = pd.diff_two_personas(a, b, control_a=control_a)
    assert out["balance.asof"]["class"] == pd.CLASS_VOLATILE


def test_key_present_for_one_persona_only_is_structural():
    out = pd.diff_two_personas({"a": "1", "only_a": "x"}, {"a": "1"})
    assert out["only_a"]["class"] == pd.CLASS_STRUCTURAL


# ── single-crawl identity echo ───────────────────────────────────────────────

def test_identity_echo_flags_member_values_from_one_crawl():
    identity = {"member@vkpowerlife.com", "member", "4429871", "Maria Young"}
    observed = {"h.name": "Welcome, Maria Young", "h.id": "Member 4429871",
                "page.title": "Dashboard"}
    out = pd.classify_single_persona(observed, identity)
    assert out["h.name"]["class"] == pd.CLASS_MEMBER_DERIVED
    assert out["h.name"]["evidence"] == pd.EV_IDENTITY_ECHO
    assert out["h.id"]["class"] == pd.CLASS_MEMBER_DERIVED
    # a value with no identity echo stays UNKNOWN (one crawl cannot prove const)
    assert out["page.title"]["class"] == pd.CLASS_UNKNOWN


# ── structure diff ───────────────────────────────────────────────────────────

def test_structure_fork_vs_cardinality_vs_identical():
    fork = pd.diff_structure(["apply", "medical"], ["apply"])
    assert fork["kind"] == "fork" and fork["journey_forks"] is True

    card = pd.diff_structure(["apply"], ["apply"],
                             {"beneficiary": 2}, {"beneficiary": 5})
    assert card["kind"] == "cardinality"
    assert card["cardinality_differences"][0]["count_a"] == 2
    assert card["cardinality_differences"][0]["count_b"] == 5

    same = pd.diff_structure(["apply"], ["apply"])
    assert same["kind"] == "identical"


# ── per-persona split (the honesty guarantee) ────────────────────────────────

def test_split_never_asserts_A_value_against_B():
    classifications = {
        "k.name": {"class": pd.CLASS_MEMBER_DERIVED},
        "k.title": {"class": pd.CLASS_APP_CONSTANT},
        "k.policy": {"class": pd.CLASS_MEMBER_DERIVED},
    }
    # persona B has an answer for k.name but NOT k.policy
    answers_b = {"k.name": {"expected_value": "John"}}
    sp = pd.per_persona_split(classifications, answers_b, is_baseline=False)
    assert sp["per_key"]["k.title"] == "PROVEN"      # app-constant
    assert sp["per_key"]["k.name"] == "PROVEN"       # B supplied it
    assert sp["per_key"]["k.policy"] == "UNVERIFIED"  # unset -> never faked
    assert sp["proven"] == 2 and sp["unverified"] == 1


def test_baseline_persona_proves_its_own_member_values():
    classifications = {"k.name": {"class": pd.CLASS_MEMBER_DERIVED}}
    sp = pd.per_persona_split(classifications, {}, is_baseline=True)
    assert sp["per_key"]["k.name"] == "PROVEN"


# ── endpoints + report wiring ────────────────────────────────────────────────

def test_endpoints_exist():
    assert "/oracles/classify" in _ROUTER
    assert "/oracles/persona-diff" in _ROUTER
    assert "/oracle-split/{environment_id}" in _ROUTER


def test_diff_writes_each_persona_value_to_its_own_answer_sheet():
    seg = _ROUTER[_ROUTER.index("async def persona_diff_endpoint"):]
    assert "set_expected_value(" in seg
    assert 'source="diff_proven"' in seg


def test_report_surfaces_the_per_persona_oracle_split():
    assert '"oracle_split"' in _REPORT
    assert "per_persona_split(" in _REPORT
