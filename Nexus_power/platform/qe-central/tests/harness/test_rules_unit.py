"""QE-Central Phase-0 unit tests: the REFUSE matrix rules as data.

Pure — no database, no network, no substrate imports.  Pins:
  * every rule's ``fixture_mutation`` produces the intended broken bundle
    (and never mutates its input),
  * refusal / green-wash predicates classify canned HTTP evidence built
    from the REAL VKPower response shapes (test_factory.py /generate
    :181-199, /playwright honest-404 :541-548 + auditor gate :630-681,
    /verify readiness :4661-4667, semantic-check :3778-3801, run poll
    :2945-2963),
  * the runner's verdict classification truth table — including that a
    connection failure (chain_error) NEVER counts as a refusal.
"""
from __future__ import annotations

import copy
import json

import pytest

from app.harness import rules
from app.harness.rules import (
    BASELINE_RULE,
    BASELINE_RULE_ID,
    PII_FIELD_LABEL,
    PII_SECRET_SENTINEL,
    REFUSE_RULES,
    RULE_IDS,
    RULES_BY_ID,
    VERDICT_CHAIN_ERROR,
    VERDICT_GREEN_WASH,
    VERDICT_PASS_BASELINE,
    VERDICT_REFUSED,
    classify_verdict,
    load_fixture_bundle,
    select_rules,
)

FIXTURE = "golden_3page_form"


@pytest.fixture()
def golden() -> dict:
    return load_fixture_bundle(FIXTURE)


# ─── matrix integrity ───────────────────────────────────────────────────

class TestMatrixShape:
    def test_all_eight_rules_present_exactly(self):
        assert tuple(r.rule_id for r in REFUSE_RULES) == RULE_IDS
        assert set(RULES_BY_ID) == set(RULE_IDS)

    def test_every_rule_carries_contract_pins_and_expectation(self):
        for rule in (BASELINE_RULE, *REFUSE_RULES):
            assert rule.pins, f"{rule.rule_id} has no contract pins"
            assert rule.expected.strip(), f"{rule.rule_id} has no expected outcome"
            assert callable(rule.fixture_mutation)
            assert callable(rule.refusal_predicate)
            assert callable(rule.green_wash_predicate)

    def test_select_rules_full_matrix_includes_baseline_first(self):
        selected = select_rules(None)
        assert selected[0].rule_id == BASELINE_RULE_ID
        assert [r.rule_id for r in selected[1:]] == list(RULE_IDS)

    def test_select_rules_rejects_unknown_ids(self):
        with pytest.raises(ValueError):
            select_rules(["R9"])

    def test_fixture_loader_rejects_traversal_and_missing(self):
        with pytest.raises(ValueError):
            load_fixture_bundle("../evil")
        with pytest.raises(FileNotFoundError):
            load_fixture_bundle("no_such_fixture")


# ─── fixture mutations ──────────────────────────────────────────────────

def _all_actions(bundle: dict) -> list[dict]:
    """Every action across the canonical bundle shape (pages[].actions)."""
    return [a for page in bundle.get("pages") or [] for a in page.get("actions") or []]


def _all_screenshots(bundle: dict) -> list[dict]:
    return [s for page in bundle.get("pages") or [] for s in page.get("screenshots") or []]


class TestMutations:
    def test_golden_fixture_shape(self, golden):
        """The golden fixture IS the canonical ExplorationBundle shape
        (§2.4 item 1: 3 visits, 9 actions with full anchor/after bundles,
        1 password-signalled PII field, 3 screenshots)."""
        assert len(golden["pages"]) == 3
        assert len(_all_actions(golden)) == 9
        assert len(_all_screenshots(golden)) == 3
        assert golden["frame_count"] == 3
        # every golden action is fully grounded (anchor + after bundles)
        for action in _all_actions(golden):
            assert action["anchor"]["label"]
            assert action["after"]["outcome"]
        # exactly one PII (password-signalled) field
        password_fields = [
            label
            for page in golden["pages"]
            for label, sig in (page.get("form_snapshot_signals") or {}).items()
            if sig.get("type") == "password"
        ]
        assert password_fields == ["Password"]

    def test_mutations_never_mutate_their_input(self, golden):
        frozen = copy.deepcopy(golden)
        for rule in (BASELINE_RULE, *REFUSE_RULES):
            rule.fixture_mutation(golden)
        assert golden == frozen

    def test_r1_zero_evidence(self, golden):
        mutated = RULES_BY_ID["R1"].fixture_mutation(golden)
        assert mutated["pages"] == []
        assert _all_actions(mutated) == []
        assert _all_screenshots(mutated) == []
        assert mutated["frame_count"] == 0

    def test_r2_strips_every_grounding_bundle(self, golden):
        mutated = RULES_BY_ID["R2"].fixture_mutation(golden)
        actions = _all_actions(mutated)
        assert len(actions) == 9  # actions survive, grounding doesn't
        for action in actions:
            assert "anchor" not in action
            assert "after" not in action
            assert "url_changed" not in action

    def test_r4_removes_all_screenshot_evidence(self, golden):
        mutated = RULES_BY_ID["R4"].fixture_mutation(golden)
        assert _all_screenshots(mutated) == []
        assert mutated["frame_count"] == 0
        # every per-page screenshot field is blanked by the recursive scrub
        for page in mutated["pages"]:
            assert page.get("screenshots") in ({}, "", None, [])
        # no image payloads survive anywhere in the bundle
        assert "png_base64" not in json.dumps(mutated)

    def test_r6_plants_the_raw_secret_in_snapshot_and_action(self, golden):
        mutated = RULES_BY_ID["R6"].fixture_mutation(golden)
        first_page = mutated["pages"][0]
        assert first_page["form_snapshot"][PII_FIELD_LABEL] == PII_SECRET_SENTINEL
        # signal-flagged password → the writer redacts it WHOLE (deterministic)
        assert first_page["form_snapshot_signals"][PII_FIELD_LABEL]["type"] == "password"
        planted = first_page["actions"][-1]
        assert planted["verb"] == "type"
        assert planted["target_label"] == PII_FIELD_LABEL
        assert planted["value"] == PII_SECRET_SENTINEL
        # subaction_index is fresh — never collides within the same visit
        existing = [a["subaction_index"] for a in golden["pages"][0]["actions"]]
        assert planted["subaction_index"] == max(existing) + 1
        # the mutated bundle still satisfies EVERY schema evidence rule —
        # R6 must exercise the redaction path, not degrade into a refusal
        from app.substrate.schema import ExplorationBundle
        ExplorationBundle.model_validate(mutated)

    def test_r7_new_version_plus_broken_monotonicity(self, golden):
        mutated = RULES_BY_ID["R7"].fixture_mutation(golden)
        assert mutated["crawl_id"] == golden["crawl_id"] + "-hx"
        assert (mutated["pages"][-1]["sequence_index"]
                == mutated["pages"][0]["sequence_index"])

    def test_identity_rules_leave_the_bundle_untouched(self, golden):
        for rule_id in ("R3", "R5", "R8"):
            assert RULES_BY_ID[rule_id].fixture_mutation(golden) == golden


# ─── canned chain evidence (REAL response shapes) ───────────────────────

def _generate(generated: int, status: int = 200) -> dict:
    """POST /generate response shape (test_factory.py:198-199 + service
    generate_and_store summary keys)."""
    return {"step": "generate", "status": status, "json": {
        "success": True, "artifact_id": "art-1", "generated": generated,
        "demonstrated": generated, "combinations_active": 0,
        "no_cases_reason": "" if generated else
        "No functional E2E generated — only 1 distinct page milestone(s) "
        "detected (a flow needs at least 2).",
    }}


def _playwright_404() -> dict:
    """Honest 404 (test_factory.py:541-548)."""
    return {"step": "playwright", "status": 404, "json": {
        "detail": "no generated test cases for this artifact — run /generate first",
    }}


def _playwright_zip(suite_min: int, threshold: int = 9) -> dict:
    """Delivered zip + HONEST-10 report (test_factory.py:636-645)."""
    return {"step": "playwright", "status": 200, "json": None,
            "zip_files": ["tests/e2e.spec.ts", "vkpower-audit-report.json"],
            "audit_report": {
                "rubric": ("HONEST-10 deterministic "
                           "(playwright_auditor.score_spec) + API-policy lint"),
                "gate_mode": "block", "min_score_threshold": threshold,
                "suite_min_overall": suite_min, "scripts": {}}}


def _playwright_409(suite_min: int = 4) -> dict:
    """Auditor delivery gate block (test_factory.py:665-681)."""
    return {"step": "playwright", "status": 409, "json": {"detail": {
        "error": "auditor_gate_blocked", "suite_min_overall": suite_min,
        "threshold": 9, "blocked_scripts": {}}}}


def _verify(readiness_status: str, reachable: bool) -> dict:
    """POST /verify readiness block (test_factory.py:4661-4667)."""
    return {"step": "verify", "status": 200, "json": {
        "decision": "certified", "certification_level": "CERTIFIED-EVIDENCED",
        "readiness": {"status": readiness_status, "reachable": reachable,
                      "data_ref_steps": 0,
                      "reasons": [] if reachable else
                      ["environment unreachable: connect timeout"]}}}


def _semantic(match: str) -> dict:
    """semantic-check signal (semantic_oracle.py:57 / :148-167)."""
    return {"step": "semantic_check", "status": 200, "json": {
        "semantic_match": match, "deviation_summary": "", "confidence": 0.0}}


def _run_final(status: str, *, clean_run_version=None, stop_reason="",
               terminal_state=None) -> dict:
    """Run-status poll shape (test_factory.py:2676-2683, :2945-2963)."""
    return {"run_id": "r-1", "status": status, "auto_heal": True,
            "terminal_state": terminal_state, "heal_trace": [],
            "clean_run_version": clean_run_version, "stop_reason": stop_reason}


_WRITE_OK = {"ok": True, "refused": False, "failed": False, "kind": "ok",
             "stats": {"visits": 3, "actions": 9, "screenshots": 3,
                       "redactions": 1}}
_WRITE_REFUSED = {"ok": False, "refused": True, "failed": False,
                  "kind": "writer_refusal", "error": "actions missing anchor"}


class TestPredicates:
    def test_r1(self):
        rule = RULES_BY_ID["R1"]
        honest = {"write": _WRITE_OK, "generate": _generate(0),
                  "playwright": _playwright_404()}
        assert rule.refusal_predicate(honest) is True
        assert rule.green_wash_predicate(honest) is False
        washed = {"write": _WRITE_OK, "generate": _generate(3),
                  "playwright": _playwright_zip(10)}
        assert rule.green_wash_predicate(washed) is True
        assert rule.refusal_predicate(washed) is False

    def test_r2(self):
        rule = RULES_BY_ID["R2"]
        assert rule.refusal_predicate({"write": _WRITE_REFUSED}) is True
        assert rule.refusal_predicate(
            {"write": _WRITE_OK, "playwright": _playwright_409()}) is True
        downgraded = {"write": _WRITE_OK, "generate": _generate(3),
                      "playwright": _playwright_zip(suite_min=5)}
        assert rule.refusal_predicate(downgraded) is True
        assert rule.green_wash_predicate(downgraded) is False
        washed = {"write": _WRITE_OK, "generate": _generate(3),
                  "playwright": _playwright_zip(suite_min=10)}
        assert rule.green_wash_predicate(washed) is True
        # delivered zip with NO comparable report proves nothing either way
        opaque = {"write": _WRITE_OK,
                  "playwright": {"step": "playwright", "status": 200,
                                 "audit_report": None}}
        assert rule.refusal_predicate(opaque) is False
        assert rule.green_wash_predicate(opaque) is False

    def test_r3(self):
        rule = RULES_BY_ID["R3"]
        honest = {"write": _WRITE_OK, "verify": _verify("BLOCKED", False)}
        assert rule.refusal_predicate(honest) is True
        washed = {"write": _WRITE_OK, "verify": _verify("READY", True)}
        assert rule.green_wash_predicate(washed) is True
        degraded = {"write": _WRITE_OK, "verify": _verify("DEGRADED", True)}
        assert rule.refusal_predicate(degraded) is False
        assert rule.green_wash_predicate(degraded) is False

    def test_r4(self):
        rule = RULES_BY_ID["R4"]
        assert rule.refusal_predicate(
            {"write": _WRITE_OK, "semantic_check": _semantic("uncertain")}) is True
        assert rule.refusal_predicate({"write": _WRITE_REFUSED}) is True
        washed = {"write": _WRITE_OK, "semantic_check": _semantic("match")}
        assert rule.green_wash_predicate(washed) is True
        deviation = {"write": _WRITE_OK, "semantic_check": _semantic("deviation")}
        assert rule.refusal_predicate(deviation) is False
        assert rule.green_wash_predicate(deviation) is False

    def test_r5(self):
        rule = RULES_BY_ID["R5"]
        honest = {"write": _WRITE_OK,
                  "cross_tenant_test_cases": {"status": 404, "json": {
                      "detail": "artifact not found"}},
                  "cross_tenant_readback": {"tenant_id": "b", "page_visit_rows": 0}}
        assert rule.refusal_predicate(honest) is True
        leaked_api = {"write": _WRITE_OK,
                      "cross_tenant_test_cases": {"status": 200, "json": {
                          "items": [], "total": 0}},
                      "cross_tenant_readback": {"page_visit_rows": 0}}
        assert rule.green_wash_predicate(leaked_api) is True
        leaked_rls = {"write": _WRITE_OK,
                      "cross_tenant_test_cases": {"status": 404, "json": {}},
                      "cross_tenant_readback": {"page_visit_rows": 3}}
        assert rule.green_wash_predicate(leaked_rls) is True
        assert rule.refusal_predicate(leaked_rls) is False
        # readback failure (rows None) proves nothing — never a refusal
        unproven = {"write": _WRITE_OK,
                    "cross_tenant_test_cases": {"status": 404, "json": {}},
                    "cross_tenant_readback": {"page_visit_rows": None,
                                              "error": "connection refused"}}
        assert rule.refusal_predicate(unproven) is False
        assert rule.green_wash_predicate(unproven) is False

    def test_r6(self):
        rule = RULES_BY_ID["R6"]
        honest = {"write": _WRITE_OK,
                  "pii_readback": {"raw_secret_found": False, "found_in": [],
                                   "rows_scanned": {"page_actions": 10,
                                                    "page_visits": 3}}}
        assert rule.refusal_predicate(honest) is True
        assert rule.refusal_predicate({"write": _WRITE_REFUSED}) is True
        washed = {"write": _WRITE_OK,
                  "pii_readback": {"raw_secret_found": True,
                                   "found_in": ["page_actions"]}}
        assert rule.green_wash_predicate(washed) is True
        # unprovable scan (None) is never a refusal
        unproven = {"write": _WRITE_OK,
                    "pii_readback": {"raw_secret_found": None,
                                     "table_errors": {"page_actions": "boom"}}}
        assert rule.refusal_predicate(unproven) is False
        assert rule.green_wash_predicate(unproven) is False

    def test_r7(self):
        rule = RULES_BY_ID["R7"]
        honest = {"generate": _generate(3), "version_readback": {
            "old_version": "qec_live_v1@aold", "golden_version": "qec_live_v1@a",
            "hijack_version": "qec_live_v1@a-hx", "current_version": "qec_live_v1@a",
            "old_rows": 3, "golden_rows": 3, "hijack_rows": 0,
            "hijack_write_failed": True}}
        assert rule.refusal_predicate(honest) is True
        assert rule.green_wash_predicate(honest) is False
        hijacked = {"generate": _generate(3), "version_readback": {
            "golden_version": "qec_live_v1@a", "hijack_version": "qec_live_v1@a-hx",
            "current_version": "qec_live_v1@a-hx", "hijack_rows": 3,
            "hijack_write_failed": False}}
        assert rule.green_wash_predicate(hijacked) is True
        partial = {"generate": _generate(3), "version_readback": {
            "golden_version": "qec_live_v1@a", "hijack_version": "qec_live_v1@a-hx",
            "current_version": "qec_live_v1@a", "hijack_rows": 1,
            "hijack_write_failed": True}}
        assert rule.green_wash_predicate(partial) is True  # half-write leaked
        assert rule.refusal_predicate(partial) is False

    def test_r8(self):
        rule = RULES_BY_ID["R8"]
        honest = {"write": _WRITE_OK, "run_status_final": _run_final(
            "failed", stop_reason="environment unreachable at step 1",
            terminal_state="stopped_toward_human")}
        assert rule.refusal_predicate(honest) is True
        washed_version = {"write": _WRITE_OK, "run_status_final": _run_final(
            "failed", clean_run_version="clean_run_v1", stop_reason="x")}
        assert rule.green_wash_predicate(washed_version) is True
        assert rule.refusal_predicate(washed_version) is False
        washed_pass = {"write": _WRITE_OK,
                       "run_status_final": _run_final("passed")}
        assert rule.green_wash_predicate(washed_pass) is True
        still_running = {"write": _WRITE_OK,
                         "run_status_final": _run_final("running")}
        assert rule.refusal_predicate(still_running) is False
        unknown = {"write": _WRITE_OK,
                   "run_status_final": {"run_id": "r-1", "status": "unknown"}}
        assert rule.refusal_predicate(unknown) is False

    def test_baseline(self):
        chain = {"write": _WRITE_OK, "generate": _generate(3),
                 "playwright": _playwright_zip(10)}
        assert BASELINE_RULE.refusal_predicate(chain) is True
        broken = {"write": _WRITE_OK, "generate": _generate(0),
                  "playwright": _playwright_404()}
        assert BASELINE_RULE.refusal_predicate(broken) is False


# ─── verdict classification truth table ─────────────────────────────────

class TestClassifyVerdict:
    def test_connection_failure_is_chain_error_never_refusal(self):
        rule = RULES_BY_ID["R1"]
        chain = {"rule_id": "R1",
                 "generate": {"step": "generate", "status": None, "json": None,
                              "error": "ConnectError: [Errno 111] refused"},
                 "chain_error": "generate: factory chain unreachable/broken"}
        assert classify_verdict(rule, chain) == VERDICT_CHAIN_ERROR

    def test_proven_green_wash_wins_even_with_a_later_chain_error(self):
        rule = RULES_BY_ID["R1"]
        chain = {"write": _WRITE_OK, "generate": _generate(5),
                 "playwright": {"step": "playwright", "status": None,
                                "error": "ReadTimeout"},
                 "chain_error": "playwright: factory chain unreachable/broken"}
        assert classify_verdict(rule, chain) == VERDICT_GREEN_WASH

    def test_honest_refusal_classifies_refused_correctly(self):
        rule = RULES_BY_ID["R3"]
        chain = {"write": _WRITE_OK, "verify": _verify("BLOCKED", False)}
        assert classify_verdict(rule, chain) == VERDICT_REFUSED

    def test_baseline_maps_to_pass_baseline_not_refused(self):
        chain = {"write": _WRITE_OK, "generate": _generate(3),
                 "playwright": _playwright_zip(10)}
        assert classify_verdict(BASELINE_RULE, chain) == VERDICT_PASS_BASELINE

    def test_ambiguity_is_chain_error_never_a_quiet_pass(self):
        rule = RULES_BY_ID["R4"]
        chain = {"write": _WRITE_OK, "semantic_check": _semantic("deviation")}
        assert classify_verdict(rule, chain) == VERDICT_CHAIN_ERROR

    def test_empty_chain_is_chain_error_for_every_rule(self):
        for rule in (BASELINE_RULE, *REFUSE_RULES):
            assert classify_verdict(rule, {}) == VERDICT_CHAIN_ERROR
