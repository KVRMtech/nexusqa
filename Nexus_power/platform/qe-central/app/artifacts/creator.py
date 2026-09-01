"""QE-Central — crawl artifact creator (design §2.1 rows 1-3 + Belt-1).

Creates, in ONE tenant-scoped transaction on the nexus DB:

  1. tenant bootstrap — ``INSERT ... ON CONFLICT (tenant_id) DO NOTHING``
     (verbatim mirror of spine-engine ``_ensure_tenant_exists``,
     spine-engine/main.py:4600-4631: canonical_artifacts.tenant_id is an
     FK, so the tenant row MUST exist first);
  2. ``sessions`` row — ``session_type='live_crawl'`` (free string,
     §2.1 discriminator table);
  3. ``canonical_artifacts`` row — field-for-field mirror of the spine
     persist path (spine-engine/main.py:4698-4725) with the §2.1 crawl
     values: ``source_type='live_crawl'`` (the E2 composer-guard
     discriminator), ``source_filename=target_url``, ``scene_count=0``,
     honest quality-gate flags (never forced-pass), and
     ``media_fingerprint = sha256(target_url + config_fingerprint +
     explorer_version)`` for re-crawl dedup: an identical configuration
     REUSES the artifact so the new crawl lands as a new
     ``extractor_version`` on the same artifact (§2.3 rule 2 — latest
     wins, stale versions stay immutable history).

Then, in a SEPARATE best-effort transaction (a missing ``surface_prefs``
table must never poison the artifact transaction — Postgres aborts a
transaction after any statement error):

  4. ``surface_prefs`` all-vision-off upsert (Belt-1 anti-clobber, R-6):
     mirrors platform-api ``surface_prefs.set_artifact_override``
     (surface_prefs.py:136-157).  Failure is logged loudly and tolerated —
     Belt-2 (the E2 composer guard) still protects the rows.

The dedup/UPDATE posture respects the least-privilege ``qec_substrate``
role (scripts/qec_db_bootstrap.sql): INSERT/SELECT only on
``canonical_artifacts``/``sessions`` — the creator NEVER updates them.
"""
from __future__ import annotations

import hashlib
import logging
from urllib.parse import urlparse

from pydantic import BaseModel
from sqlalchemy import insert, select, text

from nexus_sdk.db.models import CanonicalArtifactRow, SessionRow

from ..db import new_id, tenant_scoped_substrate_session, utc_now
from ..substrate.schema import CRAWL_ID_PATTERN, RefusalError

logger = logging.getLogger(__name__)

#: §2.2 discriminators — the single literals every gated extension reads.
SOURCE_TYPE_LIVE_CRAWL = "live_crawl"
SESSION_TYPE_LIVE_CRAWL = "live_crawl"

_TENANT_BOOTSTRAP_SQL = text(
    "INSERT INTO tenants (tenant_id, name, domain, plan, status) "
    "VALUES (:tid, :name, :domain, 'starter', 'active') "
    "ON CONFLICT (tenant_id) DO NOTHING"
)

# Belt-1 anti-clobber: all three vision surfaces OFF for this artifact
# (SurfacePrefRow composite PK (tenant_id, artifact_id) — surface_prefs.py:53-54).
_SURFACE_PREFS_OFF_SQL = text(
    "INSERT INTO surface_prefs "
    "  (tenant_id, artifact_id, storyboard, pages_forms, three_d_journey, updated_at) "
    "VALUES (:tid, :aid, false, false, false, now()) "
    "ON CONFLICT (tenant_id, artifact_id) DO UPDATE SET "
    "  storyboard = false, pages_forms = false, three_d_journey = false, "
    "  updated_at = now()"
)


class CreatedArtifact(BaseModel):
    """Result of :func:`create_crawl_artifact` — ``reused`` is True when an
    identical (url, config, explorer) fingerprint matched an existing
    artifact and the crawl will land there as a new extractor_version."""

    artifact_id: str
    session_id: str
    media_fingerprint: str
    reused: bool = False


def compute_media_fingerprint(
    target_url: str, config_fingerprint: str, explorer_version: str,
    app_id: str = "",
) -> str:
    """Deterministic re-crawl dedup key (§2.1 canonical_artifacts row).

    sha256 over (APP id, base URL, crawl config fingerprint, explorer version) —
    deliberately EXCLUDES crawl_id so an identical re-crawl OF THE SAME APP maps to
    the same artifact and versions accumulate there (§2.3 rule 2).

    ``app_id`` is part of the key so TWO DIFFERENT APPS that happen to crawl the SAME
    URL/config never collapse onto ONE shared artifact — without it, onboarding a
    second app on the same site made its crawl dedup onto the FIRST app's artifact,
    so the new app's Studio showed the first crawl's cases. Each app owns its own
    artifact; only the SAME app re-crawling identical content reuses one.
    """
    payload = "\n".join([
        (app_id or "").strip(),
        (target_url or "").strip(),
        (config_fingerprint or "").strip(),
        (explorer_version or "").strip(),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _arm_belt1_vision_off(tenant_id: str, artifact_id: str) -> None:
    """Best-effort surface_prefs all-vision-off upsert (own transaction)."""
    try:
        async with tenant_scoped_substrate_session(tenant_id) as session:
            await session.execute(
                _SURFACE_PREFS_OFF_SQL, {"tid": tenant_id, "aid": artifact_id},
            )
    except Exception as exc:
        logger.warning(
            "qec.artifacts.surface_prefs_off_failed belt1 absent — the E2 "
            "composer guard (Belt-2) remains the anti-clobber protection",
            extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
                   "error": str(exc)[:300]},
        )


async def create_crawl_artifact(
    *,
    tenant_id: str,
    target_url: str,
    crawl_id: str,
    config_fingerprint: str,
    frame_count: int,
    meta: dict,
) -> CreatedArtifact:
    """Create (or dedup-reuse) the session + canonical artifact for a crawl.

    ``meta`` carries provenance recorded into ``full_artifact_json.qec``
    (exploration_id, app_id, explorer_version, created_by, ...).  Raises
    :class:`RefusalError` on invalid identifiers (honest 422 upstream).
    """
    if not tenant_id or not str(tenant_id).strip():
        raise ValueError("tenant_id is required")
    if not CRAWL_ID_PATTERN.match(crawl_id or ""):
        raise RefusalError("invalid_crawl_id",
                           f"crawl_id {crawl_id!r:.60} must match "
                           f"{CRAWL_ID_PATTERN.pattern}")
    parsed = urlparse(target_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RefusalError("invalid_target_url",
                           f"expected an http(s) URL, got {target_url!r:.120}")
    if frame_count < 0:
        raise ValueError(f"frame_count must be >= 0, got {frame_count}")
    meta = dict(meta or {})

    explorer_version = str(meta.get("explorer_version") or "")
    # Scope the dedup key to the APP so a second app on the same URL gets its OWN
    # artifact instead of reusing the first app's (cross-app artifact bleed).
    fingerprint = compute_media_fingerprint(
        target_url, config_fingerprint, explorer_version,
        app_id=str(meta.get("app_id") or ""),
    )
    host = (parsed.hostname or "").lower()
    now = utc_now()

    async with tenant_scoped_substrate_session(tenant_id) as session:
        # 1) Tenant bootstrap (FK precondition — spine mirror).
        await session.execute(_TENANT_BOOTSTRAP_SQL, {
            "tid": tenant_id,
            "name": f"Auto-provisioned {tenant_id}",
            "domain": f"auto.{tenant_id}.nexus.internal",
        })

        # 2) Re-crawl dedup: identical (url, config, explorer) → same artifact.
        existing = (await session.execute(
            select(CanonicalArtifactRow.artifact_id, CanonicalArtifactRow.session_id)
            .where(
                CanonicalArtifactRow.tenant_id == tenant_id,
                CanonicalArtifactRow.media_fingerprint == fingerprint,
                CanonicalArtifactRow.source_type == SOURCE_TYPE_LIVE_CRAWL,
            )
            .order_by(CanonicalArtifactRow.created_at.desc())
            .limit(1)
        )).first()
        if existing is not None:
            artifact_id, session_id = existing
            logger.info(
                "qec.artifacts.dedup_reused",
                extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
                       "crawl_id": crawl_id, "media_fingerprint": fingerprint},
            )
            await _arm_belt1_vision_off(tenant_id, artifact_id)
            return CreatedArtifact(
                artifact_id=artifact_id,
                session_id=session_id,
                media_fingerprint=fingerprint,
                reused=True,
            )

        # 3) Session row (§2.1: title='Live crawl — {host}', completed).
        session_id = new_id()
        artifact_id = new_id()
        await session.execute(insert(SessionRow), [{
            "session_id": session_id,
            "tenant_id": tenant_id,
            "title": f"Live crawl — {host}",
            "status": "completed",
            "session_type": SESSION_TYPE_LIVE_CRAWL,
            "sme_name": "",
            "duration_minutes": 0,
            "rules_extracted": 0,
            "confidence_score": 0.0,
            "events": [],
            "transcript": [],
            "metadata_json": {"qec": {"crawl_id": crawl_id,
                                      "target_url": target_url,
                                      **{k: str(v) for k, v in meta.items()}}},
            "created_at": now,
            "updated_at": now,
        }])

        # 4) Canonical artifact (spine-engine/main.py:4698-4725 mirror with
        #    §2.1 crawl values). Quality gate computed HONESTLY from what a
        #    crawl actually has: no transcript, no vision semantics — the
        #    only coverage signal at creation time is captured screenshots.
        quality_gate_passed = frame_count > 0
        quality_gate_outcome = "passed" if quality_gate_passed else "no_visual_evidence"
        await session.execute(insert(CanonicalArtifactRow), [{
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "media_fingerprint": fingerprint,
            "status": "completed",
            "workflow_id": None,
            "source_type": SOURCE_TYPE_LIVE_CRAWL,
            "source_filename": target_url[:500],
            "created_by": str(meta.get("created_by") or "svc-qe-central"),
            "duration_seconds": 0.0,
            "scene_count": 0,
            "frame_count": frame_count,
            "safe_transcript_text": "",
            "visual_summary": "",
            "application_types_seen": ["web"],
            "brain_quality_score": None,
            "quality_gate_passed": quality_gate_passed,
            "quality_gate_outcome": quality_gate_outcome,
            "has_real_transcript": False,
            "has_visual_semantics": False,
            "semantic_completeness_score": 0.0,
            "full_artifact_json": {
                "qec": {
                    "crawl_id": crawl_id,
                    "config_fingerprint": config_fingerprint,
                    "explorer_version": explorer_version,
                    "target_url": target_url,
                    "meta": {k: str(v) for k, v in meta.items()},
                }
            },
            "processing_time_seconds": 0.0,
            "created_at": now,
            "completed_at": now,
            "error": None,
        }])

    # 5) Belt-1 (separate transaction — must never poison the artifact txn).
    await _arm_belt1_vision_off(tenant_id, artifact_id)

    logger.info(
        "qec.artifacts.created",
        extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
               "session_id": session_id, "crawl_id": crawl_id,
               "source_type": SOURCE_TYPE_LIVE_CRAWL,
               "media_fingerprint": fingerprint, "frame_count": frame_count},
    )
    return CreatedArtifact(
        artifact_id=artifact_id,
        session_id=session_id,
        media_fingerprint=fingerprint,
        reused=False,
    )
