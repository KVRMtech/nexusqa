"""Fail-closed PII egress guard for the Data Agent (Phase 3).

Source redaction (substrate/redact) runs at PERSISTENCE and is fail-OPEN. Sending
field metadata to a (possibly cloud) LLM tier is a DIFFERENT egress point that needs a
FAIL-CLOSED guard: if PII is detected in the payload — or if detection cannot run at
all — the guard refuses to send, and the Data Agent falls back to the deterministic
floor. A regulated buyer's SSN/account# must never egress to an LLM.

Two defences:
  1. VALUE-FREE payload — only labels/types/options are ever assembled for the LLM,
     never a filled or observed value. So a real secret a user typed cannot leave.
  2. FAIL-CLOSED scan — the value-free payload is still scanned (a captured option or
     label could itself contain a real identifier). Any match, or any failure of the
     detector to run, marks the payload UNSAFE — the caller then skips the LLM.

Reuses the shipped ``nexus_sdk.llm.pii_guard`` detector (SSN / Luhn-valid card / etc.).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)


class EgressBlocked(Exception):
    """Raised when the payload cannot be proven free of PII before an LLM call."""


def _detector():
    # Imported lazily so a missing SDK is a fail-CLOSED block, never an import crash.
    from nexus_sdk.llm.pii_guard import check_text
    return check_text


def scan(text: str) -> list:
    """Detect PII in a string. FAIL-CLOSED: if the detector cannot run, RAISE
    ``EgressBlocked`` so the caller blocks egress rather than send unscanned text."""
    try:
        check_text = _detector()
    except Exception as exc:  # SDK absent/broken → never send unscanned
        raise EgressBlocked(f"PII detector unavailable ({exc}); refusing to send to the LLM") from exc
    try:
        return list(check_text(text or ""))
    except Exception as exc:  # a scan error is also fail-closed
        raise EgressBlocked(f"PII scan failed ({exc}); refusing to send to the LLM") from exc


def value_free_payload(inventory: Iterable[Mapping]) -> str:
    """Assemble ONLY labels/types/options for the LLM — never a value the user typed."""
    parts: list[str] = []
    for f in inventory:
        parts.append(str(f.get("label") or ""))
        parts.append(str(f.get("type") or ""))
        for o in (f.get("options") or ()):
            parts.append(str(o))
    return "\n".join(p for p in parts if p)


def guard_inventory(inventory: Iterable[Mapping]) -> dict:
    """Decide whether the field inventory is SAFE to send to an LLM tier.

    Returns ``{safe, reason, matches, payload}``. ``safe`` is False — and the caller
    MUST skip the LLM and use the deterministic floor — when PII is detected in the
    (value-free) metadata OR the detector could not run (fail-closed).
    """
    inv = list(inventory)
    payload = value_free_payload(inv)
    try:
        matches = scan(payload)
    except EgressBlocked as exc:
        logger.warning("qec.data_agent.egress_blocked", extra={"reason": str(exc)[:200]})
        return {"safe": False, "reason": str(exc), "matches": [], "payload": payload}
    if matches:
        names = [getattr(m, "pattern_name", "pii") for m in matches]
        logger.warning("qec.data_agent.egress_pii_detected", extra={"patterns": names})
        return {"safe": False, "reason": "PII detected in field metadata", "matches": names, "payload": payload}
    return {"safe": True, "reason": "", "matches": [], "payload": payload}
