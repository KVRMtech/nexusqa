"""WHAT THE APPLICATION ITSELF DEMANDS OF THE VALUE.

Every one of these attributes was already captured.  ``app.inventory`` reads
``pattern``, ``min``, ``max``, ``step``, ``minlength``, ``maxlength`` and
``required`` off the DOM and puts them on the control record;
``app.field_signature`` folds them into the learning key; ``app.field_semantics``
uses ``pattern`` to CLASSIFY a field.

Nothing used them to GENERATE.  ``field_values`` honoured ``maxlength`` and a
numeric ``min``/``max`` and ignored the rest — so a field declaring
``pattern="[0-9]{5}(-[0-9]{4})?"`` was answered with a postcode that happened to
fit, and one declaring ``pattern="^[A-Z]{2}\\d{6}$"`` was answered with
``autotest`` and rejected.  The application had described exactly what it wanted,
in a machine-readable form, and the generator never read it.

This module turns those declarations into a first-class object:

    :class:`Constraints`   everything the control declares, normalised
    :func:`extract`        read them off a control record
    :func:`violations`     which of them a candidate value breaks, and how
    :func:`conform`        reshape a value so it stops breaking them

``violations`` is the same predicate the repair loop uses to decide whether a
retry is warranted, and the same one the generator uses to check its own output
before committing — so a value that passes here and is then rejected by the
application tells us something we did not know, which is exactly the signal
:mod:`app.fill_engine.repair` is built to act on.

PURE + DETERMINISTIC.  No I/O, no clock.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence

from . import patterns as pattern_engine

__all__ = [
    "Constraints", "Violation", "extract", "violations", "conform", "satisfies",
    "CODE_REQUIRED", "CODE_PATTERN", "CODE_MINLENGTH", "CODE_MAXLENGTH",
    "CODE_MIN", "CODE_MAX", "CODE_STEP", "CODE_NOT_AN_OPTION",
    "CODE_DATE_MIN", "CODE_DATE_MAX", "CODE_TYPE",
]

CODE_REQUIRED = "required"
CODE_PATTERN = "pattern"
CODE_MINLENGTH = "minlength"
CODE_MAXLENGTH = "maxlength"
CODE_MIN = "min"
CODE_MAX = "max"
CODE_STEP = "step"
CODE_NOT_AN_OPTION = "not_an_option"
CODE_DATE_MIN = "date_min"
CODE_DATE_MAX = "date_max"
CODE_TYPE = "type"

_DIGITS_RE = re.compile(r"\D+")
_NUMERIC_TYPES = frozenset({"number", "range"})
_DATE_TYPES = frozenset({"date", "month", "week", "datetime-local"})
#: Types whose value has a shape the browser itself enforces, so a generator
#: that ignores it produces a value the field silently refuses to hold.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
_URL_RE = re.compile(r"^https?://[^\s]+$")


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _qec(control: Mapping[str, Any]) -> Mapping[str, Any]:
    q = control.get("qec")
    return q if isinstance(q, Mapping) else {}


def _attr(control: Mapping[str, Any], *keys: str) -> str:
    """First non-empty of several attribute spellings, checking the nested
    ``qec`` envelope too — the inventory populates one or the other depending on
    how the control was discovered."""
    q = _qec(control)
    for key in keys:
        v = control.get(key)
        if v in (None, ""):
            v = q.get(key)
        if v not in (None, ""):
            return _s(v)
    return ""


def _as_int(text: str) -> Optional[int]:
    try:
        n = int(float(text))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _as_float(text: str) -> Optional[float]:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


#: Each temporal input type has its OWN wire format, and a value in the wrong
#: one is not merely out of range — the browser refuses to hold it, so the field
#: never advances.  Validating every temporal control against ISO dates marked
#: correct ``2026-W15`` and ``2026-08`` values invalid and threw away a fill that
#: would have worked.
_TEMPORAL_FORMATS = {
    "date": ("%Y-%m-%d",),
    "month": ("%Y-%m",),
    "datetime-local": ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"),
    "": ("%Y-%m-%d", "%Y-%m", "%Y-%m-%dT%H:%M"),
}
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def _as_date(text: str, input_type: str = "") -> Optional[date]:
    """Parse a temporal value IN THE FORMAT ITS CONTROL DEMANDS.

    ``week`` has no ``strptime`` round trip that is safe across platforms, so it
    is matched structurally and resolved to the Monday of that ISO week — enough
    to compare against a declared bound, which is all a bound needs."""
    text = (text or "").strip()
    if not text:
        return None
    if input_type == "week" or (not input_type and _WEEK_RE.match(text)):
        m = _WEEK_RE.match(text)
        if not m:
            return None
        year, week = int(m.group(1)), int(m.group(2))
        if not 1 <= week <= 53:
            return None
        try:
            return date.fromisocalendar(year, week, 1)
        except ValueError:
            return None
    for fmt in _TEMPORAL_FORMATS.get(input_type, _TEMPORAL_FORMATS[""]):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Violation:
    """One rule a value breaks, and the fact needed to stop breaking it.

    ``code``    which rule (a ``CODE_*`` constant).
    ``detail``  the declared bound, verbatim — what to aim at.
    ``message`` a sentence a human reads in the evidence, and the repair loop
                records as its reason for choosing a different value.
    """

    code: str
    detail: str = ""
    message: str = ""


@dataclass(frozen=True)
class Constraints:
    """Everything the control declares about acceptable values."""

    required: bool = False
    pattern: str = ""
    minlength: Optional[int] = None
    maxlength: Optional[int] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    date_min: Optional[date] = None
    date_max: Optional[date] = None
    input_type: str = ""
    options: tuple[str, ...] = ()
    multiple: bool = False
    #: True when the control declared NOTHING — used to say honestly that a
    #: value was unconstrained rather than that it satisfied every constraint.
    declared: bool = False

    @property
    def is_numeric(self) -> bool:
        return (self.input_type in _NUMERIC_TYPES
                or self.minimum is not None or self.maximum is not None)

    @property
    def is_temporal(self) -> bool:
        return self.input_type in _DATE_TYPES

    def as_dict(self) -> dict[str, Any]:
        """Evidence projection — the declaration a generated value was shaped by."""
        out: dict[str, Any] = {}
        if self.required:
            out["required"] = True
        if self.pattern:
            out["pattern"] = self.pattern[:120]
        for key, value in (("minlength", self.minlength),
                           ("maxlength", self.maxlength),
                           ("min", self.minimum), ("max", self.maximum),
                           ("step", self.step)):
            if value is not None:
                out[key] = value
        if self.date_min is not None:
            out["date_min"] = self.date_min.isoformat()
        if self.date_max is not None:
            out["date_max"] = self.date_max.isoformat()
        if self.input_type:
            out["input_type"] = self.input_type
        if self.options:
            out["option_count"] = len(self.options)
        return out


def extract(control: Mapping[str, Any], *, kind: str = "") -> Constraints:
    """Read the control's own declarations.

    A numeric ``min``/``max`` and a temporal one share the attribute name, so
    both are parsed and only the one that parses is kept — an
    ``<input type=date min="2020-01-01">`` must not produce ``minimum=None`` AND
    a silently dropped bound."""
    input_type = _attr(control, "input_type", "type").lower()
    kind_n = _s(kind).lower() or _s(control.get("kind")).lower()

    raw_min, raw_max = _attr(control, "min"), _attr(control, "max")
    numeric_min = _as_float(raw_min) if raw_min else None
    numeric_max = _as_float(raw_max) if raw_max else None
    date_min = _as_date(raw_min, input_type) if raw_min else None
    date_max = _as_date(raw_max, input_type) if raw_max else None
    # A date bound parses as a float only by accident ("2020" would); when the
    # control is temporal the date reading is the correct one.
    if input_type in _DATE_TYPES or kind_n == "date":
        numeric_min = numeric_max = None
    elif date_min is not None or date_max is not None:
        # Conversely a numeric field whose bound happens to look like a date is
        # numeric.  Only a genuinely temporal control keeps the date reading.
        date_min = date_max = None

    options = control.get("options")
    if not isinstance(options, (list, tuple)) or not options:
        options = control.get("group_options")
    opts = tuple(_s(o) for o in (options or ()) if _s(o))

    required = bool(control.get("required") or _qec(control).get("required"))
    pattern = _attr(control, "pattern")
    minlength = _as_int(_attr(control, "minlength"))
    maxlength = _as_int(_attr(control, "maxlength"))
    step = _as_float(_attr(control, "step"))

    declared = bool(required or pattern or minlength is not None
                    or maxlength is not None or numeric_min is not None
                    or numeric_max is not None or date_min is not None
                    or date_max is not None or opts)

    return Constraints(
        required=required, pattern=pattern, minlength=minlength,
        maxlength=maxlength, minimum=numeric_min, maximum=numeric_max,
        step=step, date_min=date_min, date_max=date_max,
        input_type=input_type, options=opts,
        multiple=bool(control.get("multiple") or _qec(control).get("multiple")),
        declared=declared,
    )


def violations(value: Any, c: Constraints, *,
               check_options: bool = True) -> list[Violation]:
    """Which declared rules this value breaks.

    The list is ORDERED by how much the repair loop can do about it: a length or
    range breach names a number to aim at, a pattern breach names a shape, and a
    missing required value names nothing but its own absence.  A caller that
    repairs the first violation and re-checks converges, which is what
    :mod:`app.fill_engine.repair` relies on."""
    text = "" if value is None else str(value)
    out: list[Violation] = []

    if not text.strip():
        if c.required:
            out.append(Violation(CODE_REQUIRED, "",
                                 "the application declares this field required"))
        return out

    if c.minlength is not None and len(text) < c.minlength:
        out.append(Violation(
            CODE_MINLENGTH, str(c.minlength),
            f"declared minlength={c.minlength}, value is {len(text)} character(s)"))
    if c.maxlength is not None and len(text) > c.maxlength:
        out.append(Violation(
            CODE_MAXLENGTH, str(c.maxlength),
            f"declared maxlength={c.maxlength}, value is {len(text)} character(s)"))

    if c.is_numeric:
        n = _as_float(text)
        if n is None:
            out.append(Violation(CODE_TYPE, c.input_type,
                                 "the control is numeric and the value is not"))
        else:
            if c.minimum is not None and n < c.minimum:
                out.append(Violation(CODE_MIN, str(c.minimum),
                                     f"declared min={c.minimum}, value is {n}"))
            if c.maximum is not None and n > c.maximum:
                out.append(Violation(CODE_MAX, str(c.maximum),
                                     f"declared max={c.maximum}, value is {n}"))
            if c.step and c.step > 0:
                base = c.minimum if c.minimum is not None else 0.0
                offset = (n - base) / c.step
                if abs(offset - round(offset)) > 1e-9:
                    out.append(Violation(
                        CODE_STEP, str(c.step),
                        f"declared step={c.step} from {base}, value is {n}"))

    if c.is_temporal:
        d = _as_date(text, c.input_type)
        if d is None:
            out.append(Violation(CODE_TYPE, c.input_type,
                                 f"the control is {c.input_type} and the value "
                                 "is not a date in its format"))
        else:
            if c.date_min is not None and d < c.date_min:
                out.append(Violation(
                    CODE_DATE_MIN, c.date_min.isoformat(),
                    f"declared min={c.date_min.isoformat()}, value is {d.isoformat()}"))
            if c.date_max is not None and d > c.date_max:
                out.append(Violation(
                    CODE_DATE_MAX, c.date_max.isoformat(),
                    f"declared max={c.date_max.isoformat()}, value is {d.isoformat()}"))

    if c.pattern and not pattern_engine.matches(text, c.pattern):
        out.append(Violation(
            CODE_PATTERN, c.pattern[:120],
            f"declared pattern={c.pattern[:60]!r}, value does not match"))

    if c.input_type == "email" and not _EMAIL_RE.match(text):
        out.append(Violation(CODE_TYPE, "email",
                             "the control is type=email and the value is not "
                             "an address the browser will accept"))
    if c.input_type == "url" and not _URL_RE.match(text):
        out.append(Violation(CODE_TYPE, "url",
                             "the control is type=url and the value is not an "
                             "absolute http(s) URL"))

    if check_options and c.options:
        norm = _norm(text)
        if norm and not any(_norm(o) == norm for o in c.options):
            out.append(Violation(
                CODE_NOT_AN_OPTION, "",
                "the value is not one of the options the control offers"))

    return out


def satisfies(value: Any, c: Constraints, **kwargs: Any) -> bool:
    """True when nothing declared is broken."""
    return not violations(value, c, **kwargs)


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def conform(value: Any, c: Constraints, *, semantic: str = "") -> Optional[str]:
    """Reshape a semantically-correct value so it stops breaking a declaration.

    Ordered by how much meaning each step preserves, because a value that still
    MEANS the right thing is always better than one that merely validates:

      1. strip punctuation the declared length has no room for — a phone number
         that must fit ``maxlength=10`` is the same phone number without its
         dashes, and this alone fixes the most common real breach;
      2. clamp a number into the declared range, and snap it to the declared
         step;
      3. pad or trim to the declared length;
      4. only if the value still breaks a declared PATTERN, ask
         :mod:`app.fill_engine.patterns` for a value the pattern accepts — which
         is grounded (the application described it) but carries no meaning, so it
         is the last resort rather than the first.

    Returns ``None`` when nothing here can produce a conforming value; the caller
    then leaves the field honestly empty rather than typing something that will
    be rejected."""
    if value is None:
        return None
    text = str(value)

    if c.is_numeric:
        text = _conform_number(text, c)
        if text is None:
            return None
    elif c.is_temporal:
        text = _conform_date(text, c) or text

    # 1 · make room by dropping formatting, never by dropping information.
    if c.maxlength is not None and len(text) > c.maxlength:
        stripped = _DIGITS_RE.sub("", text)
        if stripped and len(stripped) <= c.maxlength and _looks_numeric(text):
            text = stripped
        else:
            text = text[:c.maxlength]

    # 3 · a declared minimum length, met without changing what the value means
    # where possible: repeat the value's own last character rather than invent a
    # word, so "12" under minlength=5 becomes "12222" and not "12abc".
    if c.minlength is not None and len(text) < c.minlength and text:
        if c.is_numeric or text[-1].isdigit():
            text = text.ljust(c.minlength, text[-1] if text[-1].isdigit() else "0")
        else:
            text = text.ljust(c.minlength, text[-1])

    if not c.pattern or pattern_engine.matches(text, c.pattern):
        return text

    # 4 · the value does not match a declared pattern.  Try the value's own
    # digits first — an SSN typed as 123456789 against ``\d{3}-\d{2}-\d{4}`` is
    # the same SSN, and re-deriving one would throw away a value the persona
    # chose for a reason.
    reshaped = pattern_engine.reshape(text, c.pattern)
    if reshaped is not None and pattern_engine.matches(reshaped, c.pattern):
        return _fit(reshaped, c)

    generated = pattern_engine.satisfy(c.pattern, hint=text)
    if generated is not None and pattern_engine.matches(generated, c.pattern):
        return _fit(generated, c)
    return None


def _fit(text: str, c: Constraints) -> str:
    if c.maxlength is not None and len(text) > c.maxlength:
        return text[:c.maxlength]
    return text


def _looks_numeric(text: str) -> bool:
    """Is this a formatted NUMBER (a phone, a card, a postcode) rather than
    prose?  Only then is dropping punctuation information-preserving."""
    digits = sum(1 for ch in text if ch.isdigit())
    return digits >= max(3, len(text) // 2)


def _conform_number(text: str, c: Constraints) -> Optional[str]:
    n = _as_float(text)
    if n is None:
        n = _as_float(_DIGITS_RE.sub("", text) or "")
    if n is None:
        return None
    if c.minimum is not None and n < c.minimum:
        n = c.minimum
    if c.maximum is not None and n > c.maximum:
        n = c.maximum
    if c.step and c.step > 0:
        base = c.minimum if c.minimum is not None else 0.0
        n = base + round((n - base) / c.step) * c.step
        # Snapping can leave the range; clamp back INWARD so the result is
        # always legal, preferring the bound that is itself a valid step.
        if c.maximum is not None and n > c.maximum:
            n = base + int((c.maximum - base) / c.step) * c.step
        if c.minimum is not None and n < c.minimum:
            n = c.minimum
    return str(int(n)) if float(n).is_integer() else str(n)


def _conform_date(text: str, c: Constraints) -> Optional[str]:
    d = _as_date(text, c.input_type) or _as_date(text)
    if d is None:
        return None
    if c.date_min is not None and d < c.date_min:
        d = c.date_min
    if c.date_max is not None and d > c.date_max:
        d = c.date_max
    if c.input_type == "month":
        return d.strftime("%Y-%m")
    if c.input_type == "week":
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if c.input_type == "datetime-local":
        return f"{d.isoformat()}T12:00"
    return d.isoformat()


def option_matching(options: Sequence[str], *wanted: str) -> Optional[str]:
    """The offered option that best matches any of ``wanted``.

    Exact first, then containment in either direction — a region dropdown that
    offers "California" must match an identity whose region_code is "CA" and
    whose region_name is "California", and neither spelling can be assumed."""
    opts = [o for o in (options or ()) if _s(o)]
    if not opts:
        return None
    targets = [_norm(w) for w in wanted if _s(w)]
    for t in targets:
        for o in opts:
            if _norm(o) == t:
                return o
    for t in targets:
        if not t:
            continue
        for o in opts:
            if t in _norm(o) or _norm(o) in t:
                return o
    return None
