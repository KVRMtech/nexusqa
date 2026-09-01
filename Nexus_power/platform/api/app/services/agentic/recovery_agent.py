"""Recovery Agent v1 — the R5 'Super Agent' loop, encoded (propose-only).

The loop a human engineer performed manually during the 2026-07-22/23 pilot
session — watch a run, classify every failure through the honest taxonomy, and
for each finding produce an ACTIONABLE ARTIFACT — is encoded here as a pure,
deterministic scanner:

    scan(timeline, diagnoses)  ->  RecoveryScan
        findings: every failing scenario classified into the 9-class taxonomy
        proposals: for PRODUCT-CAPABILITY-GAP class findings, a PROPOSAL BUNDLE
                   (diagnosis + failing-repro pointer + suggested strategy)
        defects:   for APPLICATION-DEFECT class findings, the pointer to the
                   auto-authored defect report (already built at the stop)

DOCTRINE (non-negotiable, from the founder's requirements):
  * PROPOSE-ONLY. The agent NEVER modifies product code, never edits a case,
    never re-runs anything by itself. Every proposal carries a human-gate
    ``status: "proposed"`` and an explicit ``apply_requires`` note. Silent
    self-modification would violate the platform's own auditability principle.
  * NEVER GREEN-WASH. A finding the taxonomy cannot place is UNKNOWN /
    needs-human — never guessed into a bucket. Application defects are
    reported, never softened.
  * GROUNDED. Suggested strategies come from the existing recipe registry /
    coverage-ledger vocabulary — named capabilities, not invented code.

v1 boundaries (honest): no persistence table (the scan is recomputed on
demand), no auto-generated regression-test code (the proposal names the repro
case + step — the failing test IS the repro), no LLM. Those are the v2 items,
each behind the same human gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import triage as triage_mod

_FAIL = frozenset({"failed", "broken", "timed_out"})

#: The requirement's 9-class outcome taxonomy, keyed from (triage source,
#: diagnosis cause). Order of checks matters — most specific first.
CLASS_PRODUCT_GAP = "PRODUCT_CAPABILITY_GAP"
CLASS_APP_DEFECT = "APPLICATION_DEFECT"
CLASS_TEST_DATA = "TEST_DATA_ISSUE"
CLASS_ENVIRONMENT = "ENVIRONMENT_ISSUE"
CLASS_CONFIG = "CONFIGURATION_ISSUE"
CLASS_EXTERNAL = "EXTERNAL_DEPENDENCY_FAILURE"
CLASS_LOCATOR = "SCRIPT_LOCATOR_ISSUE"     # script-side; heal-able
CLASS_TIMING = "SCRIPT_TIMING_ISSUE"       # script-side; heal-able
CLASS_UNKNOWN = "NEEDS_HUMAN"

#: Diagnosis causes that mean the PLATFORM lacks a capability (the app is not
#: accused; the product must grow) — the findings the agent proposes work for.
_GAP_CAUSES = frozenset({"WRONG_CONTROL_KIND", "CANVAS_NO_DOM", "UNHANDLED_CONTROL"})
_DATA_CAUSES = frozenset({"DATA_PRECONDITION_UNMET", "DATA_VALIDITY_CROSS_FIELD"})
_CONFIG_CAUSES = frozenset({"AUTH_PRECONDITION", "AUTH_NOT_AUTHENTICATED",
                            "LOGIN_FAILED", "INVALID_TARGET_URL"})
_LOCATOR_CAUSES = frozenset({"LOCATOR_NOT_FOUND", "SELECTOR_DRIFT",
                             "SELECTOR_REANCHOR", "AMBIGUOUS_LOCATOR"})
_TIMING_CAUSES = frozenset({"FLAKE", "TIMEOUT", "TIMING"})

#: Grounded strategy suggestions per gap cause — names from the EXISTING
#: recipe/primitive vocabulary (interaction_resolver / matcher), never invented.
_GAP_STRATEGIES = {
    "WRONG_CONTROL_KIND": (
        "Route the control through the UACR interaction resolver "
        "(interaction_resolver.INTERACTION_RECIPES) with the control kind "
        "observed at failure time; if no recipe fits, add one — with its own "
        "oracle — to the registry."),
    "CANVAS_NO_DOM": (
        "Canvas surfaces are OPAQUE-ledgered by design. Candidate strategy: "
        "coordinate-level interaction with an orthogonal (network/displayed-"
        "value) oracle, per the hard-UI research notes — human design "
        "required before any build."),
    "UNHANDLED_CONTROL": (
        "The coverage ledger names this control UNHANDLED. Implement its "
        "primitive in the matcher registry (matcher.py) with a grounded "
        "read-back oracle, mirroring the RANGE_SET / GROUP_ASSEMBLE roadmap "
        "pattern."),
}


def classify_finding(diag: dict) -> str:
    """Map one unified diagnosis (cause + triage source + network signal) to the
    9-class taxonomy. Conservative: unknown stays NEEDS_HUMAN."""
    diag = diag or {}
    cause = (diag.get("cause") or "").strip().upper()
    net = diag.get("network") or {}
    if (net.get("kind") == "external_dependency"):
        return CLASS_EXTERNAL
    if cause in _GAP_CAUSES:
        return CLASS_PRODUCT_GAP
    if cause in _DATA_CAUSES:
        return CLASS_TEST_DATA
    if cause in _CONFIG_CAUSES:
        return CLASS_CONFIG
    if cause in _LOCATOR_CAUSES:
        return CLASS_LOCATOR
    if cause in _TIMING_CAUSES:
        return CLASS_TIMING
    source = ((diag.get("triage") or {}).get("source")
              or diag.get("source") or "").strip().upper()
    if cause == "REAL_REGRESSION" or source == "PRODUCT":
        return CLASS_APP_DEFECT
    if source == "ENVIRONMENT" or cause in ("ENV_BLOCK", "ENVIRONMENT"):
        return CLASS_ENVIRONMENT
    if source == "SCRIPT":
        return CLASS_LOCATOR
    return CLASS_UNKNOWN


@dataclass
class RecoveryScan:
    run_id: str
    findings: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    defects: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def build_proposal(finding: dict) -> dict:
    """A human-gated PROPOSAL BUNDLE for one product-capability-gap finding.

    The failing case+step IS the repro (re-running it red proves the gap;
    green after the strategy lands proves the fix) — v1 names it rather than
    generating new test code."""
    cause = (finding.get("cause") or "").strip().upper()
    return {
        "kind": "capability_gap_proposal",
        "status": "proposed",                       # the human gate
        "apply_requires": "founder/maintainer approval — the agent never self-applies",
        "scenario_id": finding.get("scenario_id", ""),
        "step_number": finding.get("step_number"),
        "cause": cause,
        "cause_label": finding.get("cause_label", ""),
        "evidence": list(finding.get("evidence") or []),
        "failing_repro": {
            "scenario_id": finding.get("scenario_id", ""),
            "step_number": finding.get("step_number"),
            "note": ("Re-run this case: RED reproduces the gap now; GREEN after "
                     "the strategy lands is the acceptance proof. A regression "
                     "test must land in the same change (R6 discipline)."),
        },
        "suggested_strategy": _GAP_STRATEGIES.get(
            cause, "No registered strategy — design work needed (human)."),
    }


def scan(timeline: dict, diagnoses: dict) -> RecoveryScan:
    """Classify every diagnosed failure of one run; bundle proposals/defects.

    ``timeline``  — the run timeline (build_run_timeline_by_id shape).
    ``diagnoses`` — {scenario_id: unified diagnosis} (auto_diagnosis output,
                    or the on-click analyze shape passed through triage).
    Pure + deterministic; no DB, no LLM, no writes."""
    run_id = str((timeline or {}).get("run_id")
                 or (timeline or {}).get("run", {}).get("run_id") or "")
    out = RecoveryScan(run_id=run_id)
    counts: dict[str, int] = {}
    for sc in (timeline or {}).get("scenarios") or []:
        sid = sc.get("scenario_id") or ""
        diag = (diagnoses or {}).get(sid)
        first_fail = next((st for st in (sc.get("steps") or [])
                           if st.get("status") in _FAIL), None)
        if first_fail is None:
            continue
        if not diag:
            klass = CLASS_UNKNOWN
            diag = {}
        else:
            klass = classify_finding(diag)
        finding = {
            "scenario_id": sid,
            "scenario_name": sc.get("name") or sc.get("case_name") or "",
            "step_number": first_fail.get("step_number"),
            "classification": klass,
            "cause": diag.get("cause", ""),
            "cause_label": diag.get("cause_label", ""),
            "triage": diag.get("triage") or {},
            "evidence": list(diag.get("evidence") or []),
            "recommended_action": diag.get("recommended_action", ""),
        }
        out.findings.append(finding)
        counts[klass] = counts.get(klass, 0) + 1
        if klass == CLASS_PRODUCT_GAP:
            out.proposals.append(build_proposal(finding))
        elif klass == CLASS_APP_DEFECT:
            out.defects.append({
                "scenario_id": sid,
                "step_number": first_fail.get("step_number"),
                "defect_report": diag.get("defect_report"),   # authored at the stop
                "defect_markdown": diag.get("defect_markdown"),
                "note": ("Evidence preserved; report honestly — the agent never "
                         "modifies or hides an application failure."),
            })
    out.summary = {
        "run_id": run_id,
        "failing_scenarios": len(out.findings),
        "by_class": counts,
        "proposals": len(out.proposals),
        "defects": len(out.defects),
        "doctrine": "propose-only; human gate on every apply; never green-wash",
    }
    return out


def scan_to_dict(s: RecoveryScan) -> dict:
    return {"run_id": s.run_id, "summary": s.summary, "findings": s.findings,
            "proposals": s.proposals, "defects": s.defects}
