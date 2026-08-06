"""Thin LLM completion endpoint (ANSWERS P2 support) + vision endpoint (R5).

qe-central owns the answer_key authoring flow but has NO LLM client, so it calls
this endpoint to run the model when compiling a client's plain-English brief. The
GROUNDING + answer_key assembly stay in qe-central (brief_compiler) — this endpoint
ONLY runs the model and returns the raw text. It touches no factory generation
logic; it merely exposes the already-wired LLMRouter.

The ``/vision`` endpoint adds multimodal (screenshot + text) support for the R5
Vision Medic — same resilience contract, same router, image attached as base64.
"""
from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["LLM"])


class CompleteIn(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=200_000)
    system: str = Field(default="", max_length=20_000)
    max_tokens: int = Field(default=2400, ge=1, le=8000)
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    task: str = Field(default="brief_compile", max_length=64)


@router.post("/api/v1/llm/complete")
async def complete(
    body: CompleteIn, request: Request, user: dict = Depends(get_current_user),
) -> dict:
    """Run one LLM completion via the platform router. 503 when no LLM is
    configured; 502 on a provider error (the caller then degrades honestly)."""
    composer = getattr(request.app.state, "storyboard_composer", None)
    llm_router = getattr(composer, "_llm_router", None) if composer else None
    if llm_router is None:
        raise HTTPException(status_code=503, detail="LLM router not configured")

    from app.services.llm import CompletionRequest, FinishReason

    req = CompletionRequest(
        system=body.system, prompt=body.prompt, max_tokens=body.max_tokens,
        temperature=body.temperature, metadata={"task": body.task},
    )
    try:
        resp = await llm_router.complete(task=body.task, request=req)
    except Exception as exc:  # never leak a stack; the caller falls back to manual
        logger.warning("llm.complete_failed", extra={"error": str(exc)[:300]})
        raise HTTPException(status_code=502, detail="LLM completion failed")
    if getattr(resp, "finish_reason", None) == FinishReason.ERROR:
        raise HTTPException(status_code=502, detail=str(getattr(resp, "error_detail", "") or "LLM error")[:300])
    fr = getattr(resp, "finish_reason", "")
    return {
        "text": getattr(resp, "text", "") or "",
        "finish_reason": str(getattr(fr, "value", fr)),
        "provider": getattr(resp, "provider", ""),
        "model": getattr(resp, "model", ""),
    }


class VisionCompleteIn(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=200_000)
    system: str = Field(default="", max_length=20_000)
    image: str = Field(..., min_length=1, max_length=20_000_000)
    max_tokens: int = Field(default=800, ge=1, le=4000)
    temperature: float = Field(default=0.1, ge=0.0, le=1.0)
    task: str = Field(default="vision_medic", max_length=64)


@router.post("/api/v1/llm/vision")
async def vision_complete(
    body: VisionCompleteIn, request: Request, user: dict = Depends(get_current_user),
) -> dict:
    """Run one multimodal LLM completion (text + screenshot). Same resilience
    contract as ``/complete``: 503 when unconfigured, 502 on provider error."""
    composer = getattr(request.app.state, "storyboard_composer", None)
    llm_router = getattr(composer, "_llm_router", None) if composer else None
    if llm_router is None:
        raise HTTPException(status_code=503, detail="LLM router not configured")

    from app.services.llm import CompletionRequest, FinishReason
    from app.services.llm.types import ImageContent

    try:
        image_bytes = base64.b64decode(body.image)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid base64 image")

    req = CompletionRequest(
        system=body.system, prompt=body.prompt, max_tokens=body.max_tokens,
        temperature=body.temperature,
        images=(ImageContent(data=image_bytes, media_type="image/png"),),
        metadata={"task": body.task},
    )
    try:
        resp = await llm_router.complete(task=body.task, request=req)
    except Exception as exc:
        logger.warning("llm.vision_failed", extra={"error": str(exc)[:300]})
        raise HTTPException(status_code=502, detail="LLM vision completion failed")
    if getattr(resp, "finish_reason", None) == FinishReason.ERROR:
        raise HTTPException(status_code=502, detail=str(getattr(resp, "error_detail", "") or "LLM error")[:300])
    fr = getattr(resp, "finish_reason", "")
    return {
        "text": getattr(resp, "text", "") or "",
        "finish_reason": str(getattr(fr, "value", fr)),
        "provider": getattr(resp, "provider", ""),
        "model": getattr(resp, "model", ""),
    }
