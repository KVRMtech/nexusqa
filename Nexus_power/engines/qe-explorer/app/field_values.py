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
from .identity_pack import Identity

__all__ = ["value_for", "PROVENANCE_SYNTHESIZED"]

PROVENANCE_SYNTHESIZED = "synthesized"

_DIGITS_RE = re.compile(r"\D+")
#: Option text that is a prompt, never a real choice.
_PLACEHOLDER = frozenset({
    "", "select", "choose", "please select", "select one", "select an option",
    "--", "---", "-- select --", "none", "choose one", "pick one", "select...",
})

#: THE canonical placeholder rule. It lives here — the lowest layer that has to
#: choose an option — and forms.py imports it, because there used to be two
#: lists and fixing one left the other choosing "Select coverage amount...".
#:
#: An exact-phrase set cannot survive real applications: they write "Select
#: coverage amount...", "Choose your state", "-- Select term length --". Those
#: are the SAME thing — the option whose underlying value is "". Picking one
#: leaves the field EMPTY while the fill reports success, so a validation-gated
#: form never enables Continue and the crawl stalls on a page it believes it
#: completed. Observed live on a quote funnel, twice, in two different modules.
_PLACEHOLDER_LEAD_VERBS = ("select", "choose", "pick")


def is_placeholder_option(label: Any, *, first: bool = False) -> bool:
    """Is this the "nothing chosen yet" entry rather than a real answer?

    Deliberately conservative: the leading-verb rule applies only when ``first``
    (where placeholders conventionally live) or when the text trails off in an
    ellipsis, so a genuine product named "Choose Life Term 20" further down a
    list is still selectable. A false positive silently discards a real business
    path, which is worse than occasionally keeping a placeholder.
    """
    text = _norm(label)
    if not text or text in _PLACEHOLDER:
        return True
    stripped = text.strip("-–—_ .·:…")
    if not stripped:
        return True
    lead = stripped.split()[0]
    if text.endswith(("...", "…")) and lead in _PLACEHOLDER_LEAD_VERBS:
        return True
    return bool(first) and lead in _PLACEHOLDER_LEAD_VERBS


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
    raw = control.get("options")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(o).strip() for o in enumerate_real(raw)]


def enumerate_real(raw: Any) -> list[str]:
    """Every option that is a real ANSWER, in order — placeholders dropped."""
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(o).strip() for i, o in enumerate(raw)
            if str(o).strip() and not is_placeholder_option(o, first=(i == 0))]


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
              *, kind: str = "", data_mode: str = DATA_MODE_USER) -> Optional[str]:
    """The value to type, or ``None`` when nothing can honestly be produced.

    ``None`` is a real answer: for a one-time code or a password there is no value
    a generator could invent that would mean anything, so the field becomes part
    of the residue the client is asked for. Inventing one would produce a test
    that passes against nothing."""
    sem = S.coerce(semantic_type)
    k = _norm(kind) or _norm(_attr(control, "kind"))

    if sem in S.UNGENERATABLE or sem == S.UNKNOWN:
        return None

    # A radio group is a semantic choice. In USER mode it is left alone, exactly as
    # before — the crawl must not decide which business path gets exercised and then
    # not say so. In AGENT mode it is answered, and the ledger records what was
    # chosen so the report can.
    if k == "radio" and data_mode != DATA_MODE_AGENT:
        return None

    # A choice control is answered by CHOOSING, whatever the semantics — but the
    # semantics decide WHICH option, so the answer stays internally consistent.
    if k in ("select", "radio") or _options(control):
        if sem == S.REGION:
            return _pick_option(control, identity.region_name, identity.region_code)
        if sem == S.COUNTRY:
            return _pick_option(control, identity.country, "US", "USA")
        if sem == S.CITY:
            return _pick_option(control, identity.city)
        if sem == S.CARD_EXPIRY:
            return _pick_option(control, identity.card_expiry.split("/")[0])
        if sem == S.DOB:
            return _pick_option(control, identity.date_of_birth[:4])
        picked = _pick_option(control)
        if picked is not None:
            return picked

    if k in ("checkbox", "toggle"):
        # Only a REQUIRED consent is cleared — an optional toggle changes what the
        # application does, and choosing for the client would invent a scenario
        # they never asked to test.
        if sem == S.CONSENT and (control.get("required")
                                 or (control.get("qec") or {}).get("required")):
            return "true"
        return None

    mapping = {
        S.GIVEN_NAME: identity.given_name,
        S.FAMILY_NAME: identity.family_name,
        S.FULL_NAME: identity.full_name,
        S.USERNAME: identity.username,
        S.EMAIL: identity.email,
        S.COMPANY: identity.company,
        S.JOB_TITLE: identity.job_title,
        S.STREET: identity.street_address,
        S.STREET_2: identity.street_address_2 or identity.street_address,
        S.CITY: identity.city,
        S.REGION: identity.region_name,
        S.POSTAL_CODE: identity.postal_code,
        S.COUNTRY: identity.country,
        S.URL: "https://example.com",
        S.FREE_TEXT: "autotest",
    }
    if sem in mapping:
        return _fit(mapping[sem], control)

    if sem == S.PHONE:
        return _fit(_digits_only(identity.phone, control), control)
    if sem == S.SSN:
        return _fit(_digits_only(identity.national_id, control), control)
    if sem == S.CARD_NUMBER:
        return _fit(identity.card_number, control)
    if sem == S.CARD_CVC:
        return _fit(identity.card_cvc, control)
    if sem == S.CARD_EXPIRY:
        return _fit(identity.card_expiry, control)
    if sem == S.DOB:
        return _date_for(control, identity.date_of_birth)
    if sem == S.DATE:
        return _date_for(control, date.today().isoformat())
    if sem == S.TIME:
        return "12:00"
    if sem == S.AGE:
        return _number_in_range(control, identity.age)
    if sem == S.QUANTITY:
        return _number_in_range(control, 1)
    if sem == S.CURRENCY:
        return _number_in_range(control, 100)
    if sem == S.PERCENT:
        return _number_in_range(control, 10)
    if sem == S.CHOICE:
        return _pick_option(control)
    if sem == S.CONSENT:
        return "true" if (control.get("required")
                          or (control.get("qec") or {}).get("required")) else None
    return None
