"""R7 — the canonical 8-class taxonomy: every vocabulary folds in; nothing
unknown can ever become PASS."""
from app.services import outcome_taxonomy as ot


def test_the_eight_classes_are_exactly_the_requirements():
    assert set(ot.ALL_CLASSES) == {
        "PASS", "APPLICATION_DEFECT", "PRODUCT_CAPABILITY_GAP",
        "CONFIGURATION_ISSUE", "ENVIRONMENT_ISSUE", "TEST_DATA_ISSUE",
        "EXTERNAL_DEPENDENCY_FAILURE", "BLOCKED"}
    assert set(ot.LABELS) == set(ot.ALL_CLASSES)


def test_run_verdicts_fold():
    assert ot.from_run_verdict("passed") == ot.PASS
    assert ot.from_run_verdict("real_regression") == ot.APPLICATION_DEFECT
    assert ot.from_run_verdict("selector_drift") == ot.BLOCKED
    assert ot.from_run_verdict("flake") == ot.ENVIRONMENT_ISSUE
    assert ot.from_run_verdict("needs_review") == ot.BLOCKED


def test_cycle_dispositions_fold():
    assert ot.from_cycle_disposition("PASS_UNCHANGED") == ot.PASS
    assert ot.from_cycle_disposition("SELF_HEALED") == ot.PASS
    assert ot.from_cycle_disposition("GENUINE_REGRESSION") == ot.APPLICATION_DEFECT
    assert ot.from_cycle_disposition("HONEST_UNPROVEN") == ot.BLOCKED


def test_recovery_and_qe_agent_classes_fold():
    assert ot.from_recovery_class("PRODUCT_CAPABILITY_GAP") == ot.PRODUCT_CAPABILITY_GAP
    assert ot.from_recovery_class("EXTERNAL_DEPENDENCY_FAILURE") == ot.EXTERNAL_DEPENDENCY_FAILURE
    assert ot.from_recovery_class("SCRIPT_LOCATOR_ISSUE") == ot.BLOCKED
    assert ot.from_qe_agent_class("product_defect") == ot.APPLICATION_DEFECT
    assert ot.from_qe_agent_class("data") == ot.TEST_DATA_ISSUE


def test_diagnosis_path_reuses_the_recovery_classifier():
    assert ot.from_diagnosis({"cause": "WRONG_CONTROL_KIND"}) == ot.PRODUCT_CAPABILITY_GAP
    assert ot.from_diagnosis({"cause": "REAL_REGRESSION"}) == ot.APPLICATION_DEFECT
    assert ot.from_diagnosis(
        {"cause": "NEEDS_REVIEW", "network": {"kind": "external_dependency"}}
    ) == ot.EXTERNAL_DEPENDENCY_FAILURE


def test_unknown_never_becomes_pass():
    """The doctrine invariant: any unknown input in ANY vocabulary folds to
    BLOCKED — never PASS, never a guessed class."""
    for fn in (ot.from_run_verdict, ot.from_cycle_disposition,
               ot.from_qe_agent_class, ot.from_recovery_class):
        assert fn("definitely_new_value") == ot.BLOCKED
        assert fn("") == ot.BLOCKED
        assert fn(None) == ot.BLOCKED
    assert ot.from_diagnosis({}) == ot.BLOCKED
    assert ot.outcome("nonsense_vocab", "x")["class"] == ot.BLOCKED


def test_outcome_object_shape():
    o = ot.outcome("run_verdict", "real_regression")
    assert o == {"class": ot.APPLICATION_DEFECT,
                 "label": ot.LABELS[ot.APPLICATION_DEFECT],
                 "source_vocabulary": "run_verdict", "raw": "real_regression"}
