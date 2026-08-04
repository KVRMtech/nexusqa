"""Advance/commit vocabulary — qe-central's mirror of the explorer's packs.

MIRROR LAW: this file's ``LANGUAGE_PACKS`` must stay data-identical to
``qe-explorer/app/vocab.py`` — the services share no library, so the
vocabulary is deliberately duplicated and parity is pinned by the mirrored
``test_commit_vocabulary_parity_pin`` tests in BOTH suites (they assert the
same compiled pattern literal). Change BOTH files or neither.

qe-central uses the COMMIT union as the server-side eligibility veto in
``advance_agent`` (defense in depth: a commit-labeled control never reaches
the LLM prompt, whoever calls the endpoint). Union across packs fails
CLOSED — adding a language only ever widens the veto.
"""
from __future__ import annotations

import re

LANGUAGE_PACKS: dict[str, dict[str, list[str]]] = {
    "en": {
        "advance": ["next", "continue", "proceed", "forward"],
        "commit": [
            "submit", "send", "pay", "paying", "paid", "payment", "payments",
            "buy", "buying", "purchase", "purchasing", "order", "checkout",
            r"check\s*out", r"place\s*order", "confirm", "finish", "complete",
            "done", "agree", "accept", "sign", "book", "reserve", "schedule",
            "activate", "create", "register", "subscribe", "delete", "cancel",
            "remove", "apply",
        ],
        "destination_prepositions": ["to"],
    },
}


def _union(kind: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for pack in LANGUAGE_PACKS.values():
        for word in pack.get(kind, ()):  # pragma: no branch
            if word not in seen:
                seen.add(word)
                out.append(word)
    return out


def compile_advance_re() -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(_union("advance")) + r")\b", re.I)


def compile_commit_re() -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(_union("commit")) + r")\b", re.I)


ADVANCE_RE = compile_advance_re()
COMMIT_RE = compile_commit_re()
