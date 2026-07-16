"""Value-oracle INFERENCE (#2) — crawl-side candidate expected-value classifier.

From a page's captured ``displayed_values`` (rendered value nodes — a premium, a
total, a decision message), classify each node's VALUE TYPE and flag the
high-signal ones as CANDIDATE expected values a generated test should assert on.

This is INFERENCE ONLY — it *surfaces candidates for confirmation*.  The actual
PROVING of whether the app's output is correct stays in the FROZEN factory value
oracle (grounded reducer), which this module never touches.  Pure + deterministic
so the classification is auditable and reproducible from the evidence alone.
"""
from __future__ import annotations

import re

VALUE_TYPE_CURRENCY = "currency"
VALUE_TYPE_PERCENT = "percent"
VALUE_TYPE_DECISION = "decision"
VALUE_TYPE_DATE = "date"
VALUE_TYPE_REFERENCE = "reference"
VALUE_TYPE_NUMBER = "number"
VALUE_TYPE_OTHER = "other"

#: A monetary amount ($75.00 / USD 75 / £1,200.50) — the archetypal quote/premium
#: /total outcome a test should assert.
_CURRENCY_RE = re.compile(r"(?:[$€£¥]\s?|\b(?:usd|eur|gbp|cad|aud)\s+)\d[\d,]*(?:\.\d+)?", re.I)
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s?%")
_DATE_RE = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b")
#: A whole node that is essentially one number (allowing surrounding punctuation).
_NUMBER_RE = re.compile(r"^\D{0,3}\d[\d,]*(?:\.\d+)?\D{0,3}$")
#: An alphanumeric reference / confirmation / policy id (has BOTH letters+digits).
_REFERENCE_RE = re.compile(r"\b[A-Za-z0-9]{2,}-?[A-Za-z0-9]{3,}\b")

#: Words that render a decision / status OUTCOME (a value a test asserts on).
_DECISION_RE = re.compile(
    r"\b(approved|declined?|denied|accepted|rejected|eligible|ineligible|"
    r"qualified|unqualified|pending|confirmed|successful|success|failed|"
    r"complete|completed|active|inactive|expired|cancell?ed|bound|issued|"
    r"in\s+review|under\s+review|in\s+progress)\b", re.I)

#: Labels that mark a value as a meaningful OUTCOME (boosts candidacy + confidence).
_OUTCOME_LABEL_RE = re.compile(
    r"\b(premium|total|subtotal|amount|cost|price|balance|due|payment|quote|"
    r"rate|apr|apy|interest|fee|deductible|coverage|benefit|payout|refund|"
    r"decision|status|result|outcome|eligibility|approval|confirmation|"
    r"reference|policy|account|number|score|estimate)\b", re.I)

#: Value types that are strong candidate expected outcomes on their own.
_HIGH_SIGNAL = frozenset({VALUE_TYPE_CURRENCY, VALUE_TYPE_PERCENT, VALUE_TYPE_DECISION})


def classify_value(text: str) -> str:
    """Classify one rendered value's TEXT into a value type (pure).  Ordered most-
    to least specific so a ``$75.00`` is currency, not a bare number."""
    t = (text or "").strip()
    if not t:
        return VALUE_TYPE_OTHER
    if _CURRENCY_RE.search(t):
        return VALUE_TYPE_CURRENCY
    if _PERCENT_RE.search(t):
        return VALUE_TYPE_PERCENT
    if _DECISION_RE.search(t):
        return VALUE_TYPE_DECISION
    if _DATE_RE.search(t):
        return VALUE_TYPE_DATE
    if _NUMBER_RE.match(t):
        return VALUE_TYPE_NUMBER
    if _REFERENCE_RE.search(t) and any(c.isdigit() for c in t) and any(c.isalpha() for c in t):
        return VALUE_TYPE_REFERENCE
    return VALUE_TYPE_OTHER


def infer_candidate(label: str, text: str) -> dict:
    """Infer whether a value node is a CANDIDATE expected value + why (pure).

    Returns ``{value_type, is_candidate, confidence, reason}``.  A currency /
    percent / decision value is a candidate on its own; a reference id or a bare
    number is a candidate only under an outcome-shaped label (premium/total/…) —
    otherwise it is context, not an assertable outcome."""
    vtype = classify_value(text)
    label_outcome = bool(_OUTCOME_LABEL_RE.search(label or ""))
    if vtype in _HIGH_SIGNAL:
        confidence = 0.9 if label_outcome else 0.75
        reason = f"{vtype} value" + (" under an outcome label" if label_outcome else "")
        return {"value_type": vtype, "is_candidate": True,
                "confidence": confidence, "reason": reason}
    if label_outcome and vtype in (VALUE_TYPE_REFERENCE, VALUE_TYPE_NUMBER):
        return {"value_type": vtype, "is_candidate": True, "confidence": 0.55,
                "reason": f"{vtype} value under an outcome label"}
    return {"value_type": vtype, "is_candidate": False, "confidence": 0.0, "reason": ""}
