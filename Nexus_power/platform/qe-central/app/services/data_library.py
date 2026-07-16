"""Per-client data library — ASK→CARRY slot resolution (Phase 4 WS3, pure core).

A client enters a real secret ONCE (a test SSN, member id, sandbox login) under a
HUMAN-ASSIGNED semantic slot key. Every future field that clearly maps to that slot is
resolved as CARRY instead of re-ASKed. The never-green-wash guarantee lives here: a
secret's identity is NEVER shape-inferred (a 9-digit SSN and a 9-digit routing number
must not be interchanged), and when a field ambiguously matches TWO distinct slots the
library REFUSES to carry and the field stays ASK.

This module is the pure slot-resolution + collision guard. The encrypted storage
(``qec_data_library`` table sealed with the tenant KEK via EnvelopeService, fail-closed
when no KEK) is a thin persistence layer over these decisions and is wired separately;
the guarantee that matters — the right value into the right field, or none — is proven
here without any DB.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# Resolution outcomes.
CARRY = "CARRY"
ASK = "ASK"

# Reasons (machine-checkable).
R_MATCH = "matched a single library slot by semantic key"
R_NO_SLOT = "no library slot matches this field — stays ASK"
R_COLLISION = "ambiguous: field matches more than one distinct slot — refusing to carry"
R_NO_VALUE = "slot matched but holds no value yet — stays ASK"


def _norm(s: str) -> str:
    s = re.sub(r"[\s_\-]+", " ", str(s or "").strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def _slot_matches(slot_key: str, field_label: str) -> bool:
    """True when a field CLEARLY maps to a slot — a strict, symmetric substring test on
    the normalised, multi-word semantic key (NOT a shape/length inference). Strictness
    is deliberate: a false non-match just re-ASKs (safe); a false match could carry the
    wrong secret (unsafe)."""
    slot = _norm(slot_key)
    label = _norm(field_label)
    if not slot or not label:
        return False
    if slot == label:
        return True
    # Multi-word semantic keys ("member id") must appear as a phrase; single short
    # tokens ("ssn") must match the whole label or a whole-word occurrence to avoid
    # spurious substrings.
    if " " in slot:
        return slot in label
    return re.search(rf"\b{re.escape(slot)}\b", label) is not None


def matching_slots(field_label: str, slot_keys: Iterable[str]) -> list[str]:
    """Every library slot that maps to ``field_label`` (usually 0 or 1)."""
    return [s for s in slot_keys if _slot_matches(s, field_label)]


def resolve(field_label: str, slots: Mapping[str, bool]) -> dict:
    """Resolve one field against the library. PURE.

    ``slots`` maps ``slot_key → has_value``. Returns
    ``{disposition, slot_key, reason}``: CARRY only when EXACTLY ONE slot matches AND it
    holds a value; ASK on no match, ambiguous match (collision), or an empty slot — the
    secret is never guessed and never carried into the wrong field.
    """
    matches = matching_slots(field_label, slots.keys())
    if len(matches) == 0:
        return {"disposition": ASK, "slot_key": None, "reason": R_NO_SLOT}
    if len(matches) > 1:
        return {"disposition": ASK, "slot_key": None, "reason": R_COLLISION}
    slot = matches[0]
    if not slots.get(slot):
        return {"disposition": ASK, "slot_key": slot, "reason": R_NO_VALUE}
    return {"disposition": CARRY, "slot_key": slot, "reason": R_MATCH}


def carry_library_keys(slots: Mapping[str, bool]) -> list[str]:
    """The slot keys that currently hold a value — the CARRY vocabulary the Seed
    Manifest / Data Agent classify against."""
    return sorted(k for k, has in slots.items() if has)
