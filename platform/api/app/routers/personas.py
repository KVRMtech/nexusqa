"""
Platform API — Persona routes (QI Engineer Portal).

CRUD for AI personas — system-provided and tenant-custom.
System personas (tenant_id='__system__') are visible to all tenants
but cannot be modified via API.

Process Oracle: Generate persona drafts from canonical artifacts.
Platform owns artifact lookup, validation, provenance, and persistence.
Brain owns generation logic and scoring only.
"""
from __future__ import annotations

import logging
import os
import time

import httpx
import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Query, Path, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from typing import Optional

from nexus_sdk.db.models import PersonaRow, CanonicalArtifactRow

from ..database import require_db, new_id, utc_now, row_to_dict
from ..auth import get_current_user
from ..config import PlatformAPIConfig

router = APIRouter(tags=["Personas"])

SYSTEM_TENANT = "__system__"


# ─── Request / Response Models ─────────────────────────────────

class CreatePersonaRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    slug: str = Field(..., min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
    description: str = ""
    avatar_icon: str = "brain"
    system_prompt: str = ""
    capabilities: list[str] = []
    stage_config: dict = {}
    specialty_domains: list[str] = []
    metadata_json: Optional[dict] = None


class UpdatePersonaRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=200)
    description: Optional[str] = None
    avatar_icon: Optional[str] = None
    system_prompt: Optional[str] = None
    capabilities: Optional[list[str]] = None
    stage_config: Optional[dict] = None
    specialty_domains: Optional[list[str]] = None
    is_active: Optional[bool] = None


# ─── GET Endpoints ─────────────────────────────────────────────

@router.get("/api/v1/personas")
async def list_personas(
    include_inactive: bool = Query(False),
    user: dict = Depends(get_current_user),
):
    """List personas visible to this tenant (system + custom)."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        stmt = select(PersonaRow).where(
            or_(
                PersonaRow.tenant_id == SYSTEM_TENANT,
                PersonaRow.tenant_id == tenant_id,
            )
        )
        if not include_inactive:
            stmt = stmt.where(PersonaRow.is_active == True)  # noqa: E712
        stmt = stmt.order_by(PersonaRow.sort_order, PersonaRow.name)
        result = await db.execute(stmt)
        rows = result.scalars().all()
        return [_persona_to_response(r) for r in rows]


@router.get("/api/v1/personas/{persona_id}")
async def get_persona(
    persona_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Get a specific persona by ID (tenant-scoped)."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        row = await db.get(PersonaRow, persona_id)
        if not row:
            raise HTTPException(404, f"Persona {persona_id} not found")
        if row.tenant_id != tenant_id and row.tenant_id != SYSTEM_TENANT:
            raise HTTPException(404, f"Persona {persona_id} not found")
        return _persona_to_response(row)


# ─── POST / Create ────────────────────────────────────────────

@router.post("/api/v1/personas", status_code=201)
async def create_persona(
    req: CreatePersonaRequest,
    user: dict = Depends(get_current_user),
):
    """Create a custom persona for this tenant."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        # Check slug uniqueness within tenant
        existing = await db.execute(
            select(PersonaRow).where(
                PersonaRow.tenant_id == tenant_id,
                PersonaRow.slug == req.slug,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(409, f"Persona with slug '{req.slug}' already exists")

        row = PersonaRow(
            persona_id=new_id(),
            tenant_id=tenant_id,
            name=req.name,
            slug=req.slug,
            description=req.description,
            avatar_icon=req.avatar_icon,
            system_prompt=req.system_prompt,
            capabilities=req.capabilities,
            stage_config=req.stage_config or _default_stage_config(),
            specialty_domains=req.specialty_domains,
            metadata_json=req.metadata_json or {},
            is_system=False,
            is_active=True,
            sort_order=100,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _persona_to_response(row)


# ─── PUT / Update ─────────────────────────────────────────────

@router.put("/api/v1/personas/{persona_id}")
async def update_persona(
    req: UpdatePersonaRequest,
    persona_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Update a custom persona. System personas cannot be modified."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        row = await db.get(PersonaRow, persona_id)
        if not row:
            raise HTTPException(404, f"Persona {persona_id} not found")
        if row.tenant_id != tenant_id:
            raise HTTPException(404, f"Persona {persona_id} not found")
        if row.is_system:
            raise HTTPException(403, "System personas cannot be modified")

        update_fields = req.model_dump(exclude_none=True)
        for field, value in update_fields.items():
            setattr(row, field, value)
        row.updated_at = utc_now()
        await db.commit()
        await db.refresh(row)
        return _persona_to_response(row)


# ─── DELETE ────────────────────────────────────────────────────

@router.delete("/api/v1/personas/{persona_id}")
async def delete_persona(
    persona_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Delete a custom persona. System personas cannot be deleted."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        row = await db.get(PersonaRow, persona_id)
        if not row:
            raise HTTPException(404, f"Persona {persona_id} not found")
        if row.tenant_id != tenant_id:
            raise HTTPException(404, f"Persona {persona_id} not found")
        if row.is_system:
            raise HTTPException(403, "System personas cannot be deleted")
        await db.delete(row)
        await db.commit()
        return {"deleted": True, "persona_id": persona_id}


# ─── Helpers ───────────────────────────────────────────────────

def _default_stage_config() -> dict:
    """Default 5-stage config with all engines enabled."""
    return {
        "1_capture": {"engines": ["ears", "eyes", "spine", "shield"], "auto_advance": False},
        "2_understand": {"engines": ["heart", "backbone", "nerves"], "auto_advance": False},
        "3_strategize": {"engines": ["heart", "nerves"], "auto_advance": False},
        "4_generate": {"engines": ["legs", "hands", "mouth"], "auto_advance": False},
        "5_validate": {"engines": ["legs", "nerves"], "auto_advance": False},
    }


def _persona_to_response(row: PersonaRow) -> dict:
    """Convert a PersonaRow to an API response dict."""
    d = row_to_dict(row)
    # Exclude internal metadata_json from default list view
    d.pop("metadata_json", None)
    return d


# ═══════════════════════════════════════════════════════════════
#  PROCESS ORACLE — Persona Draft Generation
# ═══════════════════════════════════════════════════════════════

_logger = logging.getLogger(__name__)
_config = PlatformAPIConfig()

_BRAIN_ENGINE_URL = os.environ.get("BRAIN_ENGINE_URL", _config.brain_engine_url)


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


class GeneratePersonaDraftRequest(BaseModel):
    """Request to generate a Process Oracle persona draft from a canonical artifact."""
    artifact_id: str = Field(..., description="Canonical artifact ID to analyse")
    session_id: Optional[str] = Field(None, description="Session context (optional)")
    force_regenerate: bool = Field(False, description="Skip cache and regenerate from scratch")


@router.post("/api/v1/personas/generate-draft")
async def generate_persona_draft(
    req: GeneratePersonaDraftRequest,
    user: dict = Depends(get_current_user),
):
    """Generate a Process Oracle persona draft from a canonical artifact.

    Platform owns:
      - Artifact lookup and validation
      - Payload normalisation
      - Provenance metadata
      - Response shaping

    Brain owns:
      - LLM analysis and generation
      - Domain map extraction
      - Evidence grounding
      - Confidence scoring

    Returns the draft for user review — does NOT persist the persona.
    Call POST /api/v1/personas to save after user approval.
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

        # ── 1b. Check for cached draft ─────────────────────
        if not req.force_regenerate:
            full_json = artifact.get("full_artifact_json") or {}
            cached = full_json.get("persona_draft_cache")
            if cached and isinstance(cached, dict) and cached.get("persona"):
                _logger.info(
                    "Returning cached persona draft for artifact=%s (generated=%s)",
                    req.artifact_id, cached.get("provenance", {}).get("generated_at", "?"),
                )
                elapsed_ms = (time.monotonic() - start) * 1000
                cached["cached"] = True
                cached["cache_hit_ms"] = round(elapsed_ms, 2)
                return cached

    # ── 2. Validate artifact quality ───────────────────────
    status = artifact.get("status", "")
    if status not in ("completed", "persisted", "needs_review"):
        raise HTTPException(
            422,
            f"Artifact is in '{status}' state — persona generation requires "
            f"a completed or persisted artifact.",
        )

    safe_text = artifact.get("safe_transcript_text", "") or ""
    visual_summary = artifact.get("visual_summary", "") or ""
    if not safe_text and not visual_summary:
        raise HTTPException(
            422,
            "Artifact has no transcript and no visual summary — "
            "insufficient data for persona generation.",
        )

    # ── 3. Normalise payload for Brain ─────────────────────
    full_json = artifact.get("full_artifact_json") or {}
    transcript_data = full_json.get("transcript") or {}
    visual_data = full_json.get("visual_analysis") or {}
    visual_graph = full_json.get("visual_graph") or {}

    brain_payload = {
        "tenant_id": tenant_id,
        "artifact_id": req.artifact_id,
        "session_id": req.session_id or artifact.get("session_id", ""),
        "workflow_id": artifact.get("workflow_id", ""),
        "safe_transcript_text": safe_text,
        "visual_summary": visual_summary,
        "application_types_seen": artifact.get("application_types_seen") or [],
        "duration_seconds": artifact.get("duration_seconds", 0.0) or 0.0,
        "scene_count": artifact.get("scene_count", 0) or 0,
        "frame_count": artifact.get("frame_count", 0) or 0,
        "transcript_segments": transcript_data.get("segments", []),
        "visual_graph_nodes": visual_graph.get("nodes", []),
        "visual_graph_edges": visual_graph.get("edges", []),
        "scene_descriptions": [
            f.get("description", "")
            for f in visual_data.get("frames", [])
            if f.get("description")
        ],
        "quality_score": artifact.get("brain_quality_score", 0.0) or 0.0,
    }

    # ── 4. Call Brain engine ───────────────────────────────
    token = _make_service_token(tenant_id)
    try:
        async with httpx.AsyncClient(timeout=900.0) as client:
            resp = await client.post(
                f"{_BRAIN_ENGINE_URL}/api/v1/brain/generate-persona-draft",
                json=brain_payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "Brain engine timed out during persona generation")
    except httpx.ConnectError:
        raise HTTPException(503, "Brain engine is unreachable")
    except httpx.RemoteProtocolError as e:
        _logger.error("Brain disconnected mid-response: %s", e)
        raise HTTPException(502, "Brain engine disconnected during persona generation")

    if resp.status_code >= 400:
        _logger.error(
            "Brain generate-persona-draft failed: HTTP %d — %s",
            resp.status_code, resp.text[:300],
        )
        raise HTTPException(502, f"Brain engine error: HTTP {resp.status_code}")

    brain_result = resp.json()

    # ── 5. Attach platform provenance ──────────────────────
    elapsed_ms = (time.monotonic() - start) * 1000

    # Enrich provenance with platform-side metadata
    provenance = brain_result.get("provenance", {})
    provenance["artifact_status"] = status
    provenance["has_real_transcript"] = bool(safe_text.strip())
    provenance["has_visual_semantics"] = bool(visual_summary.strip())
    provenance["platform_processing_ms"] = round(elapsed_ms, 2)

    # Detect stub/fallback at Brain level via provenance
    model_backend = provenance.get("model_backend", "")
    is_stub = model_backend == "stub"

    response_data = {
        "success": True,
        "artifact_id": req.artifact_id,
        "session_id": req.session_id or artifact.get("session_id", ""),
        "persona": brain_result.get("persona", {}),
        "domain_map": brain_result.get("domain_map", {}),
        "grounding_contract": brain_result.get("grounding_contract", {}),
        "provenance": provenance,
        "processing_time_ms": round(elapsed_ms, 2),
    }

    # ── 6. Cache draft in artifact for instant re-load ─────
    # Always cache — even fallback results — so revisit never re-generates.
    # Tag quality so UI can distinguish full vs fallback drafts.
    if is_stub:
        # Stub/fallback always produces zero evidence
        response_data["draft_quality"] = "fallback"
    else:
        dm = response_data.get("domain_map", {})
        has_evidence = any(
            len(dm.get(cat, [])) > 0
            for cat in ("actors", "systems", "workflows", "risks")
        )
        response_data["draft_quality"] = "full" if has_evidence else "fallback"

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
                existing_json["persona_draft_cache"] = response_data
                art_row.full_artifact_json = existing_json
                await db.commit()
                _logger.info(
                    "Cached persona draft for artifact=%s quality=%s (%dms generation)",
                    req.artifact_id, response_data["draft_quality"], round(elapsed_ms),
                )
    except Exception:
        _logger.warning(
            "Failed to cache persona draft for artifact=%s (non-fatal)",
            req.artifact_id, exc_info=True,
        )

    response_data["cached"] = False
    return response_data
