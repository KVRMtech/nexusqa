"""
Platform API — E2E Test Architect routes.

Generates critical end-to-end test scenarios from multimodal evidence
(visual + transcript) via a two-pass Brain LLM strategy.

Platform owns: artifact lookup, persona/test-strategy cache retrieval,
               multimodal payload assembly, provenance, persistence.
Brain owns:    LLM analysis — variable extraction (pass 1) and
               scenario generation (pass 2).

This route is INDEPENDENT from test_strategy.py and personas.py.
It reads their caches (read-only) but never writes to them.
"""
from __future__ import annotations

import logging
import os
import time

import httpx
import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from typing import Optional

from nexus_sdk.db.models import CanonicalArtifactRow

from ..database import require_db, row_to_dict
from ..auth import get_current_user
from ..config import PlatformAPIConfig
from ..services.multimodal import build_e2e_brain_payload

router = APIRouter(tags=["E2E Architect"])

_logger = logging.getLogger(__name__)
_config = PlatformAPIConfig()
_BRAIN_ENGINE_URL = os.environ.get("BRAIN_ENGINE_URL", _config.brain_engine_url)


# ─── Request / Response Models ─────────────────────────────────

class GenerateE2EArchitectRequest(BaseModel):
    """Request to generate E2E Architect scenarios from a canonical artifact."""
    artifact_id: str = Field(..., description="Canonical artifact ID (must have persona draft cached)")
    session_id: Optional[str] = Field(None, description="Session context (optional)")
    force_regenerate: bool = Field(False, description="Skip cache and regenerate from scratch")


# ─── Helpers ───────────────────────────────────────────────────

def _make_service_token(tenant_id: str) -> str:
    """Create a short-lived JWT for service-to-service calls."""
    now = int(time.time())
    payload = {
        "sub": "platform-api-service",
        "tenant_id": tenant_id,
        "email": "platform-api@internal.nexus",
        "role": "admin",
        "permissions": ["*"],
        "iat": now,
        "exp": now + 3600,
    }
    return pyjwt.encode(payload, _config.jwt_secret, algorithm=_config.jwt_algorithm)


# ─── Endpoint ──────────────────────────────────────────────────

@router.post("/api/v1/e2e-architect/generate")
async def generate_e2e_architect(
    req: GenerateE2EArchitectRequest,
    user: dict = Depends(get_current_user),
):
    """Generate critical E2E test scenarios from multimodal evidence.

    Platform owns:
      - Artifact + persona/test-strategy cache retrieval (read-only)
      - Multimodal payload assembly via build_e2e_brain_payload()
      - Provenance enrichment
      - Result caching (e2e_architect_cache)

    Brain owns:
      - Two-pass LLM analysis (variable extraction → scenario generation)
      - Evidence-grounded scenario scoring
    """
    start = time.monotonic()
    tenant_id = user["tenant_id"]

    # ── 1. Load artifact from DB (tenant-scoped) ──────────
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(CanonicalArtifactRow).where(
                CanonicalArtifactRow.artifact_id == req.artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            raise HTTPException(404, f"Artifact {req.artifact_id} not found")

        artifact = row_to_dict(row)

        # ── 1b. Load persona draft for lineage check ──
        full_json = artifact.get("full_artifact_json") or {}
        persona_cache = full_json.get("persona_draft_cache")
        persona_generated_at = ""
        if persona_cache and isinstance(persona_cache, dict):
            persona_generated_at = persona_cache.get("provenance", {}).get("generated_at", "")

        # ── 1c. Check for cached E2E Architect (with lineage validation) ──
        if not req.force_regenerate:
            cached = full_json.get("e2e_architect_cache")
            if cached and isinstance(cached, dict) and cached.get("e2e_architect"):
                # Lineage check: invalidate cache if persona was regenerated
                cached_source = cached.get("provenance", {}).get("source_persona_generated_at", "")
                if not cached_source and persona_generated_at:
                    _logger.info(
                        "Invalidating pre-lineage E2E Architect cache for artifact=%s "
                        "(no source_persona_generated_at, persona has %s)",
                        req.artifact_id, persona_generated_at,
                    )
                elif cached_source and persona_generated_at and cached_source != persona_generated_at:
                    _logger.info(
                        "Invalidating stale E2E Architect cache for artifact=%s "
                        "(persona regenerated: cached_source=%s != current=%s)",
                        req.artifact_id, cached_source, persona_generated_at,
                    )
                else:
                    _logger.info(
                        "Returning cached E2E Architect for artifact=%s (generated=%s)",
                        req.artifact_id, cached.get("provenance", {}).get("generated_at", "?"),
                    )
                    elapsed_ms = (time.monotonic() - start) * 1000
                    cached["cached"] = True
                    cached["cache_hit_ms"] = round(elapsed_ms, 2)
                    return cached

    # ── 2. Validate persona draft exists (required input) ──
    full_json = artifact.get("full_artifact_json") or {}
    persona_cache = full_json.get("persona_draft_cache")
    if not persona_cache or not isinstance(persona_cache, dict):
        raise HTTPException(
            422,
            "No persona draft found for this artifact. "
            "Generate a Process Oracle persona first before running E2E Architect.",
        )

    persona_quality = persona_cache.get("draft_quality", "")
    persona_generated_at = persona_cache.get("provenance", {}).get("generated_at", "")

    if not (persona_cache.get("domain_map") or {}).get("workflows"):
        raise HTTPException(
            422,
            "Persona draft has no workflow steps — insufficient data for E2E Architect.",
        )

    # ── 2b. Quality gate ──
    if persona_quality == "fallback":
        raise HTTPException(
            422,
            "Persona draft quality is 'fallback'. "
            "Regenerate the persona from a higher-quality recording first.",
        )

    # ── 3. Assemble multimodal payload for Brain ──────────
    brain_payload = build_e2e_brain_payload(artifact)
    # Enrich with lineage metadata
    brain_payload["source_persona_generated_at"] = persona_generated_at
    brain_payload["source_persona_quality"] = persona_quality or "full"

    # ── 3b. Assess visual substrate quality ───────────────
    visual_data = full_json.get("visual_analysis") or {}
    vis_frames = visual_data.get("frames") or []
    vis_stages = visual_data.get("pipeline_stages") or []
    if isinstance(vis_stages, str):
        vis_stages = [vis_stages]

    # Determine what visual profile was originally used
    has_multimodal_markers = any("multimodal" in str(s).lower() for s in vis_stages)
    has_deep_markers = any("deep" in str(s).lower() for s in vis_stages)
    frame_count = len(vis_frames)
    has_ocr = any("ocr" in str(s).lower() and "skipped" not in str(s).lower() for s in vis_stages)

    if has_multimodal_markers:
        visual_substrate_quality = "multimodal"
    elif has_deep_markers or frame_count >= 8:
        visual_substrate_quality = "deep"
    elif frame_count > 0:
        visual_substrate_quality = "fast"
    else:
        visual_substrate_quality = "minimal"

    _logger.info(
        "Visual substrate for artifact=%s: quality=%s frames=%d ocr=%s stages=%s",
        req.artifact_id, visual_substrate_quality, frame_count, has_ocr, vis_stages[:5],
    )

    # ── 4. Call Brain engine ──────────────────────────────
    token = _make_service_token(tenant_id)
    try:
        async with httpx.AsyncClient(timeout=900.0) as client:
            resp = await client.post(
                f"{_BRAIN_ENGINE_URL}/api/v1/brain/generate-e2e-architect",
                json=brain_payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "Brain engine timed out during E2E Architect generation")
    except httpx.ConnectError:
        raise HTTPException(503, "Brain engine is unreachable")
    except httpx.RemoteProtocolError as e:
        _logger.error("Brain disconnected mid-response: %s", e)
        raise HTTPException(502, "Brain engine disconnected during E2E Architect generation")

    if resp.status_code >= 400:
        _logger.error(
            "Brain generate-e2e-architect failed: HTTP %d — %s",
            resp.status_code, resp.text[:300],
        )
        raise HTTPException(502, f"Brain engine error: HTTP {resp.status_code}")

    brain_result = resp.json()

    # ── 5. Attach platform provenance ─────────────────────
    elapsed_ms = (time.monotonic() - start) * 1000
    provenance = brain_result.get("provenance", {})
    provenance["platform_processing_ms"] = round(elapsed_ms, 2)
    provenance["source_persona_generated_at"] = persona_generated_at

    response_data = {
        "success": True,
        "artifact_id": req.artifact_id,
        "session_id": req.session_id or artifact.get("session_id", ""),
        "e2e_architect": brain_result.get("e2e_architect", {}),
        "provenance": provenance,
        "processing_time_ms": round(elapsed_ms, 2),
        "visual_substrate": {
            "quality": visual_substrate_quality,
            "frame_count": frame_count,
            "has_ocr": has_ocr,
            "recommendation": (
                "Re-upload with 'Multimodal' processing profile for richer visual evidence"
                if visual_substrate_quality == "fast"
                else None
            ),
        },
    }

    # ── 6. Cache result in artifact ───────────────────────
    try:
        async with factory() as db:
            result = await db.execute(
                select(CanonicalArtifactRow).where(
                    CanonicalArtifactRow.artifact_id == req.artifact_id,
                    CanonicalArtifactRow.tenant_id == tenant_id,
                )
            )
            art_row = result.scalar_one_or_none()
            if art_row:
                existing_json = dict(art_row.full_artifact_json or {})
                existing_json["e2e_architect_cache"] = response_data
                art_row.full_artifact_json = existing_json
                await db.commit()
                _logger.info(
                    "Cached E2E Architect for artifact=%s (%d scenarios, %dms)",
                    req.artifact_id,
                    len((response_data.get("e2e_architect") or {}).get("critical_combinations", [])),
                    round(elapsed_ms),
                )
    except Exception:
        _logger.warning(
            "Failed to cache E2E Architect for artifact=%s (non-fatal)",
            req.artifact_id, exc_info=True,
        )

    response_data["cached"] = False
    return response_data
