"""Value harvest — learn which values belong to a person, by observing two of them.

Classification is what makes running a suite as another member possible at all: a
value proven to belong to a person must come from THAT person, while a value the
application shows everyone must keep asserting what was recorded. Until now the
classifier existed but had to be hand-fed a pair of observed maps; nothing turned
what a run actually saw into that input, so in practice nothing was ever
classified and every value stayed pinned to the crawl member.

This module closes that loop:

  * ``harvest(cases)`` reads the values a suite observed, keyed exactly as a
    classification and an answer sheet are keyed (``<scenario>:<step>:<kind>``).
  * ``compare(...)`` turns two members' harvests into classifications, delegating
    the judgement to ``persona_diff.diff_two_personas`` — differs between members
    is member-derived, identical is app-constant, differs against a member's own
    control run is volatile, present for only one is structural.

EARNED, NOT GUESSED. Nothing here inspects a field's name, label or type to decide
whether it is identity. A value becomes member-derived only because two members
were observed to see different things at the same point in the same journey. That
is the whole reason this is a diff and not a heuristic: a vocabulary would be
wrong for the next client, and silently wrong.

WHAT IT REFUSES. A key observed for only one of the two members is reported as
structural, never as shared — absence of evidence for the second member is not
evidence they agree. And a comparison against a member's own control run isolates
values that simply move over time, so a timestamp is not mistaken for identity.

Pure — no DB, no I/O. The caller loads and persists.
"""
from __future__ import annotations

from . import persona_diff

# The three faces of a step that can carry a value, and the key suffix each uses.
# ``observed_value`` is what was typed; the other two are what the page showed.
KINDS = ("expected", "observed_value", "observed_text")

__all__ = ["harvest", "compare", "KINDS"]


def _case_parts(case) -> tuple:
    """(scenario_id, [steps]) from a stored case row or a bare test-case dict."""
    tc = getattr(case, "test_case", None)
    tc = dict(tc or {}) if tc is not None else dict(case or {})
    scenario_id = str(getattr(case, "test_case_id", "") or tc.get("test_id") or "")
    steps = [s for s in (tc.get("steps") or []) if isinstance(s, dict)]
    return scenario_id, steps


def harvest(cases) -> dict:
    """value_key -> the value this suite observed, for every step that carries one.

    The key is ``<scenario>:<step>:<kind>``, identical to the key a classification
    and an answer sheet use, so a harvest can be stored as a member's answers and
    compared against another member's without any translation."""
    out: dict = {}
    for case in (cases or []):
        scenario_id, steps = _case_parts(case)
        if not scenario_id:
            continue
        for step in steps:
            try:
                number = int(step.get("step_number") or 0)
            except (TypeError, ValueError):
                number = 0
            observed = step.get("observed") or {}
            values = {
                "expected": step.get("expected_result") or step.get("expected"),
                "observed_value": observed.get("value"),
                "observed_text": observed.get("text"),
            }
            for kind in KINDS:
                value = values.get(kind)
                if value is None or str(value).strip() == "":
                    continue
                out[f"{scenario_id}:{number}:{kind}"] = str(value)
    return out


def _flatten(sheet: dict | None) -> dict:
    """Accept either a flat ``{key: value}`` map or the answer-sheet shape the
    store returns (``{key: {expected_value, source}}``)."""
    out: dict = {}
    for key, entry in (sheet or {}).items():
        if isinstance(entry, dict):
            value = entry.get("expected_value")
        else:
            value = entry
        if value is None:
            continue
        out[str(key)] = str(value)
    return out


def compare(*, observed_a: dict | None, observed_b: dict | None,
            control_a: dict | None = None,
            identity_values: set | None = None) -> dict:
    """Classify every value two members were observed to see.

    ``observed_a`` / ``observed_b`` are the two members' harvests on the SAME
    journey; ``control_a`` is an optional second run of member A, which lets a
    value that merely drifts over time be recognised as volatile rather than as
    belonging to a person. ``identity_values`` are member A's own credential-ish
    values, so a page echoing them back is not mistaken for app-constant.

    Returns ``{value_key: {class, evidence, detail?}}`` ready to persist. Only the
    verdicts are returned — the caller decides what to store and when."""
    a = _flatten(observed_a)
    b = _flatten(observed_b)
    if not a or not b:
        # One side unobserved: there is nothing to compare, and guessing from a
        # single member is precisely the heuristic this design refuses.
        return {}
    classified = persona_diff.diff_two_personas(
        a, b, control_a=_flatten(control_a) if control_a else None)
    if identity_values:
        # A value that is simply the member's own identifier echoed back is
        # member-derived by construction; let the existing single-persona pass
        # sharpen anything the diff left ambiguous.
        echo = persona_diff.classify_single_persona(a, set(identity_values))
        for key, entry in (echo or {}).items():
            if entry.get("class") == persona_diff.CLASS_MEMBER_DERIVED:
                classified[key] = entry
    return classified
