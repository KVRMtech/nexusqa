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


#: Per-language packs for an option label that asserts the ABSENCE of a
#: condition — "None", "N/A", "None of the above".
#:
#: Deliberately OUTSIDE ``LANGUAGE_PACKS``: that table is mirrored byte-for-byte
#: in qe-central and pinned by parity tests in both suites, and this vocabulary
#: has no counterpart there. Adding a key to it would break the pin for no gain.
#:
#: WHY THE PRODUCT NEEDS IT. A multi-select question is answered by choosing,
#: and the member that asserts nothing is the only one true of a SYNTHETIC
#: identity: checking "Type 2 Diabetes" on an insurance application fabricates a
#: medical history the crawl has no grounds for, while "None" answers the same
#: question and invents nothing. Preferring it is the least-assertive answer,
#: not a guess about the app.
NEGATIVE_OPTION_PACKS: dict[str, list[str]] = {
    "en": [
        r"none\s*of\s*(?:the\s*)?(?:above|these)",
        r"none", r"no", r"n/?a", r"not\s*applicable", r"neither", r"nothing",
        # ── Gate 1 / T-RG-02 · THE FREQUENCY SCALE ──────────────────────────
        # A health questionnaire does not only ask yes/no. "How often do you use
        # tobacco?" offers Never / Rarely / Weekly / Daily, and until this line
        # NONE of those matched — so the group fell through to DOM order and the
        # walk could answer "Daily", fabricating a habit for a synthetic person
        # on an insurance application. That is the exact failure this vocabulary
        # exists to prevent, on the exact domain it was written for.
        #
        # "RARELY" IS DELIBERATELY ABSENT. The rule is "the option that asserts
        # the LEAST", and "Rarely" asserts that the person does the thing — just
        # not often. It is a positive disclosure, and auto-selecting it would
        # invent a smaller version of the same fabrication. Only outright
        # denials belong here.
        #
        # Safe by construction: the compiled pattern is FULL-STRING anchored, so
        # "never" cannot match inside "Nevertheless" any more than "none" can
        # match inside "Nonexistent condition" — the property the pinned
        # non-match suite already guards.
        r"never", r"not\s*at\s*all",
        r"no\s+known\b.*", r"decline\s*to\s*(?:answer|state)",
        r"prefer\s*not\s*to\s*(?:say|answer)",
    ],
}


#: M1.3 — WALK PERSISTENCE vocabulary: controls whose whole purpose is to write
#: server state WITHOUT moving the funnel forward, or to ask the server a
#: question that only a write can answer (a quote, an eligibility decision).
#:
#: Deliberately OUTSIDE ``LANGUAGE_PACKS`` for the same reason as
#: ``NEGATIVE_OPTION_PACKS``: that table is mirrored byte-for-byte in qe-central
#: and pinned by parity tests in both suites, and this vocabulary has no
#: counterpart there.
#:
#: MATCHING THIS LIST GRANTS NOTHING. It only makes a control ELIGIBLE to be
#: actuated inside a walk-persistence window; the control must additionally
#: carry no commit word, no advance word, and no refuse-pack irreversible verb,
#: and the window itself opens only under a verified platform attestation. The
#: list widens what the crawler will TRY, never what the guard will ALLOW.
PERSISTENCE_PACKS: dict[str, list[str]] = {
    "en": [
        r"save\s*(?:as\s*)?draft", r"save\s*(?:my\s*)?progress",
        r"save\s*(?:and|&)\s*(?:continue|finish|resume)\s*later",
        r"save\s*for\s*later", r"save\s*(?:and|&)\s*exit", r"save\s*changes",
        r"re\s*-?\s*calculate", r"calculate\s*(?:premium|quote|cost|total)?",
        r"get\s*(?:a\s*)?quote", r"update\s*quote", r"price\s*it",
        r"check\s*eligibility", r"check\s*availability",
        r"validate\s*(?:address|details|information)?",
        r"verify\s*(?:address|details|eligibility)",
        r"estimate\s*(?:premium|cost)?",
    ],
}


def compile_persistence_re() -> re.Pattern[str]:
    """FULL-STRING match, like the negative-option rule and for the same reason.

    A substring rule would read "Save Draft" inside "Save Draft and Submit
    Application" and hand a commit control a persistence grant. The whole label
    has to say persistence and nothing else."""
    alts: list[str] = []
    for pack in PERSISTENCE_PACKS.values():
        alts.extend(pack)
    return re.compile(r"^(?:" + "|".join(alts) + r")$", re.I)


PERSISTENCE_RE = compile_persistence_re()


def compile_negative_option_re() -> re.Pattern[str]:
    """FULL-STRING match — an option is negative only when the WHOLE label says
    so. A substring rule would read "None" inside "Nonexistent condition" and
    "no" inside "Nodule", turning a positive disclosure into a denial: exactly
    the class of error that puts a value in the ledger the form never held."""
    alts: list[str] = []
    for pack in NEGATIVE_OPTION_PACKS.values():
        alts.extend(pack)
    return re.compile(r"^(?:" + "|".join(alts) + r")$", re.I)


NEGATIVE_OPTION_RE = compile_negative_option_re()


#: B1-S — STEP-BACK vocabulary: a control whose whole purpose is to return the
#: user to the PREVIOUS step of a multi-step form without committing anything.
#:
#: Deliberately OUTSIDE ``LANGUAGE_PACKS`` for the same reason as
#: ``PERSISTENCE_PACKS`` and ``NEGATIVE_OPTION_PACKS``: that table is mirrored
#: byte-for-byte in qe-central and pinned by parity tests in both suites, and
#: this vocabulary has no counterpart there.
#:
#: WHY THE PRODUCT NEEDS IT. Measured on summit-life-carrier: a five-step
#: wizard renders its per-field refusals with ``<FormMessage/>`` inside each
#: step, and the REVIEW step where ``Submit Application`` lives renders none.
#: The schema refuses on submit, the messages belong to fields on earlier steps
#: whose message nodes are unmounted, and a reader standing on the review step
#: sees a blank page and truthfully reports nothing. The message lives where the
#: field lives, so the reader has to go there.
#:
#: MATCHING THIS LIST GRANTS NOTHING. It only makes a control ELIGIBLE to be
#: clicked by the post-crossing rejection reader, which additionally requires a
#: button kind, no danger flag, no commit word, no advance word, and a boundary
#: that is already spent. The list widens what the reader will TRY, never what
#: the guard will ALLOW.
BACK_PACKS: dict[str, list[str]] = {
    "en": [
        r"back", r"go\s*back", r"back\s*(?:a\s*)?step",
        r"previous", r"prev", r"previous\s*step", r"go\s*to\s*previous",
        r"back\s*to\s*(?:the\s*)?previous(?:\s*step)?",
        r"return\s*to\s*(?:the\s*)?previous(?:\s*step)?",
    ],
}


def compile_back_re() -> re.Pattern[str]:
    """FULL-STRING match, like the persistence and negative-option rules.

    A substring rule would read "Back" inside "Back to Dashboard" — a control
    that LEAVES the funnel rather than stepping one page up it — and inside
    "Roll Back Payment", which is a mutation wearing a navigation word. The
    whole label has to say step-back and nothing else.

    Bare ``return`` is deliberately absent: on a commerce application "Return"
    alone is a product return, which is a mutation. Only the phrase forms that
    name the previous STEP are admitted.
    """
    alts: list[str] = []
    for pack in BACK_PACKS.values():
        alts.extend(pack)
    return re.compile(r"^(?:" + "|".join(alts) + r")$", re.I)


BACK_RE = compile_back_re()


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
