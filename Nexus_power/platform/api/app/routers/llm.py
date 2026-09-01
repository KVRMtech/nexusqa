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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["LLM"])


# ── Token usage at the HTTP boundary (M0.6 / T-OB-03) ───────────────────────
# The providers already normalise their disagreeing usage shapes — OpenAI's
# ``usage.prompt_tokens``, Anthropic's ``usage.input_tokens``, Ollama's
# ``prompt_eval_count`` — into ``CompletionResponse.prompt_tokens`` /
# ``completion_tokens``.  Everything downstream of this router was then blind to
# spend for one reason only: this boundary computed the usage and dropped it on
# the floor.  Both endpoints now return it, and both do so on the ERROR path as
# well, because a provider that billed a request before failing must not vanish
# from the spend account.

#: Response headers carrying usage. Headers (not just the JSON body) because a
#: 502 has no body we can extend without breaking the existing ``detail``
#: contract every caller already parses as a string — this way usage survives
#: BOTH outcomes over the same wire, and no caller has to change to keep working.
HDR_PROMPT_TOKENS = "X-LLM-Prompt-Tokens"
HDR_COMPLETION_TOKENS = "X-LLM-Completion-Tokens"
HDR_TOTAL_TOKENS = "X-LLM-Total-Tokens"
HDR_PROVIDER = "X-LLM-Provider"
HDR_MODEL = "X-LLM-Model"


def _usage_payload(resp) -> dict:
    """Extract provider-REPORTED token usage from a ``CompletionResponse``.

    ``None`` is preserved as ``None`` and never coerced to ``0``: "the provider
    did not report usage" and "the provider reported zero" are different facts,
    and flattening them would let a silent reporting regression read as a free
    call.  ``total_tokens`` is therefore ``None`` unless at least one half was
    reported, and is the SUM of prompt+completion — cache tokens are a separate
    provider accounting dimension and are never folded in.
    """
    prompt = getattr(resp, "prompt_tokens", None)
    completion = getattr(resp, "completion_tokens", None)
    total = None
    if prompt is not None or completion is not None:
        total = int(prompt or 0) + int(completion or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_read_tokens": getattr(resp, "cache_read_tokens", None),
        "cache_creation_tokens": getattr(resp, "cache_creation_tokens", None),
        "provider": getattr(resp, "provider", "") or "",
        "model": getattr(resp, "model", "") or "",
        # The router's own attempt accounting — every retry is billed, so the
        # caller needs the attempt count to reconcile spend against calls.
        "retries": int(getattr(resp, "retries", 0) or 0),
        "latency_ms": int(getattr(resp, "latency_ms", 0) or 0),
        "fell_back": bool(getattr(resp, "fell_back", False)),
    }


def _usage_headers(usage: dict) -> dict:
    """Render usage as response headers (only the fields actually reported)."""
    headers = {}
    for name, key in (
        (HDR_PROMPT_TOKENS, "prompt_tokens"),
        (HDR_COMPLETION_TOKENS, "completion_tokens"),
        (HDR_TOTAL_TOKENS, "total_tokens"),
    ):
        if usage.get(key) is not None:
            headers[name] = str(usage[key])
    if usage.get("provider"):
        headers[HDR_PROVIDER] = str(usage["provider"])[:64]
    if usage.get("model"):
        headers[HDR_MODEL] = str(usage["model"])[:64]
    return headers


class CompleteIn(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=200_000)
    system: str = Field(default="", max_length=20_000)
    max_tokens: int = Field(default=2400, ge=1, le=8000)
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    task: str = Field(default="brief_compile", max_length=64)


@router.post("/api/v1/llm/complete")
async def complete(
    body: CompleteIn, request: Request, response: Response,
    user: dict = Depends(get_current_user),
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
        # No response object exists here, so there is genuinely no usage to
        # retain — the call never reached a provider that could report one.
        raise HTTPException(status_code=502, detail="LLM completion failed")

    usage = _usage_payload(resp)
    logger.info(
        "llm.usage",
        extra={"task": body.task, "endpoint": "complete", **{
            k: v for k, v in usage.items() if k != "fell_back"}},
    )
    if getattr(resp, "finish_reason", None) == FinishReason.ERROR:
        # A provider that reported usage and THEN failed still billed us. Carry
        # the usage out on headers so the 502 is accounted for rather than lost.
        raise HTTPException(
            status_code=502,
            detail=str(getattr(resp, "error_detail", "") or "LLM error")[:300],
            headers=_usage_headers(usage),
        )
    fr = getattr(resp, "finish_reason", "")
    for header, value in _usage_headers(usage).items():
        response.headers[header] = value
    return {
        "text": getattr(resp, "text", "") or "",
        "finish_reason": str(getattr(fr, "value", fr)),
        "provider": getattr(resp, "provider", ""),
        "model": getattr(resp, "model", ""),
        # T-OB-03: the token usage this boundary used to compute and discard.
        "usage": usage,
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
    body: VisionCompleteIn, request: Request, response: Response,
    user: dict = Depends(get_current_user),
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

    usage = _usage_payload(resp)
    logger.info(
        "llm.usage",
        extra={"task": body.task, "endpoint": "vision", **{
            k: v for k, v in usage.items() if k != "fell_back"}},
    )
    if getattr(resp, "finish_reason", None) == FinishReason.ERROR:
        raise HTTPException(
            status_code=502,
            detail=str(getattr(resp, "error_detail", "") or "LLM error")[:300],
            headers=_usage_headers(usage),
        )
    fr = getattr(resp, "finish_reason", "")
    for header, value in _usage_headers(usage).items():
        response.headers[header] = value
    return {
        "text": getattr(resp, "text", "") or "",
        "finish_reason": str(getattr(fr, "value", fr)),
        "provider": getattr(resp, "provider", ""),
        "model": getattr(resp, "model", ""),
        "usage": usage,
    }
