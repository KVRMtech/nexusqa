"""VALIDITY IS A PROPERTY OF A CONTROL, NOT OF A PAGE.

``BrowserPort.error_texts()`` returns every visible ``[role=alert]`` and
``[aria-live=assertive]`` region on the page, and the fill path took the FIRST
one as ``error_detail`` on the observation of whatever control it had just
typed into.  Three consequences, all of them observed:

  * a COOKIE BANNER — which is very often marked ``role=alert`` so screen
    readers announce it — made every fill on every page of the crawl look
    rejected;
  * an error raised by field 3 stayed in the DOM while fields 4 through 12 were
    filled, so one real failure was reported as ten;
  * conversely, an alert that was ALREADY THERE when the page loaded could never
    be told apart from one the fill had just caused, so the one signal a repair
    loop needs — "the application rejected THIS value" — did not exist.

This module makes the attribution explicit and fail-closed in the honest
direction: an alert affects a control only when something ANCHORS it to that
control, and an alert that was on the page before the fill is stale by
construction.

    :class:`PageAlertFilter`   snapshot the alerts present BEFORE a fill; every
                               one of them is stale for that fill, forever.
    :func:`signals_for_control` the validity signals that genuinely belong to one
                               control, from strongest evidence to weakest.
    :func:`interpret`          turn a message into a CONSTRAINT HINT the
                               generator can act on, so a retry is driven by
                               what the application said rather than by hope.

ANCHORING RUNGS, strongest first — each is a link the application itself
published:

  1. ``aria-errormessage`` / ``aria-describedby`` naming the node the message is
     in.  This is the accessibility contract for exactly this purpose.
  2. the control's own ``aria-invalid=true``, plus the browser's native
     ``validationMessage``.
  3. an error node whose ``id`` is the control's ``id`` with a conventional
     suffix (``-error``, ``_error``, ``-err``, ``Error``) — what every form
     library generates.
  4. the message NAMES the control ("Date of birth is required" while filling
     "Date of birth").
  5. nothing anchors it — the alert is PAGE-level and is recorded as page
     context, never as a verdict on this field.

Rung 5 is the whole fix.  It is not that page alerts are ignored; it is that
they stop being attributed to a control that has nothing to do with them.

PURE + DETERMINISTIC.  Reads structures the browser port already produces; does
no I/O of its own, so the whole attribution is unit-testable without a browser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import constraints as C

__all__ = [
    "ValidationSignal", "PageAlertFilter", "signals_for_control", "interpret",
    "ConstraintHint", "is_cookie_banner", "is_informational",
    "SOURCE_ARIA_ERRORMESSAGE", "SOURCE_ARIA_INVALID", "SOURCE_NATIVE",
    "SOURCE_ID_CONVENTION", "SOURCE_NAMED", "SOURCE_PAGE",
]

SOURCE_ARIA_ERRORMESSAGE = "aria_errormessage"
SOURCE_ARIA_INVALID = "aria_invalid"
SOURCE_NATIVE = "native_validation_message"
SOURCE_ID_CONVENTION = "id_convention"
SOURCE_NAMED = "message_names_control"
SOURCE_PAGE = "page_alert"

#: Anchoring strength, so a caller can prefer the best evidence when several
#: signals point at one control.
_STRENGTH = {
    SOURCE_ARIA_ERRORMESSAGE: 5,
    SOURCE_NATIVE: 4,
    SOURCE_ARIA_INVALID: 3,
    SOURCE_ID_CONVENTION: 3,
    SOURCE_NAMED: 2,
    SOURCE_PAGE: 0,
}

#: Cookie / consent / privacy banners.  They are the single most common
#: ``role=alert`` on the public web, they are present on page load, and they
#: have nothing whatever to say about a form field.
_COOKIE_RE = re.compile(
    r"\b(?:cookie|cookies|consent|privacy\s+(?:policy|notice|preferences)"
    r"|gdpr|ccpa|tracking\s+preferences|we\s+use\s+cookies"
    r"|accept\s+all|manage\s+preferences)\b", re.I)

#: Informational and promotional live regions — a session-timeout warning, a
#: "saved" toast, a marketing banner.  Announced assertively, not a rejection.
_INFORMATIONAL_RE = re.compile(
    r"\b(?:saved|success|successfully|welcome|loading|please\s+wait"
    r"|session\s+will\s+expire|you\s+are\s+now|thank\s+you|congratulations"
    r"|new\s+feature|maintenance\s+window|beta)\b", re.I)

#: Words that make a message a REJECTION.  A message with none of them and no
#: anchor is not treated as a verdict, which is what keeps a "3 items in your
#: basket" live region from failing a fill.
_REJECTION_RE = re.compile(
    r"\b(?:required|invalid|must|cannot|can't|should|please\s+enter"
    r"|please\s+select|please\s+provide|not\s+valid|is\s+not|too\s+(?:short|long"
    r"|small|large)|at\s+least|at\s+most|no\s+more\s+than|minimum|maximum"
    r"|does\s+not\s+match|enter\s+a\s+valid|choose|missing|incorrect|error"
    r"|between)\b", re.I)

#: Conventional suffixes a form library appends to a field id for its error node.
_ERROR_ID_SUFFIXES = ("-error", "_error", "-err", "_err", "-error-message",
                      "-errormessage", "-helper-text", "-validation")


@dataclass(frozen=True)
class ValidationSignal:
    """One thing the application said about one control's value."""

    code: str
    message: str
    source: str
    #: The declared bound the message named, when it named one ("18", "5").
    detail: str = ""

    @property
    def strength(self) -> int:
        return _STRENGTH.get(self.source, 0)

    @property
    def is_anchored(self) -> bool:
        return self.source != SOURCE_PAGE

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "source": self.source,
                "detail": self.detail, "message": self.message[:200]}


@dataclass(frozen=True)
class ConstraintHint:
    """What a rejection message TELLS THE GENERATOR to change.

    A repair driven by this is driven by the application's own words; a repair
    driven by anything else is a guess wearing a retry's clothes."""

    code: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    minlength: Optional[int] = None
    maxlength: Optional[int] = None
    exact_length: Optional[int] = None
    #: The message asked for a different KIND of value ("enter a valid email").
    wants_type: str = ""
    #: Nothing actionable was extractable.  A retry on this alone would be blind.
    @property
    def actionable(self) -> bool:
        return bool(self.code and (
            self.minimum is not None or self.maximum is not None
            or self.minlength is not None or self.maxlength is not None
            or self.exact_length is not None or self.wants_type
            or self.code == C.CODE_REQUIRED))


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split())


def _lower(text: Any) -> str:
    return _norm(text).lower()


def is_cookie_banner(text: Any) -> bool:
    """A consent banner is never a verdict on a form field."""
    return bool(_COOKIE_RE.search(_norm(text)))


def is_informational(text: Any) -> bool:
    """A live region that announces rather than rejects.

    Checked AFTER the rejection vocabulary, because "your changes were saved but
    the postcode is invalid" is a rejection that happens to mention success."""
    body = _norm(text)
    if _REJECTION_RE.search(body):
        return False
    return bool(_INFORMATIONAL_RE.search(body))


class PageAlertFilter:
    """Which alerts are NEW, and therefore capable of being about this fill.

    Constructed from the alerts visible BEFORE the fill.  Everything in that
    snapshot is stale for every fill that follows on this page state, which is
    the property the old page-wide read could not express and the reason one
    real failure was reported as ten.

    Alerts are fingerprinted on their normalised text, so a banner that
    re-renders identically is still the same banner.  A message whose text
    genuinely changes ("2 errors" → "1 error") is a new fact and is treated as
    one — that is the honest direction to fail in."""

    __slots__ = ("_seen", "_suppressed")

    def __init__(self, before: Iterable[Any] = ()) -> None:
        self._seen: set[str] = set()
        self._suppressed = 0
        self.observe(before)

    def observe(self, alerts: Iterable[Any]) -> None:
        """Fold a fresh snapshot into the stale set."""
        for alert in alerts or ():
            key = self._key(alert)
            if key:
                self._seen.add(key)

    def fresh(self, alerts: Iterable[Any]) -> list[str]:
        """The alert texts that were NOT present before, minus the ones that are
        never verdicts.  Order-preserving and de-duplicated."""
        out: list[str] = []
        emitted: set[str] = set()
        for alert in alerts or ():
            text = _norm(self._text(alert))
            key = self._key(alert)
            if not text or not key:
                continue
            if key in self._seen:
                self._suppressed += 1
                continue
            if is_cookie_banner(text) or is_informational(text):
                self._suppressed += 1
                continue
            if key in emitted:
                continue
            emitted.add(key)
            out.append(text)
        return out

    @property
    def suppressed(self) -> int:
        """How many alerts were held back as stale, consenting or informational.

        Reported as a metric because it is the direct measure of the
        false-positive class this module removes: it used to be zero by
        construction, and every one of those was a field wrongly marked failed."""
        return self._suppressed

    @staticmethod
    def _text(alert: Any) -> str:
        if isinstance(alert, Mapping):
            return str(alert.get("text") or alert.get("message") or "")
        return str(alert or "")

    def _key(self, alert: Any) -> str:
        return _lower(self._text(alert))[:300]


def _control_ids(control: Mapping[str, Any]) -> set[str]:
    """Every id the control answers to — its own, and the ones it points at."""
    out: set[str] = set()
    qec = control.get("qec") if isinstance(control.get("qec"), Mapping) else {}
    for key in ("id", "testid"):
        for source in (control, qec):
            v = str((source or {}).get(key) or "").strip()
            if v:
                out.add(v)
    for key in ("aria_errormessage", "aria_describedby", "describedby",
                "errormessage"):
        for source in (control, qec):
            v = str((source or {}).get(key) or "").strip()
            for token in v.split():
                if token:
                    out.add(token)
    return out


def _referenced_ids(control: Mapping[str, Any]) -> set[str]:
    """Ids the control explicitly points AT for its message — rung 1."""
    out: set[str] = set()
    qec = control.get("qec") if isinstance(control.get("qec"), Mapping) else {}
    for key in ("aria_errormessage", "errormessage", "aria_describedby",
                "describedby"):
        for source in (control, qec):
            for token in str((source or {}).get(key) or "").split():
                if token:
                    out.add(token)
    return out


def _id_conventions(control: Mapping[str, Any]) -> set[str]:
    """Ids a form library would give this control's error node — rung 3."""
    base = str(control.get("id") or "").strip()
    if not base:
        return set()
    return {base + suffix for suffix in _ERROR_ID_SUFFIXES}


def _classify_message(message: str) -> tuple[str, str]:
    """Which declared rule the message is about, and the bound it named."""
    body = _lower(message)
    number = re.search(r"(\d[\d,]*(?:\.\d+)?)", body)
    detail = (number.group(1).replace(",", "") if number else "")
    if re.search(r"\brequired\b|\bmust be (?:provided|entered)\b|\bcannot be (?:blank|empty)\b"
                 r"|\bplease (?:enter|select|provide|choose)\b|\bmissing\b", body):
        # "Please enter a valid email" is a FORMAT complaint wearing a required
        # message's words; the "valid" is what tells them apart.
        if not re.search(r"\bvalid\b|\bformat\b|\bmatch\b", body):
            return C.CODE_REQUIRED, ""
    if re.search(r"\bat least\b|\bminimum\b|\bno less than\b|\bgreater than or equal\b"
                 r"|\btoo (?:small|low|young)\b|\bmust be (?:over|above)\b", body):
        if re.search(r"\bcharacters?\b|\bletters?\b|\bdigits?\b", body):
            return C.CODE_MINLENGTH, detail
        return C.CODE_MIN, detail
    if re.search(r"\bat most\b|\bmaximum\b|\bno more than\b|\bless than or equal\b"
                 r"|\btoo (?:large|big|high|long|old)\b|\bmust be (?:under|below)\b"
                 r"|\bexceeds?\b", body):
        if re.search(r"\bcharacters?\b|\bletters?\b|\bdigits?\b", body):
            return C.CODE_MAXLENGTH, detail
        return C.CODE_MAX, detail
    if re.search(r"\bmust be exactly\b|\bexactly \d+\b", body) and \
            re.search(r"\bcharacters?\b|\bdigits?\b", body):
        return C.CODE_MINLENGTH, detail
    if re.search(r"\bbetween\b", body):
        return C.CODE_MIN, detail
    if re.search(r"\bdoes not match\b|\bformat\b|\binvalid\b|\bnot valid\b"
                 r"|\bvalid\b|\bincorrect\b|\bpattern\b", body):
        return C.CODE_PATTERN, detail
    if re.search(r"\bselect\b|\bchoose\b|\bnot (?:an? )?(?:valid )?option\b", body):
        return C.CODE_NOT_AN_OPTION, ""
    return "", detail


def interpret(message: str) -> ConstraintHint:
    """Turn a rejection message into something the generator can act on.

    Deliberately conservative.  A hint that is not :attr:`ConstraintHint.actionable`
    tells the repair loop to STOP rather than to try again differently — a retry
    that cannot say what it is changing and why is exactly the blind retry this
    architecture forbids."""
    body = _norm(message)
    if not body:
        return ConstraintHint()
    code, detail = _classify_message(body)
    if not code:
        return ConstraintHint()

    number: Optional[float] = None
    try:
        number = float(detail) if detail else None
    except ValueError:
        number = None

    lower = body.lower()
    wants = ""
    for token, kind in (("email", "email"), ("phone", "phone"),
                        ("date", "date"), ("number", "number"),
                        ("url", "url"), ("postcode", "postal_code"),
                        ("zip", "postal_code")):
        if re.search(r"\b" + token + r"\b", lower):
            wants = kind
            break

    if code == C.CODE_MINLENGTH:
        n = int(number) if number is not None else None
        if re.search(r"\bexactly\b", lower) and n is not None:
            return ConstraintHint(code=code, exact_length=n, wants_type=wants)
        return ConstraintHint(code=code, minlength=n, wants_type=wants)
    if code == C.CODE_MAXLENGTH:
        return ConstraintHint(code=code,
                              maxlength=int(number) if number is not None else None,
                              wants_type=wants)
    if code == C.CODE_MIN:
        # "must be between 18 and 65" names both bounds; take them both.
        both = re.search(r"between\s+(\d[\d,]*)\s+and\s+(\d[\d,]*)", lower)
        if both:
            return ConstraintHint(code=code,
                                  minimum=float(both.group(1).replace(",", "")),
                                  maximum=float(both.group(2).replace(",", "")),
                                  wants_type=wants)
        return ConstraintHint(code=code, minimum=number, wants_type=wants)
    if code == C.CODE_MAX:
        return ConstraintHint(code=code, maximum=number, wants_type=wants)
    if code == C.CODE_REQUIRED:
        return ConstraintHint(code=code)
    return ConstraintHint(code=code, wants_type=wants)


def signals_for_control(
    control: Mapping[str, Any],
    *,
    fresh_alerts: Sequence[Any] = (),
    after_controls: Sequence[Mapping[str, Any]] = (),
    native_message: str = "",
    control_name: str = "",
) -> list[ValidationSignal]:
    """Everything the application said about THIS control's value.

    ``fresh_alerts`` must already have been through :class:`PageAlertFilter` —
    passing a raw page read here re-opens the very hole this module closes, so
    the caller is required to have made the staleness decision first.

    ``after_controls`` is the re-read inventory: it carries the control's own
    ``aria-invalid`` and the error nodes the DOM now contains, which is where
    rungs 1 to 3 come from.

    Returns the signals ANCHORED to this control, strongest first.  A page-level
    alert that anchors to nothing is deliberately NOT returned — the caller
    records it as page context, and it never fails a field."""
    name = _norm(control_name or control.get("name"))
    own_ids = _control_ids(control)
    referenced = _referenced_ids(control)
    conventions = _id_conventions(control)
    out: list[ValidationSignal] = []

    # rung 2 — the browser's own verdict, and the control saying it is invalid.
    if _norm(native_message):
        code, detail = _classify_message(native_message)
        out.append(ValidationSignal(code or C.CODE_PATTERN, _norm(native_message),
                                    SOURCE_NATIVE, detail))

    invalid_declared = False
    for candidate in after_controls or ():
        if not isinstance(candidate, Mapping):
            continue
        if not _same_control(candidate, control, name):
            continue
        flag = str(candidate.get("aria_invalid")
                   or (candidate.get("qec") or {}).get("aria_invalid") or "").lower()
        if flag in ("true", "1", "yes"):
            invalid_declared = True
        message = _norm(candidate.get("error_text")
                        or (candidate.get("qec") or {}).get("error_text"))
        if message:
            code, detail = _classify_message(message)
            out.append(ValidationSignal(code or C.CODE_PATTERN, message,
                                        SOURCE_ARIA_ERRORMESSAGE, detail))

    # rungs 1 and 3 — an error node the control points at, or one named by
    # convention after the control's own id.
    for candidate in after_controls or ():
        if not isinstance(candidate, Mapping):
            continue
        node_id = str(candidate.get("id") or "").strip()
        if not node_id:
            continue
        text = _norm(candidate.get("text") or candidate.get("name"))
        if not text:
            continue
        if node_id in referenced:
            code, detail = _classify_message(text)
            out.append(ValidationSignal(code or C.CODE_PATTERN, text,
                                        SOURCE_ARIA_ERRORMESSAGE, detail))
        elif node_id in conventions:
            code, detail = _classify_message(text)
            out.append(ValidationSignal(code or C.CODE_PATTERN, text,
                                        SOURCE_ID_CONVENTION, detail))

    # rung 4 — the message NAMES the control.  Weakest anchor, and only used
    # when the alert is a rejection at all.
    for alert in fresh_alerts or ():
        text = _norm(alert if not isinstance(alert, Mapping)
                     else (alert.get("text") or alert.get("message")))
        if not text or not _REJECTION_RE.search(text):
            continue
        anchored = False
        alert_id = ""
        if isinstance(alert, Mapping):
            alert_id = str(alert.get("id") or "").strip()
        if alert_id and (alert_id in referenced or alert_id in conventions):
            code, detail = _classify_message(text)
            out.append(ValidationSignal(
                code or C.CODE_PATTERN, text,
                SOURCE_ARIA_ERRORMESSAGE if alert_id in referenced
                else SOURCE_ID_CONVENTION, detail))
            anchored = True
        # Case-folded on BOTH sides: the control's accessible name is title-cased
        # ("Date of Birth") and the message is a sentence, so an un-folded
        # containment test silently never matched and rung 4 was dead.
        if (not anchored and name and len(name) >= 3
                and name.lower() in text.lower()):
            code, detail = _classify_message(text)
            out.append(ValidationSignal(code or C.CODE_PATTERN, text,
                                        SOURCE_NAMED, detail))

    if invalid_declared and not out:
        # The control declares itself invalid and published no message.  That is
        # still a verdict about THIS control, and it is honestly unspecific.
        out.append(ValidationSignal(
            C.CODE_PATTERN,
            "the control declares aria-invalid=true and published no message",
            SOURCE_ARIA_INVALID))

    out.sort(key=lambda s: -s.strength)
    # De-duplicate on (code, message): a form library often publishes the same
    # sentence through two channels, and counting it twice would exhaust a
    # repair budget on one complaint.
    seen: set[tuple[str, str]] = set()
    deduped: list[ValidationSignal] = []
    for signal in out:
        key = (signal.code, signal.message.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(signal)
    return deduped


def _same_control(candidate: Mapping[str, Any], control: Mapping[str, Any],
                  name: str) -> bool:
    """Is this re-read record the SAME control we just filled?

    Anchored on the identifiers that survive a React re-render — the id, the
    test id, the css hint — and only then on the accessible name, which a widget
    may have rewritten to show its own selection."""
    for key in ("id", "testid", "css_hint"):
        mine = str(control.get(key) or (control.get("qec") or {}).get(key) or "").strip()
        theirs = str(candidate.get(key) or (candidate.get("qec") or {}).get(key) or "").strip()
        if mine and theirs and mine == theirs:
            return True
    return bool(name) and _norm(candidate.get("name")).lower() == name.lower()
