"""Phase 0 WS-A — exhaustive tests for the pure crawl-diagnosis classifier.

Proves: every code is produced from a representative row snapshot; the ordering
edges (a productive crawl with residual seeds reads OK, a blank one reads SEEDS_NEEDED);
login-failure is matched conservatively (an unrelated failure is NEVER labelled a login
problem); and an unrecognised row yields UNCLASSIFIED carrying the raw error verbatim
(never a fabricated reason).
"""
from __future__ import annotations

from app.services import crawl_diagnosis as cd


def _diag(status, error="", stats=None):
    return cd.diagnose(status=status, error=error, stats=stats or {})


# ── In-flight / not-yet-run ───────────────────────────────────────────────────
def test_pending_is_running_info():
    d = _diag("pending")
    assert d["code"] == cd.CODE_RUNNING and d["severity"] == cd.SEV_INFO


def test_writing_is_running():
    assert _diag("writing")["code"] == cd.CODE_RUNNING


def test_queued_and_claimed_are_info_not_failure():
    for st in ("queued", "claimed"):
        d = _diag(st)
        assert d["code"] == cd.CODE_QUEUED and d["severity"] == cd.SEV_INFO


def test_none_when_never_crawled():
    d = cd.diagnose(status="none", error="", stats={})
    assert d["code"] == cd.CODE_NONE and d["severity"] == cd.SEV_INFO


# ── Refusal / stalled ─────────────────────────────────────────────────────────
def test_refused_carries_reason_verbatim():
    d = _diag("refused", error="invalid_target_url: expected http(s)")
    assert d["code"] == cd.CODE_REFUSED
    assert d["evidence"]["error"] == "invalid_target_url: expected http(s)"
    assert "invalid_target_url" in d["human"]


def test_stalled_status():
    d = _diag("stalled", error="stalled: no completion callback within wall budget")
    assert d["code"] == cd.CODE_STALLED and d["severity"] == cd.SEV_WARN


def test_stalled_detected_from_error_prefix_even_if_status_failed():
    d = _diag("failed", error="stalled: worker crashed")
    assert d["code"] == cd.CODE_STALLED


# ── Login failure — conservative matching ─────────────────────────────────────
def test_login_failed_on_explicit_token():
    d = _diag("failed", error="authentication failed for user qa@acme")
    assert d["code"] == cd.CODE_LOGIN_FAILED and d["severity"] == cd.SEV_ACTION
    assert "credential" in d["remediation"].lower()


def test_login_failed_on_401():
    assert _diag("failed", error="server returned HTTP 401").get("code") == cd.CODE_LOGIN_FAILED


def test_login_blocked_on_completed_crawl_with_auth_failed_stop_reason():
    # LIVE: a login-required app whose scripted sign-in can't complete reports
    # status=COMPLETED with stop_reason=auth_failed and a 1-page substrate. It must
    # read as "Login blocked", NOT a confusing "nothing captured" / "needs seeds".
    d = _diag("completed", stats={
        "visits": 1, "stop_reason": "auth_failed",
        "coverage": {"forms_found": 1, "fields_needing_seed": ["Remember me"]},
    })
    assert d["code"] == cd.CODE_LOGIN_FAILED and d["severity"] == cd.SEV_ACTION
    assert d["evidence"]["stop_reason"] == "auth_failed"


def test_completed_loginless_crawl_with_stray_401_is_not_login_blocked():
    # GENERICITY AUDIT P1: a login-less public app whose crawl hit a stray 401 from a
    # sub-resource (and produced no cases) must NOT be diagnosed "Login blocked" — the
    # generic 401 token only counts on an outright FAILED crawl, not a COMPLETED one.
    d = _diag("completed", stats={"visits": 5, "generate": {"generated": 0}},
              error="GET /api/telemetry returned 401 unauthorized")
    assert d["code"] != cd.CODE_LOGIN_FAILED


def test_auth_failed_does_not_override_a_productive_crawl():
    # If a crawl still generated cases, an auth wall deeper in must not downgrade it.
    d = _diag("completed", stats={
        "visits": 9, "stop_reason": "auth_failed", "generate": {"generated": 4},
        "coverage": {"forms_found": 3},
    })
    assert d["code"] == cd.CODE_COMPLETED_OK


def test_unrelated_failure_is_NOT_login():
    # A timeout / navigation error must never be mislabelled a login problem.
    d = _diag("failed", error="Timeout 30000ms exceeded waiting for selector .foo")
    assert d["code"] == cd.CODE_FAILED
    assert d["evidence"]["error"].startswith("Timeout")


def test_generic_failed_carries_reason():
    d = _diag("failed", error="substrate write failed: disk full")
    assert d["code"] == cd.CODE_FAILED and "disk full" in d["human"]


# ── Completed — the honest grading order ──────────────────────────────────────
def test_completed_with_cases_is_ok():
    d = _diag("completed", stats={"visits": 11, "generate": {"generated": 7}})
    assert d["code"] == cd.CODE_COMPLETED_OK and d["severity"] == cd.SEV_OK
    assert "7 test cases" in d["human"]


def test_completed_one_case_singular_grammar():
    d = _diag("completed", stats={"visits": 3, "generate": {"generated": 1}})
    assert "1 test case." in d["human"]


def test_completed_with_cases_and_residual_seeds_is_still_ok_with_hint():
    # A productive crawl must NOT alarm even if deeper fields remain unseeded.
    d = _diag("completed", stats={
        "visits": 9, "generate": {"generated": 4},
        "coverage": {"fields_needing_seed": ["Payee"]},
    })
    assert d["code"] == cd.CODE_COMPLETED_OK
    assert "Payee" in d["remediation"] and d["fields"] == ["Payee"]


def test_completed_blank_studio_is_seeds_needed():
    # The flagship case: explored, zero cases, named seeds → the actionable message.
    d = _diag("completed", stats={
        "visits": 4, "generate": {"generated": 0},
        "coverage": {"fields_needing_seed": ["From Account", "Payee", "Amount"]},
    })
    assert d["code"] == cd.CODE_SEEDS_NEEDED and d["severity"] == cd.SEV_ACTION
    assert d["fields"] == ["From Account", "Payee", "Amount"]
    assert "From Account, Payee, Amount" in d["remediation"]


def test_completed_no_cases_no_seeds_carries_reason():
    d = _diag("completed", stats={
        "visits": 6, "generate": {"generated": 0, "no_cases_reason": "no grounded navigations"},
    })
    assert d["code"] == cd.CODE_NO_CASES
    assert d["human"] == "no grounded navigations"


def test_completed_zero_visits_is_empty_substrate():
    d = _diag("completed", stats={"visits": 0})
    assert d["code"] == cd.CODE_EMPTY_SUBSTRATE


def test_empty_seed_field_strings_are_ignored():
    d = _diag("completed", stats={
        "visits": 2, "generate": {"generated": 0},
        "coverage": {"fields_needing_seed": ["", "  ", "State"]},
    })
    assert d["code"] == cd.CODE_SEEDS_NEEDED and d["fields"] == ["State"]


# ── Unclassified — the honest fallback ────────────────────────────────────────
def test_unclassified_carries_raw_error_verbatim():
    d = _diag("weird_state", error="something the classifier has never seen")
    assert d["code"] == cd.CODE_UNCLASSIFIED
    assert d["evidence"]["error"] == "something the classifier has never seen"
    assert "something the classifier" in d["human"]


def test_unclassified_never_invents_a_reason_when_error_empty():
    d = _diag("bizarre", error="")
    assert d["code"] == cd.CODE_UNCLASSIFIED
    # Falls back to naming the raw status, never a made-up friendly reason.
    assert "bizarre" in d["human"]


# ── Purity / robustness ───────────────────────────────────────────────────────
def test_deterministic_same_input_same_output():
    args = dict(status="completed", error="",
                stats={"visits": 5, "generate": {"generated": 2}})
    assert cd.diagnose(**args) == cd.diagnose(**args)


def test_tolerates_non_mapping_stats_and_none():
    assert cd.diagnose(status="completed", error="", stats=None)["code"] == cd.CODE_EMPTY_SUBSTRATE
    assert cd.diagnose(status="completed", error="", stats="garbage")["code"] == cd.CODE_EMPTY_SUBSTRATE


def test_tolerates_malformed_generated_value():
    d = _diag("completed", stats={"visits": 3, "generate": {"generated": "not-a-number"}})
    # Malformed generated → treated as 0 → falls to NO_CASES, never crashes.
    assert d["code"] == cd.CODE_NO_CASES


def test_all_codes_are_reachable():
    seen = set()
    seen.add(_diag("pending")["code"])
    seen.add(_diag("queued")["code"])
    seen.add(cd.diagnose(status="none", error="", stats={})["code"])
    seen.add(_diag("refused", error="x")["code"])
    seen.add(_diag("stalled")["code"])
    seen.add(_diag("failed", error="unauthorized")["code"])
    seen.add(_diag("failed", error="boom")["code"])
    seen.add(_diag("completed", stats={"visits": 1, "generate": {"generated": 1}})["code"])
    seen.add(_diag("completed", stats={"visits": 1, "coverage": {"fields_needing_seed": ["a"]}})["code"])
    seen.add(_diag("completed", stats={"visits": 1})["code"])
    seen.add(_diag("completed", stats={"visits": 0})["code"])
    seen.add(_diag("???", error="x")["code"])
    # R2 codes
    seen.add(_diag("completed", stats={
        "visits": 5, "coverage": {
            "flow_summary": {"intent_unmet": 3},
            "field_ledger": [{"name": "x", "provenance": "intent_unmet"}]}})["code"])
    seen.add(_diag("completed", stats={
        "visits": 5, "coverage": {
            "flows": [{"completed": False, "fields_unanswered": 2}]}})["code"])
    seen.add(_diag("completed", stats={
        "visits": 5, "coverage": {
            "field_ledger": [{"name": "Plan", "provenance": "needs_input",
                              "options": ["Gold", "Silver"]}]}})["code"])
    assert seen == {
        cd.CODE_RUNNING, cd.CODE_QUEUED, cd.CODE_NONE, cd.CODE_REFUSED, cd.CODE_STALLED,
        cd.CODE_LOGIN_FAILED, cd.CODE_FAILED, cd.CODE_COMPLETED_OK, cd.CODE_SEEDS_NEEDED,
        cd.CODE_NO_CASES, cd.CODE_EMPTY_SUBSTRATE, cd.CODE_UNCLASSIFIED,
        cd.CODE_INTERACTION_BLOCKED, cd.CODE_WALK_BLOCKED_VALIDATION,
        cd.CODE_DECISION_UNRESOLVED,
    }


# ── Advance-oracle unavailable — PLATFORM fault, stated before any green ──────
def test_oracle_unavailable_journeys_win_over_completed_ok():
    """An E2E crawl whose journeys silently did not finish is the green-wash
    this product exists to prevent — the diagnosis must say so even when the
    crawl generated cases."""
    stats = {"visits": 12,
             "generate": {"generated": 9},
             "coverage": {"flow_summary": {
                 "truncation_reasons": {"oracle_unavailable": 2}}}}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_ADVANCE_ORACLE_UNAVAILABLE
    assert d["severity"] == cd.SEV_ACTION
    assert d["evidence"]["oracle_unavailable_journeys"] == 2


def test_oracle_unavailable_blames_platform_never_the_app():
    stats = {"visits": 3,
             "coverage": {"flow_summary": {
                 "truncation_reasons": {"oracle_unavailable": 1}}}}
    d = _diag("completed", stats=stats)
    text = (d["human"] + " " + d["remediation"]).lower()
    assert "platform" in text
    assert "not a problem with your application" in d["human"].lower()
    assert "re-crawl" in text


def test_no_oracle_unavailable_keeps_completed_ok():
    stats = {"visits": 12,
             "generate": {"generated": 9},
             "coverage": {"flow_summary": {
                 "truncation_reasons": {"budget_exhausted": 1}}}}
    assert _diag("completed", stats=stats)["code"] == cd.CODE_COMPLETED_OK


def test_oracle_unavailable_is_an_attention_code():
    assert cd.CODE_ADVANCE_ORACLE_UNAVAILABLE in cd.TERMINAL_ATTENTION_CODES


# ── R2 INTERACTION_BLOCKED — controls that refused to commit (R0/R1 evidence) ─


def test_interaction_blocked_when_intent_unmet_and_no_cases():
    stats = {"visits": 8, "generate": {"generated": 0}, "coverage": {
        "flow_summary": {"intent_unmet": 3},
        "field_ledger": [
            {"name": "Coverage Type", "provenance": "intent_unmet"},
            {"name": "Rider Option", "provenance": "intent_unmet"},
            {"name": "Term Length", "provenance": "intent_unmet"},
        ],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_INTERACTION_BLOCKED
    assert d["severity"] == cd.SEV_ACTION
    assert d["evidence"]["intent_unmet"] == 3
    assert d["evidence"]["blocked_controls"] == ["Coverage Type", "Rider Option", "Term Length"]
    assert "Coverage Type" in d["human"]


def test_interaction_blocked_singular_grammar():
    stats = {"visits": 4, "coverage": {
        "flow_summary": {"intent_unmet": 1},
        "field_ledger": [{"name": "Plan", "provenance": "intent_unmet"}],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_INTERACTION_BLOCKED
    assert "1 control" in d["human"]
    assert "its" in d["human"]


def test_interaction_blocked_does_not_fire_on_productive_crawl():
    stats = {"visits": 12, "generate": {"generated": 5}, "coverage": {
        "flow_summary": {"intent_unmet": 2},
        "field_ledger": [{"name": "x", "provenance": "intent_unmet"}],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_COMPLETED_OK


def test_interaction_blocked_wins_over_seeds_needed():
    stats = {"visits": 6, "coverage": {
        "flow_summary": {"intent_unmet": 1},
        "field_ledger": [{"name": "Plan", "provenance": "intent_unmet"}],
        "fields_needing_seed": ["Beneficiary"],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_INTERACTION_BLOCKED


def test_interaction_blocked_tolerates_empty_field_ledger():
    stats = {"visits": 5, "coverage": {
        "flow_summary": {"intent_unmet": 2},
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_INTERACTION_BLOCKED
    assert "unnamed controls" in d["human"]


def test_interaction_blocked_is_an_attention_code():
    assert cd.CODE_INTERACTION_BLOCKED in cd.TERMINAL_ATTENTION_CODES


# ── R2 WALK_BLOCKED_VALIDATION — journeys blocked by unfilled required fields ─


def test_walk_blocked_validation_with_truncated_unanswered_flows():
    stats = {"visits": 10, "generate": {"generated": 0}, "coverage": {
        "flows": [
            {"completed": True, "fields_unanswered": 0},
            {"completed": False, "fields_unanswered": 3},
            {"completed": False, "fields_unanswered": 1},
        ],
        "fields_needing_seed": ["SSN", "Income", "DOB", "Employer"],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_WALK_BLOCKED_VALIDATION
    assert d["severity"] == cd.SEV_ACTION
    assert d["evidence"]["validation_blocked_flows"] == 2
    assert d["evidence"]["total_unanswered"] == 4
    assert d["fields"] == ["SSN", "Income", "DOB", "Employer"]


def test_walk_blocked_singular_journey():
    stats = {"visits": 5, "coverage": {
        "flows": [{"completed": False, "fields_unanswered": 1}],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_WALK_BLOCKED_VALIDATION
    assert "1 journey" in d["human"]
    assert "1 required field" in d["human"]


def test_walk_blocked_does_not_fire_on_productive_crawl():
    stats = {"visits": 10, "generate": {"generated": 4}, "coverage": {
        "flows": [{"completed": False, "fields_unanswered": 2}],
    }}
    assert _diag("completed", stats=stats)["code"] == cd.CODE_COMPLETED_OK


def test_walk_blocked_does_not_fire_when_all_flows_completed():
    stats = {"visits": 8, "coverage": {
        "flows": [
            {"completed": True, "fields_unanswered": 0},
            {"completed": True, "fields_unanswered": 2},
        ],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] != cd.CODE_WALK_BLOCKED_VALIDATION


def test_interaction_blocked_wins_over_walk_blocked():
    stats = {"visits": 8, "coverage": {
        "flow_summary": {"intent_unmet": 2},
        "field_ledger": [{"name": "x", "provenance": "intent_unmet"}],
        "flows": [{"completed": False, "fields_unanswered": 3}],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_INTERACTION_BLOCKED


def test_walk_blocked_is_an_attention_code():
    assert cd.CODE_WALK_BLOCKED_VALIDATION in cd.TERMINAL_ATTENTION_CODES


# ── R2 DECISION_UNRESOLVED — enumerable forks with no available value ────────


def test_decision_unresolved_with_enumerable_needs_input():
    stats = {"visits": 6, "coverage": {
        "field_ledger": [
            {"name": "Coverage Level", "provenance": "needs_input",
             "options": ["Bronze", "Silver", "Gold", "Platinum"]},
            {"name": "Rider", "provenance": "needs_input",
             "options": ["Accidental Death", "Waiver of Premium"]},
        ],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_DECISION_UNRESOLVED
    assert d["severity"] == cd.SEV_ACTION
    assert d["evidence"]["unresolved_decisions"] == 2
    assert "Coverage Level" in d["human"]
    assert "Rider" in d["human"]
    assert d["fields"] == ["Coverage Level", "Rider"]


def test_decision_unresolved_singular():
    stats = {"visits": 5, "coverage": {
        "field_ledger": [
            {"name": "Plan Type", "provenance": "needs_input",
             "options": ["Term", "Whole Life"]},
        ],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_DECISION_UNRESOLVED
    assert "1 decision point" in d["human"]


def test_decision_unresolved_not_for_non_enumerable_fields():
    stats = {"visits": 5, "coverage": {
        "field_ledger": [
            {"name": "SSN", "provenance": "needs_input"},
        ],
        "fields_needing_seed": ["SSN"],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_SEEDS_NEEDED


def test_decision_unresolved_ignores_empty_options():
    stats = {"visits": 5, "coverage": {
        "field_ledger": [
            {"name": "Empty", "provenance": "needs_input", "options": []},
        ],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] != cd.CODE_DECISION_UNRESOLVED


def test_decision_unresolved_does_not_fire_on_productive_crawl():
    stats = {"visits": 10, "generate": {"generated": 3}, "coverage": {
        "field_ledger": [
            {"name": "Plan", "provenance": "needs_input",
             "options": ["A", "B"]},
        ],
    }}
    assert _diag("completed", stats=stats)["code"] == cd.CODE_COMPLETED_OK


def test_decision_unresolved_wins_over_seeds_needed():
    stats = {"visits": 6, "coverage": {
        "field_ledger": [
            {"name": "Plan", "provenance": "needs_input",
             "options": ["Term", "Whole"]},
        ],
        "fields_needing_seed": ["Plan"],
    }}
    d = _diag("completed", stats=stats)
    assert d["code"] == cd.CODE_DECISION_UNRESOLVED


def test_decision_unresolved_is_an_attention_code():
    assert cd.CODE_DECISION_UNRESOLVED in cd.TERMINAL_ATTENTION_CODES
