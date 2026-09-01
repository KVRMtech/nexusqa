"""VALUE GENERATION — turn a semantic type into a value this control will accept.

The split this module exists to enforce: **a classifier decides WHAT a field is;
a generator decides what to TYPE.**  Nothing that classifies ever emits a value.

That matters most for the field agent that runs outside this service.  A language
model is excellent at reading a label and saying "this is a national identity
number"; it must never be the thing that produces the digits.  If it were, a
model would emit personal-looking data token by token, the value could not be
reproduced from evidence, and no one could prove the number was fictional.  Here
the model's answer selects a branch, and the branch reads from a synthetic
identity that is fictional by construction.

Every value is also reconciled against what the control itself declares — the
option list, ``maxlength``, ``min``/``max`` — because a semantically perfect value
that violates the field's own constraint fails exactly like a wrong one.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Optional

from . import field_semantics as S
from . import vocab
from .fill_engine import generator as _generator
from .fill_engine import options as _option_rules
from .fill_engine import persona as _persona
from .identity_pack import Identity

__all__ = ["value_for", "explain", "persona_for", "PROVENANCE_SYNTHESIZED"]

PROVENANCE_SYNTHESIZED = "synthesized"

_DIGITS_RE = re.compile(r"\D+")

#: THE CANONICAL PLACEHOLDER RULE now lives in :mod:`app.fill_engine.options`,
#: because the fill engine's generator needs it at a layer below this one and a
#: function-local import would have hidden the dependency rather than removed
#: it.  Re-exported here — unchanged in behaviour and identity — so every
#: existing caller keeps working and there is still exactly ONE rule.
#:
#: The incident that produced the rule is worth keeping in view: there used to
#: be two lists, and fixing one still left the other choosing "Select coverage
#: amount…".  That option's underlying value is "", so the field is EMPTY while
#: the fill reports success, a validation-gated form never enables Continue, and
#: the crawl stalls on a page it believes it completed.
is_placeholder_option = _option_rules.is_placeholder_option
enumerate_real = _option_rules.enumerate_real


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def _attr(control: Mapping[str, Any], *keys: str) -> str:
    q = control.get("qec") if isinstance(control.get("qec"), Mapping) else {}
    for k in keys:
        v = control.get(k)
        if v in (None, ""):
            v = q.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _options(control: Mapping[str, Any]) -> list[str]:
    """The answers a control offers — from ``options`` OR ``group_options``.

    A RADIO GROUP KEEPS ITS ENUMERATION SOMEWHERE ELSE. GROUP_ASSEMBLE writes the
    question's answers to ``group_options`` and deliberately NOT to ``options``,
    so that a radio's field signature does not shift when a sibling appears. This
    function read only ``options``, so a grouped radio looked like a control with
    no answers at all and agent mode returned None for it — the one mode whose
    entire purpose is to answer a semantic choice could never answer the most
    common semantic choice there is.

    Fleet-wide, not app-specific: every native radio group on every application
    was unanswerable. Live consequence on a five-step application wizard: the
    Coverage step filled 3 of 5 fields, the app's own validation kept Continue
    disabled, and the walk stopped two steps short of the end.
    """
    raw = control.get("options")
    if not isinstance(raw, (list, tuple)) or not raw:
        # GROUP MEMBERS ARE REAL CONTROLS, AND A REAL CONTROL IS NEVER A
        # PLACEHOLDER. The placeholder filter answers a question about a
        # DROPDOWN — is this first entry a prompt ("Select…", "None") or an
        # answer? — and a set of checkboxes or radios has no prompt among them
        # by construction. Applying it here deleted the member labelled "None"
        # from a health-conditions question: the group's only negative answer,
        # and the one the fill prefers precisely because it asserts nothing. The
        # question was then answered with the first POSITIVE option instead,
        # disclosing a condition on the applicant's behalf.
        raw = control.get("group_options")
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(o).strip() for o in raw if str(o).strip()]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(o).strip() for o in enumerate_real(raw)]


def _pick_option(control: Mapping[str, Any], *wanted: str) -> Optional[str]:
    """Choose the option that MATCHES the identity, not merely the first one.

    A region dropdown must land on the same region the postcode belongs to, or
    the form is internally inconsistent and the application rejects it on a field
    nobody was looking at. Falls back to the first real option so a choice is
    still made."""
    opts = _options(control)
    if not opts:
        return None
    targets = [_norm(w) for w in wanted if str(w).strip()]
    for t in targets:                                 # exact, then contained
        for o in opts:
            if _norm(o) == t:
                return o
    for t in targets:
        for o in opts:
            if t and (t in _norm(o) or _norm(o) in t):
                return o
    return opts[0]


def _fit(value: str, control: Mapping[str, Any]) -> str:
    """Respect the control's own declared length limit."""
    maxlen = _attr(control, "maxlength")
    if maxlen.isdigit() and int(maxlen) > 0:
        return value[:int(maxlen)]
    return value


def _digits_only(value: str, control: Mapping[str, Any]) -> str:
    """Some fields declare a length that only fits the unpunctuated form."""
    maxlen = _attr(control, "maxlength")
    if maxlen.isdigit() and 0 < int(maxlen) < len(value):
        stripped = _DIGITS_RE.sub("", value)
        if len(stripped) <= int(maxlen):
            return stripped
    return value


def _number_in_range(control: Mapping[str, Any], preferred: int) -> str:
    """A number that satisfies the control's OWN min/max.

    A constraint-blind value passes the fill and then voids the whole submit via
    native validation — the failure looks like the application's, and is ours."""
    lo_s, hi_s = _attr(control, "min"), _attr(control, "max")
    value = preferred
    try:
        if lo_s not in ("",) and float(lo_s) > value:
            value = int(float(lo_s))
    except ValueError:
        pass
    try:
        if hi_s not in ("",) and float(hi_s) < value:
            value = int(float(hi_s))
    except ValueError:
        pass
    return str(value)


def _date_for(control: Mapping[str, Any], iso: str) -> str:
    """Render a date the way THIS input flavour demands. A blanket ISO string
    makes a time/month/week input throw, so the field never advances."""
    itype = _norm(_attr(control, "input_type", "type"))
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        d = date.today()
    if itype == "month":
        return d.strftime("%Y-%m")
    if itype == "week":
        return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
    if itype == "datetime-local":
        return f"{d.isoformat()}T12:00"
    if itype == "time":
        return "12:00"
    return d.isoformat()


#: DATA MODE (the operator's second dial).
#:   "user"  — today's behaviour exactly. A radio group is a SEMANTIC CHOICE, and
#:             choosing one invents a scenario the client never asked to test:
#:             picking "smoker = no" silently decides which business path is
#:             exercised, and the report would not say so.
#:   "agent" — the agent answers everything it honestly can, so a funnel is
#:             completed without a human. The choice it makes is recorded in the
#:             field ledger, so the report still says which path was taken.
DATA_MODE_USER = "user"
DATA_MODE_AGENT = "agent"


def value_for(semantic_type: str, control: Mapping[str, Any], identity: Identity,
              *, kind: str = "", data_mode: str = DATA_MODE_USER,
              section: str = "") -> Optional[str]:
    """The value to type, or ``None`` when nothing can honestly be produced.

    THE SIGNATURE IS UNCHANGED AND THE BODY IS NOT.  Everything below now runs
    through :mod:`app.fill_engine`, which decides a value from three inputs
    instead of one — the semantic type this function has always received, plus
    WHOSE field it is and WHAT THE CONTROL WILL ACCEPT.  The old body could only
    ever answer the first, which is why "Beneficiary Name" came back as the
    applicant, a money field came back as the constant ``100``, all three parts
    of a split birth date came back as the YEAR, and a declared ``pattern`` was
    read to classify the field and then ignored when filling it.

    ``None`` is still a real answer: for a one-time code or a password there is
    no value a generator could invent that would mean anything, so the field
    becomes residue the client is asked for.  Inventing one produces a test that
    passes against nothing.

    ``section`` is the heading the control sits under — a bare "First Name"
    below a legend reading "Beneficiary Information" belongs to the beneficiary,
    and reading only the control's own label is exactly how it used to be
    answered with the applicant.  Optional, so every existing caller keeps
    working and simply gets the weaker of the two rungs.
    """
    persona = persona_for(identity)
    candidate = _generator.generate(
        semantic_type, control, persona, kind=kind,
        name=str(control.get("name") or ""), section=section,
        # THE OPERATOR'S DATA DIAL, unchanged.  ``user`` still declines to make a
        # semantic choice on the client's behalf; what changed is that the
        # DISPATCH default is now ``agent`` (see ``main.ExploreRequest``), so a
        # funnel completes without anyone having to change posture — which is
        # what "radio groups are skipped" actually meant.
        answer_choices=(_norm(data_mode) == DATA_MODE_AGENT),
    )
    return candidate.value


def explain(semantic_type: str, control: Mapping[str, Any], identity: Identity,
            *, kind: str = "", data_mode: str = DATA_MODE_USER,
            section: str = "") -> "_generator.Candidate":
    """:func:`value_for`, plus the reasoning that produced the value.

    Same decision, same determinism — the caller that wants provenance in the
    field ledger reads this, and the caller that only wants a string keeps the
    older, narrower contract above.  Two entry points onto ONE decision, so the
    explanation can never drift from the value it explains."""
    return _generator.generate(
        semantic_type, control, persona_for(identity), kind=kind,
        name=str(control.get("name") or ""), section=section,
        answer_choices=(_norm(data_mode) == DATA_MODE_AGENT))


#: One household per identity, for the life of the process.
#:
#: Deriving it costs a handful of SHA-256 blocks, which is nothing per field and
#: real across the thousands of fields a deep crawl fills.  Keyed on the
#: identity's own seed AND its birth date, so an identity built against an
#: explicit reference date can never collide with one built against today's —
#: two different people who share a seed must not share a household.
_PERSONA_CACHE: "dict[tuple[str, str], _persona.Persona]" = {}
#: A crawl meets one identity, occasionally a handful; a runaway cache would be
#: a leak in a long-lived process, so it is bounded and simply stops caching.
_PERSONA_CACHE_MAX = 32


def persona_for(identity: Identity) -> "_persona.Persona":
    """The coherent household grown around this identity.

    The applicant IS the identity, verbatim — every value the old body produced
    from ``identity.x`` still comes from the same place, so nothing that already
    worked moves."""
    key = (identity.seed, identity.date_of_birth)
    cached = _PERSONA_CACHE.get(key)
    if cached is not None:
        return cached
    built = _persona.derive_persona(identity.seed, identity=identity)
    if len(_PERSONA_CACHE) < _PERSONA_CACHE_MAX:
        _PERSONA_CACHE[key] = built
    return built
