"""Requirement -> Step -> Assertion Traceability Matrix (RTM).

Pure, $0, no-LLM, no-migration, ADDITIVE. This module produces a READ-ONLY
artifact: it NEVER changes a pass/fail verdict and never touches the frozen
deterministic verdict/heal core.

The "requirement" here is the DEMONSTRATED recorded behavior — the observed
evidence Nexus captured. For each ``ProductionTestStep`` we join:

  * the recording evidence anchors (provenance / confidence / observed label /
    frame screenshot), read via the compiler's own pure helpers
    (``_observed`` / ``_provenance`` / ``_confidence`` — compiler.py:126-135),
  * to the EXACT ``await expect(...)`` oracle lines the compiler emits for that
    same step.

CRITICAL ANTI-DRIFT RULE: the assertion codes are NOT hand-written. They are
derived by REUSING the compiler's own pure emission — ``_action_lines``
(compiler.py:300) — and FILTERING to the ``await expect(`` lines. If we forked
and hand-wrote assertion strings, the RTM would become a lie the instant the
compiler changed (compile_case would emit different code than the RTM claims).
By reusing ``_action_lines`` the RTM is byte-identical to ``compile_case`` by
construction.

Honesty rule (mirrors fidelity.py:49 + compiler.py:570): a step whose
provenance is 'inferred' OR confidence is 'review' is one the compiler SKIPS
(emits ``test.skip`` for) — it is ``unproven=True``. A step whose only oracle is
non-grounded is also ``unproven=True`` (we never claim a non-grounded check
proves the requirement).
"""

from __future__ import annotations

import re
from typing import Iterable

# Reuse the compiler's OWN pure emission + evidence helpers. Reusing these (not
# re-implementing them) is what guarantees the RTM never drifts from the code
# that actually compiles + runs.
from ..script_factory.compiler import (
    _action_lines,
    _assertion_from_expected_result,  # noqa: F401  (documented in the anti-drift contract)
    _confidence,
    _observed,
    _provenance,
)

# An emitted assertion line, optionally trailed by a `// comment` the compiler
# appends. We strip the comment for display but classify on the code part.
_EXPECT_PREFIX = "await expect("
_COMMENT_RX = re.compile(r"\s*//.*$")


def _strip_comment(line: str) -> str:
    """Remove a trailing `// ...` comment from an emitted line (kept off the
    code so a comment-only change never reads as drift)."""
    return _COMMENT_RX.sub("", line).strip()


def _oracle_kind(code: str) -> tuple[str, bool]:
    """Classify one ``await expect(...)`` assertion -> (oracle_kind, grounded).

    Mapping mirrors what the compiler actually emits:
      * navigation     — toHaveURL              (grounded: recorded next page)
      * value-oracle   — toHaveValue(/.../i)    (grounded: committed value token)
      * value-presence — not.toHaveValue('')    (grounded but WEAK — tolerant
                          no-op-fill guard; not a specific value proof)
      * state          — toBeChecked            (grounded: radio/checkbox state)
      * outcome-region — getByText visibility   (grounded: observed outcome region)
      * a11y           — toBeVisible(getByLabel)(grounded: field-present check)
      * presence       — toBeVisible (other)    (non-grounded best-effort)
    """
    c = code
    if ".toHaveURL(" in c:
        return ("navigation", True)
    if ".not.toHaveValue(" in c:
        # Tolerant "a value was committed" guard — real + grounded, but weak.
        return ("value-presence", True)
    if ".toHaveValue(" in c:
        return ("value-oracle", True)
    if ".toBeChecked(" in c:
        return ("state", True)
    if ".toBeVisible(" in c:
        # getByLabel visibility = the assert_required field-present oracle (a11y);
        # getByText visibility = the observed outcome region. Both are grounded
        # in recorded evidence. Any other toBeVisible is non-grounded best-effort.
        if "getByLabel(" in c:
            return ("a11y", True)
        if "getByText(" in c:
            return ("outcome-region", True)
        return ("presence", False)
    # Unknown expect() shape — treat as non-grounded so it can never inflate the
    # "proven" claim.
    return ("assertion", False)


def _emitted_assertions(step, field_meta: dict) -> list[dict]:
    """The EXACT ``await expect(...)`` oracles the compiler emits for this step.

    Anti-drift: produced by FILTERING the compiler's own ``_action_lines`` output
    — never hand-written. ``code`` is the comment-stripped assertion exactly as it
    appears in the compiled spec, so it is a byte-identical SUBSET of the spec's
    lines."""
    out: list[dict] = []
    for raw in _action_lines(step, field_meta):
        if _EXPECT_PREFIX not in raw:
            continue
        code = _strip_comment(raw)
        if not code:
            continue
        kind, grounded = _oracle_kind(code)
        out.append({"code": code, "oracle_kind": kind, "grounded": grounded})
    return out


def build_rtm(tc, field_meta: dict) -> dict:
    """Build the Requirement -> Step -> Assertion Traceability Matrix for one
    ``ProductionTestCase``.

    Pure + $0 + read-only. Each row ties a recorded step's evidence anchors to the
    EXACT compiler-emitted assertions for that step (reused from ``_action_lines``,
    never re-authored), tags each assertion's oracle kind + grounded flag, and
    marks the step ``unproven`` when the compiler would skip it (inferred /
    review) or when no GROUNDED oracle backs it.

    NEVER changes a verdict — it only reports what the owned, compiled code
    actually verifies."""
    field_meta = field_meta or {}
    steps = list(getattr(tc, "steps", None) or [])

    rows: list[dict] = []
    for step in steps:
        observed = _observed(step)
        provenance = _provenance(step)
        confidence = _confidence(step)

        # Compiler honesty: provenance=='inferred' or confidence=='review' is a
        # step the compiler SKIPS (compiler.py:570 / fidelity.py:49). For those we
        # emit NO assertions (the compiled spec emits test.skip, not expect()).
        compiler_skips = provenance == "inferred" or confidence == "review"
        if compiler_skips:
            emitted: list[dict] = []
        else:
            emitted = _emitted_assertions(step, field_meta)

        # Unproven when the compiler skips the step, OR when nothing GROUNDED
        # backs it (no grounded oracle => the requirement is not actually proven).
        has_grounded = any(a["grounded"] for a in emitted)
        unproven = compiler_skips or not has_grounded

        rows.append({
            "step_number": getattr(step, "step_number", None),
            "action": (getattr(step, "action", "") or "").strip(),
            "provenance": provenance,
            "confidence": confidence,
            "observed_label": (observed.get("label", "") or "").strip(),
            "frame_ref": getattr(step, "screenshot", None) or "",
            "emitted_assertions": emitted,
            "unproven": unproven,
        })

    return {
        "test_id": getattr(tc, "test_id", "") or "",
        "name": (getattr(tc, "name", None) or "Generated test").strip(),
        "expected_outcome": (getattr(tc, "expected_outcome", "") or "").strip(),
        "rows": rows,
    }


__all__ = ["build_rtm"]
