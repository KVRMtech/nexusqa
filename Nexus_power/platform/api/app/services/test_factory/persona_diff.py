"""Persona diff classifier (P3) — what is member-derived vs app-constant.

The intellectual core of honest per-persona oracles. Changing the member
changes the ORACLES, not just the inputs: a run recorded as member A baked in
A's name, A's policy number, A's balances. Re-running as B with A's expectations
would fail dishonestly; deleting those checks would green-wash the suite.

So we classify every observed value by EVIDENCE, never by guess, on two layers:

  * VALUE — same page, different content. Two personas' captured value at the
    same (scenario, step, key): differ ⇒ member_derived (diff_proven);
    identical ⇒ app_constant (stable_across_personas). A value that also
    differs between two runs of the SAME persona is volatile (time-drift), not
    member-derived — so a third same-persona control run isolates it.
  * STRUCTURE — the JOURNEY itself forks or repeats. Steps/pages present for one
    persona and not the other, or a block repeated a different number of times,
    are structural differences (diff_proven) that feed cardinality-driven
    repetition and behavior-class scoping (P4).

ZERO LLM. Every classification carries the evidence that produced it, so a
reviewer argues with the rule, not a black box. Fail-closed: what we cannot
classify stays ``unknown`` and, at run time for a non-baseline persona, becomes
a STRUCTURAL check reported UNVERIFIED — never a fabricated pass.
"""

from __future__ import annotations

import re

CLASS_MEMBER_DERIVED = "member_derived"
CLASS_APP_CONSTANT = "app_constant"
CLASS_VOLATILE = "volatile"
CLASS_STRUCTURAL = "structural"
CLASS_UNKNOWN = "unknown"

EV_DIFF_PROVEN = "diff_proven"
EV_STABLE = "stable_across_personas"
EV_IDENTITY_ECHO = "identity_echo"
EV_UNCLASSIFIED = "unclassified"

# Values that vary with time rather than identity — masked so a mere timestamp
# difference is not mistaken for a member difference.
_VOLATILE_RX = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                     # dates
    re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?\b", re.I),  # times
    re.compile(r"\b\d+\s*(seconds?|minutes?|hours?|days?)\s+ago\b", re.I),
)


def _norm(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _looks_volatile(a: str, b: str) -> bool:
    """True when the only difference between a and b is time-shaped."""
    ma, mb = a, b
    for rx in _VOLATILE_RX:
        ma = rx.sub("<t>", ma)
        mb = rx.sub("<t>", mb)
    return ma == mb and a != b


def _identity_echo(value: str, identity_values: set[str]) -> bool:
    """True when a captured value contains one of the logged-in member's own
    seed fields (name, member number, email…). This is the SINGLE-persona
    signal — provable from one crawl, before any diff."""
    v = value.lower()
    for iv in identity_values:
        iv = (iv or "").strip().lower()
        if iv and len(iv) >= 3 and iv in v:
            return True
    return False


def classify_single_persona(observed: dict[str, str],
                            identity_values: set[str]) -> dict[str, dict]:
    """One-crawl classification: tag values echoing the logged-in member's own
    identity as member_derived/identity_echo. Everything else stays unknown
    (a single crawl cannot prove app-constancy). Keyed by value_key."""
    out: dict[str, dict] = {}
    for key, val in (observed or {}).items():
        v = _norm(val)
        if v and _identity_echo(v, identity_values):
            out[key] = {"class": CLASS_MEMBER_DERIVED, "evidence": EV_IDENTITY_ECHO}
        else:
            out[key] = {"class": CLASS_UNKNOWN, "evidence": EV_UNCLASSIFIED}
    return out


def diff_two_personas(observed_a: dict[str, str], observed_b: dict[str, str],
                      control_a: dict[str, str] | None = None
                      ) -> dict[str, dict]:
    """Two-persona VALUE diff, the proven classifier.

    ``observed_a`` / ``observed_b`` map value_key → captured value for persona A
    and B on the same journey. ``control_a`` (optional) is a SECOND run of
    persona A, used to isolate time-drift: a key that differs between A and its
    own control is volatile, not member-derived.
    """
    out: dict[str, dict] = {}
    keys = set(observed_a) | set(observed_b)
    for key in keys:
        a = _norm(observed_a.get(key, ""))
        b = _norm(observed_b.get(key, ""))
        if key not in observed_a or key not in observed_b:
            # present for one persona only — a structural signal, not a value one
            out[key] = {"class": CLASS_STRUCTURAL, "evidence": EV_DIFF_PROVEN,
                        "detail": {"present_for": "a" if key in observed_a else "b"}}
            continue
        if a == b:
            out[key] = {"class": CLASS_APP_CONSTANT, "evidence": EV_STABLE}
            continue
        # a != b. Is it just time-drift? Check A against its own control.
        if control_a is not None and key in control_a:
            ca = _norm(control_a.get(key, ""))
            if ca != a and _looks_volatile(ca, a):
                out[key] = {"class": CLASS_VOLATILE, "evidence": EV_DIFF_PROVEN}
                continue
        if _looks_volatile(a, b):
            out[key] = {"class": CLASS_VOLATILE, "evidence": EV_DIFF_PROVEN}
        else:
            out[key] = {"class": CLASS_MEMBER_DERIVED, "evidence": EV_DIFF_PROVEN,
                        "detail": {"a": a[:120], "b": b[:120]}}
    return out


def diff_structure(pages_a: list[str], pages_b: list[str],
                   repeat_counts_a: dict[str, int] | None = None,
                   repeat_counts_b: dict[str, int] | None = None) -> dict:
    """STRUCTURE diff: which pages/steps one persona reached and the other did
    not, and where a repeated block's cardinality differs (5 beneficiary rows
    vs 2). Feeds cardinality-driven repetition and behavior-class scoping (P4).
    """
    sa, sb = list(pages_a or []), list(pages_b or [])
    set_a, set_b = set(sa), set(sb)
    only_a = [p for p in sa if p not in set_b]
    only_b = [p for p in sb if p not in set_a]
    ra, rb = dict(repeat_counts_a or {}), dict(repeat_counts_b or {})
    cardinality = []
    for block in set(ra) | set(rb):
        na, nb = int(ra.get(block, 0)), int(rb.get(block, 0))
        if na != nb:
            cardinality.append({"block": block, "count_a": na, "count_b": nb})
    forked = bool(only_a or only_b)
    return {
        "pages_only_a": only_a,
        "pages_only_b": only_b,
        "cardinality_differences": cardinality,
        "journey_forks": forked,
        "kind": ("fork" if forked else ("cardinality" if cardinality else "identical")),
        "note": ("The journey FORKS between these personas — affected cases bind "
                 "to the demonstrating behavior class and refuse an out-of-class "
                 "persona (P4)." if forked else
                 ("The journey differs only by COUNT — the repeated block becomes "
                  "cardinality-driven (one suite, any count)." if cardinality else
                  "Identical structure — the same journey serves both personas.")),
    }


def per_persona_split(classifications: dict[str, dict],
                      answer_sheet: dict[str, dict],
                      is_baseline: bool) -> dict:
    """How much of a suite is PROVEN for a given persona (report §Trust).

    For a NON-baseline persona, a member_derived value_key is PROVEN only when
    the persona's answer sheet supplies its expected value; otherwise it is
    UNVERIFIED (a structural check runs, but no member value is asserted). App-
    constant values are proven for everyone. The baseline persona (whose crawl
    IS the recording) proves its own member-derived values by construction.
    """
    proven = inferred = unverified = 0
    per_key: dict[str, str] = {}
    for key, c in (classifications or {}).items():
        cls = c.get("class")
        if cls == CLASS_APP_CONSTANT:
            per_key[key] = "PROVEN"; proven += 1
        elif cls == CLASS_MEMBER_DERIVED:
            if is_baseline:
                per_key[key] = "PROVEN"; proven += 1
            elif key in (answer_sheet or {}):
                per_key[key] = "PROVEN"; proven += 1
            else:
                per_key[key] = "UNVERIFIED"; unverified += 1
        elif cls == CLASS_VOLATILE:
            per_key[key] = "INFERRED"; inferred += 1   # asserted structurally
        else:  # unknown / structural
            per_key[key] = "UNVERIFIED"; unverified += 1
    return {
        "proven": proven, "inferred": inferred, "unverified": unverified,
        "total": proven + inferred + unverified,
        "per_key": per_key,
        "note": ("PROVEN = an app-constant value, or a member-derived value the "
                 "persona's own answer sheet supplies. UNVERIFIED = we assert the "
                 "field is present/well-formed but do NOT claim a member value we "
                 "cannot ground — never a fabricated pass, never a false red."),
    }


__all__ = [
    "CLASS_MEMBER_DERIVED", "CLASS_APP_CONSTANT", "CLASS_VOLATILE",
    "CLASS_STRUCTURAL", "CLASS_UNKNOWN", "EV_DIFF_PROVEN", "EV_STABLE",
    "EV_IDENTITY_ECHO", "EV_UNCLASSIFIED",
    "classify_single_persona", "diff_two_personas", "diff_structure",
    "per_persona_split",
]
