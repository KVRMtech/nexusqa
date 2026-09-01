"""De-identification for the flywheel ledger (Phase 2 foundation).

Turns raw, app-specific signals into COARSE, non-identifying features so a human
correction can become a labeled example WITHOUT ever persisting raw text,
values, selectors, or accessible names. Every output here is an enum / bucket /
shape / per-tenant HMAC — the privacy floor the consent contract promises and
the thing that lets a regulated buyer's security team say yes.

Pure, deterministic, $0. The raw input NEVER flows into a returned value — only a
class/shape/bucket/hash derived from it. (The ledger schema also has no raw
column, so de-id is enforced structurally, not just by convention.)
"""

from __future__ import annotations

import hashlib
import hmac
import re

# Reuse the SAME error regexes the verdict reducer uses, so an error's CLASS here
# aligns with classify_failure's interpretation (consistency, not a fork).
from ..test_runs import _LOCATOR_MISSING_RE, _OUTCOME_ASSERT_RE

_TIMEOUT_RE = re.compile(r"timeout|timed out", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")
# A clean number / currency / percent (whole string): $30,000 · 3800 · 12.5%
_NUM_RE = re.compile(r"^[\s$£€]*\d[\d,]*(\.\d+)?\s*%?$")
# A run of 3+ digits anywhere → treat as a likely identifier (SSN/acct/VIN/card).
_DIGIT_RUN_RE = re.compile(r"\d{3,}")


def error_pattern_class(error_message: str) -> str:
    """The CLASS of a failure error (matched-regex bucket) — never the text.
    Aligned to the verdict reducer's own signals."""
    e = error_message or ""
    if not e.strip():
        return "none"
    if _OUTCOME_ASSERT_RE.search(e):
        return "outcome_contradicted"   # a failed toHaveURL — the real-regression signal
    if _LOCATOR_MISSING_RE.search(e):
        return "locator_missing"
    if _TIMEOUT_RE.search(e):
        return "timeout"
    return "other"


def value_shape_sig(value: str) -> str:
    """Coarse SHAPE of an entered value — never the value itself. Order matters:
    date → clean-number → digit-run(identifier-ish) → free-text."""
    v = (value or "").strip()
    if not v:
        return "empty"
    if _DATE_RE.match(v):
        return "date"
    if _NUM_RE.match(v):
        return "numeric"
    if _DIGIT_RUN_RE.search(v):
        return "masked"                 # contains a digit-run → sensitive/identifier
    return "free-text"


def options_count_bucket(n: int | None) -> str:
    n = int(n or 0)
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    return "2+"


def score_bucket(x: float | None) -> str:
    """Coarse confidence/similarity bucket — high ≥0.8 / medium 0.5–0.8 / low."""
    if x is None:
        return ""
    if x >= 0.8:
        return "high"
    if x >= 0.5:
        return "medium"
    return "low"


def label_token_hash(name: str, tenant_secret: str) -> str:
    """Per-tenant HMAC of an accessible name: joins examples WITHIN a tenant,
    opaque ACROSS tenants (no global rainbow table; the name is never stored).
    Empty in → empty out. The tenant_secret is the per-tenant salt (e.g. a
    derived key), so the same label in two tenants hashes differently."""
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not n:
        return ""
    key = (tenant_secret or "nexus-flywheel").encode("utf-8")
    return hmac.new(key, n.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


__all__ = [
    "error_pattern_class",
    "value_shape_sig",
    "options_count_bucket",
    "score_bucket",
    "label_token_hash",
]
