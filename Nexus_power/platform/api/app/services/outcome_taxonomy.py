"""R7 — the CANONICAL 8-class outcome taxonomy (the requirement's exact list).

The platform grew four verdict vocabularies (run-reducer VERDICT_*, cycle
dispositions, qe_agents classes, agentic-triage sources, recovery-agent
classes) — all honest, none canonical (requirements-audit finding). This module
is the single mapping surface: every vocabulary folds into the requirement's
own eight outcome classes, so every UI/export/API can speak ONE language.

    PASS · APPLICATION_DEFECT · PRODUCT_CAPABILITY_GAP · CONFIGURATION_ISSUE ·
    ENVIRONMENT_ISSUE · TEST_DATA_ISSUE · EXTERNAL_DEPENDENCY_FAILURE · BLOCKED

Doctrine invariants (test-pinned):
  * NOTHING maps to PASS except the explicit pass-family inputs — an unknown
    or ambiguous input can NEVER become PASS (fail-toward-BLOCKED).
  * Script-side repairable causes (locator drift, timing) are NOT one of the
    eight business outcomes — while unresolved they map to BLOCKED (blocked on
    automation repair), with the detail preserved; they never accuse the app.
"""
from __future__ import annotations

from .agentic import recovery_agent as _ra

# ── The canonical eight ─────────────────────────────────────────────────────
PASS = "PASS"
APPLICATION_DEFECT = "APPLICATION_DEFECT"
PRODUCT_CAPABILITY_GAP = "PRODUCT_CAPABILITY_GAP"
CONFIGURATION_ISSUE = "CONFIGURATION_ISSUE"
ENVIRONMENT_ISSUE = "ENVIRONMENT_ISSUE"
TEST_DATA_ISSUE = "TEST_DATA_ISSUE"
EXTERNAL_DEPENDENCY_FAILURE = "EXTERNAL_DEPENDENCY_FAILURE"
BLOCKED = "BLOCKED"

ALL_CLASSES = (PASS, APPLICATION_DEFECT, PRODUCT_CAPABILITY_GAP,
               CONFIGURATION_ISSUE, ENVIRONMENT_ISSUE, TEST_DATA_ISSUE,
               EXTERNAL_DEPENDENCY_FAILURE, BLOCKED)

#: Human labels (UI/export).
LABELS = {
    PASS: "Pass (verified)",
    APPLICATION_DEFECT: "Application defect",
    PRODUCT_CAPABILITY_GAP: "Product capability gap (ours, not yours)",
    CONFIGURATION_ISSUE: "Configuration issue",
    ENVIRONMENT_ISSUE: "Environment issue",
    TEST_DATA_ISSUE: "Test data issue",
    EXTERNAL_DEPENDENCY_FAILURE: "External dependency failure",
    BLOCKED: "Blocked — needs a human / repair in flight",
}

# ── Mappers (every known vocabulary → canonical) ────────────────────────────

#: run-reducer verdicts (services/test_runs VERDICT_*).
_RUN_VERDICTS = {
    "passed": PASS,
    "real_regression": APPLICATION_DEFECT,
    "selector_drift": BLOCKED,      # automation repair in flight — never the app's fault
    "visual_change": BLOCKED,       # needs review — never auto-judged
    "flake": ENVIRONMENT_ISSUE,
    "needs_review": BLOCKED,
}

#: cycle regression dispositions (controlplane/cycle/regression_verdict).
_CYCLE_DISPOSITIONS = {
    "PASS_UNCHANGED": PASS,
    "SELF_HEALED": PASS,            # proven green after an oracle-gated heal
    "BENIGN_DRIFT": PASS,
    "FIRST_BASELINE": PASS,         # the establishing run itself proved green
    "GENUINE_REGRESSION": APPLICATION_DEFECT,
    "HONEST_UNPROVEN": BLOCKED,
}

#: qe_agents triage classes.
_QE_AGENT_CLASSES = {
    "product_defect": APPLICATION_DEFECT,
    "environment": ENVIRONMENT_ISSUE,
    "script_defect": BLOCKED,
    "data": TEST_DATA_ISSUE,
    "unknown": BLOCKED,
}

#: recovery-agent classes (already 9-class; fold the two script-side ones).
_RECOVERY_CLASSES = {
    _ra.CLASS_PRODUCT_GAP: PRODUCT_CAPABILITY_GAP,
    _ra.CLASS_APP_DEFECT: APPLICATION_DEFECT,
    _ra.CLASS_TEST_DATA: TEST_DATA_ISSUE,
    _ra.CLASS_ENVIRONMENT: ENVIRONMENT_ISSUE,
    _ra.CLASS_CONFIG: CONFIGURATION_ISSUE,
    _ra.CLASS_EXTERNAL: EXTERNAL_DEPENDENCY_FAILURE,
    _ra.CLASS_LOCATOR: BLOCKED,
    _ra.CLASS_TIMING: BLOCKED,
    _ra.CLASS_UNKNOWN: BLOCKED,
}


def _fold(mapping: dict, value: str) -> str:
    """Case-tolerant lookup; ANYTHING unknown folds to BLOCKED — never PASS."""
    v = (value or "").strip()
    return mapping.get(v) or mapping.get(v.lower()) or mapping.get(v.upper()) or BLOCKED


def from_run_verdict(verdict: str) -> str:
    return _fold(_RUN_VERDICTS, verdict)


def from_cycle_disposition(disposition: str) -> str:
    return _fold(_CYCLE_DISPOSITIONS, disposition)


def from_qe_agent_class(klass: str) -> str:
    return _fold(_QE_AGENT_CLASSES, klass)


def from_recovery_class(klass: str) -> str:
    return _fold(_RECOVERY_CLASSES, klass)


def from_diagnosis(diag: dict) -> str:
    """Canonical class for a unified diagnosis (the recovery agent's classifier
    is the single grounded path — reuse it, never a parallel heuristic)."""
    return from_recovery_class(_ra.classify_finding(diag or {}))


def outcome(kind: str, value) -> dict:
    """One canonical outcome object: {class, label, source_vocabulary, raw}."""
    mapper = {
        "run_verdict": from_run_verdict,
        "cycle_disposition": from_cycle_disposition,
        "qe_agent_class": from_qe_agent_class,
        "recovery_class": from_recovery_class,
        "diagnosis": from_diagnosis,
    }.get(kind)
    klass = mapper(value) if mapper else BLOCKED
    return {"class": klass, "label": LABELS[klass],
            "source_vocabulary": kind, "raw": value if kind != "diagnosis" else
            (value or {}).get("cause", "")}
