"""Behavioral regression diff (Phase 5, pure core).

Compares a cycle's OBSERVED outcomes against the last-accepted baseline and classifies
each field's change. The never-green-wash key is FAIL-SAFE oracle detection: because a
reliable per-field OBSERVE tag does not exist in the substrate yet, any value that LOOKS
like a business value — currency, an amount, a number — is treated as an ORACLE field.
So a premium/balance change ($250 → $180) is a GENUINE regression even when nothing
explicitly tagged it, closing the hole where an untagged value change could be waved
through as "benign display drift".

Pure and deterministic — no DB, no clock.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# Change kinds.
UNCHANGED = "unchanged"
VALUE_CHANGED = "value_changed"
MISSING = "missing"      # baseline had it, this run did not observe it
NEW = "new"              # this run observed it, baseline had none

# A value that looks like money / an amount / a number → treated as an oracle field
# (fail-safe). Currency symbols, thousands separators, decimals, or a bare number.
_VALUE_SHAPED_RE = re.compile(
    r"""^\s*(?:
        [$£€¥]\s*\d[\d,]*(?:\.\d+)?      |   # $250, £1,000.00
        \d[\d,]*(?:\.\d+)?\s*(?:usd|dollars|%)? |  # 250, 1,000.00, 12%
        \d+(?:\.\d+)?
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def is_value_shaped(value: object) -> bool:
    """True when a value looks like money/amount/number — the fail-safe oracle signal."""
    if value is None:
        return False
    return bool(_VALUE_SHAPED_RE.match(str(value).strip()))


def diff_field(
    field: str, baseline_val: object, current_val: object, *, oracle_fields: set[str] | None = None,
) -> dict:
    """Diff one observed field. ``is_oracle`` is True when the field is explicitly
    tagged OR either value is value-shaped (fail-safe)."""
    oracle_fields = oracle_fields or set()
    b_none = baseline_val is None
    c_none = current_val is None
    is_oracle = (
        field in oracle_fields
        or is_value_shaped(baseline_val)
        or is_value_shaped(current_val)
    )
    base = {"field": field, "old": baseline_val, "new": current_val, "is_oracle": is_oracle}
    if b_none and not c_none:
        return {**base, "kind": NEW}
    if c_none and not b_none:
        return {**base, "kind": MISSING}
    if str(baseline_val) == str(current_val):
        return {**base, "kind": UNCHANGED}
    return {**base, "kind": VALUE_CHANGED}


def diff(
    baseline: Mapping[str, object], current: Mapping[str, object], *,
    oracle_fields: Iterable[str] = (),
) -> list[dict]:
    """Diff all observed fields (union of baseline + current keys). Deterministic order."""
    of = set(oracle_fields)
    b = dict(baseline or {})
    c = dict(current or {})
    fields = sorted(set(b) | set(c))
    return [diff_field(f, b.get(f), c.get(f), oracle_fields=of) for f in fields]


def changed(diffs: Iterable[Mapping]) -> list[dict]:
    return [d for d in diffs if d.get("kind") != UNCHANGED]


def oracle_value_changes(diffs: Iterable[Mapping]) -> list[dict]:
    """Value changes on oracle fields — the ones that force a GENUINE regression."""
    return [d for d in diffs if d.get("kind") == VALUE_CHANGED and d.get("is_oracle")]
