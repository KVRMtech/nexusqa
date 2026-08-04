"""Wizard advance / commit vocabulary — per-language packs, union-compiled.

The advance and commit word lists stop being inline regex literals and become
DATA: one pack per language, compiled at import into union patterns.  A word
is an advance word if it is one in ANY supported language; commit likewise.
Union matching removes per-page language detection as a failure point, and a
wider commit union only ever fails CLOSED (more vetoes, never fewer).

Pack law (Release B Phase 5 of E2E_ADVANCE_HARDENING_PLAN.md):
  * new packs are added from fleet telemetry (which languages actually hit
    Tier 3), never guessed;
  * the ``en`` pack is order-preserving with the historical inline patterns,
    so the compiled patterns are byte-identical to what every existing test
    and the qe-central parity pin already assert;
  * qe-central mirrors this data in
    ``app/services/advance_vocab.py`` (the services share no library) —
    parity is pinned by the mirrored ``test_commit_vocabulary_parity_pin``
    tests in BOTH suites. Change BOTH files or neither.

``destination_prepositions`` power the Tier-2 shape rule: a commit word is
tolerated in an advance label ONLY as a destination ("Continue to Payment"
navigates; "Continue & Place Order" commits — it ends the walk instead).
"""
from __future__ import annotations

import re

#: Per-language word packs. Entries are regex alternatives (word-boundary
#: wrapped at compile time), so multi-word forms use ``\s*`` joiners.
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
    """All alternatives of ``kind`` across every pack, order-preserving and
    de-duplicated (first pack wins the position — 'en' stays byte-stable)."""
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


def compile_destination_re() -> re.Pattern[str]:
    return re.compile(
        r"\b(" + "|".join(_union("destination_prepositions")) + r")\b", re.I)


ADVANCE_RE = compile_advance_re()
COMMIT_RE = compile_commit_re()
DESTINATION_RE = compile_destination_re()


def is_destination_advance(name: str) -> bool:
    """The Tier-2 shape rule: an advance label may carry a commit word ONLY as
    a destination — advance word, then a destination preposition, then the
    commit word strictly after it.

    "Continue to Payment"     → True  (navigates to the payment STEP)
    "Proceed to Checkout"     → True
    "Continue & Place Order"  → False (conjunction — the label says it commits)
    "Next: Confirm Order"     → False (no destination preposition)
    "Continue"                → True  (no commit word at all)
    """
    n = (name or "").strip()
    m_adv = ADVANCE_RE.search(n)
    if not m_adv:
        return False
    m_commit = COMMIT_RE.search(n)
    if not m_commit:
        return True
    m_prep = DESTINATION_RE.search(n, m_adv.end())
    return bool(m_prep and m_prep.end() <= m_commit.start())
