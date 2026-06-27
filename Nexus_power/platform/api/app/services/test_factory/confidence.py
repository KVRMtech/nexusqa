"""Per-step confidence scoring (Phase 2).

Honest, grounded flag — never a guess dressed as certainty.  Each step is scored
``high`` (solid, automate as-is) or ``review`` (a human should glance before it
goes into an automated suite), with a plain-language reason.

Signals (all derived from what was actually captured — no invention):
  * ``inferred`` provenance (negative / boundary / un-captured transition) →
    review: it was derived, not directly observed.
  * the target label appears multiple times in the same page's captured actions
    (ambiguous) → review: we can't yet pinpoint which element (anchor capture,
    Phase 3, will close this).
  * no usable handle captured (neither a label nor a URL) → review.
  * otherwise → high: a directly-observed step with a clear, named element or a
    verifiable URL.

Stored additively on each step as ``confidence`` + ``confidence_reason`` (the
step model allows extras — no schema change).  Display is gated by the same
role/toggle as the evidence column.
"""

from __future__ import annotations

from typing import Iterable

from .generator import PageActionInput, _norm

HIGH = "high"
REVIEW = "review"
# CONFIRM: a value-level disagreement (action stream vs form snapshot). Distinct
# from REVIEW because it must NOT skip the test — the step still runs with the
# snapshot value; we just flag it amber so a human confirms which reading is right.
CONFIRM = "confirm"


def compute_ambiguous_labels(actions: Iterable[PageActionInput]) -> set[str]:
    """Normalized labels that appear on 2+ distinct actions within one page.

    A repeated label on the same page means the visible name alone can't
    uniquely locate the element (e.g. five 'Select' buttons).  Until anchor
    capture lands, those steps are flagged for review rather than trusted.
    """
    per_page: dict[str, dict[str, int]] = {}
    for a in actions:
        label = (a.target_label or "").strip()
        if not label:
            continue
        key = _norm(label)
        if not key:
            continue
        bucket = per_page.setdefault(a.page_visit_id, {})
        bucket[key] = bucket.get(key, 0) + 1
    ambiguous: set[str] = set()
    for bucket in per_page.values():
        for key, count in bucket.items():
            if count > 1:
                ambiguous.add(key)
    return ambiguous


def score_step(step, ambiguous: set[str]) -> tuple[str, str]:
    """Return (level, reason) for one step."""
    prov = (getattr(step, "provenance", "") or "")
    obs = getattr(step, "observed", None) or {}
    label = (obs.get("label") or "").strip()
    url = (obs.get("url") or "").strip()
    nlabel = _norm(label)

    if prov == "inferred":
        return REVIEW, (
            "Derived step — not directly observed in the recording. "
            "Confirm before adding to an automated suite."
        )
    # Assertion-achievability axis: a step that asserts a navigation (this action →
    # the next page's URL) is "solid" ONLY when the recording proves this action
    # caused it. A next_url with no grounded navigation signal is an UNPROVEN causal
    # claim — score it REVIEW so it never counts as a solid step (this is the
    # impossible-transition green-wash the auditor flagged). Belt-and-suspenders:
    # the generator no longer sets an ungrounded next_url, but this stays honest if
    # one ever slips through another path.
    if obs.get("next_url") and not obs.get("navigation_grounded"):
        return REVIEW, (
            "This step asserts a page navigation the recording does not show this "
            "action caused — re-point to the real submit/next control, or confirm."
        )
    has_anchor = bool((obs.get("anchor") or "").strip())
    if nlabel and nlabel in ambiguous and not has_anchor:
        return REVIEW, (
            f"'{label}' appears multiple times on the page — the exact element "
            "isn't pinpointed yet (anchor capture pending)."
        )
    if not label and not url:
        return REVIEW, "No clear element handle was captured — confirm the target."
    # Value-level disagreement: the keystroke (action stream) and the committed
    # (form snapshot) values differ — surface BOTH and ask for confirmation rather
    # than asserting one. Step still runs (CONFIRM, not REVIEW → never skipped).
    vc = obs.get("value_conflict")
    if isinstance(vc, dict) and vc.get("typed") is not None:
        return CONFIRM, (
            f"Two readings of this value disagree — the recording shows the user typed "
            f"'{vc.get('typed')}', but the form snapshot captured '{vc.get('committed')}'. "
            "Using the snapshot value; confirm which one is correct."
        )
    if url and not label:
        return HIGH, "Verified by the page URL."
    return HIGH, "Directly observed with a clear, named element."


def _strip_trailing_punct(s: str) -> str:
    """Drop trailing punctuation/space so 'I confirm …' and 'I confirm ….' match —
    a trailing period is the classic difference between a double-captured label."""
    return (s or "").strip().rstrip(".:;,!?-– ").strip()


def is_consecutive_duplicate(cur, prev) -> bool:
    """True when ``cur`` re-interacts the SAME control as the immediately preceding
    step — the double-capture signature: one control captured once as a select/
    toggle/fill and AGAIN as the trailing commit click, which then gets the page's
    navigation outcome wrongly pinned to it (so a wrong control asserts a nav it
    cannot cause — exactly the mis-recorded step-20 case).

    Conservative, to avoid false positives: requires near-identical labels (modulo
    trailing punctuation) AND that ``cur`` is a click/commit interaction.
    """
    if cur is None or prev is None:
        return False
    cobs = getattr(cur, "observed", None) or {}
    pobs = getattr(prev, "observed", None) or {}
    cl = _norm(_strip_trailing_punct(cobs.get("label") or ""))
    pl = _norm(_strip_trailing_punct(pobs.get("label") or ""))
    if not cl or not pl or cl != pl:
        return False
    cverb = (cobs.get("verb") or "").strip().lower()
    pverb = (pobs.get("verb") or "").strip().lower()
    return cverb in ("click", "press", "tap") and pverb in (
        "select", "click", "press", "tap", "type", "fill", "input")


def annotate(test_case, ambiguous: set[str]) -> tuple[int, int]:
    """Annotate each step in-place with confidence + reason.

    Returns (high_count, review_count).
    """
    high = 0
    review = 0
    prev = None
    for step in (test_case.steps or []):
        level, reason = score_step(step, ambiguous)
        # ── Duplicate-step guard ─────────────────────────────────────────────
        # A control re-interacted immediately (double-capture) is flagged CONFIRM
        # — never REVIEW — so the step STILL RUNS (review/inferred steps get a
        # test.skip(); skipping a mis-recorded "click X again → navigates" would
        # GREEN-WASH the very failure we want surfaced). CONFIRM shows amber, counts
        # in the review tally, and keeps the run honest (fails RED if the pinned
        # navigation never happens). Non-destructive + frozen-compiler-safe; only
        # upgrades a step that otherwise scored HIGH (never masks a stronger flag).
        if level == HIGH and is_consecutive_duplicate(step, prev):
            _pn = getattr(prev, "step_number", None)
            _plabel = ((getattr(prev, "observed", None) or {}).get("label") or "")
            _obs = getattr(step, "observed", None)
            _nav = bool((_obs or {}).get("url")) or \
                "proceeds to" in (getattr(step, "expected", "") or "").lower()
            level = CONFIRM
            reason = (
                f"Possible duplicate of step {_pn}: the same control "
                f"('{_plabel}') was captured twice in a row"
                + ("; this step also asserts a page navigation the duplicate may not "
                   "cause — point it at the real Submit/Next control, or remove it."
                   if _nav else " — confirm it isn't a double-capture.")
            )
            if isinstance(_obs, dict):
                _obs["possible_duplicate"] = {"of_step": _pn, "navigation_pinned": _nav}
        step.confidence = level
        step.confidence_reason = reason
        if level == HIGH:
            high += 1
        else:
            review += 1
        prev = step
    return high, review
