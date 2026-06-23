"""Nexus TrueFix — grounded root-cause diagnosis for a failed test step (P1).

Deterministic, $0 LLM, read-only. Given a failed run step it names the most
likely cause — wrong-control-kind / selector-drift / locator-not-found /
timing / flake / environment-bot-block / real-regression(suspected) — with a
confidence, grounded evidence, a recommended action, and (when applicable) a
SUGGESTED fix whose before/after is produced by the SAME kind-aware compiler
that emitted the script. The "after" is real compiler output for the corrected
control kind, not a hand-written guess.

Safety: this phase only DIAGNOSES + suggests. It never edits the script and
never claims a heal. `auto_fixable` is true only when a grounded control-kind
signal is held (recorded `observed.kind`, captured form options, or — later —
the live evidence graph); with no such signal the suggestion is offered for a
human to confirm, and the verdict falls toward needs_review. Apply + closed-loop
verify (re-run headed, prove green, version) is the next phase.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    E2ETestRunRow,
    E2ETestRunStepRow,
    E2E_STEP_STATUS_FAILED,
    E2E_STEP_STATUS_BROKEN,
    E2E_STEP_STATUS_TIMED_OUT,
)

from ..test_runs import (
    classify_failure,
    last_run_summary_by_scenario,
    outcome_contradicted_from_error,
    VERDICT_REAL_REGRESSION,
    _LOCATOR_MISSING_RE,
)
from ..script_factory.compiler import (
    _observed,
    _refine_kind,
    _action_lines,
    build_field_meta,
    compile_case,
    _norm,
)
from ..test_factory import service as factory_service
from ..script_factory import interaction_resolver
from . import heal_capture_store
from .action_resolver import recorded_state_intent, resolve_reanchor

logger = logging.getLogger(__name__)

_FAIL = frozenset({E2E_STEP_STATUS_FAILED, E2E_STEP_STATUS_BROKEN, E2E_STEP_STATUS_TIMED_OUT})

_TIMEOUT_RE = re.compile(r"timeout\b", re.IGNORECASE)
_FILL_RE = re.compile(r"locator\.fill", re.IGNORECASE)
_CLICK_RE = re.compile(r"locator\.click", re.IGNORECASE)
# Playwright's definitive "you called .fill() on something that isn't a text input"
# error (e.g. a native <select> or a custom widget) — a GROUNDED control-kind signal.
_NOT_EDITABLE_RE = re.compile(
    r"not an <input>|not an <textarea>|not.*editable|"
    r"element is not.*(input|textarea|contenteditable|select)|"
    r"<select>|is not a <select>",
    re.IGNORECASE,
)
_SELECT_SIGNAL = frozenset({"select", "dropdown", "combobox", "listbox"})

# A failure that landed on a login / sign-in / forbidden page (or 401/403): the run
# lost its authenticated session — a PRECONDITION failure, NOT a control-kind/selector
# issue. We REFUSE to heal it (healing would re-bind into the login page and green-wash
# a broken precondition; later step failures are cascades of this one).
_AUTH_REDIRECT_RE = re.compile(
    r"/login\b|/sign-?in\b|/auth(?:/|\b)|\b401\b|\b403\b|unauthor|forbidden|"
    r"session\s+expired|not\s+logged\s+in|please\s+(?:log|sign)\s*in",
    re.IGNORECASE,
)

# DATA / STATE PRECONDITION NOT MET — a step fails because required DATA or state is
# absent: an empty list/grid, "no records/results/rows found", a missing record, or a
# legitimately-absent CONDITIONAL field. This is NOT a control-kind drift or a rename —
# the control resolved (or was never expected) but the DATA the flow depends on isn't
# there. Healing would re-bind to whatever IS on the page and green-wash a broken
# precondition, so we REFUSE and escalate. High-precision: we only claim this on EXPLICIT
# empty/missing-DATA phrasing (false escalation is acceptable; a false HEAL of a real
# defect is not). NOTE: we deliberately do NOT match a bare "not found" / "404": those
# are already a LOCATOR signal (test_runs._LOCATOR_MISSING_RE) and stealing them here —
# before the LOCATOR_NOT_FOUND branch and the re-anchor upgrade — would refuse healable
# renames (a renamed control's error often reads "… not found"). We require explicit
# record/data context (e.g. "record not found", "no such record").
_DATA_PRECONDITION_RE = re.compile(
    r"\bno\s+(?:results?|records?|rows?|items?|matches?|data|entries)\b|"
    r"\b(?:results?|records?|rows?|items?|list|grid|table)\s+(?:is|are|was|were)\s+empty\b|"
    r"\bempty\s+(?:results?|list|grid|table|state|set|response)\b|"
    r"\b0\s+(?:results?|records?|rows?|items?)\b|"
    r"\b(?:record|results?|rows?|items?|entry|entries|data)\s+not\s+found\b|"
    r"\bno\s+such\s+(?:record|item|entry)\b",
    re.IGNORECASE,
)

# A/B / FEATURE-FLAG VARIANT SUSPECTED — the live UI is a DIFFERENT variant than recorded
# (an experiment bucket / feature-flag flip changed the structure). Healing would bake ONE
# variant's selectors into the spec — green for that bucket, red (or wrong) for the other —
# so we REFUSE and escalate. High-precision: we only claim this on an explicit variant
# signal carried on the failure: a recorded variant/experiment cookie or feature-flag
# marker in the observed capture, OR variant/experiment phrasing in the error text. We do
# NOT infer it from generic locator drift (that stays SELECTOR_DRIFT / LOCATOR_NOT_FOUND).
_VARIANT_TEXT_RE = re.compile(
    r"\b(?:a/?b[\s-]?test|experiment|feature[\s-]?flag|variant|bucket(?:ed)?|"
    r"treatment\s+group|rollout)\b",
    re.IGNORECASE,
)
_VARIANT_COOKIE_KEYS = frozenset({
    "variant", "experiment", "ab_test", "abtest", "ab-test", "bucket",
    "feature_flag", "featureflag", "ff", "treatment", "split",
})


def _data_precondition_signal(observed: dict, err: str) -> bool:
    """High-precision DATA-precondition signal: EXPLICIT empty-data / missing-record
    phrasing in the error, OR the recording marked this step's outcome as
    conditional/optional AND there is an actual absence signal (a legitimately-absent
    conditional field — empty/missing data or a locator that resolved to nothing).

    The conditional/optional gate is deliberately NOT unconditional: a conditional step
    can fail for an ordinary reason too (e.g. a pure RENAME of its control). Firing on
    ANY failure of a conditional step would make conditional/optional steps permanently
    un-healable — a rename would be escalated as a data gap and never reach the re-anchor
    / control-kind heal. So we require an absence signal (empty/missing data OR a missing
    locator) to corroborate that the field is genuinely ABSENT, not merely renamed."""
    e = err or ""
    if _DATA_PRECONDITION_RE.search(e):
        return True
    # A recorded conditional/optional step — ONLY a precondition gap when corroborated by
    # an actual absence signal (otherwise it's an ordinary drift/rename of the field).
    if (observed.get("conditional") is True or observed.get("optional") is True):
        if _DATA_PRECONDITION_RE.search(e) or _LOCATOR_MISSING_RE.search(e):
            return True
    return False


def _variant_signal(observed: dict, live_capture: dict | None, err: str) -> tuple[bool, str]:
    """High-precision A/B-variant signal. Returns (matched, rationale). We only fire on a
    GROUNDED variant marker carried on the LIVE failure: an explicit live-capture variant
    flag, a variant/experiment COOKIE observed on the live run, or variant/experiment
    phrasing literally in the error — never on generic structural drift.

    `live_capture` is the failure-state capture from heal_capture_store (the SAME capture
    resolve_reanchor_for_step loads), NOT the recorded baseline `observed`. A variant flip
    is a property of the LIVE run, so the flag/cookies MUST come from the live capture: a
    recorded step can never tell us the live UI flipped buckets. `observed` is consulted
    only as a defensive fallback for a flag a recorder may have stamped; the authoritative
    source is the live capture."""
    cap = live_capture or {}
    # 1) an explicit variant flag stamped on the LIVE capture (authoritative; recorded
    #    `observed` is only a defensive fallback).
    if cap.get("variant_suspected") is True or observed.get("variant_suspected") is True:
        return True, "The failure capture flagged this step as served from a different A/B / feature-flag variant."
    # 2) a variant / experiment COOKIE present on the LIVE run.
    cookies = cap.get("cookies")
    if not isinstance(cookies, dict):
        cookies = observed.get("cookies") if isinstance(observed.get("cookies"), dict) else {}
    for k in (cookies or {}):
        kn = str(k).strip().lower()
        if kn in _VARIANT_COOKIE_KEYS or kn.startswith(("exp_", "ff_", "ab_", "variant_")):
            return True, f"A variant / experiment cookie ('{k}') is set on the live run — the live UI may be a different bucket than recorded."
    # 3) variant / experiment phrasing literally in the failure error.
    if _VARIANT_TEXT_RE.search(err or ""):
        return True, "The failure error references an A/B / experiment / feature-flag variant — the recorded structure may belong to a different bucket."
    return False, ""


# Grounded OUTCOME assertions a heal must never DROP or tolerant-downgrade. The tolerant
# "a value was committed" check (not.toHaveValue('')) is explicitly NOT grounded — it
# passes for any non-empty value, so a heal can't lean on it to go green.
_GROUNDED_ASSERT_RE = re.compile(
    r"expect\(.*?\)\.(?:not\.)?(?:toHaveURL|toHaveValue|toHaveText|toBeChecked|"
    r"toBeVisible|toHaveAttribute)\b"
)
_TOLERANT_ASSERT_RE = re.compile(r"\.not\.toHaveValue\(\s*''\s*\)")


def _count_grounded_assertions(spec: str) -> int:
    """Number of GROUNDED outcome assertions in a compiled spec (excludes the tolerant
    not.toHaveValue('') 'something committed' check)."""
    n = 0
    for line in (spec or "").splitlines():
        if "expect(" not in line or _TOLERANT_ASSERT_RE.search(line):
            continue
        if _GROUNDED_ASSERT_RE.search(line):
            n += 1
    return n


def assert_assertions_unchanged(baseline_spec: str, candidate_spec: str) -> tuple[bool, str]:
    """ASSERTION-IMMUTABILITY GUARD: a heal may rewrite locators / re-synthesise an
    interaction, but it must NEVER reduce the grounded OUTCOME assertions. If the
    candidate carries fewer grounded assertions than the baseline, the heal weakened the
    oracle — REJECT it (nothing is proved or saved). This makes 'weaken-an-assertion-to-
    go-green' structurally impossible, independent of which resolver produced the
    candidate."""
    b = _count_grounded_assertions(baseline_spec)
    c = _count_grounded_assertions(candidate_spec)
    if c < b:
        return False, (f"heal would WEAKEN the oracle (grounded assertions {b} -> {c}); "
                       "refusing — a heal may add or re-bind assertions, never drop one.")
    return True, f"oracle preserved (grounded assertions {b} -> {c})"


def step_outcome_grounded(spec: str, step_number: int) -> bool:
    """Does the compiled spec carry a GROUNDED outcome oracle for this step (vs only a
    tolerant check / an '// UNVERIFIED' marker)? Used to label a heal honestly: 'proven'
    (a grounded outcome held) vs 'outcome_not_grounded' (re-ran green but the recorded
    outcome was not independently asserted). A transparency signal, never a silent
    'proven'."""
    marker = f"step {step_number}:"
    in_step = False
    for line in (spec or "").splitlines():
        if "await test.step(" in line:
            in_step = marker in line
            continue
        if in_step and "expect(" in line and not _TOLERANT_ASSERT_RE.search(line) \
                and _GROUNDED_ASSERT_RE.search(line):
            return True
    return False


def suite_outcome_grounding(specs_by_sid: dict, steps_by_sid: dict) -> dict:
    """METAMORPHIC ACCEPTANCE roll-up (T1.3). Over the CANDIDATE specs a heal is about
    to freeze as a Clean Run, classify every executed step as ``grounded`` (it carries a
    non-tolerant grounded OUTCOME oracle) or ``outcome_not_grounded`` (it re-ran green but
    asserts no recorded outcome). Returns the per-scenario lists + suite totals + a
    ``hollow`` flag (a suite that ran all-green while NO step asserts ANY recorded outcome
    — a hollow pass that must never be silently frozen as 'proven').

    This is the HONESTY layer: a metamorphic heal must keep the recorded OUTCOME chain
    observable, not just make the steps stop failing. (A step that HAS a grounded oracle
    and fails it already breaks all_green upstream; this surfaces the steps that have NO
    oracle so an all-green-but-hollow suite is VISIBLE, never hidden.)"""
    by = {}
    for sid, spec in (specs_by_sid or {}).items():
        g, u = [], []
        for n in (steps_by_sid or {}).get(sid, []):
            (g if step_outcome_grounded(spec, n) else u).append(n)
        by[sid] = {"grounded": g, "outcome_not_grounded": u}
    total_g = sum(len(v["grounded"]) for v in by.values())
    total_u = sum(len(v["outcome_not_grounded"]) for v in by.values())
    return {"by_scenario": by, "grounded": total_g,
            "outcome_not_grounded": total_u, "hollow": total_g == 0 and total_u > 0}


def _baseline_step(tc: Any, n: int | None) -> Any:
    if tc is None or n is None:
        return None
    for st in (getattr(tc, "steps", None) or []):
        if getattr(st, "step_number", None) == n:
            return st
    return None


def _emitted_method(observed: dict, field_meta: dict) -> str:
    """The Playwright method the compiler emits for this step today."""
    verb = (observed.get("verb") or "").strip().lower()
    if verb == "type":
        return "selectOption" if _refine_kind(observed, field_meta) == "select" else "fill"
    if verb == "select":
        kind = _refine_kind(observed, field_meta)
        if kind in ("radio", "checkbox"):
            return "check"
        if kind == "toggle":
            return "click"
        return "selectOption"
    if verb == "click":
        return "click"
    if verb == "navigate":
        return "goto"
    return verb or "?"


def _has_select_signal(observed: dict, field_meta: dict) -> bool:
    """True when we hold a GROUNDED signal that the control is a chooser:
    recorded observed.kind, or captured form options for the label."""
    if (observed.get("kind") or "").strip().lower() in _SELECT_SIGNAL:
        return True
    fm = field_meta.get(_norm(observed.get("label", "") or "")) or {}
    if (fm.get("control") or "").strip().lower() in _SELECT_SIGNAL:
        return True
    return len(fm.get("options") or []) >= 2


def _excerpt(text: str, n: int = 400) -> str:
    t = (text or "").strip()
    return t[:n] + ("…" if len(t) > n else "")


def diagnose(
    *,
    error_message: str,
    status: str,
    observed: dict,
    field_meta: dict,
    baseline_step: Any,
    is_flaky: bool,
    selector_drifted: bool,
    prior_step_passed: bool,
    reanchor: dict | None = None,
    live_capture: dict | None = None,
) -> dict:
    """Pure deterministic diagnosis for one failed step. No I/O, no LLM.

    `reanchor` is an optional, already-resolved re-anchor (a confident, unambiguous
    live match for a renamed control — see action_resolver). When present it
    UPGRADES a selector-class failure (selector-drift / locator-not-found) to an
    auto-fixable SELECTOR_REANCHOR. It can never override REAL_REGRESSION: that
    branch short-circuits first, so a contradicted outcome is never re-anchored.

    `live_capture` is the optional failure-state capture (heal_capture_store payload:
    {nodes, status, optionally cookies / variant_suspected}) for the LIVE run. It feeds
    the VARIANT_SUSPECTED signal a GROUNDED live marker — a recorded step can never tell
    us the live UI flipped A/B buckets, so without the live capture a genuine variant flip
    with no 'variant' word in the error would escape to SELECTOR_DRIFT / LOCATOR_NOT_FOUND
    and could heal green (a mis-heal of a real defect). None => the live-marker channels
    are simply inert (error-text phrasing still applies)."""
    err = error_message or ""
    verb = (observed.get("verb") or "").strip().lower()
    label = observed.get("label") or ""
    value = observed.get("value") or ""
    method = _emitted_method(observed, field_meta)
    locator_missing = bool(_LOCATOR_MISSING_RE.search(err))
    is_timeout = bool(_TIMEOUT_RE.search(err))
    is_fill = bool(_FILL_RE.search(err))
    fill_timeout = is_fill and is_timeout
    not_editable = is_fill and bool(_NOT_EDITABLE_RE.search(err))
    # Grounded if the recording marked it a chooser OR the browser itself proved
    # the target is not a fillable input.
    grounded_select = _has_select_signal(observed, field_meta) or not_editable
    # A/B-variant signal — evaluated ONCE (pure, no side effects) and reused by both the
    # branch guard and its body. Fed the LIVE failure capture (not the recorded baseline)
    # so a genuine variant flip is grounded in the live run. REAL_REGRESSION / AUTH / DATA
    # branches precede it in the elif chain, so they still take precedence.
    variant_matched, variant_why = _variant_signal(observed, live_capture, err)

    # The conservative verdict (real_regression / needs_review / …), reused as-is.
    # outcome_contradicted is grounded in a FAILED recorded-outcome assertion
    # (the navigated next page wasn't reached) — see outcome_contradicted_from_error.
    verdict = classify_failure(
        failed=status in _FAIL,
        is_flaky=is_flaky,
        selector_drifted=selector_drifted,
        bbox_drifted=False,
        outcome_contradicted=outcome_contradicted_from_error(err),
        error_message=err,
    )

    cause = "NEEDS_REVIEW"
    cause_label = "Needs review"
    confidence = 0.4
    auto_fixable = False
    evidence: list[str] = []
    recommended_action = ""
    suggested_fix: dict | None = None

    # 0) REAL REGRESSION — the recorded outcome was contradicted (the navigated
    #    next page was not reached). The action ran but the app produced a different
    #    result: a real bug, NOT a locator/control-kind issue. We REFUSE to heal —
    #    healing would paper over a genuine defect. File a defect instead.
    if verdict.label == VERDICT_REAL_REGRESSION:
        cause = "REAL_REGRESSION"
        cause_label = "Real regression"
        confidence = verdict.confidence
        auto_fixable = False
        evidence = [
            "The recorded outcome was contradicted: the flow did not reach the "
            "recorded next page (a grounded navigation-outcome assertion failed).",
            "The action executed but the application produced a different result — "
            "this is a real regression, not a locator or control-kind issue.",
        ]
        recommended_action = (
            "Do NOT heal — file a defect. Healing a real regression would hide a "
            "genuine bug. Confirm the expected outcome against the recorded baseline "
            "below, then route it to engineering.")
        suggested_fix = None

    # 0b) AUTH / PRECONDITION NOT MET — the step landed on a login / sign-in / forbidden
    #     page (or a 401/403), so the run lost its authenticated session. This is a
    #     PRECONDITION failure, NOT a control-kind or selector issue, and the rest of the
    #     run are cascades of it. We REFUSE to heal — healing would re-bind into the login
    #     page and green-wash a broken precondition. (Refuse before any heal branch.)
    elif _AUTH_REDIRECT_RE.search(err):
        cause = "AUTH_PRECONDITION"
        cause_label = "Auth / precondition not met"
        confidence = 0.7
        auto_fixable = False
        evidence = [
            "The step landed on a login / sign-in / forbidden page (or returned "
            "401/403) — the run lost its authenticated session.",
            "This is a PRECONDITION failure, not a control-kind or locator issue; any "
            "later step failures are cascades of this one.",
            "Healing here would re-bind into the login page and green-wash a broken "
            "precondition — so we refuse and escalate.",
        ]
        recommended_action = (
            "Restore the login session (re-capture the auth profile / fix the "
            "Environment URL + credentials) and re-run. Do NOT heal individual steps — "
            "fix the session, not the controls.")
        suggested_fix = None

    # 0c) DATA / STATE PRECONDITION NOT MET — the step failed because required DATA or
    #     state is absent (an empty list/grid, "no records found", a record explicitly
    #     "record not found", a legitimately-absent conditional field), NOT because a
    #     control drifted or changed kind. The control may even have resolved fine; the
    #     flow simply has nothing to act on. Healing would re-bind to whatever IS on the
    #     page and green-wash a broken data precondition. We REFUSE — escalate to fix the
    #     data/state, not the controls. High-precision: only on explicit empty/missing-DATA
    #     phrasing (a bare "not found"/"404" is left to the LOCATOR branch so renames stay
    #     healable). (Refuse before any heal branch; REAL_REGRESSION / AUTH returned above.)
    elif _data_precondition_signal(observed, err):
        cause = "DATA_PRECONDITION_UNMET"
        cause_label = "Data / state precondition not met"
        confidence = 0.7
        auto_fixable = False
        evidence = [
            "The step failed because required DATA or state is absent — an empty "
            "list/grid, no matching records, an explicitly-missing record, or a "
            "legitimately-absent conditional field.",
            "This is a PRECONDITION (data/state) gap, NOT a control-kind mismatch or a "
            "selector rename — the control may have resolved fine; there is simply "
            "nothing for the flow to act on.",
            "Healing here would re-bind to whatever happens to be on the page and "
            "green-wash a broken data precondition — so we refuse and escalate.",
        ]
        if prior_step_passed:
            evidence.append(
                "The previous step passed, so the page/flow loaded — this points to "
                "absent data, not a blocked page.")
        recommended_action = (
            "Seed / restore the required data or fixture (the record, list rows, or "
            "the state that enables the conditional field) and re-run. Do NOT heal the "
            "step — fix the precondition, not the locator.")
        suggested_fix = None

    # 0d) A/B / FEATURE-FLAG VARIANT SUSPECTED — the live UI looks like a DIFFERENT
    #     variant than was recorded (an experiment bucket or feature-flag flip changed the
    #     structure). Healing would bake ONE variant's selectors into the spec — green for
    #     that bucket, broken for the other. We REFUSE — escalate to pin the variant (or
    #     record both). High-precision: only on a GROUNDED variant marker (an observed
    #     variant flag, a variant/experiment cookie, or variant phrasing in the error) —
    #     never on generic drift. (Refuse before any heal branch.)
    elif variant_matched:
        cause = "VARIANT_SUSPECTED"
        cause_label = "A/B / feature-flag variant suspected"
        confidence = 0.65
        auto_fixable = False
        evidence = [
            variant_why,
            "The live UI appears to be a different A/B or feature-flag VARIANT than the "
            "recording — a structural difference driven by an experiment bucket, not a "
            "rename or a control-kind change.",
            "Healing here would bake one variant's selectors into the spec (green for "
            "that bucket, broken for the other) — so we refuse and escalate.",
        ]
        recommended_action = (
            "Pin the recording's variant for this run (force the experiment bucket / "
            "feature flag), or record the test against each variant. Do NOT heal — "
            "healing would lock in a single variant.")
        suggested_fix = None

    # 1) WRONG CONTROL KIND — typed/filled a control that is not a text input.
    elif verb == "type" and method == "fill" and (fill_timeout or not_editable or (is_timeout and not locator_missing)):
        cause = "WRONG_CONTROL_KIND"
        cause_label = "Wrong control kind" + ("" if grounded_select else " (suspected)")
        confidence = 0.85 if grounded_select else 0.6
        auto_fixable = grounded_select
        if not_editable:
            evidence = [
                f"The step calls .fill() — a text-input method — on '{label}'.",
                "The browser reported the target is NOT a fillable input (it's a "
                "<select> or custom control) — a definitive control-kind mismatch.",
            ]
        else:
            evidence = [
                f"The step calls .fill() — a text-input method — on '{label}'.",
                "Playwright waited the full timeout for a textbox/getByLabel match that "
                "never appeared, so the target is likely not a fillable text input "
                "(e.g. a dropdown / combobox / custom widget).",
            ]
        if grounded_select:
            evidence.append(
                "Grounded: the recording / captured form signals mark this control "
                "as a chooser, so the fix is derived from truth — safe to auto-apply.")
        else:
            evidence.append(
                "The recording captured this control only as a generic 'field' (no "
                "control-type signal), so the dropdown can't be confirmed from the "
                "recording alone — confirm on the live page before applying.")
        if prior_step_passed:
            evidence.append(
                "The previous step passed, so the page loaded — this points to a "
                "control-type mismatch rather than the page being fully blocked. If "
                "the widget genuinely never renders for automation, it is an "
                "environment / bot-block instead.")
        recommended_action = (
            f"Treat '{label}' as a chooser: open it and pick the option "
            "(.selectOption / click the option), then re-run headed against the real "
            "app to confirm. If it still never appears headed, it's an environment / "
            "bot-block, not a code fix."
        )
        # Suggested fix = the SAME compiler's output for the corrected kind.
        before_lines = _action_lines(baseline_step, field_meta) if baseline_step is not None else []
        fm2 = dict(field_meta)
        if label:
            fm2[_norm(label)] = {"control": "select",
                                 "options": [value] if value else [],
                                 "required": False}
        after_lines = _action_lines(baseline_step, fm2) if baseline_step is not None else []
        suggested_fix = {
            "summary": f"Treat '{label}' as a dropdown — open + select instead of typing.",
            "before": "\n".join(before_lines),
            "after": "\n".join(after_lines),
            "grounded": grounded_select,
            "needs": ("Ready to apply + verify."
                      if grounded_select
                      else "Confirm the control is a chooser (or capture its type), "
                           "then Apply & re-run to prove it green."),
        }

    # 2) SELECTOR DRIFT — resolved to a different element than recorded.
    elif selector_drifted:
        cause = "SELECTOR_DRIFT"
        cause_label = "Selector drift"
        confidence = 0.7
        evidence = [
            f"The control's selector changed since the recording for '{label}'.",
            "The flow may still reach the recorded outcome — but the locator no "
            "longer matches what was recorded.",
        ]
        recommended_action = (
            "Re-bind the locator to the moved/renamed control (Self-Healing "
            "Selectors), then re-run to confirm.")

    # 3) LOCATOR NOT FOUND — element absent; could be renamed/removed (real) or moved.
    elif locator_missing:
        cause = "LOCATOR_NOT_FOUND"
        cause_label = "Locator not found"
        confidence = 0.5
        evidence = [
            f"The locator for '{label or verb}' resolved to no element.",
            "Could be a rename/removal (a real change) or a moved control — the "
            "outcome can't be proven intact, so this needs a human.",
        ]
        recommended_action = (
            "Confirm on the live page whether the element was renamed/removed (a real "
            "regression → file a defect) or merely moved (re-bind the selector).")

    # 4) FLAKE — alternates pass/fail across runs with no deterministic cause.
    elif is_flaky:
        cause = "FLAKE"
        cause_label = "Flake"
        confidence = 0.6
        evidence = [
            f"'{label or verb}' alternates pass/fail across recent runs with no "
            "selector or outcome change — environmental flake.",
        ]
        recommended_action = (
            "Stabilize the environment / add a deterministic wait; not a code defect.")

    # 5) Anything else — fail toward a human.
    else:
        cause = "NEEDS_REVIEW"
        cause_label = "Needs review"
        confidence = 0.4
        evidence = [
            "Failed with no detectable control-kind mismatch, selector drift, or "
            "flake pattern — needs a human to look.",
        ]
        if is_timeout:
            evidence.append("The step timed out — check for a slow/blocked target or "
                            "a missing wait.")
        recommended_action = "Review the error against the recorded baseline below."

    # ── Re-anchor upgrade (P-B) — broadens auto-fix beyond control-kind. ──
    # Only a selector-class break (drift / locator-not-found) is eligible, and only
    # when a CONFIDENT, unambiguous live match exists. REAL_REGRESSION already
    # returned above, so a contradicted outcome can never reach here — the
    # refuse-on-real-regression safety is preserved by construction.
    if reanchor and reanchor.get("name") and cause in ("SELECTOR_DRIFT", "LOCATOR_NOT_FOUND"):
        new_name = reanchor["name"]
        cause = "SELECTOR_REANCHOR"
        cause_label = "Renamed control — re-anchor"
        confidence = round(float(reanchor.get("confidence") or 0.0), 2)
        auto_fixable = True
        evidence = [
            reanchor.get("rationale")
            or f"'{label}' appears to have been renamed to '{new_name}' on the live page.",
            "The recorded outcome was NOT contradicted, so the flow still reaches the "
            "recorded result — a locator rename, not a real regression.",
            "Grounded in the accessibility tree captured at the failure: the fix "
            "re-binds the resilient locator to the renamed control's new name.",
        ]
        # L4: surface the matched node's captured ARIA state as an extra grounded
        # signal (a same-named control was disambiguated by state, or the live
        # control's state confirms it is the recorded one). Additive evidence only.
        _ra_state = reanchor.get("state") or {}
        if _ra_state:
            _state_str = ", ".join(f"{k}={_ra_state[k]}" for k in sorted(_ra_state))
            evidence.append(
                f"Live accessibility state of the re-anchored control: {_state_str} "
                "(captured from the failure-state a11y snapshot, used to disambiguate "
                "same-named controls — an extra grounded signal, not a heal trigger).")
        recommended_action = (
            f"Re-anchor '{label}' → '{new_name}' and re-run headed to prove it green. "
            "If the re-run isn't green (or the outcome is contradicted), nothing is saved."
        )
        before_lines = _action_lines(baseline_step, field_meta) if baseline_step is not None else []
        after_lines = (
            _action_lines(baseline_step, field_meta, reanchor={"name": new_name})
            if baseline_step is not None else []
        )
        suggested_fix = {
            "summary": f"Re-bind '{label}' → '{new_name}' (renamed control).",
            "before": "\n".join(before_lines),
            "after": "\n".join(after_lines),
            "grounded": True,
            "reanchor": {"name": new_name, "role": reanchor.get("role", ""),
                         "state": _ra_state},  # L4: matched node's live ARIA state ({} when none)
            "needs": "Ready to apply + verify.",
        }

    # ── Control-KIND interaction upgrade — the recorded RECIPE is wrong because the
    # control changed KIND (e.g. native <select> → custom ARIA combobox needing
    # open + pick, not selectOption). Eligible on a selector/kind/timeout-class break
    # (REAL_REGRESSION already returned above) when a GROUNDED interaction recipe
    # exists. The recipe carries its OWN committed-value oracle and nothing persists
    # unless the headed re-run proves green — so this can never green-wash. Marked
    # WRONG_CONTROL_KIND so the Auto-Heal loop treats it as an auto-fixable control-
    # kind issue; `interaction` carries the recipe the override path threads to the
    # compiler's additive `interactions` channel.
    interaction = None
    if cause != "SELECTOR_REANCHOR" and cause in (
            "WRONG_CONTROL_KIND", "SELECTOR_DRIFT", "LOCATOR_NOT_FOUND", "NEEDS_REVIEW"):
        _intr = interaction_resolver.detect_interaction(observed, field_meta, err)
        if _intr:
            interaction = _intr
            cause = "WRONG_CONTROL_KIND"
            _hint = _intr.get("hint", "") or _intr.get("kind", "")
            cause_label = ("Wrong control kind — adaptive interaction re-synthesis"
                           + (f" (likely {_hint})" if _hint else ""))
            auto_fixable = True
            evidence = [
                f"The recorded recipe for '{label}' no longer drives the live control.",
                "Grounded: a recorded type/select step failed as a control-class error, so "
                "the control changed KIND (custom combobox / range slider / role=switch / "
                "accordion-disclosure / progressively-revealed field). The fix introspects "
                "the LIVE element at run time and drives it the right way — the recorded "
                f"value-shape only hints the likely kind ({_hint or 'unknown'}).",
                "The re-synthesised step carries its OWN committed-value oracle (the "
                "recorded value must actually commit) — nothing is saved unless the "
                "headed re-run proves it green, so a wrong guess fails RED, never green.",
            ]
            recommended_action = (
                f"Re-synthesise '{label}' as an open+pick interaction and re-run headed "
                "to prove the recorded value commits. If it isn't green, nothing is saved.")
            _bl = _action_lines(baseline_step, field_meta) if baseline_step is not None else []
            _al = (_action_lines(baseline_step, field_meta, interaction=_intr)
                   if baseline_step is not None else [])
            suggested_fix = {
                "summary": (f"Drive '{label}' with the adaptive control recipe "
                            f"(introspect the live element; likely {_intr.get('hint','') or _intr.get('kind','')})."),
                "before": "\n".join(_bl),
                "after": "\n".join(_al),
                "grounded": True,
                "interaction": _intr,
                "needs": "Apply & re-run to prove the recorded value commits.",
            }

    return {
        "label": label,
        "verb": verb,
        "emitted_method": method,
        "error_excerpt": _excerpt(err),
        "cause": cause,
        "cause_label": cause_label,
        "confidence": round(confidence, 2),
        "verdict": verdict.label,
        "auto_fixable": auto_fixable,
        "evidence": evidence,
        "recommended_action": recommended_action,
        "suggested_fix": suggested_fix,
        "interaction": interaction,
    }


async def analyze_step(
    db: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    scenario_id: str,
    step_number: int,
) -> dict:
    """Diagnose the latest run's failure of (scenario_id, step_number).

    Read-only, $0 LLM. Joins the failed run step to the recorded baseline step
    + captured form signals + flake/drift history, then runs `diagnose`.
    """
    # newest run for this artifact
    run = (await db.execute(
        select(E2ETestRunRow)
        .where(E2ETestRunRow.artifact_id == artifact_id,
               E2ETestRunRow.tenant_id == tenant_id)
        .order_by(desc(E2ETestRunRow.started_at)).limit(1)
    )).scalars().first()
    if run is None:
        return {"found": False, "reason": "no runs ingested for this artifact"}

    # the step row + the prior step's status (did the page get this far?)
    step_rows = (await db.execute(
        select(E2ETestRunStepRow)
        .where(E2ETestRunStepRow.run_id == run.run_id,
               E2ETestRunStepRow.tenant_id == tenant_id,
               E2ETestRunStepRow.scenario_id == scenario_id)
        .order_by(E2ETestRunStepRow.step_number)
    )).scalars().all()
    step = next((r for r in step_rows if r.step_number == step_number), None)
    if step is None:
        return {"found": False, "reason": "step not found in the latest run"}
    prior_passed = any(
        r.step_number < step_number and r.status not in _FAIL for r in step_rows
    )

    # recorded baseline case + step
    cases = await factory_service.load_active_production_cases(db, artifact_id=artifact_id)
    tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == scenario_id), None)
    baseline_step = _baseline_step(tc, step_number)
    observed = _observed(baseline_step) if baseline_step is not None else {}

    # captured form signals -> field_meta (control type / options per label)
    visits, _ = await factory_service._load_current_pages_and_actions(db, artifact_id=artifact_id)
    field_meta = build_field_meta(visits)

    # flake + selector-drift history for this scenario
    summary = await last_run_summary_by_scenario(db, artifact_id=artifact_id, tenant_id=tenant_id)
    s = summary.get(scenario_id, {})

    # If a failure-state a11y capture exists for this scenario (from a prior
    # NEXUS_HEAL_CAPTURE re-run), resolve a re-anchor for the failing control so a
    # renamed control can be auto-fixed. None when absent / no confident match.
    _cap = await heal_capture_store.aget(
        tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
    )
    reanchor = resolve_reanchor_for_step(
        tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
        baseline_step=baseline_step, field_meta=field_meta, cap=_cap,
    )

    diag = diagnose(
        error_message=step.error_message or "",
        status=step.status,
        observed=observed,
        field_meta=field_meta,
        baseline_step=baseline_step,
        is_flaky=bool(s.get("is_flaky")),
        selector_drifted=bool(s.get("selector_drift_observed")),
        prior_step_passed=prior_passed,
        reanchor=reanchor,
        live_capture=_cap,
    )
    diag.update({
        "found": True,
        "scenario_id": scenario_id,
        "step_number": step_number,
        "scenario_name": (getattr(tc, "name", "") if tc is not None else scenario_id),
        "baseline_available": tc is not None,
        "run_id": run.run_id,
    })
    return diag


# ─── Apply + closed-loop verify (P1b) ────────────────────────────────────


def build_candidate_for_step(
    tc: Any, field_meta: dict, step_number: int,
) -> tuple[str, dict]:
    """Recompile ONE case with the failing control's kind corrected to a chooser,
    so the kind-aware compiler emits .selectOption() instead of .fill(). Returns
    (candidate_spec, {label, value}). Parametrized to match the run bundle — this
    is the EXACT source persisted on a verified green."""
    bs = _baseline_step(tc, step_number)
    o = _observed(bs) if bs is not None else {}
    label = o.get("label") or ""
    value = o.get("value") or ""
    fm2 = dict(field_meta)
    if label:
        fm2[_norm(label)] = {"control": "select",
                             "options": [value] if value else [],
                             "required": False}
    spec = compile_case(tc, fm2, parametrize=True)
    return spec, {"label": label, "value": value}


def build_reanchor_candidate(
    tc: Any, field_meta: dict, step_number: int, reanchor: dict,
) -> tuple[str, dict]:
    """Recompile ONE case re-anchored to the renamed control's new accessible name
    (the resilient ladder + the step's own visibility oracle then key off the new
    name). Parametrized to match the run bundle. Returns (candidate_spec, meta)."""
    bs = _baseline_step(tc, step_number)
    o = _observed(bs) if bs is not None else {}
    spec = compile_case(
        tc, field_meta, parametrize=True,
        reanchors={step_number: {"name": reanchor["name"]}},
    )
    return spec, {"label": o.get("label") or "", "reanchor": reanchor["name"]}


def build_interaction_candidate(
    tc: Any, field_meta: dict, step_number: int, error_message: str = "",
) -> tuple[str, dict] | None:
    """Recompile ONE case re-synthesising the failing control's INTERACTION for a
    changed control KIND (e.g. native <select> → custom ARIA combobox: open + pick,
    not selectOption). Returns (candidate_spec, meta) or None when there is no grounded
    interaction signal (the heal then never guesses here). Parametrized to match the
    run bundle; the recipe carries its OWN committed-value oracle so the prove is real."""
    bs = _baseline_step(tc, step_number)
    o = _observed(bs) if bs is not None else {}
    intr = interaction_resolver.detect_interaction(o, field_meta, error_message)
    if not intr:
        return None
    spec = compile_case(tc, field_meta, parametrize=True,
                        interactions={step_number: intr})
    return spec, {"label": o.get("label") or "", "interaction": intr["kind"],
                  "value": intr.get("value", "")}


def resolve_reanchor_for_step(
    *, tenant_id: str, artifact_id: str, scenario_id: str,
    baseline_step: Any, field_meta: dict, cap: dict | None = None,
) -> dict | None:
    """Read the failure-state a11y capture for this scenario (if any) and resolve a
    re-anchor for the failing control. Returns a re-anchor dict {name, role,
    confidence, rationale} or None (refuse / no capture). Pure-ish: only reads the
    transient capture store + the deterministic resolver, no LLM.

    ``cap`` (T1.4): the SHARED-store capture, pre-fetched by the async caller via
    ``heal_capture_store.aget`` so this resolver can stay sync. When omitted we
    fall back to the legacy in-memory ``get`` — correct in a single-process /
    test context, but the async callers always pass ``cap`` so the resolver sees
    captures written by ANY worker (no silent over-escalation)."""
    if cap is None:
        cap = heal_capture_store.get(
            tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
        )
    if not cap or not cap.get("nodes"):
        # OVER-ESCALATION OBSERVABILITY (T1.4): a healable-looking failure is about to be
        # escalated SOLELY because no failure-state capture was found. Under a broken
        # shared-write path (e.g. the app role lacks CREATE/ALTER → every aput throws →
        # silent in-memory fallback that another worker can't see) this becomes systemic
        # silent over-escalation. Log it WITH the store's capture-success-rate so a sudden
        # collapse toward 0 (and a rising db_errors count) is visible, not hidden.
        _rate = heal_capture_store.capture_success_rate()
        _st = heal_capture_store.stats()
        logger.warning(
            "self_heal.reanchor_escalated_no_capture",
            extra={
                "artifact_id": artifact_id, "scenario_id": scenario_id,
                "capture_success_rate": _rate,
                "get_misses": _st.get("get_misses"), "puts_db": _st.get("puts_db"),
                "puts_memory": _st.get("puts_memory"), "db_errors": _st.get("db_errors"),
            },
        )
        return None
    o = _observed(baseline_step) if baseline_step is not None else {}
    label = o.get("label") or ""
    if not label:
        return None
    ra = resolve_reanchor(
        recorded_label=label,
        recorded_kind=_refine_kind(o, field_meta),
        live_nodes=cap["nodes"],
        # L4: feed the recorded STATE intent (e.g. a toggle left checked, a
        # disclosure expanded) so two same-named live controls are disambiguated by
        # state. Bounded + floor-checked against raw name similarity in the resolver,
        # so this only ever makes the match MORE precise — never green-washes.
        recorded_state=recorded_state_intent(o),
    )
    if ra is None:
        return None
    return {"name": ra.name, "role": ra.role,
            "confidence": ra.confidence, "rationale": ra.rationale,
            # L4: the matched live node's captured ARIA state — an extra grounded
            # signal surfaced through diagnose's reanchor evidence. {} when none.
            "state": ra.state or {}}


def first_failures(timeline: dict, scenario_ids: list[str]) -> list[dict]:
    """The first failing step per selected scenario in a run timeline — the work
    list for the Auto-Heal loop. Empty => the whole selected set is green."""
    sel = set(scenario_ids or [])
    out: list[dict] = []
    for sc in (timeline.get("scenarios") or []):
        sid = sc.get("scenario_id")
        if sel and sid not in sel:
            continue
        steps = sc.get("steps") or []
        fs = next((st for st in steps if st.get("status") in _FAIL), None)
        if fs is not None:
            out.append({
                "scenario_id": sid,
                "step_number": fs.get("step_number"),
                "status": fs.get("status"),
                "error_message": fs.get("error_message", "") or "",
                "prior_passed": any(
                    (st.get("step_number") or 0) < (fs.get("step_number") or 0)
                    and st.get("status") not in _FAIL for st in steps),
            })
    return out


def select_override_for_step(tc: Any, field_meta: dict, step_number: int):
    """The field_meta override that corrects this step's control to a chooser:
    returns (label_norm, signal) or None when there's no label to fix. The
    Auto-Heal loop accumulates these per scenario and recompiles."""
    bs = _baseline_step(tc, step_number)
    o = _observed(bs) if bs is not None else {}
    label = o.get("label") or ""
    if not label:
        return None
    value = o.get("value") or ""
    return _norm(label), {"control": "select",
                          "options": [value] if value else [],
                          "required": False}


def compile_case_with_overrides(tc: Any, field_meta: dict, overrides: dict) -> str:
    """Recompile one case with accumulated control-kind corrections applied — the
    kind-aware compiler emits .selectOption() for every corrected label, AND threads
    any accumulated INTERACTION re-synthesis (under the reserved `__interactions__`
    key — e.g. a custom-combobox open+pick recipe) to the compiler's additive
    `interactions` channel, AND any accumulated TIMING/MATERIALIZE/PORTAL/FRAME
    WAIT+SCOPE recipes (under the reserved `__waits__` key — scroll-until-
    materialize / retry-un-scoped-at-root / frameLocator-by-url / baseline-relative
    wait + perf flag) to the compiler's additive `waits` channel. Both reserved keys
    default to None (absent => byte-identical). Parametrized to match the run bundle."""
    ov = dict(overrides or {})
    interactions = ov.pop("__interactions__", None) or None
    waits = ov.pop("__waits__", None) or None
    return compile_case(tc, {**field_meta, **ov}, parametrize=True,
                        interactions=interactions, waits=waits)


def evaluate_heal(timeline: dict, scenario_id: str, step_number: int) -> dict:
    """Did the headed re-run PROVE the step green (and not a real regression)?
    Returns {healed: bool, verdict, reason}. Used to gate persistence — we write
    the candidate version ONLY when healed is True."""
    scen = next((s for s in (timeline.get("scenarios") or [])
                 if s.get("scenario_id") == scenario_id), None)
    if scen is None:
        return {"healed": False, "verdict": "",
                "reason": "the re-run produced no result for this scenario"}
    step = next((st for st in (scen.get("steps") or [])
                 if st.get("step_number") == step_number), None)
    verdict = scen.get("verdict", "") or ""
    if step is not None and step.get("status") == "passed" and verdict != "real_regression":
        return {"healed": True, "verdict": verdict,
                "reason": "the step passed on the headed re-run"}
    if step is not None and step.get("status") in ("failed", "broken", "timed_out"):
        return {"healed": False, "verdict": verdict,
                "reason": "the step still failed on the headed re-run — likely an "
                          "environment / bot-block on this target, or the fix did not "
                          "resolve it"}
    return {"healed": False, "verdict": verdict,
            "reason": "could not confirm the step passed on the re-run"}
