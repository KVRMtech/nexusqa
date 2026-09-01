"""SCENARIOS DERIVED FROM THE CATALOGUE — positive, negative, boundary.

Once the Master Catalog holds a question's real shape — is it required, what
answers does it offer, what does the page itself declare about length, range,
step and pattern — the interesting test cases stop being a matter of invention.
They are DERIVABLE: a required field must reject empty, a field with a declared
maximum must accept the maximum and reject one past it, a choice must accept the
answers it offers.

THE DISCIPLINE, and the reason this module is small and boring:

    A scenario is derived ONLY from a rule the crawl actually OBSERVED.

Nothing here guesses that a field is probably an email, or that a number is
probably a percentage. If the page never declared a bound, no boundary case
exists for that field, and the summary says so rather than padding the count. An
invented rule produces a test that asserts something the application never
promised — a red that is our error and a green that means nothing.

The completeness marker earned in the catalogue is load-bearing here. A question
whose option list was CLIPPED (``options_total > len(options)``) cannot support a
scenario claiming to cover its answers, because the answers are not all known.
That is refused explicitly rather than quietly generated from the prefix.

Values are SPECIFICATIONS, not literals: ``{"strategy": "length", "chars": 51}``
says what to build, leaving the building to the layer that already owns value
generation. Option LABELS are product UI text (the same class of data the
catalogue already stores) and are carried through as-is.

Pure functions over catalogue rows — no LLM, no network, no DB.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# ── scenario kinds ──────────────────────────────────────────────────────────
KIND_POSITIVE = "positive"
KIND_NEGATIVE = "negative"
KIND_BOUNDARY = "boundary"

#: Why a scenario exists — the observed rule it was derived from. Carried on
#: every scenario so a reader can trace an assertion back to the page fact that
#: justifies it, and so a scenario whose rule later disappears can be retired.
BASIS_REQUIRED = "required"
BASIS_OPTIONS = "options"
BASIS_MIN_LENGTH = "minlength"
BASIS_MAX_LENGTH = "maxlength"
BASIS_MIN = "min"
BASIS_MAX = "max"
BASIS_PATTERN = "pattern"

#: Types whose declared min/max are NUMERIC bounds rather than string lengths.
_NUMERIC_TYPES = frozenset({"number", "range", "currency_amount", "percent",
                            "quantity", "age"})


def _num(value: Any) -> float | None:
    """A finite number from a page-declared attribute, else None (never a guess)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _int_attr(value: Any) -> int | None:
    n = _num(value)
    if n is None or n != int(n) or n < 0:
        return None
    return int(n)


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _scenario(question: Mapping[str, Any], *, kind: str, basis: str,
              intent: str, value: dict[str, Any],
              expect: str) -> dict[str, Any]:
    return {
        "question_id": str(question.get("question_id") or ""),
        "field": str(question.get("name") or "")[:200],
        "kind": kind,
        "basis": basis,
        "intent": intent,
        "value": value,
        # What the APPLICATION is expected to do. "accept"/"reject" only —
        # never a message string, because the crawl did not observe one and
        # asserting invented copy is how a suite starts failing on wording.
        "expect": expect,
    }


def _options_are_complete(question: Mapping[str, Any]) -> bool:
    """True only when the stored answers ARE all the answers.

    ``options_total`` is what the catalogue records the page as offering. When it
    exceeds what was stored, the list is a prefix — and a scenario built from a
    prefix cannot honestly claim to exercise the question's answers.
    """
    options = [o for o in (question.get("options") or []) if str(o).strip()]
    if not options:
        return False
    total = question.get("options_total")
    try:
        declared = int(total) if total is not None and not isinstance(total, bool) else 0
    except (TypeError, ValueError):
        declared = 0
    return declared <= len(options)


def derive_field_scenarios(question: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every scenario the OBSERVED rules of one catalogued question justify.

    Returns ``[]`` for a question that declared nothing testable — which is a
    real and common answer, not a failure. Padding it with a nominal "fill it in"
    case would inflate the count without adding a single assertion.
    """
    if not isinstance(question, Mapping):
        return []
    qtype = str(question.get("type") or "").strip().lower()
    semantic = str(question.get("semantic_type") or "").strip().lower()
    validation = question.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}
    out: list[dict[str, Any]] = []

    # ── the answers a choice offers ─────────────────────────────────────────
    options = [str(o) for o in (question.get("options") or []) if str(o).strip()]
    if options and _options_are_complete(question):
        out.append(_scenario(
            question, kind=KIND_POSITIVE, basis=BASIS_OPTIONS,
            intent=f"accepts each of the {len(options)} answers it offers",
            value={"strategy": "each_option", "options": options},
            expect="accept"))
    elif options:
        # A prefix is still useful — it just cannot claim to be the answer set.
        out.append(_scenario(
            question, kind=KIND_POSITIVE, basis=BASIS_OPTIONS,
            intent=f"accepts {len(options)} of the "
                   f"{question.get('options_total')} answers it offers "
                   f"(the captured list is incomplete)",
            value={"strategy": "each_option", "options": options,
                   "incomplete": True},
            expect="accept"))

    # ── required ────────────────────────────────────────────────────────────
    if question.get("required"):
        out.append(_scenario(
            question, kind=KIND_NEGATIVE, basis=BASIS_REQUIRED,
            intent="is rejected when left empty",
            value={"strategy": "empty"},
            expect="reject"))

    # ── declared bounds ─────────────────────────────────────────────────────
    numeric = qtype in _NUMERIC_TYPES or semantic in _NUMERIC_TYPES
    lo, hi = _num(validation.get("min")), _num(validation.get("max"))
    if numeric and lo is not None:
        out.append(_scenario(
            question, kind=KIND_BOUNDARY, basis=BASIS_MIN,
            intent=f"accepts its declared minimum ({_fmt(lo)})",
            value={"strategy": "number", "number": lo}, expect="accept"))
        out.append(_scenario(
            question, kind=KIND_BOUNDARY, basis=BASIS_MIN,
            intent=f"rejects one step below the minimum ({_fmt(lo)})",
            value={"strategy": "number_below_min", "min": lo,
                   "step": _num(validation.get("step"))}, expect="reject"))
    if numeric and hi is not None:
        out.append(_scenario(
            question, kind=KIND_BOUNDARY, basis=BASIS_MAX,
            intent=f"accepts its declared maximum ({_fmt(hi)})",
            value={"strategy": "number", "number": hi}, expect="accept"))
        out.append(_scenario(
            question, kind=KIND_BOUNDARY, basis=BASIS_MAX,
            intent=f"rejects one step above the maximum ({_fmt(hi)})",
            value={"strategy": "number_above_max", "max": hi,
                   "step": _num(validation.get("step"))}, expect="reject"))

    min_len = _int_attr(validation.get("minlength"))
    max_len = _int_attr(validation.get("maxlength"))
    if min_len:
        out.append(_scenario(
            question, kind=KIND_BOUNDARY, basis=BASIS_MIN_LENGTH,
            intent=f"accepts its declared minimum length ({min_len})",
            value={"strategy": "length", "chars": min_len}, expect="accept"))
        out.append(_scenario(
            question, kind=KIND_BOUNDARY, basis=BASIS_MIN_LENGTH,
            intent=f"rejects one character below the minimum ({min_len})",
            value={"strategy": "length", "chars": min_len - 1}, expect="reject"))
    if max_len:
        out.append(_scenario(
            question, kind=KIND_BOUNDARY, basis=BASIS_MAX_LENGTH,
            intent=f"accepts its declared maximum length ({max_len})",
            value={"strategy": "length", "chars": max_len}, expect="accept"))
        # NOTE deliberately no "one character over" case. A maxlength input
        # TRUNCATES rather than rejecting, so the browser never lets the invalid
        # value exist — a test asserting rejection would fail against a correct
        # application. Only the reachable half of the boundary is generated.

    if str(validation.get("pattern") or "").strip():
        out.append(_scenario(
            question, kind=KIND_NEGATIVE, basis=BASIS_PATTERN,
            intent="is rejected by its declared format rule",
            value={"strategy": "pattern_violation",
                   "pattern": str(validation["pattern"])[:200]},
            expect="reject"))

    return out


def derive_catalog_scenarios(
    catalog: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Every derivable scenario for a Master Catalog, plus an honest shortfall.

    Returns ``{"scenarios": [...], "summary": {...}}`` where the summary reports
    how many questions yielded nothing and WHY. That number is the real coverage
    signal: a catalogue of four hundred questions that produces forty scenarios is
    telling you the application declares very little about itself, which is a
    finding — not something to hide behind a large-looking total.
    """
    cat = catalog if isinstance(catalog, Mapping) else {}
    questions = cat.get("questions")
    if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
        questions = []

    scenarios: list[dict[str, Any]] = []
    no_rules = 0
    incomplete_options = 0
    by_kind: dict[str, int] = {}

    for q in questions:
        if not isinstance(q, Mapping):
            continue
        derived = derive_field_scenarios(q)
        if not derived:
            no_rules += 1
            continue
        if q.get("options") and not _options_are_complete(q):
            incomplete_options += 1
        for s in derived:
            by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1
        scenarios.extend(derived)

    return {
        "scenarios": scenarios,
        "summary": {
            "questions": len(questions),
            "scenarios": len(scenarios),
            "by_kind": by_kind,
            # Not a failure — a measurement. These questions declared no rule the
            # page itself asserts, so no case can be derived without inventing
            # one, and inventing one is how a suite starts testing our guesses
            # instead of the application.
            "questions_without_rules": no_rules,
            # Questions whose answer set was clipped: their option scenarios
            # cannot claim to cover every answer.
            "questions_with_incomplete_options": incomplete_options,
        },
    }


__all__ = [
    "KIND_POSITIVE", "KIND_NEGATIVE", "KIND_BOUNDARY",
    "BASIS_REQUIRED", "BASIS_OPTIONS", "BASIS_MIN_LENGTH", "BASIS_MAX_LENGTH",
    "BASIS_MIN", "BASIS_MAX", "BASIS_PATTERN",
    "derive_field_scenarios", "derive_catalog_scenarios",
]
