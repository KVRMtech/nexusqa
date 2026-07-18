"""The six-disposition data model + pure classifier (Phase 1 — Seed Manifest).

For every form field the crawl observed, the product must decide WHERE a trustworthy
value comes from. The answer is always exactly ONE of six dispositions:

  SYNTHESIZE — generate a format-valid, domain-appropriate value (name, DOB, amount).
  PICK       — choose an option the crawl ACTUALLY observed (a dropdown/radio value).
  CARRY      — reuse a value the client entered once (from the encrypted data library).
  OBSERVE    — do NOT fill it — read it; it is an output/oracle target (premium, conf #).
  ASK        — a real value the product must NEVER invent (SSN, account #, policy id).
  APPROVE    — a decision, not a value (permission to submit + "what correct means").

THIS MODULE OWNS THE SCHEMA. Phase 3 (Data Agent) adds an LLM layer that can only
REFINE this deterministic floor; Phases 4/5 consume the disposition tag. The classifier
is PURE (no DB, no clock, no LLM, no I/O) and FAIL-CLOSED to ASK: any identifier-shaped
or unrecognised field becomes ASK rather than get a fabricated value — because a test
that "passes" on an invented account number proves nothing (never green-wash).

Two-stage honesty: OBSERVE targets (a rendered premium) only exist AFTER a value-fed
crawl renders them, so a value-free discovery pass yields ASK/APPROVE/SYNTH/PICK/CARRY
for the surface fields and OBSERVE is enriched later. This module classifies whatever
signals it is given; the caller decides which stage's signals to pass.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field as _dc_field
from datetime import date

# ── The six dispositions (stable strings — Phases 3/4/5 import these) ──────────
SYNTHESIZE = "SYNTHESIZE"
PICK = "PICK"
CARRY = "CARRY"
OBSERVE = "OBSERVE"
ASK = "ASK"
APPROVE = "APPROVE"

DISPOSITIONS = frozenset({SYNTHESIZE, PICK, CARRY, OBSERVE, ASK, APPROVE})

#: The product auto-handles these; the human never sees them in "Recommended".
AUTO_DISPOSITIONS = frozenset({SYNTHESIZE, PICK, CARRY, OBSERVE})
#: The human 1% — always surfaced in "Recommended".
HUMAN_DISPOSITIONS = frozenset({ASK, APPROVE})
#: Dispositions whose default value is projected into the crawl's fill (the
#: pre-fill). OBSERVE (an oracle target) and ASK (never invented) are EXCLUDED.
PREFILLABLE_DISPOSITIONS = frozenset({SYNTHESIZE, PICK, CARRY})

# Options that are placeholders, never a grounded PICK default.
_PLACEHOLDER_OPTIONS = frozenset({
    "", "select", "select...", "select one", "choose", "choose...", "choose one",
    "please select", "-- select --", "--select--", "none", "n/a", "na", "--",
    "(select)", "select an option", "pick one",
})

# Sensitive identifiers the product must NEVER fabricate — a value that must match a
# real backend record. Matched as normalised substrings of the field label.
_ASK_LEXICON = (
    "ssn", "social security", "tax id", "taxpayer", "ein", "national id",
    "routing number", "routing no", "aba number", "account number", "account no",
    "acct number", "acct no", "bank account", "policy number", "policy no",
    "policy id", "member id", "member number", "membership number", "subscriber id",
    "subscriber number", "group number", "claim number", "certificate number",
    "passport", "driver license", "drivers license", "driver's license",
    "license number", "card number", "credit card", "debit card", "cvv", "cvc",
    "security code", "iban", "swift", "sort code", "external account", "payee account",
    "beneficiary account",
)

# A sensitive-noun + identifier-suffix pattern that catches novel names like
# "Reference no." (often a policy id) — closing the fabricate-a-real-id hole. Benign
# numeric fields (phone, zip) are NOT here; they are affirmatively safe below.
_ASK_NOUN = r"(reference|account|policy|member|subscriber|payee|routing|claim|contract|certificate|beneficiary|group)"
_ASK_SUFFIX = r"(id|ids|no|nos|number|numbers|#|code)"
_ASK_NOUN_ID_RE = re.compile(rf"\b{_ASK_NOUN}\b.*\b{_ASK_SUFFIX}\b|\b{_ASK_NOUN}\s*{_ASK_SUFFIX}\b")

# A login credential is a real value (never invented).
_LOGIN_TERMS = ("username", "user name", "user id", "userid", "login", "login id")

# Field types the crawler emits (normalised). Anything not here → treated as text.
_SELECT_TYPES = frozenset({"select", "radio", "checkbox", "listbox", "combobox"})
_SUBMIT_TYPES = frozenset({"submit", "button", "image"})

# Affirmatively-safe text nouns that MAY be synthesized. A text field whose label
# matches NONE of these (and is not a safe typed input) fails closed to ASK.
_SAFE_TEXT_NOUNS = (
    "first name", "last name", "middle name", "full name", "name", "email",
    "e-mail", "phone", "mobile", "telephone", "fax", "address", "street", "city",
    "state", "province", "zip", "postal", "country", "date of birth", "dob",
    "birth", "age", "amount", "coverage", "sum", "salary", "income", "quantity",
    "qty", "company", "employer", "occupation", "job title", "title", "comment",
    "comments", "note", "notes", "message", "description", "reason", "subject",
    "height", "weight", "gender", "sex", "marital",
)


@dataclass(frozen=True)
class FieldSignal:
    """A single observed form field (value-free): what the crawl saw about it."""
    label: str
    type: str = "text"
    options: tuple[str, ...] = ()
    required: bool = False


@dataclass(frozen=True)
class Disposition:
    """The classifier's verdict for one field — JSON-serialisable via ``as_dict``."""
    label: str
    disposition: str
    default: str | None
    grounded: bool
    reason: str
    options: tuple[str, ...] = ()
    required: bool = False
    editable: bool = True

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "disposition": self.disposition,
            "default": self.default,
            "grounded": self.grounded,
            "reason": self.reason,
            "options": list(self.options),
            "required": self.required,
            "editable": self.editable,
        }


def normalize_label(label: str) -> str:
    """Lower, strip, collapse separators — the stable key for matching a label to a
    library slot, an OBSERVE target, or the ASK/safe lexicons."""
    s = re.sub(r"[\s_\-]+", " ", str(label or "").strip().lower())
    return re.sub(r"\s+", " ", s).strip()


# Leading verbs / phrases that mark a UI ACTION control (a button/toggle/link), NOT a
# data-input field — a "provide a value" manifest must exclude these. "confirm" is left
# out on purpose: it collides with data fields ("Confirm Password/Email"). Password-shaped
# fields are routed to the login group before this filter runs, so they're never dropped.
_ACTION_VERBS = (
    "mark", "enable", "disable", "show", "hide", "switch", "add", "remove", "send",
    "apply", "edit", "close", "open", "reset", "download", "upload", "filter", "sort",
    "go to", "previous", "next", "continue", "submit", "save", "cancel", "delete",
    "approve", "reject", "select all", "clear",
)
_ACTION_PHRASES = ("as done", "navigation", "steps for", " mode", "page ", "report an issue")


def is_action_label(label: str) -> bool:
    """True when a label reads as a UI action control rather than a value to provide."""
    lo = normalize_label(label)
    if not lo:
        return True
    if any(lo.startswith(v + " ") or lo == v for v in _ACTION_VERBS):
        return True
    return any(p in lo for p in _ACTION_PHRASES)


def _has_word(norm: str, *words: str) -> bool:
    """Whole-WORD match for short, collision-prone tokens.

    A naive substring test is unsafe here: 'age' occurs inside 'message', 'package',
    'mileage' and 'manage'; 'sum' inside 'consumer'; 'city' inside 'capacity'; 'state'
    inside 'estate'. A live crawl caught exactly this — a "Your Message Here" textarea
    was synthesized as '35' because 'message' contains 'age'. Word boundaries fix it.
    """
    return any(re.search(rf"\b{re.escape(w)}\b", norm) for w in words)


def _first_grounded_option(options: Iterable[str]) -> str | None:
    for opt in options:
        text = str(opt or "").strip()
        if text and text.lower() not in _PLACEHOLDER_OPTIONS:
            return text
    return None


def _is_sensitive(norm: str) -> bool:
    if any(term in norm for term in _ASK_LEXICON):
        return True
    if _ASK_NOUN_ID_RE.search(norm):
        return True
    return any(term in norm for term in _LOGIN_TERMS)


def is_sensitive_label(label: str) -> bool:
    """PUBLIC: True when a field label is identifier/credential-shaped and must NEVER
    receive a fabricated value (the Phase-3 hard-line ASK guard reuses this)."""
    return _is_sensitive(normalize_label(label))


def _synthesize_value(norm: str, ftype: str, *, today: date | None) -> str | None:
    """A domain-VALID default for an affirmatively-safe field, or None if the field
    is not affirmatively safe (→ the caller fails closed to ASK). Deterministic; the
    only date math uses an injected ``today`` so a DOB is a real adult date, never
    today (which would fail an age gate)."""
    t = (ftype or "text").strip().lower()

    # Typed inputs that are safe regardless of label wording.
    if t in ("email",):
        return "qa.autotest@example.com"
    if t in ("tel", "phone"):
        return "5555550123"  # 555-01xx reserved test range
    if t in ("url",):
        return "https://example.com/qa"
    if t in ("date", "datetime-local", "month"):
        if any(k in norm for k in ("birth", "dob")):
            return _adult_dob(today)
        return (today or date(2026, 6, 15)).isoformat()[:10]
    if t in ("number", "range"):
        return _numeric_default(norm)

    # Text-like fields: only synthesize when the label is affirmatively recognised.
    if not any(noun in norm for noun in _SAFE_TEXT_NOUNS):
        return None

    if "email" in norm or "e-mail" in norm:
        return "qa.autotest@example.com"
    if any(k in norm for k in ("phone", "mobile", "telephone", "fax")):
        return "5555550123"
    if any(k in norm for k in ("birth", "dob")):
        return _adult_dob(today)
    if "first name" in norm:
        return "Test"
    if "last name" in norm:
        return "User"
    if "middle" in norm:
        return "Q"
    if "name" in norm:
        return "Test User"
    if any(k in norm for k in ("amount", "coverage", "salary", "income")) or _has_word(norm, "sum"):
        return "250000"
    if _has_word(norm, "age"):
        return "35"
    if any(k in norm for k in ("quantity", "qty")):
        return "1"
    if "zip" in norm or "postal" in norm:
        return "94105"
    if _has_word(norm, "state", "province"):
        return "CA"
    if _has_word(norm, "city"):
        return "Springfield"
    if "country" in norm:
        return "United States"
    if any(k in norm for k in ("address", "street")):
        return "123 Test Street"
    if any(k in norm for k in ("company", "employer")):
        return "QA Test Co"
    if any(k in norm for k in ("occupation", "job title", "title")):
        return "Analyst"
    if any(k in norm for k in ("comment", "note", "message", "description", "reason", "subject")):
        return "QA test"
    if "height" in norm:
        return "70"
    if "weight" in norm:
        return "170"
    # Recognised-but-unmapped safe noun (gender/sex/marital as free text etc.).
    return "Test"


def _numeric_default(norm: str) -> str:
    if any(k in norm for k in ("amount", "coverage", "salary", "income", "balance")) or _has_word(norm, "sum"):
        return "250000"
    if _has_word(norm, "age"):
        return "35"
    if "zip" in norm or "postal" in norm:
        return "94105"
    if _has_word(norm, "year"):
        return "2026"
    if any(k in norm for k in ("quantity", "qty", "count", "number of")):
        return "1"
    return "10"


def _adult_dob(today: date | None) -> str:
    base = today or date(2026, 6, 15)
    try:
        return date(base.year - 35, base.month, base.day).isoformat()
    except ValueError:  # Feb 29 edge — step back a day
        return date(base.year - 35, base.month, 28).isoformat()


def classify_field(
    field: FieldSignal,
    *,
    library_keys: Iterable[str] = (),
    observe_labels: Iterable[str] = (),
    submit_labels: Iterable[str] = (),
    today: date | None = None,
) -> Disposition:
    """Assign ONE disposition to one field. Fail-closed to ASK. Deterministic.

    Precedence (each rule below the last only fires when the earlier ones do not):
      OBSERVE → APPROVE → CARRY → ASK(sensitive) → PICK(observed options) →
      SYNTHESIZE(affirmatively-safe) → ASK(fail-closed fallback).
    """
    label = str(field.label or "").strip()
    norm = normalize_label(label)
    ftype = (field.type or "text").strip().lower()
    options = tuple(str(o) for o in (field.options or ()))
    lib = {normalize_label(k) for k in library_keys}
    obs = {normalize_label(k) for k in observe_labels}
    sub = {normalize_label(k) for k in submit_labels}

    def make(disp, default, grounded, reason, *, editable=True):
        return Disposition(
            label=label, disposition=disp, default=default, grounded=grounded,
            reason=reason, options=options, required=bool(field.required), editable=editable,
        )

    # OBSERVE — an output/oracle target, never filled.
    if norm in obs:
        return make(OBSERVE, None, True, "output the run produces — asserted, never filled", editable=False)

    # APPROVE — a terminal submit control (a decision, not a value).
    if ftype in _SUBMIT_TYPES or norm in sub:
        return make(APPROVE, None, False, "a submit/terminal control — needs permission + an invariant")

    # CARRY — the client has already provided this value once.
    if norm and norm in lib:
        return make(CARRY, None, True, "reused from the client's data library (entered once)", editable=False)

    # ASK (fail-closed) — a real value the product must never invent.
    if ftype == "password":
        return make(ASK, None, False, "a password — never filled or invented")
    if _is_sensitive(norm):
        return make(ASK, None, False, "a real value the product must never invent (identifier/credential)")

    # PICK — a select/radio/checkbox: the default MUST be an observed option.
    if ftype in _SELECT_TYPES:
        grounded_opt = _first_grounded_option(options)
        if grounded_opt is not None:
            return make(PICK, grounded_opt, True, "chosen from an option the crawl actually observed")
        # No observed option → we will NOT invent one. Fail closed to ASK.
        return make(ASK, None, False, "a choice with no observed options — cannot ground a value")

    # SYNTHESIZE — only for affirmatively-safe fields; otherwise fail closed.
    value = _synthesize_value(norm, ftype, today=today)
    if value is not None:
        return make(SYNTHESIZE, value, True, "a format-valid value the field accepts")
    return make(ASK, None, False, "unrecognised field — asked rather than guessed (fail-closed)")


def classify_manifest(
    fields: Iterable[FieldSignal],
    *,
    library_keys: Iterable[str] = (),
    observe_labels: Iterable[str] = (),
    submit_labels: Iterable[str] = (),
    observe_targets: Iterable[Mapping] | None = None,
    today: date | None = None,
) -> dict:
    """Classify a whole field set into a two-mode Seed Manifest.

    Returns ``{recommended, full, prefill, counts}`` where ``recommended`` is only the
    ASK + APPROVE items (the human 1%, never hiding an ASK), ``full`` is every field
    with its grounded default, and ``prefill`` is the SYNTHESIZE/PICK/CARRY defaults to
    project into the crawl's answer_key.fill (OBSERVE and ASK are omitted). Any
    ``observe_targets`` (post-crawl oracle nodes, ``[{label, source_hint}]``) are
    appended as OBSERVE items (never filled).
    """
    lib = list(library_keys)
    obs = list(observe_labels)
    sub = list(submit_labels)
    items: list[Disposition] = [
        classify_field(f, library_keys=lib, observe_labels=obs, submit_labels=sub, today=today)
        for f in fields
    ]

    seen = {normalize_label(i.label) for i in items}
    for tgt in (observe_targets or ()):
        lbl = str((tgt or {}).get("label") or "").strip()
        if not lbl or normalize_label(lbl) in seen:
            continue
        items.append(Disposition(
            label=lbl, disposition=OBSERVE, default=None, grounded=True,
            reason="output the run produces — asserted, never filled", editable=False,
        ))
        seen.add(normalize_label(lbl))

    full = [i.as_dict() for i in items]
    # Recommended NEVER hides an ASK — if unsure, the field surfaces to the human.
    recommended = [i.as_dict() for i in items if i.disposition in HUMAN_DISPOSITIONS]
    prefill = {
        i.label: i.default
        for i in items
        if i.disposition in PREFILLABLE_DISPOSITIONS and i.default is not None
    }
    counts: dict[str, int] = {d: 0 for d in DISPOSITIONS}
    for i in items:
        counts[i.disposition] = counts.get(i.disposition, 0) + 1

    return {
        "recommended": recommended,
        "full": full,
        "prefill": prefill,
        "counts": counts,
        "ask_count": counts[ASK],
        "approve_count": counts[APPROVE],
        "autonomous_count": counts[SYNTHESIZE] + counts[PICK] + counts[CARRY] + counts[OBSERVE],
    }
