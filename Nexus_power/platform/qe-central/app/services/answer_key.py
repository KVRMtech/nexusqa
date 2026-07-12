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
