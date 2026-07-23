"""R5 — Recovery Agent v1: propose-only classification of run failures.

Encodes the manual loop from the live pilot session. Doctrine under test:
propose-only (human gate on every bundle), never green-wash (unknowns stay
NEEDS_HUMAN; app defects reported, never softened), grounded strategies only.
"""
from app.services.agentic.recovery_agent import (
    CLASS_APP_DEFECT,
    CLASS_CONFIG,
    CLASS_ENVIRONMENT,
    CLASS_EXTERNAL,
    CLASS_LOCATOR,
    CLASS_PRODUCT_GAP,
    CLASS_TEST_DATA,
    CLASS_TIMING,
    CLASS_UNKNOWN,
    build_proposal,
    classify_finding,
    scan,
    scan_to_dict,
)


def test_taxonomy_mapping_covers_the_nine_classes():
    assert classify_finding({"cause": "WRONG_CONTROL_KIND"}) == CLASS_PRODUCT_GAP
    assert classify_finding({"cause": "CANVAS_NO_DOM"}) == CLASS_PRODUCT_GAP
    assert classify_finding({"cause": "REAL_REGRESSION"}) == CLASS_APP_DEFECT
    assert classify_finding({"cause": "DATA_PRECONDITION_UNMET"}) == CLASS_TEST_DATA
    assert classify_finding({"cause": "ENV_BLOCK"}) == CLASS_ENVIRONMENT
    assert classify_finding({"cause": "AUTH_PRECONDITION"}) == CLASS_CONFIG
    assert classify_finding({"cause": "LOCATOR_NOT_FOUND"}) == CLASS_LOCATOR
    assert classify_finding({"cause": "FLAKE"}) == CLASS_TIMING
    assert classify_finding(
        {"cause": "NEEDS_REVIEW",
         "network": {"kind": "external_dependency"}}) == CLASS_EXTERNAL


def test_unknown_never_guessed_into_a_bucket():
    assert classify_finding({}) == CLASS_UNKNOWN
    assert classify_finding({"cause": "SOMETHING_NEW"}) == CLASS_UNKNOWN


def test_triage_source_backstops_cause():
    assert classify_finding(
        {"cause": "NEEDS_REVIEW", "triage": {"source": "PRODUCT"}}) == CLASS_APP_DEFECT
    assert classify_finding(
        {"cause": "NEEDS_REVIEW", "triage": {"source": "ENVIRONMENT"}}) == CLASS_ENVIRONMENT
    assert classify_finding(
        {"cause": "NEEDS_REVIEW", "triage": {"source": "SCRIPT"}}) == CLASS_LOCATOR


def test_proposal_is_human_gated_and_grounded():
    p = build_proposal({"cause": "WRONG_CONTROL_KIND", "scenario_id": "s1",
                        "step_number": 3, "cause_label": "wrong kind",
                        "evidence": ["e1"]})
    assert p["status"] == "proposed"
    assert "approval" in p["apply_requires"]
    assert "never self-applies" in p["apply_requires"]
    assert p["failing_repro"]["scenario_id"] == "s1"
    assert "regression test" in p["failing_repro"]["note"].lower()
    assert "INTERACTION_RECIPES" in p["suggested_strategy"]  # grounded registry name
    unknown = build_proposal({"cause": "SOMETHING_NEW"})
    assert "human" in unknown["suggested_strategy"].lower()  # never invents a strategy


def _timeline():
    return {
        "run_id": "r1",
        "scenarios": [
            {"scenario_id": "gap", "name": "slider case", "steps": [
                {"step_number": 1, "status": "passed"},
                {"step_number": 2, "status": "failed"}]},
            {"scenario_id": "bug", "name": "quote case", "steps": [
                {"step_number": 1, "status": "failed"}]},
            {"scenario_id": "green", "name": "nav case", "steps": [
                {"step_number": 1, "status": "passed"}]},
            {"scenario_id": "mystery", "name": "odd case", "steps": [
                {"step_number": 1, "status": "broken"}]},
        ],
    }


def _diags():
    return {
        "gap": {"cause": "WRONG_CONTROL_KIND", "cause_label": "slider not typeable",
                "evidence": ["kind=slider"], "triage": {"source": "SCRIPT"}},
        "bug": {"cause": "REAL_REGRESSION", "cause_label": "outcome contradicted",
                "defect_report": {"title": "[Product Bug] quote"},
                "defect_markdown": "# [Product Bug] quote"},
        # 'mystery' has NO diagnosis — must surface as NEEDS_HUMAN, never guessed.
    }


def test_scan_classifies_proposes_and_preserves_defects():
    s = scan(_timeline(), _diags())
    assert s.run_id == "r1"
    assert s.summary["failing_scenarios"] == 3          # green excluded
    assert s.summary["by_class"][CLASS_PRODUCT_GAP] == 1
    assert s.summary["by_class"][CLASS_APP_DEFECT] == 1
    assert s.summary["by_class"][CLASS_UNKNOWN] == 1
    assert len(s.proposals) == 1 and s.proposals[0]["scenario_id"] == "gap"
    assert len(s.defects) == 1
    d = s.defects[0]
    assert d["defect_markdown"].startswith("# ")
    assert "never" in d["note"] and "hide" in d["note"]
    assert "propose-only" in s.summary["doctrine"]
    # Round-trips to a JSON-able dict for the endpoint.
    out = scan_to_dict(s)
    assert out["summary"]["proposals"] == 1
    assert out["findings"][0]["classification"] in (
        CLASS_PRODUCT_GAP, CLASS_APP_DEFECT, CLASS_UNKNOWN)


def test_scan_never_invents_a_finding_for_green_scenarios():
    s = scan({"run_id": "r2", "scenarios": [
        {"scenario_id": "g", "steps": [{"step_number": 1, "status": "passed"}]}]}, {})
    assert s.findings == [] and s.proposals == [] and s.defects == []
    assert s.summary["failing_scenarios"] == 0
