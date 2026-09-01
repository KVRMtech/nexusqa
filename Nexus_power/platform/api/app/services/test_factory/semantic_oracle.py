"""Gated, ADVISORY VLM semantic-deviation check (Phase 2 additive foundation).

The deterministic verdict/heal core is the source of truth for pass/fail — it is
FROZEN and this module must NEVER change it. The single job here is to produce an
*advisory* signal for ONE human-escalated card: looking at the recorded baseline
screenshot, the recorded expected-outcome text, and the actual screenshot, does
the actual STILL satisfy what was recorded?

HARD CONTRACT (the whole point of this module):
  * ADVISORY ONLY. It NEVER imports or calls ``classify_failure``; it NEVER reads,
    returns, or mutates a ``verdict.label``. It returns a ``semantic_match`` SIGNAL
    that the caller MAY surface, or use to escalate a deterministically-DISMISSED
    scenario to needs-review. That escalation is a SEPARATE concern handled by the
    integrator (see the wiring note), not by this file.
  * SILENT NO-OP. If ``router`` is None, OR either image is missing, OR the LLM
    call / parse fails for any reason → it returns the uncertain sentinel
    ``{'semantic_match': 'uncertain', 'deviation_summary': '', 'confidence': 0.0}``
    and NEVER raises. A degraded oracle must never block, crash, or flip a verdict.

It reuses the PROVEN forced-tool vision pattern from ``enrich_extractor`` /
``options_extractor`` (see ``enrich_extractor.py`` lines 106-160 and
``options_extractor.py`` lines 78-87): build a ``CompletionRequest`` carrying the
images as ``ImageContent``, register a single ``ToolDefinition`` whose
``input_schema`` is a small pydantic model's ``model_json_schema()``, force it via
``tool_choice=tool.name``, ``temperature=0.0``, then read
``response.tool_calls[].arguments`` → ``model_validate``.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from pydantic import BaseModel, Field

from ..llm import (
    CompletionRequest,
    FinishReason,
    ImageContent,
    ToolDefinition,
)

logger = logging.getLogger(__name__)

SEMANTIC_ORACLE_VERSION = "v1"

# Reuse the same vision-capable task lane the other test_factory extractors use,
# so this rides the configured tier/fallback chain rather than inventing a lane.
_LLM_TASK = os.environ.get("LLM_TASK_SEMANTIC_ORACLE", "form_snapshot_extraction")
_MAX_TOKENS = int(os.environ.get("TEST_FACTORY_SEMANTIC_ORACLE_MAX_TOKENS", "400"))

_VALID_MATCH = ("match", "deviation", "uncertain")

# The sentinel returned on every no-op / failure path. A frozen, copied dict so
# the caller can never mutate module state.
_UNCERTAIN: dict = {"semantic_match": "uncertain", "deviation_summary": "", "confidence": 0.0}


def _uncertain() -> dict:
    """A fresh uncertain-sentinel dict (never share a mutable instance)."""
    return dict(_UNCERTAIN)


# ─── LLM response schema + forced tool (mirrors enrich/options extractors) ────


class SemanticJudgement(BaseModel):
    """Structured output the VLM is FORCED to produce.

    ``semantic_match`` is the advisory signal; it is NEVER a verdict label.
    """

    semantic_match: Literal["match", "deviation", "uncertain"] = Field(
        description=(
            "match = the ACTUAL screenshot still satisfies the RECORDED expected "
            "outcome; deviation = it clearly does NOT; uncertain = cannot tell from "
            "the screenshots alone."
        ),
    )
    deviation_summary: str = Field(
        default="",
        description=(
            "ONE short concrete sentence describing the visible deviation, grounded "
            "ONLY in the screenshots. Empty when semantic_match is 'match'."
        ),
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="0.0-1.0 confidence in this judgement based on what is visible.",
    )


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="record_semantic_judgement",
        description=(
            "Record whether the ACTUAL screenshot still satisfies the RECORDED "
            "expected outcome. Judge ONLY against the recorded baseline; do not "
            "infer intent or guess. You MUST call this tool."
        ),
        input_schema=SemanticJudgement.model_json_schema(),
    )


_SYSTEM_PROMPT = (
    "You are a meticulous, conservative UI regression analyst. You are shown a "
    "RECORDED baseline screenshot (the known-good reference), the RECORDED expected "
    "outcome described in words, and an ACTUAL screenshot captured on a later run. "
    "Decide ONE thing via the record_semantic_judgement tool: does the ACTUAL "
    "screenshot STILL satisfy the RECORDED expected outcome?\n\n"
    "Rules:\n"
    "- Judge ONLY against the recorded baseline and the recorded expected outcome. "
    "Do NOT infer the user's intent and do NOT guess beyond what is visible.\n"
    "- 'match' = the actual still satisfies the recorded expectation.\n"
    "- 'deviation' = the actual clearly does NOT (e.g. an error replaced the "
    "expected content, the expected result is missing, the wrong page is shown).\n"
    "- 'uncertain' = you cannot tell from the screenshots alone. Prefer 'uncertain' "
    "over guessing.\n"
    "- This is advisory only; you are not deciding pass or fail."
)


def _user_prompt(baseline_expected_text: str) -> str:
    expected = (baseline_expected_text or "").strip() or "(no recorded expected text)"
    return (
        "RECORDED expected outcome (what the baseline run was confirmed to show):\n"
        f"{expected}\n\n"
        "The first image is the RECORDED baseline screenshot. The second image is "
        "the ACTUAL screenshot from the later run. Does the ACTUAL still satisfy the "
        "RECORDED expected outcome? Answer ONLY via record_semantic_judgement."
    )


def _sniff_media_type(raw: bytes) -> str:
    """Best-effort image MIME sniff; defaults to png (the recorder's format)."""
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


async def judge_semantic_match(
    baseline_expected_text: str,
    baseline_bytes: bytes | None,
    actual_bytes: bytes | None,
    router,
) -> dict:
    """Advisory VLM judgement for ONE human-escalated card.

    Returns ``{'semantic_match': 'match'|'deviation'|'uncertain',
    'deviation_summary': str, 'confidence': float}``. NEVER raises. NEVER touches a
    verdict. On any missing input or failure, returns the uncertain sentinel.

    Designed for a SINGLE call per escalated card — never a batch — to avoid the
    token-heavy vision burst that trips provider TPM limits.
    """
    # ── silent no-op guards ──
    if router is None:
        return _uncertain()
    if not baseline_bytes or not actual_bytes:
        return _uncertain()

    try:
        images = (
            ImageContent(data=baseline_bytes, media_type=_sniff_media_type(baseline_bytes)),
            ImageContent(data=actual_bytes, media_type=_sniff_media_type(actual_bytes)),
        )
        tool = _tool()
        request = CompletionRequest(
            system=_SYSTEM_PROMPT,
            prompt=_user_prompt(baseline_expected_text),
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
            correlation_id="semantic_oracle",
            images=images,
            tools=(tool,),
            tool_choice=tool.name,
            enable_prompt_cache=True,
            metadata={"task": _LLM_TASK, "image_count": len(images)},
        )
        response = await router.complete(task=_LLM_TASK, request=request)
    except Exception:  # pragma: no cover - defensive: a degraded oracle never raises
        logger.warning("test_factory.semantic_oracle.call_failed", exc_info=True)
        return _uncertain()

    # ── parse the forced tool call (advisory signal only) ──
    parsed: SemanticJudgement | None = None
    try:
        for call in getattr(response, "tool_calls", ()) or ():
            if getattr(call, "name", None) == tool.name:
                parsed = SemanticJudgement.model_validate(call.arguments)
                break
    except Exception:  # pragma: no cover
        parsed = None

    if parsed is None:
        # ERROR finish or no tool call → uncertain. Never fabricate a signal.
        if getattr(response, "finish_reason", None) == FinishReason.ERROR:
            logger.info("test_factory.semantic_oracle.llm_error")
        return _uncertain()

    match = parsed.semantic_match if parsed.semantic_match in _VALID_MATCH else "uncertain"
    summary = (parsed.deviation_summary or "").strip()[:300]
    # A match has no deviation to summarize; keep the contract clean.
    if match == "match":
        summary = ""
    try:
        confidence = float(parsed.confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "semantic_match": match,
        "deviation_summary": summary,
        "confidence": confidence,
    }
