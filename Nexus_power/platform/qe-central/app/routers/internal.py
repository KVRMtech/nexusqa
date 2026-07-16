"""QE-Central — internal explorer completion callback (design §3.2 / §1.1).

``POST /internal/crawls/{crawl_id}/complete`` is the HMAC-authenticated seam the
contained explorer calls when a crawl finishes.  It:

  1. verifies the HMAC-SHA256 signature over the RAW body (``X-QEC-Signature``)
     against the shared ``QEC_EXPLORER_TOKEN`` — fail-closed: an unsigned or
     mis-provisioned callback is rejected (401), never trusted;
  2. locates the pending ``qe_explorations`` row (tenant-scoped, RLS);
  3. reads the staged ``manifest.jsonl`` from the shared crawl volume (path
     DERIVED from the validated crawl_id — a client path is never trusted) and
     maps it to an :class:`ExplorationBundle` via the pure manifest mapper;
  4. creates the crawl artifact + atomically writes the §2 substrate through
     the SAME ``artifacts.creator`` / ``substrate.writer`` seam the Phase-0
     inline path uses;
  5. relays the in-memory ``storageState`` (if any) to platform-api's E3
     auth-import endpoint (best-effort — a disabled/unavailable auth-import
     never discards a successful substrate write);
  6. persists an HONEST terminal state on the exploration row
     (completed | refused | failed) — refusal is first-class, never a silently
     empty artifact.

This router lives OUTSIDE the ``/api/*`` prefix so the JWT middleware does not
apply (the explorer holds no JWT — only the HMAC token); authentication is the
HMAC verification below.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from ..artifacts.creator import create_crawl_artifact
from ..clients import factory, platform_api
from ..clients.config import SIGNATURE_HEADER, phase1_settings
from ..clients.manifest_mapper import (
    ManifestMappingError,
    map_manifest_records_to_bundle,
)
from ..db import tenant_scoped_qec_session, utc_now
from ..db.models import ClientAppRow, QEExplorationRow
from ..substrate.schema import CRAWL_ID_PATTERN, ExplorationBundle, RefusalError
from ..substrate.writer import (
    EXTRACTOR_VERSION_PREFIX,
    validate_extractor_version,
    write_exploration,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["QEC Internal"])

_MANIFEST_FILENAME = "manifest.jsonl"
#: Terminal states that make a repeat callback a no-op (idempotency).
_TERMINAL_STATES = frozenset({"completed", "refused", "failed"})


class CompletionCallback(BaseModel):
    """The explorer's completion callback body (design §3.2).

    ``storage_state`` is the in-memory Playwright session relayed for E3 import;
    it is NEVER persisted by qe-central directly (platform-api encrypts it).
    ``error`` is the explorer's honest failure reason when no usable manifest
    was produced (login failed, crawl aborted before any state).
    """

    model_config = {"extra": "ignore"}

    tenant_id: str = Field(min_length=1, max_length=64)
    exploration_id: str = Field(min_length=1, max_length=64)
    crawl_id: str = Field(min_length=1, max_length=36)
    storage_state: dict | None = None
    auth_label: str | None = Field(default=None, max_length=200)
    stop_reason: str = Field(default="", max_length=200)
    guard_events: int = Field(default=0, ge=0)
    error: str = Field(default="", max_length=2000)
    #: Crawl coverage (P4): forms_found / fields_inferred / fields_needing_seed /
    #: submit_candidates. Carried on the exploration ``stats`` so the app UI can
    #: turn "why so shallow?" into a NAMED, seed-this-field remediation list.
    coverage: dict | None = None


def _extractor_version(crawl_id: str) -> str:
    """Build + validate the ONE version string for the whole write (§2.3)."""
    return validate_extractor_version(f"{EXTRACTOR_VERSION_PREFIX}{crawl_id}")


def _crawl_dir(crawl_id: str) -> Path:
    """DERIVE the per-crawl directory from the validated crawl_id.

    crawl_id is pinned to ``CRAWL_ID_PATTERN`` (no separators / traversal), so
    ``{storage_root}/{crawl_id}`` is always contained — a client-supplied path
    is never used.
    """
    return Path(phase1_settings.crawl_storage_root) / crawl_id


def _read_manifest(path: Path) -> list[dict]:
    """Read all COMPLETE JSONL records; a trailing partial line is discarded
    (crash-mid-write leaves a durable, resumable prefix — emit.py contract)."""
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(
                    "qec.internal.partial_manifest_line_discarded",
                    extra={"path": str(path)},
                )
                break
    return records


def _make_screenshot_loader(crawl_dir: Path) -> Callable[[str], bytes]:
    """A path→bytes loader hardened against traversal outside the crawl dir."""
    base = crawl_dir.resolve()

    def _load(rel_path: str) -> bytes:
        candidate = (base / rel_path).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ManifestMappingError(
                f"screenshot path {rel_path!r} escapes the crawl directory",
                reason="screenshot_missing_data",
            ) from exc
        if not candidate.is_file():
            raise FileNotFoundError(rel_path)
        return candidate.read_bytes()

    return _load


async def _mark(
    tenant_id: str, exploration_id: str, *, status: str, **fields,
) -> None:
    """Persist a status transition on the exploration row (own transaction, so
    an honest terminal state survives even when the write txn rolled back)."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (
            await session.execute(
                select(QEExplorationRow).where(
                    QEExplorationRow.exploration_id == exploration_id,
                    QEExplorationRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:  # pragma: no cover — verified present moments earlier
            logger.error(
                "qec.internal.mark_lost_row",
                extra={"exploration_id": exploration_id, "status": status},
            )
            return
        row.status = status
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = utc_now()


async def _promote_latest_artifact(tenant_id: str, app_id: str, artifact_id: str) -> None:
    """Promote a freshly-recorded crawl artifact onto its registered app so a
    cycle can run against it (own transaction; best-effort — a promote failure
    NEVER fails the crawl, which already produced a valid, evidence-passing
    artifact). Without this the app keeps ``latest_artifact_id=''`` and every
    cycle 409s with 'register a crawl/exploration first'."""
    if not (app_id and artifact_id):
        return
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(ClientAppRow).where(
                        ClientAppRow.app_id == app_id,
                        ClientAppRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is not None and row.status != "deleted":
                row.latest_artifact_id = artifact_id
                row.updated_at = utc_now()
    except Exception as exc:  # pragma: no cover — promotion is best-effort
        logger.warning(
            "qec.internal.promote_failed",
            extra={"app_id": app_id, "artifact_id": artifact_id, "error": str(exc)[:300]},
        )


async def _app_answer_key(tenant_id: str, app_id: str) -> dict:
    """The app's value-oracle contract (``{outcomes, rules}``) for auto-generate,
    or ``{}``.  Best-effort — a read failure just means a body-less generate (still
    materialises the demonstrated cases from the substrate)."""
    if not app_id:
        return {}
    try:
        from ..services.answer_key import value_oracle_contract
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
                return {}
            return value_oracle_contract(row.answer_key)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("qec.internal.answer_key_read_failed",
                       extra={"app_id": app_id, "error": str(exc)[:200]})
        return {}


@router.post("/crawls/{crawl_id}/complete")
async def complete_crawl(crawl_id: str, request: Request) -> dict:
    """Ingest a finished crawl: verify HMAC → map manifest → write substrate.

    Returns ``{status, exploration_id, artifact_id, extractor_version, stats,
    auth_import}`` on success; 401 on a bad signature; 404 for an unknown
    exploration; 422 on a refused (dishonest) manifest; 500 on infrastructure
    failure.  Every outcome is persisted on the ``qe_explorations`` row.
    """
    # ── 1) HMAC over the RAW body (fail-closed) ───────────────────────────
    raw = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER, "")
    if not phase1_settings.verify_signature(raw, signature):
        logger.warning(
            "qec.internal.bad_signature",
            extra={"crawl_id": crawl_id, "has_sig": bool(signature)},
        )
        raise HTTPException(status_code=401, detail="invalid or missing callback signature")

    # ── 2) Parse + cross-check the identifiers ────────────────────────────
    try:
        body = CompletionCallback.model_validate(json.loads(raw or b"{}"))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=f"malformed callback body: {str(exc)[:300]}")
    if body.crawl_id != crawl_id:
        raise HTTPException(status_code=400, detail="crawl_id path/body mismatch")
    if not CRAWL_ID_PATTERN.match(crawl_id):
        raise HTTPException(status_code=400, detail="invalid crawl_id")

    tenant_id = body.tenant_id
    exploration_id = body.exploration_id

    # ── 3) Locate the pending exploration (tenant-scoped, RLS) ────────────
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
        current_status = row.status
        row_extractor_version = row.extractor_version
        app_id = row.app_id
        # Cross-check: the version stamped at dispatch encodes the crawl_id.
        expected_crawl_id = (row_extractor_version or "").removeprefix(EXTRACTOR_VERSION_PREFIX)
        artifact_id_existing = row.artifact_id
        session_id_existing = row.session_id

    if expected_crawl_id and expected_crawl_id != crawl_id:
        raise HTTPException(
            status_code=409,
            detail="crawl_id does not match the dispatched exploration",
        )

    # Idempotency: a repeat callback on a finished crawl is a no-op.
    if current_status in _TERMINAL_STATES:
        logger.info(
            "qec.internal.duplicate_callback_ignored",
            extra={"exploration_id": exploration_id, "status": current_status},
        )
        return {
            "status": current_status,
            "exploration_id": exploration_id,
            "artifact_id": artifact_id_existing,
            "session_id": session_id_existing,
            "extractor_version": row_extractor_version,
            "idempotent": True,
        }

    # ── 4) The explorer reported an honest failure with no usable manifest ─
    manifest_file = _crawl_dir(crawl_id) / _MANIFEST_FILENAME
    if not manifest_file.is_file():
        reason = body.error or f"no manifest produced (stop_reason={body.stop_reason or 'unknown'})"
        await _mark(tenant_id, exploration_id, status="failed", error=reason[:2000], finished_at=utc_now())
        logger.warning(
            "qec.internal.no_manifest",
            extra={"exploration_id": exploration_id, "crawl_id": crawl_id, "reason": reason[:300]},
        )
        return {"status": "failed", "exploration_id": exploration_id, "error": reason[:500]}

    # ── 5) Map manifest → bundle → substrate (the §2 write) ───────────────
    extractor_version = _extractor_version(crawl_id)
    try:
        records = _read_manifest(manifest_file)
        loader = _make_screenshot_loader(_crawl_dir(crawl_id))
        bundle: ExplorationBundle = map_manifest_records_to_bundle(
            records, screenshot_loader=loader,
        )
        if bundle.crawl_id != crawl_id:
            raise ManifestMappingError(
                f"manifest crawl_id {bundle.crawl_id!r} != callback crawl_id {crawl_id!r}"
            )
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
                "created_by": "svc-qe-explorer",
                "stop_reason": body.stop_reason or "",
                "guard_events": body.guard_events,
            },
        )
        stats = await write_exploration(
            bundle,
            tenant_id=tenant_id,
            artifact_id=created.artifact_id,
            session_id=created.session_id,
            extractor_version=extractor_version,
        )
    except RefusalError as exc:  # includes ManifestMappingError
        reason = str(exc)[:2000]
        await _mark(tenant_id, exploration_id, status="refused", error=reason, finished_at=utc_now())
        logger.warning(
            "qec.internal.refused",
            extra={"exploration_id": exploration_id, "crawl_id": crawl_id, "reason": reason[:300]},
        )
        raise HTTPException(
            status_code=422,
            detail={"refused": True, "reason": reason, "exploration_id": exploration_id},
        )
    except Exception as exc:
        message = str(exc)[:2000]
        await _mark(tenant_id, exploration_id, status="failed", error=message, finished_at=utc_now())
        logger.error(
            "qec.internal.write_failed",
            extra={"exploration_id": exploration_id, "crawl_id": crawl_id, "error": message[:300]},
        )
        raise HTTPException(status_code=500, detail=f"substrate write failed: {message[:500]}")

    # ── 6) Relay storageState to E3 (best-effort — never discards the write) ─
    auth_import = {"attempted": False}
    if body.storage_state:
        result = await platform_api.import_auth_profile(
            tenant_id=tenant_id,
            artifact_id=created.artifact_id,
            storage_state=body.storage_state,
            label=body.auth_label,
        )
        auth_import = {"attempted": True, "ok": result.ok,
                       "status_code": result.status_code, "detail": result.detail}

    # ── 7) Honest terminal state ──────────────────────────────────────────
    stats_dict = stats.model_dump()
    stats_dict["auth_import"] = auth_import
    if body.coverage:  # P4: named seed-remediation surface for the app UI
        stats_dict["coverage"] = body.coverage

    # Promote the recorded artifact onto the app so a cycle / the portal reads it.
    await _promote_latest_artifact(tenant_id, app_id, created.artifact_id)

    # AUTO-GENERATE: a completed crawl must YIELD test cases. Without this the
    # substrate is written but factory_test_cases stays 0, so the portal's Test
    # Studio / Command Center (which gate on cases existing) look empty and the
    # crawl appears to have "done nothing" — a showstopper for the bare-crawl path
    # (only the full cycle-driver used to trigger generate). Runs BEFORE the row is
    # marked completed so its summary is persisted in ``stats.generate`` for the UI.
    # Best-effort + NON-FATAL: the substrate write above is already durable, so a
    # generate hiccup is recorded (never a lost crawl); the row still completes. The
    # app's answer_key seeds the value oracle; re-generate upserts (cycle-safe).
    generate_result: dict = {"attempted": False}
    try:
        answer_key = await _app_answer_key(tenant_id, app_id)
        summary = await factory.generate(
            tenant_id=tenant_id, artifact_id=created.artifact_id,
            answer_key=answer_key or None,
        )
        generate_result = {
            "attempted": True, "ok": bool(summary.get("success")),
            "generated": summary.get("generated"),
            "no_cases_reason": summary.get("no_cases_reason") or "",
        }
    except Exception as exc:  # never fail the callback over generation
        generate_result = {"attempted": True, "ok": False, "error": str(exc)[:300]}
        logger.warning(
            "qec.internal.autogenerate_failed",
            extra={"exploration_id": exploration_id, "artifact_id": created.artifact_id,
                   "error": str(exc)[:300]},
        )
    stats_dict["generate"] = generate_result

    await _mark(
        tenant_id, exploration_id,
        status="completed",
        artifact_id=created.artifact_id,
        session_id=created.session_id,
        explorer_version=(bundle.explorer_version or "")[:100],
        stats=stats_dict,
        finished_at=utc_now(),
    )

    logger.info(
        "qec.internal.completed",
        extra={"exploration_id": exploration_id, "crawl_id": crawl_id,
               "artifact_id": created.artifact_id, "extractor_version": extractor_version,
               "auth_import_ok": auth_import.get("ok"),
               "generated": generate_result.get("generated"),
               "generate_ok": generate_result.get("ok")},
    )
    return {
        "status": "completed",
        "exploration_id": exploration_id,
        "artifact_id": created.artifact_id,
        "session_id": created.session_id,
        "extractor_version": extractor_version,
        "stats": stats_dict,
        "auth_import": auth_import,
        "generate": generate_result,
    }
