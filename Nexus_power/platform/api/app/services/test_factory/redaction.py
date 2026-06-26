"""PII redaction at EGRESS (governance #5, balanced scope; ADDITIVE 2026-06-21).

Reuses the SDK's domain-aware detector (nexus_sdk.evidence.pii_detector). We
redact at the boundaries where data leaves the perimeter — external SaaS push
(default-on) and export (opt-in) — and log a ShieldAuditRow trail. Redaction
happens on COPIES at the boundary only; stored case content + the grounded
guardrail are never touched, so on-prem tests stay runnable.

The vertical vocabulary is configurable via NEXUS_PII_VERTICAL (default vocab —
email / phone / DOB / etc — always runs; a vertical adds SSN / policy / MRN…).
"""

from __future__ import annotations

import os
from typing import Any

from nexus_sdk.evidence.pii_detector import detect_pii, redact
from nexus_sdk.db.models import ShieldAuditRow
from ...database import tenant_scoped_session

_VERTICAL = (os.environ.get("NEXUS_PII_VERTICAL", "") or "").strip() or None
_TEXT_FIELDS = ("action", "expected", "expected_result", "data_ref")
_CASE_FIELDS = ("name", "description", "expected_outcome")


def _redact_str(s: Any) -> tuple[Any, list[str]]:
    if not s or not isinstance(s, str):
        return s, []
    hits = detect_pii(s, _VERTICAL)
    if not hits:
        return s, []
    return redact(s, hits), [h.pattern_name for h in hits]


def redact_case(case: Any) -> tuple[Any, list[str]]:
    """Return (redacted_copy, pii_types) for one ProductionTestCase. Operates on
    a deep copy so the stored/served case is never mutated."""
    c = case.model_copy(deep=True)
    types: list[str] = []
    for k in _CASE_FIELDS:
        v = getattr(c, k, None)
        if v:
            r, t = _redact_str(v)
            setattr(c, k, r)
            types += t
    for st in (getattr(c, "steps", None) or []):
        for k in _TEXT_FIELDS:
            v = getattr(st, k, None)
            if v:
                r, t = _redact_str(v)
                setattr(st, k, r)
                types += t
        obs = getattr(st, "observed", None)
        if isinstance(obs, dict) and obs.get("value"):
            r, t = _redact_str(obs["value"])
            obs["value"] = r
            types += t
    return c, types


def redact_cases(cases: list[Any]) -> tuple[list[Any], list[str]]:
    out: list[Any] = []
    all_types: list[str] = []
    for c in cases:
        rc, t = redact_case(c)
        out.append(rc)
        all_types += t
    return out, all_types


def count_case_pii(case: Any) -> int:
    """Lightweight PII hit count for one case (for an honest UI badge)."""
    n = 0
    for k in _CASE_FIELDS:
        v = getattr(case, k, None)
        if isinstance(v, str) and v:
            n += len(detect_pii(v, _VERTICAL))
    for st in (getattr(case, "steps", None) or []):
        for k in _TEXT_FIELDS:
            v = getattr(st, k, None)
            if isinstance(v, str) and v:
                n += len(detect_pii(v, _VERTICAL))
        obs = getattr(st, "observed", None)
        if isinstance(obs, dict) and isinstance(obs.get("value"), str) and obs["value"]:
            n += len(detect_pii(obs["value"], _VERTICAL))
    return n


async def log_shield(
    tenant_id: str, action: str, entity_count: int, pii_types: list[str],
    user_id: str = "", text_length: int = 0,
) -> None:
    """Append a ShieldAuditRow egress-trail entry. Fail-safe."""
    try:
        async with tenant_scoped_session(tenant_id) as session:
            session.add(ShieldAuditRow(
                tenant_id=tenant_id,
                action=action[:30],
                entity_count=entity_count,
                pii_types=sorted({str(t) for t in pii_types}),
                text_length=text_length,
                user_id=user_id[:64],
            ))
            await session.commit()
    except Exception:
        pass
