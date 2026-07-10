"""QE-Central Phase-8 — the compliance report API (``/api/v1/qec/…``, SEAM E).

A thin, admin-only, tenant-scoped HTTP surface over the read-only compliance
projections (:mod:`app.compliance`).  It captures nothing new: it loads the
existing evidence (verdict chains / dossiers / waivers / audit_log) for the
caller's tenant and projects it into the requested framework's control-mapping
shape.

Endpoints:
  * ``GET /compliance/frameworks`` — the catalogue of supported frameworks.
  * ``GET /compliance/{framework}/report?window_hours=`` — the framework-shaped
    evidence report for the caller's tenant (admin-only).  An unknown framework
    is a 404.

ADMIN-ONLY.  The report enumerates control status across every tenant scenario;
it is gated to ``admin`` (a compliance officer), stricter than the read-open
list endpoints.  ZERO LLM; deterministic; read-only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import require_role
from ..compliance import (
    EvidenceWindow,
    UnknownFrameworkError,
    available_frameworks,
    get_adapter,
    load_evidence,
)
from ..db import utc_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Compliance"])

#: The compliance report is admin-only (a compliance officer), not read-open.
_ADMIN = require_role("admin")


def _actor(user: dict) -> str:
    """The audit actor string for a user context (sub | user_id | email)."""
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "")


def _window(window_hours: float | None) -> EvidenceWindow:
    """Build an :class:`EvidenceWindow` from a look-back in hours (None = all)."""
    if window_hours is None or window_hours <= 0:
        return EvidenceWindow(start=None, end=None, hours=None)
    start = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))
    return EvidenceWindow(start=start, end=None, hours=float(window_hours))


@router.get("/compliance/frameworks")
async def list_compliance_frameworks(user: dict = Depends(_ADMIN)) -> dict:
    """List the supported compliance frameworks (admin-only)."""
    frameworks = available_frameworks()
    return {"frameworks": frameworks, "total": len(frameworks)}


@router.get("/compliance/{framework}/report")
async def compliance_report(
    framework: str,
    window_hours: float | None = Query(
        default=None, ge=0,
        description="Only project evidence newer than this many hours (default: all).",
    ),
    user: dict = Depends(_ADMIN),
) -> dict:
    """Project the caller's tenant evidence into ``framework``'s report shape.

    Validates the framework FIRST (unknown → 404, no DB touched), then loads the
    tenant's evidence read-only and projects it deterministically.  The response
    carries a ``meta`` block (render time, requester, per-source availability)
    OUTSIDE the digested projection, so the ``report_digest`` stays a pure
    function of the evidence.
    """
    # Validate the framework before any I/O so an unknown key 404s immediately.
    try:
        adapter = get_adapter(framework)
    except UnknownFrameworkError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    tenant_id = user["tenant_id"]
    bundle, sources = await load_evidence(
        tenant_id=tenant_id, window=_window(window_hours),
    )
    report = adapter.project(bundle)

    logger.info(
        "qec.compliance.report",
        extra={
            "tenant_id": tenant_id,
            "framework": adapter.framework,
            "verdicts": len(bundle.verdicts),
            "chain_verified": report["evidence_summary"]["chain"]["verified"],
            "sources": sources,
            "actor": _actor(user),
        },
    )
    report["meta"] = {
        "generated_at": utc_now().isoformat(),
        "requested_by": _actor(user),
        "evidence_sources": sources,
    }
    return report
