"""Heal-time recording-quality classifier (Auto-Heal coverage Layer #3).

Some step failures are not application drift at all — they are artifacts of how
the session was RECORDED: a control captured twice in a row (double-click /
double-capture), or a form the recording never actually submits. Trying to
"heal" such a step with control-kind / re-anchor churn wastes heal attempts and
ends in a vague "needs a human". This module recognises the recording-quality
families UP FRONT so the loop can escalate with a PRECISE, grounded, actionable
reason (and a concrete suggested fix) instead.

Doctrine (never green-wash): every detection here ESCALATES to a human. It never
skips a step, never flips one green, and proposes no auto-fix — it only labels
the failure honestly. It is grounded purely in the recording's OWN captured
steps (labels + verbs + page URLs already on each ``step.observed``); it invents
nothing and reads no live page.

Complements the GEN-TIME duplicate guard in ``confidence.is_consecutive_duplicate``
(which flags the immediately-prior duplicate as CONFIRM so the step still runs):
this is the HEAL-TIME superset — it searches all earlier steps and gates on which
steps actually passed, so a failing redundant step is escalated with the right
reason rather than mis-routed.
"""
from __future__ import annotations

from typing import Any

try:  # reuse the SAME normalisation the gen-time guard uses, for consistency
    from .confidence import _norm, _strip_trailing_punct
except Exception:  # pragma: no cover - defensive fallback if confidence moves
    import re as _re

    def _norm(s: str) -> str:
        return " ".join((s or "").strip().lower().split())

    def _strip_trailing_punct(s: str) -> str:
        return _re.sub(r"[\s:*?.]+$", "", s or "")


_COMMIT_VERBS = {"click", "press", "tap"}
_FILL_VERBS = {"select", "type", "fill", "input", "check"}


def _label_of(step: Any) -> str:
    obs = getattr(step, "observed", None) or {}
    return _norm(_strip_trailing_punct(obs.get("label") or ""))


def _verb_of(step: Any) -> str:
    obs = getattr(step, "observed", None) or {}
    return (obs.get("verb") or "").strip().lower()


def _url_of(step: Any) -> str:
    obs = getattr(step, "observed", None) or {}
    return (obs.get("url") or "").strip()


def _raw_label(step: Any) -> str:
    obs = getattr(step, "observed", None) or {}
    return (obs.get("label") or "").strip()


def classify_recording_quality(*, baseline_step, scenario_steps, passed_step_numbers=None) -> dict | None:
    """Return a recording-quality finding for ``baseline_step``, or None.

    Returns ``{kind, confidence, rationale, suggestion, of_step}`` or None when
    the failure is NOT a recording artifact (let the normal heal routing decide).
    Conservative — fires only on a strong grounded signal. NEVER returns a
    heal/green action: the only outcome is an honest escalation finding.
    """
    if baseline_step is None:
        return None
    steps = list(scenario_steps or [])
    passed = {int(s) for s in (passed_step_numbers or []) if s is not None}
    n = getattr(baseline_step, "step_number", None)
    cl = _label_of(baseline_step)
    cverb = _verb_of(baseline_step)

    # ── duplicate-of-earlier-step (double-capture) ───────────────────────────
    # The failing step re-interacts a control the IMMEDIATELY-PRECEDING interactive
    # step already drove — a double-capture. The second interaction cannot succeed
    # (the control is already past that state) yet it is NOT an app defect.
    #
    # CONSERVATIVE to avoid false positives on legitimately-repeated controls
    # (a wizard "Next"/"Submit" clicked once per page). Require ALL of:
    #   • the current step is a COMMIT verb (click/press/tap);
    #   • the NEAREST-PRECEDING interactive step has the SAME label (a wizard's
    #     next "Next" is preceded by field FILLS with different labels, so it
    #     will NOT match — only a true back-to-back re-capture does);
    #   • NO page change between the two (same observed.url, or both unknown) —
    #     a per-page repeat crosses a page boundary, so it is excluded;
    #   • that earlier step PASSED (when we know which did) — so the duplicate's
    #     failure is genuinely redundant, never a real regression we would hide.
    if cl and cverb in _COMMIT_VERBS and n is not None:
        prior = [
            st
            for st in steps
            if getattr(st, "step_number", None) is not None
            and getattr(st, "step_number") < n
            and _verb_of(st) in (_COMMIT_VERBS | _FILL_VERBS)
        ]
        prior.sort(key=lambda s: getattr(s, "step_number"))
        if prior:
            earlier = prior[-1]  # immediately-preceding interactive step
            sn = getattr(earlier, "step_number")
            same_label = _label_of(earlier) == cl
            cu, eu = _url_of(baseline_step), _url_of(earlier)
            no_nav_between = (not cu and not eu) or (cu == eu)
            passed_ok = (not passed) or (sn in passed)
            if same_label and no_nav_between and passed_ok:
                nav = bool(cu) or (
                    "proceeds to" in (getattr(baseline_step, "expected", "") or "").lower()
                )
                return {
                    "kind": "duplicate_step",
                    "confidence": 0.9,
                    "rationale": (
                        f"step {n} re-interacts the control '{_raw_label(baseline_step)}' "
                        f"that step {sn} already drove on the same page (double-capture)."
                    ),
                    "suggestion": (
                        f"Point step {n} at the real Submit/Next control, or remove it — it "
                        "asserts a navigation the duplicate cannot cause."
                        if nav
                        else f"Remove step {n}, or CONFIRM it is not a double-capture (it does "
                        "not change app state)."
                    ),
                    "of_step": sn,
                }
    return None


def detect_same_url_nav_greenwash(scenario_steps) -> list[dict]:
    """Steps that GREEN-WASH a single-page-app (SPA) navigation.

    The compiler asserts a SUBMIT/Next step navigated by ``toHaveURL(next page)``.
    In an SPA the view changes but the URL does NOT — so when the recorded
    ``next_url`` path equals the page the step is already ON, ``toHaveURL`` is
    trivially true BEFORE and AFTER the click: the step passes without proving the
    transition happened. If, in addition, there is no grounded CONTENT oracle for
    that step (no observed outcome region / no grounded Expected-Result token),
    the step verifies NOTHING.

    Grounded only in observed (url / next_url / after / expected). Walks the steps
    in order: a navigate/page step (which carries ``observed.url``) updates the
    "current page"; a later commit step carries ``observed.next_url``. Returns the
    list of green-wash findings (empty when none) — the caller REFUSES to freeze
    such a green and escalates honestly (never auto-fixes, never green-washes).
    """
    try:  # public path helper + the compiler's OWN content-oracle predicates
        from ..script_factory.locators import url_path
        from ..script_factory.compiler import _OUTCOME_REGION_RX, _grounded_expectation_token
    except Exception:  # pragma: no cover - if those move, fail safe (flag nothing)
        return []
    out: list[dict] = []
    current_path = ""
    for st in (scenario_steps or []):
        obs = getattr(st, "observed", None) or {}
        u = (obs.get("url") or "").strip()
        if u:
            current_path = url_path(u)  # a navigate/page step sets the current page
        next_url = (obs.get("next_url") or "").strip()
        if not next_url or not current_path:
            continue
        if url_path(next_url) != current_path:
            continue  # the URL genuinely changes → toHaveURL is a real oracle
        # SAME-URL transition: does the compiler emit a grounded CONTENT oracle here?
        after = (obs.get("after") or "").strip()
        has_region = bool(_OUTCOME_REGION_RX.search(after)) if after else False
        er = (getattr(st, "expected_result", "") or getattr(st, "expected", "") or "")
        tok = _grounded_expectation_token(er, after) if after else ""
        if has_region or tok:
            continue  # a real content oracle backs the step → not a green-wash
        out.append({
            "step_number": getattr(st, "step_number", None),
            "label": obs.get("label", ""),
            "path": current_path,
        })
    return out


def scenario_missing_submit(scenario_steps) -> dict | None:
    """Scenario-level advisory: the recording fills a form but never submits it.

    Grounded: the last INTERACTIVE step is a fill/select and NO captured commit
    step pins a navigation. Advisory only — surfaced alongside an escalation so
    the human knows the suite may not exercise submission; never a heal action.
    """
    steps = [s for s in (scenario_steps or []) if _verb_of(s) in (_COMMIT_VERBS | _FILL_VERBS)]
    if len(steps) < 2:
        return None
    last = steps[-1]
    has_terminal_commit = any(_verb_of(s) in _COMMIT_VERBS and _url_of(s) for s in steps)
    if _verb_of(last) in _FILL_VERBS and not has_terminal_commit:
        return {
            "kind": "missing_submit",
            "confidence": 0.7,
            "rationale": (
                "the recording fills fields but no captured step submits the form "
                "(no commit pins a navigation)."
            ),
            "suggestion": "add the Submit/Next step so the test actually exercises submission.",
        }
    return None
