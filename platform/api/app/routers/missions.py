"""
Platform API — Mission routes (QI Engineer Portal).

Full CRUD for missions plus stage progression, artifact retrieval,
and chat messaging.  Each mission flows through 5 stages:

  1. Capture  →  2. Understand  →  3. Strategize  →  4. Generate  →  5. Validate

The MissionOrchestrator (in services/) handles engine coordination;
this module provides the HTTP interface.
"""
from __future__ import annotations

import logging
import time

import httpx
import jwt as pyjwt
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Path, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, desc, func
from sqlalchemy.orm import selectinload
from typing import Optional

from nexus_sdk.db.models import (
    MissionRow,
    MissionStageRow,
    MissionArtifactRow,
    MissionMessageRow,
    PersonaRow,
)

from ..database import require_db, new_id, utc_now, row_to_dict
from ..auth import get_current_user
from ..config import PlatformAPIConfig
from ..services.mission_orchestrator import MissionOrchestrator, StageExecutionResult

router = APIRouter(tags=["Missions"])
logger = logging.getLogger(__name__)


# ─── Constants ─────────────────────────────────────────────────

STAGE_TYPES = {
    1: "capture",
    2: "understand",
    3: "strategize",
    4: "generate",
    5: "validate",
}

STAGE_LABELS = {
    1: "Capture",
    2: "Understand",
    3: "Strategize",
    4: "Generate",
    5: "Validate",
}

VALID_STATUSES = {"draft", "active", "paused", "completed", "failed", "cancelled"}
VALID_PRIORITIES = {"critical", "high", "medium", "low"}


# ─── Engine & Auth Helpers ─────────────────────────────────────

def _get_engine_urls(config: PlatformAPIConfig) -> dict[str, str]:
    """Build engine URL dict from platform configuration."""
    return {
        "ears": config.ears_engine_url,
        "eyes": config.eyes_engine_url,
        "heart": config.heart_engine_url,
        "backbone": config.backbone_engine_url,
        "shield": config.shield_engine_url,
        "nerves": config.nerves_engine_url,
        "hands": config.hands_engine_url,
        "legs": config.legs_engine_url,
        "spine": config.spine_engine_url,
        "mouth": config.mouth_engine_url,
        "brain": config.brain_engine_url,
    }


def _make_service_token(config: PlatformAPIConfig, tenant_id: str) -> str:
    """Create a short-lived JWT for service-to-service engine calls."""
    now = int(time.time())
    payload = {
        "sub": "platform-api-service",
        "tenant_id": tenant_id,
        "role": "admin",
        "permissions": ["*"],
        "iat": now,
        "exp": now + 3600,
    }
    return pyjwt.encode(payload, config.jwt_secret, algorithm=config.jwt_algorithm)


def _infer_artifact_type(stage_type: str, output_key: str) -> str:
    """Map stage type + output key to a MissionArtifact type."""
    mapping = {
        "capture": {"spine": "document", "shield": "document", "ears": "transcript", "eyes": "document"},
        "understand": {"heart": "rules", "backbone": "graph_snapshot", "nerves": "strategy"},
        "strategize": {"heart": "strategy", "nerves": "strategy"},
        "generate": {"legs": "test_cases", "hands": "test_data", "mouth": "report"},
        "validate": {"legs": "execution_results", "nerves": "report"},
    }
    engine = output_key.split("_")[0]
    return mapping.get(stage_type, {}).get(engine, "document")


# ─── Request Models ────────────────────────────────────────────

class CreateMissionRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=500)
    description: str = ""
    objective: str = ""
    persona_id: str = Field(..., min_length=1, description="ID of the persona to guide this mission")
    priority: str = Field("medium", pattern=r"^(critical|high|medium|low)$")
    tags: list[str] = []


class UpdateMissionRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=3, max_length=500)
    description: Optional[str] = None
    objective: Optional[str] = None
    priority: Optional[str] = Field(None, pattern=r"^(critical|high|medium|low)$")
    tags: Optional[list[str]] = None
    status: Optional[str] = Field(None, pattern=r"^(draft|active|paused|completed|failed|cancelled)$")


class AdvanceStageRequest(BaseModel):
    """Request to advance to the next mission stage."""
    skip_current: bool = Field(False, description="If true, mark current stage as skipped rather than completed")
    stage_inputs: dict = Field(default_factory=dict, description="Optional inputs for the next stage")


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)
    content_type: str = Field("text", pattern=r"^(text|markdown|json|action)$")
    action_data: Optional[dict] = None


class AddArtifactRequest(BaseModel):
    artifact_type: str = Field(
        ...,
        pattern=r"^(document|transcript|rules|test_cases|test_data|report|graph_snapshot|strategy|execution_results)$",
    )
    name: str = Field(..., min_length=1, max_length=500)
    description: str = ""
    content_json: dict = {}
    content_text: str = ""
    item_count: int = 0


# ─── LIST / GET ────────────────────────────────────────────────

@router.get("/api/v1/missions")
async def list_missions(
    tenant_id: str = Query("t-1"),
    status: Optional[str] = Query(None),
    persona_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List missions for this tenant, optionally filtered by status or persona."""
    factory = require_db()
    async with factory() as db:
        stmt = (
            select(MissionRow)
            .options(selectinload(MissionRow.stages))
            .where(MissionRow.tenant_id == tenant_id)
        )
        if status:
            stmt = stmt.where(MissionRow.status == status)
        if persona_id:
            stmt = stmt.where(MissionRow.persona_id == persona_id)
        stmt = stmt.order_by(desc(MissionRow.updated_at)).offset(offset).limit(limit)
        result = await db.execute(stmt)
        missions = result.unique().scalars().all()

        # Get total count for pagination
        count_stmt = select(func.count(MissionRow.mission_id)).where(
            MissionRow.tenant_id == tenant_id
        )
        if status:
            count_stmt = count_stmt.where(MissionRow.status == status)
        if persona_id:
            count_stmt = count_stmt.where(MissionRow.persona_id == persona_id)
        total = (await db.execute(count_stmt)).scalar() or 0

        return {
            "missions": [_mission_summary(m) for m in missions],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.get("/api/v1/missions/dashboard")
async def mission_dashboard(
    tenant_id: str = Query("t-1"),
    user: dict = Depends(get_current_user),
):
    """Dashboard summary: counts by status, recent missions, stage distribution."""
    factory = require_db()
    async with factory() as db:
        # Status counts
        status_stmt = (
            select(MissionRow.status, func.count(MissionRow.mission_id))
            .where(MissionRow.tenant_id == tenant_id)
            .group_by(MissionRow.status)
        )
        status_result = await db.execute(status_stmt)
        status_counts = {row[0]: row[1] for row in status_result.all()}

        # Stage distribution (for active missions)
        stage_stmt = (
            select(MissionRow.current_stage, func.count(MissionRow.mission_id))
            .where(
                MissionRow.tenant_id == tenant_id,
                MissionRow.status == "active",
            )
            .group_by(MissionRow.current_stage)
        )
        stage_result = await db.execute(stage_stmt)
        stage_distribution = {
            STAGE_LABELS.get(row[0], f"Stage {row[0]}"): row[1]
            for row in stage_result.all()
        }

        # Recent missions
        recent_stmt = (
            select(MissionRow)
            .options(selectinload(MissionRow.stages))
            .where(MissionRow.tenant_id == tenant_id)
            .order_by(desc(MissionRow.updated_at))
            .limit(5)
        )
        recent_result = await db.execute(recent_stmt)
        recent = recent_result.unique().scalars().all()

        # Total artifact count
        artifact_count_stmt = (
            select(func.count(MissionArtifactRow.artifact_id))
            .join(MissionRow)
            .where(MissionRow.tenant_id == tenant_id)
        )
        artifact_count = (await db.execute(artifact_count_stmt)).scalar() or 0

        return {
            "total_missions": sum(status_counts.values()),
            "status_counts": status_counts,
            "stage_distribution": stage_distribution,
            "total_artifacts": artifact_count,
            "recent_missions": [_mission_summary(m) for m in recent],
        }


@router.get("/api/v1/missions/{mission_id}")
async def get_mission(
    mission_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Get full mission detail including stages, artifacts, and recent messages."""
    factory = require_db()
    async with factory() as db:
        stmt = (
            select(MissionRow)
            .options(
                selectinload(MissionRow.stages).selectinload(MissionStageRow.artifacts),
                selectinload(MissionRow.persona),
            )
            .where(MissionRow.mission_id == mission_id)
        )
        result = await db.execute(stmt)
        mission = result.unique().scalar_one_or_none()
        if not mission:
            raise HTTPException(404, f"Mission {mission_id} not found")

        # Get recent messages (last 50)
        msg_stmt = (
            select(MissionMessageRow)
            .where(MissionMessageRow.mission_id == mission_id)
            .order_by(desc(MissionMessageRow.created_at))
            .limit(50)
        )
        msg_result = await db.execute(msg_stmt)
        messages = list(reversed(msg_result.scalars().all()))

        return _mission_detail(mission, messages)


# ─── CREATE ────────────────────────────────────────────────────

@router.post("/api/v1/missions", status_code=201)
async def create_mission(
    req: CreateMissionRequest,
    tenant_id: str = Query("t-1"),
    user: dict = Depends(get_current_user),
):
    """Create a new mission with all 5 stages pre-initialized."""
    factory = require_db()
    async with factory() as db:
        # Validate persona exists
        persona = await db.get(PersonaRow, req.persona_id)
        if not persona:
            raise HTTPException(404, f"Persona {req.persona_id} not found")
        if not persona.is_active:
            raise HTTPException(400, f"Persona '{persona.name}' is inactive")

        now = utc_now()
        mission_id = new_id()

        mission = MissionRow(
            mission_id=mission_id,
            tenant_id=tenant_id,
            user_id=user.get("user_id"),
            persona_id=req.persona_id,
            title=req.title,
            description=req.description,
            objective=req.objective,
            status="draft",
            current_stage=1,
            priority=req.priority,
            tags=req.tags,
            context={},
            progress_pct=0.0,
            created_at=now,
            updated_at=now,
        )
        db.add(mission)

        # Pre-create all 5 stages
        for num in range(1, 6):
            stage = MissionStageRow(
                stage_id=new_id(),
                mission_id=mission_id,
                stage_number=num,
                stage_type=STAGE_TYPES[num],
                status="pending",
                inputs={},
                outputs={},
                engine_calls=[],
            )
            db.add(stage)

        # Add welcome system message from persona
        welcome_msg = MissionMessageRow(
            message_id=new_id(),
            mission_id=mission_id,
            role="assistant",
            content=(
                f"Welcome! I'm **{persona.name}**. I'll be guiding you through "
                f"this mission: *{req.title}*.\n\n"
                f"We'll work through 5 stages together:\n"
                f"1. **Capture** — Gather requirements and domain knowledge\n"
                f"2. **Understand** — Analyze rules and relationships\n"
                f"3. **Strategize** — Plan your testing approach\n"
                f"4. **Generate** — Create test artifacts\n"
                f"5. **Validate** — Execute and verify\n\n"
                f"When you're ready, start Stage 1 by uploading documents, "
                f"recordings, or describing your testing goals."
            ),
            stage_number=1,
            content_type="markdown",
            created_at=now,
        )
        db.add(welcome_msg)

        await db.commit()

        # Re-fetch with stages loaded
        stmt = (
            select(MissionRow)
            .options(
                selectinload(MissionRow.stages),
                selectinload(MissionRow.persona),
            )
            .where(MissionRow.mission_id == mission_id)
        )
        fresh = (await db.execute(stmt)).unique().scalar_one()
        return _mission_detail(fresh, [welcome_msg])


# ─── UPDATE ────────────────────────────────────────────────────

@router.put("/api/v1/missions/{mission_id}")
async def update_mission(
    req: UpdateMissionRequest,
    mission_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Update mission metadata or status."""
    factory = require_db()
    async with factory() as db:
        mission = await db.get(MissionRow, mission_id)
        if not mission:
            raise HTTPException(404, f"Mission {mission_id} not found")

        update_fields = req.model_dump(exclude_none=True)

        # Handle status transitions
        if "status" in update_fields:
            new_status = update_fields["status"]
            _validate_status_transition(mission.status, new_status)
            if new_status == "active" and not mission.started_at:
                mission.started_at = utc_now()
            if new_status in ("completed", "failed", "cancelled"):
                mission.completed_at = utc_now()
                mission.progress_pct = 100.0 if new_status == "completed" else mission.progress_pct

        for field, value in update_fields.items():
            setattr(mission, field, value)
        mission.updated_at = utc_now()
        await db.commit()
        await db.refresh(mission)
        return row_to_dict(mission)


# ─── DELETE ────────────────────────────────────────────────────

@router.delete("/api/v1/missions/{mission_id}")
async def delete_mission(
    mission_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Delete a mission and all child stages/artifacts/messages (cascade)."""
    factory = require_db()
    async with factory() as db:
        mission = await db.get(MissionRow, mission_id)
        if not mission:
            raise HTTPException(404, f"Mission {mission_id} not found")
        await db.delete(mission)
        await db.commit()
        return {"deleted": True, "mission_id": mission_id}


# ─── STAGE OPERATIONS ─────────────────────────────────────────

@router.post("/api/v1/missions/{mission_id}/stages/{stage_number}/start")
async def start_stage(
    request: Request,
    background_tasks: BackgroundTasks,
    mission_id: str = Path(...),
    stage_number: int = Path(..., ge=1, le=5),
    user: dict = Depends(get_current_user),
):
    """Mark a stage as active and dispatch engine execution via MissionOrchestrator."""
    config: PlatformAPIConfig = request.app.state.config
    factory = require_db()
    async with factory() as db:
        mission = await _load_mission_with_stages(db, mission_id)

        if mission.status == "draft":
            mission.status = "active"
            mission.started_at = utc_now()

        if mission.status not in ("active", "paused"):
            raise HTTPException(400, f"Cannot start stage on a {mission.status} mission")

        stage = _get_stage(mission, stage_number)
        if stage.status not in ("pending", "failed"):
            raise HTTPException(400, f"Stage {stage_number} is already {stage.status}")

        stage.status = "active"
        stage.started_at = utc_now()
        mission.current_stage = stage_number
        mission.progress_pct = _calculate_progress(mission.stages)
        mission.updated_at = utc_now()

        # Resolve persona stage config for the current stage
        persona = await db.get(PersonaRow, mission.persona_id) if mission.persona_id else None
        stage_config_key = f"{stage_number}_{STAGE_TYPES[stage_number]}"
        persona_stage_config = (
            (persona.stage_config or {}).get(stage_config_key, {})
            if persona else {}
        )

        # Snapshot data for background orchestrator task
        bg_kwargs = {
            "mission_id": mission_id,
            "stage_id": stage.stage_id,
            "stage_type": stage.stage_type,
            "stage_number": stage_number,
            "persona_stage_config": persona_stage_config,
            "mission_context": dict(mission.context or {}),
            "stage_inputs": dict(stage.inputs or {}),
            "tenant_id": mission.tenant_id,
            "config": config,
        }

        await db.commit()

        # Dispatch engine execution in background (after DB commit)
        if persona_stage_config.get("engines"):
            background_tasks.add_task(_execute_mission_stage_bg, **bg_kwargs)
            logger.info(
                "stage.execution.dispatched mission=%s stage=%d engines=%s",
                mission_id, stage_number, persona_stage_config["engines"],
            )

        return _stage_to_response(stage)


@router.post("/api/v1/missions/{mission_id}/stages/{stage_number}/complete")
async def complete_stage(
    mission_id: str = Path(...),
    stage_number: int = Path(..., ge=1, le=5),
    outputs: dict = None,
    user: dict = Depends(get_current_user),
):
    """Mark a stage as completed with optional outputs."""
    factory = require_db()
    async with factory() as db:
        mission = await _load_mission_with_stages(db, mission_id)
        stage = _get_stage(mission, stage_number)

        if stage.status != "active":
            raise HTTPException(400, f"Stage {stage_number} is not active (current: {stage.status})")

        now = utc_now()
        stage.status = "completed"
        stage.completed_at = now
        if stage.started_at:
            stage.duration_seconds = (now - stage.started_at).total_seconds()
        if outputs:
            stage.outputs = outputs

        mission.progress_pct = _calculate_progress(mission.stages)
        mission.updated_at = now

        # Auto-complete mission if all stages are done
        all_done = all(
            s.status in ("completed", "skipped") for s in mission.stages
        )
        if all_done:
            mission.status = "completed"
            mission.completed_at = now
            mission.progress_pct = 100.0

        await db.commit()
        return _stage_to_response(stage)


@router.post("/api/v1/missions/{mission_id}/advance")
async def advance_mission(
    request: Request,
    background_tasks: BackgroundTasks,
    req: AdvanceStageRequest,
    mission_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Advance to the next stage. Completes or skips current and dispatches engine execution."""
    config: PlatformAPIConfig = request.app.state.config
    factory = require_db()
    async with factory() as db:
        mission = await _load_mission_with_stages(db, mission_id)

        if mission.status not in ("active", "draft"):
            raise HTTPException(400, f"Cannot advance a {mission.status} mission")

        current = _get_stage(mission, mission.current_stage)
        now = utc_now()

        # Close current stage
        if current.status == "active":
            current.status = "skipped" if req.skip_current else "completed"
            current.completed_at = now
            if current.started_at:
                current.duration_seconds = (now - current.started_at).total_seconds()

        # Find next stage
        next_number = mission.current_stage + 1
        if next_number > 5:
            # All stages done
            mission.status = "completed"
            mission.completed_at = now
            mission.progress_pct = 100.0
            mission.updated_at = now
            await db.commit()
            return {
                "advanced": False,
                "mission_status": "completed",
                "message": "All 5 stages completed. Mission is now finished.",
            }

        # Activate next stage
        next_stage = _get_stage(mission, next_number)
        next_stage.status = "active"
        next_stage.started_at = now
        if req.stage_inputs:
            next_stage.inputs = req.stage_inputs

        if mission.status == "draft":
            mission.status = "active"
            mission.started_at = now
        mission.current_stage = next_number
        mission.progress_pct = _calculate_progress(mission.stages)
        mission.updated_at = now

        # Resolve persona stage config for the next stage
        persona = await db.get(PersonaRow, mission.persona_id) if mission.persona_id else None
        stage_config_key = f"{next_number}_{STAGE_TYPES[next_number]}"
        persona_stage_config = (
            (persona.stage_config or {}).get(stage_config_key, {})
            if persona else {}
        )

        bg_kwargs = {
            "mission_id": mission_id,
            "stage_id": next_stage.stage_id,
            "stage_type": next_stage.stage_type,
            "stage_number": next_number,
            "persona_stage_config": persona_stage_config,
            "mission_context": dict(mission.context or {}),
            "stage_inputs": dict(next_stage.inputs or {}),
            "tenant_id": mission.tenant_id,
            "config": config,
        }

        await db.commit()

        # Dispatch engine execution for the next stage
        if persona_stage_config.get("engines"):
            background_tasks.add_task(_execute_mission_stage_bg, **bg_kwargs)
            logger.info(
                "stage.execution.dispatched mission=%s stage=%d engines=%s",
                mission_id, next_number, persona_stage_config["engines"],
            )

        return {
            "advanced": True,
            "previous_stage": mission.current_stage - 1,
            "current_stage": next_number,
            "stage_type": STAGE_TYPES[next_number],
            "stage_label": STAGE_LABELS[next_number],
        }


@router.get("/api/v1/missions/{mission_id}/stages")
async def list_stages(
    mission_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """List all 5 stages for a mission with their current status."""
    factory = require_db()
    async with factory() as db:
        mission = await _load_mission_with_stages(db, mission_id)
        return [_stage_to_response(s) for s in mission.stages]


@router.get("/api/v1/missions/{mission_id}/stages/{stage_number}")
async def get_stage(
    mission_id: str = Path(...),
    stage_number: int = Path(..., ge=1, le=5),
    user: dict = Depends(get_current_user),
):
    """Get details of a specific stage including artifacts."""
    factory = require_db()
    async with factory() as db:
        stmt = (
            select(MissionStageRow)
            .options(selectinload(MissionStageRow.artifacts))
            .where(
                MissionStageRow.mission_id == mission_id,
                MissionStageRow.stage_number == stage_number,
            )
        )
        result = await db.execute(stmt)
        stage = result.unique().scalar_one_or_none()
        if not stage:
            raise HTTPException(404, f"Stage {stage_number} not found for mission {mission_id}")

        resp = _stage_to_response(stage)
        resp["artifacts"] = [row_to_dict(a) for a in stage.artifacts]
        return resp


# ─── ARTIFACTS ─────────────────────────────────────────────────

@router.get("/api/v1/missions/{mission_id}/artifacts")
async def list_artifacts(
    mission_id: str = Path(...),
    artifact_type: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    """List all artifacts for a mission, optionally filtered by type."""
    factory = require_db()
    async with factory() as db:
        stmt = select(MissionArtifactRow).where(
            MissionArtifactRow.mission_id == mission_id
        )
        if artifact_type:
            stmt = stmt.where(MissionArtifactRow.artifact_type == artifact_type)
        stmt = stmt.order_by(MissionArtifactRow.created_at)
        result = await db.execute(stmt)
        return [row_to_dict(a) for a in result.scalars().all()]


@router.post("/api/v1/missions/{mission_id}/stages/{stage_number}/artifacts", status_code=201)
async def add_artifact(
    req: AddArtifactRequest,
    mission_id: str = Path(...),
    stage_number: int = Path(..., ge=1, le=5),
    user: dict = Depends(get_current_user),
):
    """Add an artifact to a specific stage."""
    factory = require_db()
    async with factory() as db:
        # Find the stage
        stmt = select(MissionStageRow).where(
            MissionStageRow.mission_id == mission_id,
            MissionStageRow.stage_number == stage_number,
        )
        result = await db.execute(stmt)
        stage = result.scalar_one_or_none()
        if not stage:
            raise HTTPException(404, f"Stage {stage_number} not found for mission {mission_id}")

        artifact = MissionArtifactRow(
            artifact_id=new_id(),
            mission_id=mission_id,
            stage_id=stage.stage_id,
            artifact_type=req.artifact_type,
            name=req.name,
            description=req.description,
            content_json=req.content_json,
            content_text=req.content_text,
            item_count=req.item_count,
            created_at=utc_now(),
        )
        db.add(artifact)
        await db.commit()
        await db.refresh(artifact)
        return row_to_dict(artifact)


@router.get("/api/v1/missions/{mission_id}/artifacts/{artifact_id}")
async def get_artifact(
    mission_id: str = Path(...),
    artifact_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Get a specific artifact by ID."""
    factory = require_db()
    async with factory() as db:
        artifact = await db.get(MissionArtifactRow, artifact_id)
        if not artifact or artifact.mission_id != mission_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")
        return row_to_dict(artifact)


# ─── CHAT / MESSAGES ──────────────────────────────────────────

@router.get("/api/v1/missions/{mission_id}/messages")
async def list_messages(
    mission_id: str = Path(...),
    stage_number: Optional[int] = Query(None, ge=1, le=5),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """Get chat messages for a mission, optionally filtered by stage."""
    factory = require_db()
    async with factory() as db:
        stmt = select(MissionMessageRow).where(
            MissionMessageRow.mission_id == mission_id
        )
        if stage_number:
            stmt = stmt.where(MissionMessageRow.stage_number == stage_number)
        stmt = stmt.order_by(MissionMessageRow.created_at).offset(offset).limit(limit)
        result = await db.execute(stmt)
        return [row_to_dict(m) for m in result.scalars().all()]


@router.post("/api/v1/missions/{mission_id}/messages", status_code=201)
async def send_message(
    request: Request,
    req: SendMessageRequest,
    mission_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Send a message in the mission chat. Returns the user message and an assistant response."""
    config: PlatformAPIConfig = request.app.state.config
    factory = require_db()
    async with factory() as db:
        mission = await db.get(MissionRow, mission_id)
        if not mission:
            raise HTTPException(404, f"Mission {mission_id} not found")

        now = utc_now()

        # Store user message
        user_msg = MissionMessageRow(
            message_id=new_id(),
            mission_id=mission_id,
            role="user",
            content=req.content,
            stage_number=mission.current_stage,
            content_type=req.content_type,
            action_data=req.action_data,
            created_at=now,
        )
        db.add(user_msg)

        # Generate assistant response via Heart LLM (with template fallback)
        persona = await db.get(PersonaRow, mission.persona_id) if mission.persona_id else None
        persona_name = persona.name if persona else "Assistant"
        stage_label = STAGE_LABELS.get(mission.current_stage, "Unknown")

        assistant_content = await _generate_assistant_response(
            persona_name=persona_name,
            stage_label=stage_label,
            stage_number=mission.current_stage,
            user_message=req.content,
            mission_title=mission.title,
            system_prompt=persona.system_prompt if persona else "",
            mission_context=mission.context or {},
            config=config,
        )

        assistant_msg = MissionMessageRow(
            message_id=new_id(),
            mission_id=mission_id,
            role="assistant",
            content=assistant_content,
            stage_number=mission.current_stage,
            content_type="markdown",
            created_at=utc_now(),
        )
        db.add(assistant_msg)
        mission.updated_at = utc_now()
        await db.commit()

        return {
            "user_message": row_to_dict(user_msg),
            "assistant_message": row_to_dict(assistant_msg),
        }


# ─── Stage → Workflow Chain Mapping ────────────────────────────
# Maps mission stage types to generic orchestrator chains.
# If a mapping exists, the stage is executed via the orchestrator
# for full workflow tracing. Otherwise, direct engine calls are used.

_STAGE_CHAIN_MAP: dict[str, str] = {
    "capture": "nexus.canonical-processing",
    # Future: "generate" → "nexus.qa-testing", etc.
}


async def _try_start_workflow(
    config: PlatformAPIConfig,
    chain_id: str,
    tenant_id: str,
    session_id: str | None,
    input_data: dict,
    auth_token: str,
) -> str | None:
    """Attempt to start a generic orchestrator workflow. Returns workflow_id or None."""
    orchestrator_url = getattr(config, "orchestrator_url", None) or "http://localhost:8100"
    url = f"{orchestrator_url}/api/v1/orchestrator/workflows/start"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json={
                    "chain_id": chain_id,
                    "tenant_id": tenant_id,
                    "session_id": session_id or "",
                    "input_data": input_data,
                },
                headers={"Authorization": auth_token},
            )
            if resp.status_code < 400:
                data = resp.json()
                return data.get("workflow_id")
            logger.warning(
                "workflow.start_failed chain=%s status=%d",
                chain_id, resp.status_code,
            )
    except Exception as exc:
        logger.warning("workflow.start_error chain=%s error=%s", chain_id, exc)
    return None


async def _await_workflow_completion(
    config: PlatformAPIConfig,
    workflow_id: str,
    auth_token: str,
    timeout_seconds: int = 300,
    poll_interval: float = 3.0,
) -> dict:
    """Poll the generic orchestrator until the workflow reaches a terminal state.

    Returns a dict with keys: success, status, outputs, engine_calls, error, artifact_id.
    """
    import asyncio

    orchestrator_url = getattr(config, "orchestrator_url", None) or "http://localhost:8100"
    url = f"{orchestrator_url}/api/v1/orchestrator/workflows/{workflow_id}"
    terminal_states = {"completed", "failed", "cancelled"}
    deadline = time.monotonic() + timeout_seconds

    async with httpx.AsyncClient(timeout=15.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url, headers={"Authorization": auth_token})
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status", "")
                    if status in terminal_states:
                        # Extract stage outputs into a flattened dict
                        outputs: dict = {}
                        engine_calls: list = []
                        artifact_id: str | None = None
                        for sid, stage_data in (data.get("stages") or {}).items():
                            if isinstance(stage_data, dict):
                                stage_output = stage_data.get("output") or {}
                                if stage_output:
                                    outputs[sid] = stage_output
                                if stage_data.get("engine"):
                                    engine_calls.append({
                                        "engine": stage_data["engine"],
                                        "endpoint": stage_data.get("endpoint", ""),
                                        "status": stage_data.get("status", ""),
                                        "duration_ms": stage_data.get("duration_ms", 0),
                                    })
                                # Look for canonical artifact_id
                                if stage_output.get("artifact_id"):
                                    artifact_id = stage_output["artifact_id"]

                        return {
                            "success": status == "completed",
                            "status": status,
                            "outputs": outputs,
                            "engine_calls": engine_calls,
                            "error": data.get("error"),
                            "artifact_id": artifact_id,
                        }
            except Exception as exc:
                logger.warning("workflow.poll_error workflow=%s error=%s", workflow_id, exc)

            await asyncio.sleep(poll_interval)

    # Timed out
    logger.error("workflow.poll_timeout workflow=%s timeout=%ds", workflow_id, timeout_seconds)
    return {
        "success": False,
        "status": "timeout",
        "outputs": {},
        "engine_calls": [],
        "error": f"Workflow {workflow_id} did not complete within {timeout_seconds}s",
        "artifact_id": None,
    }


# ─── Background Stage Execution ────────────────────────────────

async def _execute_mission_stage_bg(
    mission_id: str,
    stage_id: str,
    stage_type: str,
    stage_number: int,
    persona_stage_config: dict,
    mission_context: dict,
    stage_inputs: dict,
    tenant_id: str,
    config: PlatformAPIConfig,
) -> None:
    """Background task: run MissionOrchestrator.execute_stage and persist results.

    Called by start_stage and advance_mission after the HTTP response is sent.
    Opens its own DB session so the request session can close normally.
    """
    engine_urls = _get_engine_urls(config)
    auth_token = f"Bearer {_make_service_token(config, tenant_id)}"

    try:
        factory = require_db()
    except Exception:
        logger.error("stage_bg.db_unavailable mission=%s stage=%d", mission_id, stage_number)
        return

    # ── Try generic orchestrator workflow dispatch ──────────
    workflow_id: str | None = None
    chain_id = _STAGE_CHAIN_MAP.get(stage_type)
    if chain_id:
        session_id = mission_context.get("session_id") or stage_inputs.get("session_id")
        workflow_id = await _try_start_workflow(
            config, chain_id, tenant_id, session_id, stage_inputs, auth_token,
        )
        if workflow_id:
            logger.info(
                "stage_bg.workflow_started mission=%s stage=%d chain=%s workflow=%s",
                mission_id, stage_number, chain_id, workflow_id,
            )
            # Store workflow_id on the stage row immediately
            async with factory() as db:
                stage = await db.get(MissionStageRow, stage_id)
                if stage:
                    stage.workflow_id = workflow_id
                    await db.commit()

            # ── Await workflow completion (poll the orchestrator) ──
            # When a generic workflow handles the stage, we do NOT run
            # the direct MissionOrchestrator path — that would duplicate
            # every engine call.
            workflow_result = await _await_workflow_completion(
                config, workflow_id, auth_token, timeout_seconds=300,
            )
            # Persist workflow outcome into the mission stage
            async with factory() as db:
                stage = await db.get(MissionStageRow, stage_id)
                mission = await db.get(MissionRow, mission_id)
                if not stage or not mission:
                    logger.warning("stage_bg.missing_rows mission=%s stage_id=%s", mission_id, stage_id)
                    return

                now = utc_now()
                stage.outputs = workflow_result.get("outputs", {})
                stage.engine_calls = workflow_result.get("engine_calls", [])
                stage.status = "completed" if workflow_result.get("success") else "failed"
                stage.error_message = workflow_result.get("error")
                stage.completed_at = now
                if stage.started_at:
                    stage.duration_seconds = (now - stage.started_at).total_seconds()

                # Merge outputs into mission context
                updated_context = dict(mission.context or {})
                for key, value in (stage.outputs or {}).items():
                    updated_context[f"stage_{stage_number}_{key}"] = value
                if stage_type == "capture" and workflow_result.get("artifact_id"):
                    updated_context["canonical_artifact_id"] = workflow_result["artifact_id"]
                mission.context = updated_context
                mission.updated_at = now

                # System message
                status_word = "completed" if stage.status == "completed" else "failed"
                system_msg = MissionMessageRow(
                    message_id=new_id(),
                    mission_id=mission_id,
                    role="system",
                    content=(
                        f"**Stage {stage_number} ({STAGE_LABELS.get(stage_number, '')})** {status_word} "
                        f"via workflow `{workflow_id}`."
                    ),
                    stage_number=stage_number,
                    content_type="markdown",
                    created_at=now,
                )
                db.add(system_msg)
                await db.commit()

            logger.info(
                "stage_bg.workflow_persisted mission=%s stage=%d workflow=%s status=%s",
                mission_id, stage_number, workflow_id, workflow_result.get("status"),
            )
            return  # ← workflow handled everything; skip direct engine path

    # ── Fallback: direct engine calls for stages without a workflow chain ──
    result: Optional[StageExecutionResult] = None
    async with httpx.AsyncClient(timeout=300.0) as http_client:
        orchestrator = MissionOrchestrator(http_client, engine_urls)
        try:
            result = await orchestrator.execute_stage(
                stage_type=stage_type,
                persona_stage_config=persona_stage_config,
                mission_context=mission_context,
                stage_inputs=stage_inputs,
                tenant_id=tenant_id,
                auth_token=auth_token,
            )
            logger.info(
                "stage_bg.executed mission=%s stage=%d success=%s engines=%d duration_ms=%.0f",
                mission_id, stage_number, result.success,
                len(result.engine_calls), result.total_duration_ms,
            )
        except Exception as exc:
            logger.error(
                "stage_bg.execution_error mission=%s stage=%d error=%s",
                mission_id, stage_number, exc,
            )
            # Record failure in DB
            async with factory() as db:
                stage = await db.get(MissionStageRow, stage_id)
                if stage:
                    stage.status = "failed"
                    stage.error_message = str(exc)[:2000]
                    stage.completed_at = utc_now()
                    if stage.started_at:
                        stage.duration_seconds = (stage.completed_at - stage.started_at).total_seconds()
                    await db.commit()
            return

    # Persist results into the database
    async with factory() as db:
        stage = await db.get(MissionStageRow, stage_id)
        mission = await db.get(MissionRow, mission_id)
        if not stage or not mission:
            logger.warning("stage_bg.missing_rows mission=%s stage_id=%s", mission_id, stage_id)
            return

        now = utc_now()
        stage.outputs = result.outputs
        stage.engine_calls = [c.to_dict() for c in result.engine_calls]

        if result.success:
            stage.status = "completed"
        else:
            stage.status = "failed"
            stage.error_message = result.error

        stage.completed_at = now
        if stage.started_at:
            stage.duration_seconds = (now - stage.started_at).total_seconds()

        # Merge outputs into mission context for downstream stages
        updated_context = dict(mission.context or {})
        for key, value in result.outputs.items():
            updated_context[f"stage_{stage_number}_{key}"] = value

        # Propagate canonical artifact IDs from capture stage
        if stage_type == "capture":
            for call in result.engine_calls:
                if call.status == "ok" and call.response_data.get("artifact_id"):
                    updated_context["canonical_artifact_id"] = call.response_data["artifact_id"]
                    break

        mission.context = updated_context
        mission.updated_at = now

        # Create artifacts from engine outputs
        for key, output_data in result.outputs.items():
            artifact_type = _infer_artifact_type(stage_type, key)
            artifact = MissionArtifactRow(
                artifact_id=new_id(),
                mission_id=mission_id,
                stage_id=stage.stage_id,
                artifact_type=artifact_type,
                name=f"{STAGE_LABELS.get(stage_number, 'Stage')} \u2014 {key}",
                description=f"Generated by {key.split('_')[0]} engine during {stage_type} stage",
                content_json=output_data if isinstance(output_data, dict) else {"data": output_data},
                created_at=now,
            )
            db.add(artifact)

        # Add system message about stage completion
        engine_count = len(result.engine_calls)
        status_word = "completed" if result.success else "failed"
        system_msg = MissionMessageRow(
            message_id=new_id(),
            mission_id=mission_id,
            role="system",
            content=(
                f"**Stage {stage_number} ({STAGE_LABELS.get(stage_number, '')})** {status_word}. "
                f"Processed {engine_count} engine call{'s' if engine_count != 1 else ''} "
                f"in {result.total_duration_ms:.0f}ms."
            ),
            stage_number=stage_number,
            content_type="markdown",
            created_at=now,
        )
        db.add(system_msg)

        await db.commit()
        logger.info(
            "stage_bg.persisted mission=%s stage=%d status=%s artifacts=%d",
            mission_id, stage_number, status_word, len(result.outputs),
        )


# ─── Helpers ───────────────────────────────────────────────────

async def _load_mission_with_stages(db, mission_id: str) -> MissionRow:
    """Load a mission with eager-loaded stages. Raises 404 if not found."""
    stmt = (
        select(MissionRow)
        .options(selectinload(MissionRow.stages))
        .where(MissionRow.mission_id == mission_id)
    )
    result = await db.execute(stmt)
    mission = result.unique().scalar_one_or_none()
    if not mission:
        raise HTTPException(404, f"Mission {mission_id} not found")
    return mission


def _get_stage(mission: MissionRow, stage_number: int) -> MissionStageRow:
    """Find a stage by number within a mission's loaded stages."""
    for stage in mission.stages:
        if stage.stage_number == stage_number:
            return stage
    raise HTTPException(404, f"Stage {stage_number} not found in mission {mission.mission_id}")


def _validate_status_transition(current: str, target: str):
    """Validate that the status transition is allowed."""
    allowed = {
        "draft": {"active", "cancelled"},
        "active": {"paused", "completed", "failed", "cancelled"},
        "paused": {"active", "cancelled"},
        "completed": set(),
        "failed": {"active"},  # Allow retry
        "cancelled": set(),
    }
    if target not in allowed.get(current, set()):
        raise HTTPException(
            400,
            f"Invalid status transition: {current} → {target}. "
            f"Allowed: {allowed.get(current, set()) or 'none'}",
        )


def _calculate_progress(stages: list[MissionStageRow]) -> float:
    """Calculate mission progress as percentage based on stage completion."""
    if not stages:
        return 0.0
    weights = {1: 15, 2: 25, 3: 15, 4: 30, 5: 15}
    total_weight = sum(weights.values())
    completed_weight = sum(
        weights.get(s.stage_number, 20)
        for s in stages
        if s.status in ("completed", "skipped")
    )
    # Active stage gets partial credit (50% of its weight)
    for s in stages:
        if s.status == "active":
            completed_weight += weights.get(s.stage_number, 20) * 0.5
    return round(min(completed_weight / total_weight * 100, 100.0), 1)


def _mission_summary(mission: MissionRow) -> dict:
    """Convert a mission to a summary dict for list views."""
    d = row_to_dict(mission)
    d["stages"] = [
        {
            "stage_number": s.stage_number,
            "stage_type": s.stage_type,
            "status": s.status,
        }
        for s in (mission.stages or [])
    ]
    d.pop("metadata_json", None)
    d.pop("context", None)
    return d


def _mission_detail(mission: MissionRow, messages: list) -> dict:
    """Convert a mission to a full detail response."""
    d = row_to_dict(mission)
    d["persona"] = row_to_dict(mission.persona) if mission.persona else None
    d["stages"] = []
    for stage in (mission.stages or []):
        stage_dict = _stage_to_response(stage)
        stage_dict["artifacts"] = [
            row_to_dict(a) for a in (stage.artifacts if hasattr(stage, "artifacts") and stage.artifacts else [])
        ]
        d["stages"].append(stage_dict)
    d["messages"] = [row_to_dict(m) for m in messages]
    d.pop("metadata_json", None)
    if d.get("persona"):
        d["persona"].pop("metadata_json", None)
    return d


def _stage_to_response(stage: MissionStageRow) -> dict:
    """Convert a stage row to API response."""
    d = row_to_dict(stage)
    d["stage_label"] = STAGE_LABELS.get(stage.stage_number, f"Stage {stage.stage_number}")
    d.pop("metadata_json", None)
    return d


async def _generate_assistant_response(
    persona_name: str,
    stage_label: str,
    stage_number: int,
    user_message: str,
    mission_title: str,
    system_prompt: str = "",
    mission_context: dict | None = None,
    config: PlatformAPIConfig | None = None,
) -> str:
    """
    Generate an assistant response via the Heart LLM engine.

    Sends the persona's system_prompt, accumulated mission context, and
    the user's message to Heart's /ask endpoint. Falls back to a
    contextual template response if Heart is unavailable.
    """
    # Attempt Heart LLM call when config is available
    if config and system_prompt:
        heart_url = config.heart_engine_url
        token = _make_service_token(config, "service")

        # Build context from mission state for the LLM
        context_parts = [
            f"Persona: {persona_name}",
            f"Mission: {mission_title}",
            f"Current Stage: {stage_number} - {stage_label}",
        ]
        if mission_context:
            for k, v in mission_context.items():
                if isinstance(v, str) and len(v) < 500:
                    context_parts.append(f"{k}: {v}")

        ask_payload = {
            "tenant_id": "service",
            "question": user_message,
            "context": f"{system_prompt}\n\n" + "\n".join(context_parts),
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{heart_url}/api/v1/heart/ask",
                    json=ask_payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                if resp.status_code < 400:
                    data = resp.json()
                    answer = data.get("answer", "")
                    if answer:
                        return answer
        except Exception as exc:
            logger.warning("heart_llm.chat_fallback reason=%s", exc)

    # ── Template fallback when Heart is unavailable ────────────
    return _template_stage_response(
        persona_name=persona_name,
        stage_label=stage_label,
        stage_number=stage_number,
        user_message=user_message,
        mission_title=mission_title,
    )


def _template_stage_response(
    persona_name: str,
    stage_label: str,
    stage_number: int,
    user_message: str,
    mission_title: str,
) -> str:
    """Contextual template response used when Heart LLM is unavailable."""
    msg_lower = user_message.lower().strip()

    if stage_number == 1:
        if any(kw in msg_lower for kw in ["upload", "document", "file", "attach"]):
            return (
                f"I'll help you process that. You can upload documents through the "
                f"Capture stage panel — I support PDFs, Word docs, audio recordings, "
                f"and video files.\n\n"
                f"Once uploaded, I'll use the **Spine** engine for document ingestion, "
                f"**Shield** for PII protection, and if it's media, **Ears** or **Eyes** "
                f"for transcription/analysis.\n\n"
                f"What type of content are you working with?"
            )
        return (
            f"We're in the **{stage_label}** stage for *{mission_title}*. "
            f"This is where we gather all the domain knowledge, requirements, "
            f"and source materials.\n\n"
            f"You can:\n"
            f"- Upload requirement documents, meeting recordings, or screenshots\n"
            f"- Describe the system under test\n"
            f"- Share business rules you already know\n\n"
            f"What would you like to start with?"
        )
    elif stage_number == 2:
        return (
            f"In the **{stage_label}** stage, I'm analyzing the captured knowledge "
            f"to extract business rules, build relationship graphs, and identify "
            f"potential contradictions.\n\n"
            f"Based on your input, I can:\n"
            f"- Extract business rules using **Heart** engine\n"
            f"- Map relationships in the **Knowledge Graph**\n"
            f"- Detect contradictions with **Nerves** engine\n\n"
            f"Let me know if you'd like me to focus on a specific area."
        )
    elif stage_number == 3:
        return (
            f"We're now in the **{stage_label}** stage. Based on the understanding "
            f"we've built, I'll help design an optimal testing strategy.\n\n"
            f"I can recommend:\n"
            f"- Test coverage priorities based on risk\n"
            f"- Testing techniques (BVA, equivalence, pairwise)\n"
            f"- Areas requiring the most attention\n\n"
            f"What aspect of your testing strategy would you like to explore?"
        )
    elif stage_number == 4:
        return (
            f"Time to **{stage_label}**! Based on our strategy, I'll help create:\n\n"
            f"- Test cases via **Legs** engine\n"
            f"- Test data profiles via **Hands** engine\n"
            f"- Documentation and reports via **Mouth** engine\n\n"
            f"Shall I start generating test cases based on the extracted rules, "
            f"or would you prefer to begin with test data?"
        )
    elif stage_number == 5:
        return (
            f"Final stage — **{stage_label}**! Here we execute tests and verify "
            f"everything meets the requirements.\n\n"
            f"I can:\n"
            f"- Execute test cases and track results\n"
            f"- Check traceability against requirements\n"
            f"- Generate compliance reports\n"
            f"- Provide confidence scores\n\n"
            f"Ready to start validation?"
        )
    else:
        return (
            f"I'm here to help with *{mission_title}*. "
            f"What would you like to work on?"
        )
