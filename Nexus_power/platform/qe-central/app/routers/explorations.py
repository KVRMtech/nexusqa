"""QE-Central — explorations router: THE substrate-write seam (design §3.1).

``POST /api/v1/qec/explorations`` has TWO mutually-exclusive shapes:

  * **Phase-0 (inline bundle)** — the caller posts an ``ExplorationBundle``
    (a deterministic fixture or a pre-built bundle); qe-central creates the
    session+artifact rows and atomically writes the §2 substrate through
    ``substrate.writer``.  Unchanged from Phase-0.
  * **Phase-1 (explorer dispatch)** — the caller posts an ``app_id`` (no
    bundle); qe-central mints a crawl, populates the egress allowlist, and
    dispatches the contained explorer, which later calls back
    ``POST /internal/crawls/{crawl_id}/complete`` (``app/routers/internal.py``)
    with the manifest → the SAME writer runs on the mapped bundle.

Both paths persist an HONEST terminal state on the ``qe_explorations`` row and
never green-wash a broken crawl.  Status lifecycle (first-class):
    pending (dispatched) → writing → completed | failed | refused

Dependency contract (implemented in ``app.substrate`` / ``app.artifacts`` /
``app.clients``):
  * ``ExplorationBundle`` — pydantic model (``crawl_id``, ``target_url``,
    ``explorer_version``, ``config_fingerprint``, ``frame_count``).
  * ``RefusalError`` — raised on a broken evidence rule; ``str(exc)`` is honest.
  * ``write_exploration(...) -> WriteStats`` (``.model_dump()``).
  * ``create_crawl_artifact(...) -> CreatedArtifact`` (``.artifact_id`` /
    ``.session_id``).
  * ``explorer_client.dispatch_crawl(ExploreDispatchRequest) -> DispatchResult``.
"""
from __future__ import annotations

import json
import logging
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from ..artifacts.creator import create_crawl_artifact
from ..auth import require_auth, require_role
from ..clients import explorer_client
from ..clients.config import phase1_settings
from ..clients.explorer_client import ExploreDispatchRequest, ExplorerDispatchError
from ..db import new_id, row_to_dict, tenant_scoped_qec_session, utc_now
from ..db.models import ClientAppRow, QEExplorationRow
from ..fleet.lifecycle import TenantNotOperational
from ..fleet.provisioning import assert_tenant_operational_db
from ..security import prod_guard
from ..substrate.schema import CRAWL_ID_PATTERN, ExplorationBundle, RefusalError
from ..substrate.writer import write_exploration

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Explorations"])

# page_visits.extractor_version is String(50) (034_page_visits.py:135):
# 'qec_live_v1@' (12) + uuid4 (36) = 48 — enforce the ceiling honestly.
_EXTRACTOR_VERSION_PREFIX = "qec_live_v1@"
_EXTRACTOR_VERSION_MAX = 50


class ExplorationCreateRequest(BaseModel):
    """POST body — EXACTLY ONE of ``bundle`` (Phase-0) or ``app_id`` (Phase-1).

    A request carrying both, or neither, is a 422 (an ambiguous write intent
    must never silently pick a path).
    """

    # Phase-0: the bundle travels inline (R-1 direct-write seam).
    bundle: ExplorationBundle | None = None
    # Phase-1: dispatch the contained explorer for this registered app.
    app_id: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "ExplorationCreateRequest":
        has_bundle = self.bundle is not None
        has_app = bool((self.app_id or "").strip())
        if has_bundle == has_app:
            raise ValueError(
                "provide EXACTLY ONE of 'bundle' (Phase-0 inline write) or "
                "'app_id' (Phase-1 explorer dispatch)"
            )
        return self


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


# ─── Phase-0: inline bundle write ────────────────────────────────────────────


async def _write_inline_bundle(
    *, tenant_id: str, exploration_id: str, extractor_version: str,
    bundle: ExplorationBundle, app_id: str,
) -> dict:
    """Create the artifact + atomically write the §2 substrate (Phase-0)."""
    try:
        created = await create_crawl_artifact(
            tenant_id=tenant_id,
            target_url=bundle.target_url,
            crawl_id=bundle.crawl_id,
            config_fingerprint=bundle.config_fingerprint,
            frame_count=int(bundle.frame_count),
            meta={
                "exploration_id": exploration_id,
                "app_id": app_id or "",
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


# ─── Phase-1: explorer dispatch ──────────────────────────────────────────────


def _allowlist_domains(base_url: str, fences: dict) -> list[str]:
    """Resolve the egress allowlist for a crawl (operator fences win).

    Uses the operator-declared ``fences.allowed_hosts`` verbatim (e.g.
    ``['.acmelife.example']``); falls back to the base_url hostname when none
    are declared.  No public-suffix guessing — the allowlist is explicit data.
    """
    declared = [str(h).strip() for h in (fences.get("allowed_hosts") or []) if str(h).strip()]
    if declared:
        return declared
    host = (urlparse(base_url).hostname or "").strip().lower()
    return [host] if host else []


def _write_egress_allowlist(domains: list[str]) -> None:
    """Populate the squid allowlist file BEFORE dispatch (fail-closed).

    Writes one destination domain per line to the shared
    ``qec-egress-allowlist`` volume; squid re-reads it on reconfigure. A write
    failure is FATAL to the dispatch (503) — never launch a browser that can
    only reach a stale/empty allowlist, and never proceed silently.
    """
    if not domains:
        raise HTTPException(
            status_code=422,
            detail="cannot dispatch: no allowed_hosts fence and no resolvable base_url host",
        )
    from pathlib import Path

    path = Path(phase1_settings.egress_allowlist_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "# populated by qe-central at dispatch (fail-closed)\n" + "\n".join(domains) + "\n"
        path.write_text(body, encoding="utf-8")
    except Exception as exc:
        logger.error(
            "qec.explorations.egress_allowlist_write_failed",
            extra={"path": str(path), "error": str(exc)[:300]},
        )
        raise HTTPException(
            status_code=503,
            detail="egress allowlist unavailable — refusing to dispatch a crawl "
                   "that could not be network-fenced",
        )


async def _decrypt_credentials(request: Request, tenant_id: str, row: ClientAppRow) -> dict | None:
    """Decrypt a registered app's credentials for in-memory relay to the explorer.

    Symmetric with ``routers/apps.py::_encrypt_credentials`` (AAD=app_id).
    503 when encryption is unavailable but creds exist — never silently drop
    the login (which would produce an unauthenticated crawl masquerading as
    authenticated).
    """
    if not row.creds_blob:
        return None
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(
            status_code=503,
            detail="encryption unavailable — cannot decrypt app credentials for dispatch",
        )
    from nexus_sdk.security.envelope import EnvelopeBlob

    try:
        blob = EnvelopeBlob.from_bytes(row.creds_blob)
        plaintext = await envelope.decrypt(
            tenant_id, blob, expected_aad=row.app_id.encode("utf-8"),
        )
        creds = json.loads(plaintext)
        return creds if isinstance(creds, dict) else None
    except Exception as exc:
        logger.error(
            "qec.explorations.creds_decrypt_failed",
            extra={"app_id": row.app_id, "error": str(exc)[:200]},
        )
        raise HTTPException(status_code=503, detail="credential decryption failed")


async def _dispatch_explorer(
    *, tenant_id: str, app_id: str, request: Request, response: Response,
) -> dict:
    """Mint a crawl, fence egress, and dispatch the contained explorer (Phase-1)."""
    if not phase1_settings.dispatch_enabled:
        raise HTTPException(
            status_code=503,
            detail="explorer dispatch is disabled (QEC_EXPLORER_DISPATCH_ENABLED unset) — "
                   "enable it to run live crawls, or POST an inline bundle (Phase-0)",
        )

    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (
            await session.execute(
                select(ClientAppRow).where(
                    ClientAppRow.app_id == app_id,
                    ClientAppRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="app not found")
        if row.status != "active":
            raise HTTPException(status_code=409, detail=f"app is not active (status={row.status})")
        # Phase-6 SAFETY SPINE — fail-closed onboarding gate on the REAL-APP crawl
        # path (this Phase-1 dispatch only; the Phase-0 inline-bundle harness path
        # never reaches here).  Even a read-only EXPLORE crawl requires a non-prod
        # attestation; refuse (409/422) unless the app is onboarding-'live'.
        try:
            prod_guard.assert_crawlable(row, phase=prod_guard.PHASE_EXPLORE)
        except prod_guard.OnboardingRefused as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())
        # Phase-7 FLEET lifecycle gate — a SUSPENDED / offboarding tenant may not
        # dispatch a crawl (fail-closed).  A tenant with no control record is
        # operational (today's behavior).  Uses the open tenant-scoped session.
        try:
            await assert_tenant_operational_db(session, tenant_id, operation="crawl")
        except TenantNotOperational as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())
        base_url = row.base_url
        fences = dict(row.fences or {})
        answer_key = dict(row.answer_key or {})
        budgets = dict(row.budgets or {})
        env_attestation = dict(row.env_attestation or {})

    credentials = await _decrypt_credentials(request, tenant_id, row)

    crawl_id = uuid.uuid4().hex  # 32 hex chars — matches CRAWL_ID_PATTERN, fits String(50)
    if not CRAWL_ID_PATTERN.match(crawl_id):  # pragma: no cover — uuid hex is always valid
        raise HTTPException(status_code=500, detail="generated crawl_id failed validation")
    extractor_version = _extractor_version(crawl_id)
    exploration_id = new_id()

    # Persist the pending row BEFORE dispatch so a lost callback still leaves an
    # honest, queryable record (never a silent orphan crawl).
    async with tenant_scoped_qec_session(tenant_id) as session:
        session.add(
            QEExplorationRow(
                exploration_id=exploration_id,
                tenant_id=tenant_id,
                app_id=app_id[:64],
                status="pending",
                extractor_version=extractor_version,
                started_at=utc_now(),
            )
        )

    # Fence egress (fail-closed) then dispatch.
    allowed_hosts = _allowlist_domains(base_url, fences)
    _write_egress_allowlist(allowed_hosts)
    dispatch_request = ExploreDispatchRequest(
        crawl_id=crawl_id,
        tenant_id=tenant_id,
        exploration_id=exploration_id,
        target_url=base_url,
        credentials=credentials,
        answer_key=answer_key,
        budgets=budgets,
        allowed_hosts=allowed_hosts,
        phase="explore",
        attestation=env_attestation or None,
    )
    try:
        result = await explorer_client.dispatch_crawl(dispatch_request)
    except ExplorerDispatchError as exc:
        await _mark(
            tenant_id, exploration_id,
            status="failed", error=str(exc)[:2000], finished_at=utc_now(),
        )
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)[:500])

    response.status_code = 202
    logger.info(
        "qec.explorations.dispatched",
        extra={"exploration_id": exploration_id, "tenant_id": tenant_id,
               "app_id": app_id, "crawl_id": crawl_id},
    )
    return {
        "exploration_id": exploration_id,
        "app_id": app_id,
        "crawl_id": crawl_id,
        "extractor_version": extractor_version,
        "status": "dispatched",
        "accepted": result.accepted,
    }


@router.post("/explorations", status_code=201)
async def create_exploration(
    payload: ExplorationCreateRequest,
    request: Request,
    response: Response,
    user: dict = Depends(require_role("admin", "manager")),
) -> dict:
    """Create session+artifact and write the crawl substrate (Phase-0), OR
    dispatch the contained explorer for a registered app (Phase-1).

    Phase-0 → 201 with ``{exploration_id, artifact_id, session_id,
    extractor_version, stats}``; Phase-1 → 202 with ``{exploration_id, app_id,
    crawl_id, extractor_version, status:'dispatched'}``.  A broken evidence
    rule is an honest 422 with the refusal reason persisted on the row.
    """
    tenant_id = user["tenant_id"]

    # Phase-1: explorer dispatch.
    if payload.bundle is None:
        return await _dispatch_explorer(
            tenant_id=tenant_id, app_id=payload.app_id.strip(),
            request=request, response=response,
        )

    # Phase-0: inline bundle write.
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
    return await _write_inline_bundle(
        tenant_id=tenant_id, exploration_id=exploration_id,
        extractor_version=extractor_version, bundle=bundle, app_id=payload.app_id,
    )


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
