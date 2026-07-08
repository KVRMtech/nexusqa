"""QE-Central — Phase-0 REFUSE harness router (design §3.1 REFUSE matrix).

``POST /api/v1/qec/harness/run`` drives the R1-R8 refusal rules against
fixture variants through the REAL factory chain and persists one
``qe_harness_runs`` row per rule — honesty results are themselves
auditable evidence.  Any ``GREEN_WASH_DETECTED`` verdict is a
deploy-gate failure.

Gated by ``QE_HARNESS_ENABLED`` (default OFF): the harness creates real
artifacts and drives real factory endpoints, so it must be an explicit
operator decision — 403 otherwise, never a silent no-op.

Dependency contract (implemented in ``app.harness.runner``):
  * ``run_refuse_matrix(*, tenant_id: str, fixture_name: str,
    rule_ids: list[str] | None) -> dict`` returning
    ``{"harness_run_ids": [...], "verdicts": [...],
       "green_wash_detected": bool}`` — the runner persists the
    ``qe_harness_runs`` rows itself, inside the same call.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import require_auth, require_role
from ..config import settings
from ..db import row_to_dict, tenant_scoped_qec_session
from ..db.models import QEHarnessRunRow
from ..harness.runner import run_refuse_matrix

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Harness"])

_RULE_ID_RE = re.compile(r"^R[1-8]$")
_DEFAULT_FIXTURE = "golden_3page_form"


class HarnessRunRequest(BaseModel):
    """Optional narrowing of the matrix; default = full R1-R8 + baseline."""

    fixture_name: str = Field(default=_DEFAULT_FIXTURE, min_length=1, max_length=200)
    # Subset of R1..R8; None/empty = run the full matrix.
    rules: list[str] | None = None


def _require_harness_enabled() -> None:
    """403 unless the operator explicitly enabled the harness (fail-closed)."""
    if not settings.qe_harness_enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "REFUSE harness is disabled (QE_HARNESS_ENABLED unset) — it "
                "creates real artifacts and drives real factory endpoints, so "
                "enabling it is an explicit operator decision"
            ),
        )


@router.post("/harness/run")
async def run_harness(
    payload: HarnessRunRequest,
    user: dict = Depends(require_role("admin", "manager")),
) -> dict:
    """Run the REFUSE matrix; returns ``{verdicts[], green_wash_detected}``."""
    _require_harness_enabled()

    rule_ids: list[str] | None = None
    if payload.rules:
        invalid = [r for r in payload.rules if not _RULE_ID_RE.match(r)]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"unknown harness rule ids: {invalid} (expected R1..R8)",
            )
        rule_ids = sorted(set(payload.rules))

    tenant_id = user["tenant_id"]
    logger.info(
        "qec.harness.run_started",
        extra={"tenant_id": tenant_id, "fixture": payload.fixture_name,
               "rules": rule_ids or "ALL", "actor": user.get("sub", "")},
    )
    try:
        result = await run_refuse_matrix(
            tenant_id=tenant_id,
            fixture_name=payload.fixture_name,
            rule_ids=rule_ids,
        )
    except (ValueError, FileNotFoundError) as exc:
        # Bad fixture name / unknown rule subset — an input error, honestly
        # 422, never a masked 500.
        raise HTTPException(status_code=422, detail=str(exc)[:500])
    if result.get("green_wash_detected"):
        # Loud, structured, deploy-gate-consumable — never a quiet pass.
        logger.error(
            "qec.harness.GREEN_WASH_DETECTED",
            extra={"tenant_id": tenant_id, "fixture": payload.fixture_name,
                   "harness_run_ids": result.get("harness_run_ids", [])},
        )
    return result


@router.get("/harness/runs/{harness_run_id}")
async def get_harness_run(
    harness_run_id: str, user: dict = Depends(require_auth),
) -> dict:
    """Full per-rule request/response evidence for one harness run."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (
            await session.execute(
                select(QEHarnessRunRow).where(
                    QEHarnessRunRow.harness_run_id == harness_run_id,
                    QEHarnessRunRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="harness run not found")
        return row_to_dict(row)


@router.get("/harness/runs")
async def list_harness_runs(
    user: dict = Depends(require_auth),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Recent harness runs for the tenant (newest first)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (
            await session.execute(
                select(QEHarnessRunRow)
                .where(QEHarnessRunRow.tenant_id == tenant_id)
                .order_by(QEHarnessRunRow.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return {"runs": [row_to_dict(r) for r in rows], "total": len(rows)}
