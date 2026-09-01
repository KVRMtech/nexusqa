"""
Platform API — Test Strategy routes (Test Architect).

Generates comprehensive test strategies from persona draft domain knowledge.
Takes the SME persona's extracted workflows, risks, and evidence → produces
structured test scenarios with full traceability to the KT recording.

Platform owns: artifact lookup, persona cache retrieval, payload normalisation,
               provenance, persistence.
Brain owns:    LLM analysis, test case generation, priority scoring.
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

router = APIRouter(tags=["Test Strategy"])

_logger = logging.getLogger(__name__)
_config = PlatformAPIConfig()
_BRAIN_ENGINE_URL = os.environ.get("BRAIN_ENGINE_URL", _config.brain_engine_url)


# ─── Request / Response Models ─────────────────────────────────

class GenerateTestStrategyRequest(BaseModel):
    """Request to generate a test strategy from a canonical artifact's persona draft."""
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

@router.post("/api/v1/test-strategy/generate")
async def generate_test_strategy(
    req: GenerateTestStrategyRequest,
    user: dict = Depends(get_current_user),
):
    """Generate a Test Architect strategy from a persona-draft's domain knowledge.

    Platform owns:
      - Artifact + persona draft cache retrieval
      - Payload normalisation for Brain
      - Provenance enrichment
      - Result caching

    Brain owns:
      - LLM-driven test scenario generation
      - Priority scoring and coverage analysis
      - Evidence traceability
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

        # ── 1b. Load persona draft first (needed for lineage check) ──
        full_json = artifact.get("full_artifact_json") or {}
        persona_cache = full_json.get("persona_draft_cache")
        persona_generated_at = ""
        if persona_cache and isinstance(persona_cache, dict):
            persona_generated_at = persona_cache.get("provenance", {}).get("generated_at", "")

        # ── 1c. Check for cached test strategy (with lineage validation) ──
        if not req.force_regenerate:
            cached = full_json.get("test_strategy_cache")
            if cached and isinstance(cached, dict) and cached.get("test_plan"):
                # Lineage check: invalidate cache if persona was regenerated
                cached_source = cached.get("provenance", {}).get("source_persona_generated_at", "")
                if not cached_source and persona_generated_at:
                    # Pre-lineage cache entry: treat as stale since we can't verify
                    _logger.info(
                        "Invalidating pre-lineage test strategy cache for artifact=%s "
                        "(no source_persona_generated_at in cached provenance, persona has %s)",
                        req.artifact_id, persona_generated_at,
                    )
                elif cached_source and persona_generated_at and cached_source != persona_generated_at:
                    _logger.info(
                        "Invalidating stale test strategy cache for artifact=%s "
                        "(persona regenerated: cached_source=%s != current=%s)",
                        req.artifact_id, cached_source, persona_generated_at,
                    )
                else:
                    _logger.info(
                        "Returning cached test strategy for artifact=%s (generated=%s)",
                        req.artifact_id, cached.get("provenance", {}).get("generated_at", "?"),
                    )
                    elapsed_ms = (time.monotonic() - start) * 1000
                    cached["cached"] = True
                    cached["cache_hit_ms"] = round(elapsed_ms, 2)
                    return cached

    # ── 2. Retrieve persona draft (required input) ─────────
    full_json = artifact.get("full_artifact_json") or {}
    persona_cache = full_json.get("persona_draft_cache")
    if not persona_cache or not isinstance(persona_cache, dict):
        raise HTTPException(
            422,
            "No persona draft found for this artifact. "
            "Generate a Process Oracle persona first before creating a test strategy.",
        )

    persona_data = persona_cache.get("persona", {})
    domain_map = persona_cache.get("domain_map", {})
    grounding = persona_cache.get("grounding_contract", {})
    persona_quality = persona_cache.get("draft_quality", "")
    persona_generated_at = persona_cache.get("provenance", {}).get("generated_at", "")

    if not domain_map.get("workflows"):
        raise HTTPException(
            422,
            "Persona draft has no workflow steps — insufficient data for test strategy generation.",
        )

    # ── 2b. Quality gate: reject degraded persona drafts ───
    if persona_quality == "fallback":
        raise HTTPException(
            422,
            "Persona draft quality is 'fallback' (insufficient evidence). "
            "Regenerate the persona from a higher-quality recording before creating a test strategy.",
        )

    # ── 3. Normalise payload for Brain ─────────────────────
    brain_payload = {
        "tenant_id": tenant_id,
        "artifact_id": req.artifact_id,
        "session_id": req.session_id or artifact.get("session_id", ""),
        "persona_name": persona_data.get("name", ""),
        "persona_description": persona_data.get("description", ""),
        "domain_map": domain_map,
        "grounding_contract": grounding,
        "duration_seconds": artifact.get("duration_seconds", 0.0) or 0.0,
        "source_persona_generated_at": persona_generated_at,
        "source_persona_quality": persona_quality or "full",
    }

    # ── 4. Call Brain engine ───────────────────────────────
    token = _make_service_token(tenant_id)
    try:
        async with httpx.AsyncClient(timeout=900.0) as client:
            resp = await client.post(
                f"{_BRAIN_ENGINE_URL}/api/v1/brain/generate-test-strategy",
                json=brain_payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "Brain engine timed out during test strategy generation")
    except httpx.ConnectError:
        raise HTTPException(503, "Brain engine is unreachable")
    except httpx.RemoteProtocolError as e:
        _logger.error("Brain disconnected mid-response: %s", e)
        raise HTTPException(502, "Brain engine disconnected during test strategy generation")

    if resp.status_code >= 400:
        _logger.error(
            "Brain generate-test-strategy failed: HTTP %d — %s",
            resp.status_code, resp.text[:300],
        )
        try:
            detail = resp.json().get("detail", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        raise HTTPException(resp.status_code if resp.status_code in (503, 504) else 502, detail)

    brain_result = resp.json()

    # ── 5. Attach platform provenance ──────────────────────
    elapsed_ms = (time.monotonic() - start) * 1000
    provenance = brain_result.get("provenance", {})
    provenance["platform_processing_ms"] = round(elapsed_ms, 2)

    response_data = {
        "success": True,
        "artifact_id": req.artifact_id,
        "session_id": req.session_id or artifact.get("session_id", ""),
        "test_plan": brain_result.get("test_plan", {}),
        "test_scenarios": brain_result.get("test_scenarios", []),
        "coverage": brain_result.get("coverage", {}),
        "traceability": brain_result.get("traceability", []),
        "provenance": provenance,
        "processing_time_ms": round(elapsed_ms, 2),
    }

    # ── 6. Cache result in artifact ────────────────────────
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
                existing_json["test_strategy_cache"] = response_data
                art_row.full_artifact_json = existing_json
                await db.commit()
                _logger.info(
                    "Cached test strategy for artifact=%s (%d scenarios, %dms generation)",
                    req.artifact_id,
                    len(response_data.get("test_scenarios", [])),
                    round(elapsed_ms),
                )
    except Exception:
        _logger.warning(
            "Failed to cache test strategy for artifact=%s (non-fatal)",
            req.artifact_id, exc_info=True,
        )

    response_data["cached"] = False
    return response_data
