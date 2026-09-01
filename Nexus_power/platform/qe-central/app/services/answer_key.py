"""Canonical ``answer_key`` → contained-explorer FILL contract (design §3.2).

The onboarding wizard / API accept a rich ``answer_key``:

    {
      "fill":        {name_or_keyword: value},   # Data tab — seed form inputs
      "exact":       {accessible_name: value},   # advanced / power users
      "semantic":    {keyword: value},           # advanced
      "regex_rules": [{pattern, value}],         # advanced
      "outcomes":    [...],  "rules": [...],     # Answers tab — value/rule oracle
      "notes":       "..."                        # free text — LLM compile source
    }

But the explorer's ``forms.AnswerKey`` understands ONLY
``{exact, semantic, regex_rules}``.  Historically the wizard emitted
``{notes, answers}``, so ``AnswerKey.from_payload`` resolved everything to an
EMPTY key and the crawler filled nothing — a silent no-op.  This adapter projects
the canonical key onto the explorer contract so forms are actually filled.

Pure + dependency-free (unit-testable with a plain dict) and tolerant: unknown
keys are ignored and it never raises on well-formed mappings.  ``outcomes`` /
``rules`` / ``notes`` are intentionally NOT projected here — they drive the value
oracle (a separate seam), not form filling.
"""
from __future__ import annotations

from typing import Any, Mapping


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def explorer_fill_contract(answer_key: Mapping[str, Any] | None) -> dict:
    """Project a canonical ``answer_key`` onto ``{exact, semantic, regex_rules}``.

    Precedence (highest first): explicit ``exact`` → ``regex_rules`` → ``semantic``
    → the Data-tab ``fill`` map.  ``fill`` entries become ``semantic`` (substring)
    matches — the most forgiving against real labels like ``"Coverage amount ($)"``
    where an exact accessible-name match would miss.  A key already present as
    ``exact`` is never downgraded to ``semantic``.
    """
    ak = dict(answer_key or {})
    exact: dict[str, str] = {}
    semantic: dict[str, str] = {}
    regex_rules: list[dict] = []

    src_exact = ak.get("exact")
    if isinstance(src_exact, Mapping):
        for k, v in src_exact.items():
            if _s(k).strip():
                exact[_s(k)] = _s(v)

    src_semantic = ak.get("semantic")
    if isinstance(src_semantic, Mapping):
        for k, v in src_semantic.items():
            if _s(k).strip():
                semantic[_s(k)] = _s(v)

    for rule in ak.get("regex_rules") or ():
        if not isinstance(rule, Mapping):
            continue
        pattern = _s(rule.get("pattern")).strip()
        if pattern:
            regex_rules.append({"pattern": pattern, "value": _s(rule.get("value"))})

    # Data-tab fill map → semantic (keyword substring on the control name).
    src_fill = ak.get("fill")
    if isinstance(src_fill, Mapping):
        for k, v in src_fill.items():
            key = _s(k).strip()
            if key and key not in exact:
                semantic.setdefault(key, _s(v))

    return {"exact": exact, "semantic": semantic, "regex_rules": regex_rules}


# ─────────────────────────── value-oracle contract ──────────────────────────
# The Answers tab drives a SEPARATE seam from form filling: proving the app's
# OUTPUT is correct (a computed premium, a decline code) against the client's
# expectations.  This projector reads ONLY ``outcomes`` / ``rules`` — never the
# fill data — and normalizes the several shapes the wizard/API may send into a
# single value-expectation record the factory can compile into a grounded
# assertion.  Pure + tolerant, exactly like :func:`explorer_fill_contract`.


def _as_number(value: Any) -> float | None:
    """Best-effort numeric parse (tolerant of ``"$28.40"``, ``"1,234"``, ``"18%"``).

    Returns ``None`` when there is no single number to read — the caller then
    treats the expectation as a string/contains match, never a silent numeric 0.
    """
    if isinstance(value, bool):  # bool is an int subclass — never a "number" here
        return None
    if isinstance(value, (int, float)):
        return float(value)
    import re

    text = _s(value).strip()
    if not text:
        return None
    # Strip currency/label noise, keep sign + digits + one decimal point.
    cleaned = re.sub(r"[,\s$%€£]", "", text)
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(m.group(0)) if m else None


def _normalize_outcome(field: str, raw_expected: Any, extra: Mapping | None = None) -> dict | None:
    """One ``{field, when, expected, tolerance, source_hint, match}`` record.

    ``match`` is ``numeric`` when the expected value reads as a number (so the
    compiler emits a tolerant numeric compare), else ``exact`` for a scalar or
    ``contains`` when the author asked for a substring.  Returns ``None`` for an
    unusable expectation (blank field, or a value we cannot ground — e.g. a list
    with no scalar) so the oracle stays honest rather than green-washing.
    """
    extra = dict(extra or {})
    field = _s(field).strip()
    if not field:
        return None
    when = extra.get("when")
    when = dict(when) if isinstance(when, Mapping) else {}
    tolerance = _as_number(extra.get("tolerance"))
    source_hint = _s(extra.get("source_hint") or extra.get("selector") or "").strip()
    requested = _s(extra.get("match")).strip().lower()

    number = _as_number(raw_expected)
    if requested == "contains" or isinstance(raw_expected, (list, tuple, set)):
        # A list/"contains" expectation grounds as a substring of the first
        # scalar member (multi-value invariants are a P3 rule, not a point value).
        members = raw_expected if isinstance(raw_expected, (list, tuple, set)) else [raw_expected]
        scalar = next((m for m in members if _s(m).strip()), None)
        if scalar is None:
            return None
        return {"field": field, "when": when, "expected": _s(scalar),
                "tolerance": None, "source_hint": source_hint, "match": "contains"}
    if number is not None and requested in ("", "numeric"):
        return {"field": field, "when": when, "expected": number,
                "tolerance": tolerance, "source_hint": source_hint, "match": "numeric"}
    scalar = _s(raw_expected).strip()
    if not scalar:
        return None
    return {"field": field, "when": when, "expected": scalar,
            "tolerance": None, "source_hint": source_hint, "match": "exact"}


def value_oracle_contract(answer_key: Mapping[str, Any] | None) -> dict:
    """Project a canonical ``answer_key`` onto ``{outcomes: [...], rules: [...]}``.

    Accepts every shape the wizard/API may store for ``outcomes``:

    * a **list** of structured records — ``[{field, equals|expected|value, when,
      tolerance, source_hint, match}]`` (the authored form); OR
    * a **flat map** ``{field: expected}`` (what the free-text Answers box compiles
      to today, e.g. ``{"monthly_premium": 28.40}``).

    The parse-failure sentinel ``{"_raw": "..."}`` is dropped (free text cannot be
    grounded — that is Phase-2 LLM authoring, not a runtime oracle).  ``rules``
    (Phase-3 invariants) are passed through normalized-minimally so a later phase
    can consume them without a schema change here.
    """
    ak = dict(answer_key or {})
    outcomes: list[dict] = []

    src = ak.get("outcomes")
    if isinstance(src, Mapping):
        for k, v in src.items():
            if _s(k).strip() == "_raw":
                continue  # free-text fallback — ungroundable
            rec = _normalize_outcome(k, v)
            if rec is not None:
                outcomes.append(rec)
    elif isinstance(src, (list, tuple)):
        for item in src:
            if not isinstance(item, Mapping):
                continue
            field = item.get("field") or item.get("name") or item.get("label")
            expected = item.get("equals")
            if expected is None:
                expected = item.get("expected", item.get("value"))
            rec = _normalize_outcome(_s(field), expected, item)
            if rec is not None:
                outcomes.append(rec)

    rules: list[dict] = []
    for rule in ak.get("rules") or ():
        if isinstance(rule, Mapping) and _s(rule.get("kind") or rule.get("type")).strip():
            rules.append(dict(rule))

    return {"outcomes": outcomes, "rules": rules}
