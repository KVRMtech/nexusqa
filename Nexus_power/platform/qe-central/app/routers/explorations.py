"""QE-Central — explorations router: THE substrate-write seam (design §3.1).

``POST /api/v1/qec/explorations`` accepts an inline ``ExplorationBundle``
(Phase-0: deterministic fixture; Phase-1 adds explorer dispatch that calls
back with the same bundle shape), creates the session+artifact rows, then
atomically writes the §2 substrate through ``substrate.writer``.

Status lifecycle (first-class, never silently empty):
    writing → completed | failed | refused
Refusal (a bundle that breaks an evidence rule) is an HONEST 422 with the
reason persisted on the exploration row — the artifact is never green-washed.

Dependency contract (implemented in ``app.substrate`` / ``app.artifacts``):
  * ``ExplorationBundle`` — pydantic model exposing at least ``crawl_id``,
    ``target_url``, ``explorer_version``, ``config_fingerprint``,
    ``frame_count`` (int).
  * ``RefusalError`` — raised by schema validation / the writer when an
    evidence rule is broken; ``str(exc)`` is the honest reason.
  * ``write_exploration(bundle, *, tenant_id, artifact_id, session_id,
    extractor_version) -> WriteStats`` (pydantic; ``.model_dump()``).
  * ``create_crawl_artifact(*, tenant_id, target_url, crawl_id,
    config_fingerprint, frame_count, meta) -> CreatedArtifact`` with
    ``.artifact_id`` and ``.session_id``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..artifacts.creator import create_crawl_artifact
from ..auth import require_auth, require_role
from ..db import new_id, row_to_dict, tenant_scoped_qec_session, utc_now
from ..db.models import QEExplorationRow
from ..substrate.schema import ExplorationBundle, RefusalError
from ..substrate.writer import write_exploration

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Explorations"])

# page_visits.extractor_version is String(50) (034_page_visits.py:135):
# 'qec_live_v1@' (12) + uuid4 (36) = 48 — enforce the ceiling honestly.
_EXTRACTOR_VERSION_PREFIX = "qec_live_v1@"
_EXTRACTOR_VERSION_MAX = 50


class ExplorationCreateRequest(BaseModel):
    """Phase-0 request: the bundle travels inline (R-1 direct-write seam)."""

    bundle: ExplorationBundle
    # Optional soft ref to a registered client_apps row.
    app_id: str = Field(default="", max_length=64)


def _extractor_version(crawl_id: str) -> str:
    """Build the ONE version string for the whole atomic write (§2.3)."""
    version = f"{_EXTRACTOR_VERSION_PREFIX}{crawl_id}"
    if len(version) > _EXTRACTOR_VERSION_MAX:
        raise HTTPException(
            status_code=422,
            detail=(
                f"crawl_id too long: extractor_version '{version[:60]}' exceeds "
                f"{_EXTRACTOR_VERSION_MAX} chars (page_visits.extractor_version cap)"
            ),
        )
    return version


async def _mark(
    tenant_id: str, exploration_id: str, *, status: str, **fields,
) -> None:
    """Persist a status transition on the exploration row (own transaction,
    so an honest terminal state survives even when the write txn rolled back)."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (
            await session.execute(
                select(QEExplorationRow).where(
                    QEExplorationRow.exploration_id == exploration_id,
                    QEExplorationRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:  # pragma: no cover — row created moments earlier
            logger.error(
                "qec.explorations.mark_lost_row",
                extra={"exploration_id": exploration_id, "status": status},
            )
            return
        row.status = status
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = utc_now()


@router.post("/explorations", status_code=201)
async def create_exploration(
    payload: ExplorationCreateRequest,
    user: dict = Depends(require_role("admin", "manager")),
) -> dict:
    """Create session+artifact and atomically write the crawl substrate.

    Returns ``{exploration_id, artifact_id, session_id, extractor_version,
    stats}`` on success; 422 with the refusal reason when an evidence rule
    is broken; 500 with an honest error otherwise.  Every outcome is
    persisted on the ``qe_explorations`` row.
    """
    tenant_id = user["tenant_id"]
    bundle = payload.bundle
    exploration_id = new_id()
    extractor_version = _extractor_version(bundle.crawl_id)

    async with tenant_scoped_qec_session(tenant_id) as session:
        session.add(
            QEExplorationRow(
                exploration_id=exploration_id,
                tenant_id=tenant_id,
                app_id=(payload.app_id or "")[:64],
                status="writing",
                explorer_version=(bundle.explorer_version or "")[:100],
                extractor_version=extractor_version,
                started_at=utc_now(),
            )
        )

    try:
        created = await create_crawl_artifact(
            tenant_id=tenant_id,
            target_url=bundle.target_url,
            crawl_id=bundle.crawl_id,
            config_fingerprint=bundle.config_fingerprint,
            frame_count=int(bundle.frame_count),
            meta={
                "exploration_id": exploration_id,
                "app_id": payload.app_id or "",
                "explorer_version": bundle.explorer_version or "",
            },
        )
        stats = await write_exploration(
            bundle,
            tenant_id=tenant_id,
            artifact_id=created.artifact_id,
            session_id=created.session_id,
            extractor_version=extractor_version,
        )
    except RefusalError as exc:
        reason = str(exc)[:2000]
        await _mark(
            tenant_id, exploration_id,
            status="refused", error=reason, finished_at=utc_now(),
        )
        logger.warning(
            "qec.explorations.refused",
            extra={"exploration_id": exploration_id, "tenant_id": tenant_id,
                   "reason": reason[:300]},
        )
        raise HTTPException(
            status_code=422,
            detail={"refused": True, "reason": reason,
                    "exploration_id": exploration_id},
        )
    except HTTPException:
        await _mark(
            tenant_id, exploration_id,
            status="failed", error="upstream HTTP error during substrate write",
            finished_at=utc_now(),
        )
        raise
    except Exception as exc:
        message = str(exc)[:2000]
        await _mark(
            tenant_id, exploration_id,
            status="failed", error=message, finished_at=utc_now(),
        )
        logger.error(
            "qec.explorations.write_failed",
            extra={"exploration_id": exploration_id, "tenant_id": tenant_id,
                   "error": message[:300]},
        )
        raise HTTPException(
            status_code=500,
            detail=f"substrate write failed: {message[:500]}",
        )

    stats_dict = stats.model_dump()
    await _mark(
        tenant_id, exploration_id,
        status="completed",
        artifact_id=created.artifact_id,
        session_id=created.session_id,
        stats=stats_dict,
        finished_at=utc_now(),
    )
    logger.info(
        "qec.explorations.completed",
        extra={
            "exploration_id": exploration_id,
            "tenant_id": tenant_id,
            "artifact_id": created.artifact_id,
            "extractor_version": extractor_version,
        },
    )
    return {
        "exploration_id": exploration_id,
        "artifact_id": created.artifact_id,
        "session_id": created.session_id,
        "extractor_version": extractor_version,
        "stats": stats_dict,
    }


@router.get("/explorations/{exploration_id}")
async def get_exploration(
    exploration_id: str, user: dict = Depends(require_auth),
) -> dict:
    """Status + stats + honest error/refusal reason for one exploration."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (
            await session.execute(
                select(QEExplorationRow).where(
                    QEExplorationRow.exploration_id == exploration_id,
                    QEExplorationRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="exploration not found")
        return row_to_dict(row)
