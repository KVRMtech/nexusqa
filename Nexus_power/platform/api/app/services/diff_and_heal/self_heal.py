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
from . import heal_capture_store
from .action_resolver import resolve_reanchor

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
) -> dict:
    """Pure deterministic diagnosis for one failed step. No I/O, no LLM.

    `reanchor` is an optional, already-resolved re-anchor (a confident, unambiguous
    live match for a renamed control — see action_resolver). When present it
    UPGRADES a selector-class failure (selector-drift / locator-not-found) to an
    auto-fixable SELECTOR_REANCHOR. It can never override REAL_REGRESSION: that
    branch short-circuits first, so a contradicted outcome is never re-anchored."""
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
            "reanchor": {"name": new_name, "role": reanchor.get("role", "")},
            "needs": "Ready to apply + verify.",
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
    reanchor = resolve_reanchor_for_step(
        tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
        baseline_step=baseline_step, field_meta=field_meta,
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


def resolve_reanchor_for_step(
    *, tenant_id: str, artifact_id: str, scenario_id: str,
    baseline_step: Any, field_meta: dict,
) -> dict | None:
    """Read the failure-state a11y capture for this scenario (if any) and resolve a
    re-anchor for the failing control. Returns a re-anchor dict {name, role,
    confidence, rationale} or None (refuse / no capture). Pure-ish: only reads the
    transient capture store + the deterministic resolver, no LLM."""
    cap = heal_capture_store.get(
        tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
    )
    if not cap or not cap.get("nodes"):
        return None
    o = _observed(baseline_step) if baseline_step is not None else {}
    label = o.get("label") or ""
    if not label:
        return None
    ra = resolve_reanchor(
        recorded_label=label,
        recorded_kind=_refine_kind(o, field_meta),
        live_nodes=cap["nodes"],
    )
    if ra is None:
        return None
    return {"name": ra.name, "role": ra.role,
            "confidence": ra.confidence, "rationale": ra.rationale}


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
    kind-aware compiler emits .selectOption() for every corrected label. Parametrized
    to match the run bundle."""
    return compile_case(tc, {**field_meta, **(overrides or {})}, parametrize=True)


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
