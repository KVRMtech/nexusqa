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

INPUT AND ASSERTION ARE RESOLVED DIFFERENTLY, because they fail differently.
``observed_value`` is something the script TYPES, so it is redirected through the
data map without the compiler knowing a member exists. An ``expected`` /
``observed_text`` value is something the page must SHOW — the spec asserts it
rather than reading it from data — so ``apply_member_expectations`` rewrites the
assertion itself, on a copy of the suite, before the case is compiled.

Either way, an unanswerable value stops the run. It is never softened and never
falls back to the value belonging to whoever the crawl ran as.

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
    "apply_member_expectations",
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
                    # A member-derived ASSERTION: this member's answer exists, and
                    # apply_member_expectations rewrites the assertion itself before
                    # the case is compiled. Nothing goes in the data map — the spec
                    # asserts expectations, it does not read them from data.
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


def apply_member_expectations(cases, *, classifications: dict | None,
                              answers: dict | None) -> tuple:
    """Rewrite PROVEN member-derived EXPECTATIONS to the running member's own.

    A value typed into a field is redirected by the data map (``plan_member_data``).
    A value the page is expected to SHOW cannot be — the compiled spec asserts it
    rather than reading it from data — so the assertion itself has to carry this
    member's truth before the case is compiled.

    The rewrite happens on a COPY of the suite handed to the compiler; the stored
    case is never mutated, so what a crawl recorded stays exactly as recorded and
    the change lives only in this run.

    Only ``expected`` and ``observed_text`` are touched, and only where a verdict of
    member-derived was EARNED and this member has an answer. Anything shared keeps
    the recorded wording, so a genuine application regression still fails red.

    Returns ``(cases_for_this_run, [rewrites])``.
    """
    derived = member_derived_keys(classifications)
    sheet = {str(k): v for k, v in (answers or {}).items()}
    if not derived or not sheet:
        return cases, []

    out: list = []
    rewrites: list = []
    for case in (cases or []):
        scenario_id, steps = _steps_of(case)
        if not scenario_id:
            out.append(case)
            continue
        tc = getattr(case, "test_case", None)
        source = dict(tc or {}) if tc is not None else dict(case or {})
        new_steps: list = []
        touched = False
        for step in steps:
            step = dict(step)
            try:
                number = int(step.get("step_number") or 0)
            except (TypeError, ValueError):
                number = 0

            key = f"{scenario_id}:{number}:expected"
            if key in derived:
                answer = _answer_text(sheet.get(key))
                if answer.strip():
                    step["expected_result"] = answer
                    if step.get("expected"):
                        step["expected"] = answer
                    rewrites.append({"value_key": key, "kind": "expected"})
                    touched = True

            key = f"{scenario_id}:{number}:observed_text"
            if key in derived:
                answer = _answer_text(sheet.get(key))
                if answer.strip():
                    observed = dict(step.get("observed") or {})
                    observed["text"] = answer
                    step["observed"] = observed
                    rewrites.append({"value_key": key, "kind": "observed_text"})
                    touched = True

            new_steps.append(step)

        if not touched:
            out.append(case)
            continue
        source["steps"] = new_steps
        # Preserve the row's identity for a stored case; the compiler and the run
        # manifest key off test_case_id, so a rewritten copy must keep it.
        if tc is not None:
            out.append(_RewrittenCase(case, source))
        else:
            out.append(source)
    return out, rewrites


class _RewrittenCase:
    """A stored case row with a per-run test_case body. Proxies every other
    attribute to the original row so nothing downstream can tell the difference,
    and the row itself is left untouched."""

    __slots__ = ("_row", "test_case")

    def __init__(self, row, test_case: dict):
        self._row = row
        self.test_case = test_case

    def __getattr__(self, name):
        return getattr(self._row, name)
