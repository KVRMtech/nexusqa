"""Member-data resolution — run a recorded suite as the member who is actually running.

A crawl-generated case carries the values observed during the crawl, baked in as
literals. Replayed as a different member those literals are that OTHER member's
data, and the compiled oracle asserts them against the value it just typed — so
the run goes green having tested the wrong person. This module is what makes the
running member's own values reach the script instead, and what refuses the run
when they are not known.

Two outputs, and the second matters more than the first:

  * ``data_by_test`` — per-test overrides in the key space the compiled spec
    already reads. The compiler emits ``(D['<label-slug>'] ?? '<recorded>')`` for
    every committed field, so supplying that key redirects the fill without the
    compiler knowing a member exists.
  * ``missing`` — every value this suite treats as belonging to a person for which
    the running member has no answer. A caller that ignores this list has
    reintroduced the original defect: the script silently falls back to the
    recorded literal, which is another member's data.

THREE KEY SPACES MEET HERE, and conflating them is the whole difficulty:

    value_key   "<scenario>:<step>:<kind>"   how a classification and an answer
                                             are stored (persona_diff, answer sheet)
    slot name   "member_number"              how a credential card names a login field
    data key    "member-number"              how the COMPILED SPEC names an override
                                             (a slug of the field's visible LABEL)

Only ``observed_value`` entries correspond to something the script types, so only
those produce an override. A member-derived *expectation* cannot be substituted —
it is asserted, not filled — so it can only ever block. That asymmetry is
deliberate: an unanswerable assertion must stop the run, never soften it.

Classification is EARNED upstream by comparing two members on the same journey
(``persona_diff.diff_two_personas``); nothing here inspects a field's name to
decide whether it is identity. Pure — no DB, no I/O.
"""
from __future__ import annotations

from . import persona_diff

# Only this kind is a value the script COMMITS into a field, so only this kind can
# be redirected by a data override. The others are assertions.
KIND_INPUT = "observed_value"

__all__ = [
    "plan_member_data",
    "member_derived_keys",
    "KIND_INPUT",
]


def _answer_text(entry) -> str:
    """The member's value, from either shape an answer sheet is handed to us in.

    ``persona_store.get_expected_values`` returns ``{value_key: {expected_value,
    source}}``; callers with a flat ``{value_key: value}`` map are also supported.
    Without this the dict would stringify into the override and the script would
    type its repr into the field — a wrong value that still looks committed."""
    if isinstance(entry, dict):
        return str(entry.get("expected_value") or "")
    return "" if entry is None else str(entry)


def _steps_of(case) -> tuple:
    """(scenario_id, [step dicts]) for a stored case row or a bare test-case dict."""
    tc = getattr(case, "test_case", None)
    tc = dict(tc or {}) if tc is not None else dict(case or {})
    scenario_id = str(getattr(case, "test_case_id", "") or tc.get("test_id") or "")
    steps = [s for s in (tc.get("steps") or []) if isinstance(s, dict)]
    return scenario_id, steps


def member_derived_keys(classifications: dict | None) -> set:
    """The value_keys this artifact has PROVEN belong to a person.

    Anything unclassified is deliberately absent: an unknown value is not treated
    as identity (that would block every run on first use) and not treated as
    shared either — it simply keeps the recorded literal, exactly as today. Only a
    proven member-derived value changes behaviour."""
    out = set()
    for key, entry in (classifications or {}).items():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("class") or "") == persona_diff.CLASS_MEMBER_DERIVED:
            out.add(str(key))
    return out


def plan_member_data(cases, *, classifications: dict | None,
                     answers: dict | None, data_key) -> dict:
    """Resolve the running member's values for a suite, and list what is unknown.

    ``cases``           the selected suite (stored rows or test-case dicts)
    ``classifications`` value_key -> {class, ...}, earned by a two-member diff
    ``answers``         value_key -> this member's own value (their answer sheet)
    ``data_key``        callable(label) -> the compiled spec's override key. Injected
                        rather than imported so this module stays pure and the key
                        derivation cannot drift out of step silently.

    Returns ``{data_by_test, resolved, missing, member_derived_total}``.
    """
    derived = member_derived_keys(classifications)
    sheet = {str(k): v for k, v in (answers or {}).items()}

    data_by_test: dict = {}
    resolved: list = []
    missing: list = []

    for case in (cases or []):
        scenario_id, steps = _steps_of(case)
        if not scenario_id:
            continue
        for step in steps:
            try:
                number = int(step.get("step_number") or 0)
            except (TypeError, ValueError):
                number = 0
            observed = step.get("observed") or {}
            label = str(observed.get("label") or "").strip()

            for kind in ("expected", KIND_INPUT, "observed_text"):
                key = f"{scenario_id}:{number}:{kind}"
                if key not in derived:
                    continue
                answer = _answer_text(sheet.get(key))
                # An empty answer is NOT an answer. A member whose value is
                # genuinely blank must be recorded as blank deliberately, not
                # inferred from a missing row.
                if answer.strip() == "":
                    missing.append({"value_key": key, "scenario_id": scenario_id,
                                    "step_number": number, "kind": kind,
                                    "label": label})
                    continue
                if kind != KIND_INPUT:
                    # A member-derived ASSERTION. We have this member's answer, but
                    # the compiled spec asserts expectations rather than reading them
                    # from the data map, so there is nothing to override here. It is
                    # recorded as resolved so it does not block; substituting it is
                    # the separate input-vs-assertion phase.
                    resolved.append({"value_key": key, "kind": kind,
                                     "override_key": "", "label": label})
                    continue
                override_key = str(data_key(label) or "") if label else ""
                if not override_key:
                    # Classified as this member's data, but the step gives no label,
                    # so the compiled spec has no override key to redirect. Silently
                    # running would type the other member's value: refuse instead.
                    missing.append({"value_key": key, "scenario_id": scenario_id,
                                    "step_number": number, "kind": kind,
                                    "label": label, "reason": "no_override_key"})
                    continue
                data_by_test.setdefault(scenario_id, {})[override_key] = str(answer)
                resolved.append({"value_key": key, "kind": kind,
                                 "override_key": override_key, "label": label})

    return {
        "data_by_test": data_by_test,
        "resolved": resolved,
        "missing": missing,
        "member_derived_total": len(resolved) + len(missing),
    }
