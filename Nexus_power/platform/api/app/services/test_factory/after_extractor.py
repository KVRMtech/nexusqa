"""Additive vision outcome-extractor (Phase 4 — "what happened after").

For each interactive action, records what VISIBLY changed right after it — a
results list / panel appeared, a validation or error message showed, a loading
indicator ran, or the page navigated.  That is the real-world source for a
test's wait condition and its assertion: instead of guessing "the click
probably worked", the step asserts the outcome the recording actually shows.

Additive (mirrors the anchor / options extractors):
* Reuses the frozen frame-loading helpers read-only.
* Writes ONLY into the existing ``page_actions.evidence_signals['after']`` JSONB
  — no schema change.

Grounding rule: only outcomes actually VISIBLE in a frame are recorded.  If
nothing observable changed, no outcome is written — never invented.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import PageActionRow

from ..llm import (
    CompletionRequest,
    FinishReason,
    ImageContent,
    LLMRouter,
    ToolDefinition,
)
from ..storyboard import load_config
from ..storyboard.form_snapshot_extractor import (
    _collect_snapshot_inputs,
    _extract_python_tool_args,
    _fetch_frame_bytes,
    _maybe_downsize,
    _strip_code_fence,
)

logger = logging.getLogger(__name__)

AFTER_EXTRACTOR_VERSION = "v1"
_LLM_TASK = os.environ.get("LLM_TASK_PAGE_OUTCOMES", "form_snapshot_extraction")
_MAX_TOKENS = int(os.environ.get("STORYBOARD_PAGE_OUTCOMES_MAX_TOKENS", "700"))
_INTERACTIVE = {"click", "press", "tap", "submit", "select", "check", "choose"}
_OUTCOMES = {"navigation", "content_appeared", "error", "loading", "none"}


class OutcomeItem(BaseModel):
    element_label: str = Field(description="The visible label of the element the user interacted with.")
    outcome: str = Field(
        default="none",
        description="What visibly happened after: navigation | content_appeared | error | loading | none.",
    )
    detail: str = Field(
        default="",
        description="A short, concrete description of the visible result "
        "(e.g. 'a results list appears', 'a validation error is shown next to the "
        "field', 'a confirmation dialog opens'). Empty if nothing observable changed.",
    )


class OutcomeResponse(BaseModel):
    outcomes: list[OutcomeItem] = Field(default_factory=list)


def _outcome_tool() -> ToolDefinition:
    return ToolDefinition(
        name="record_action_outcomes",
        description=(
            "For each listed element the user interacted with, record what "
            "VISIBLY changed on the page immediately after — a list/panel "
            "appearing, a validation/error message, a loading indicator, or a "
            "navigation. Use only what is visible in the screenshots."
        ),
        input_schema=OutcomeResponse.model_json_schema(),
    )


_SYSTEM_PROMPT = (
    "You are a meticulous UI analyst. You are given ordered screenshots of a page "
    "and a list of elements the user interacted with. For each element, describe "
    "what VISIBLY happened right after the interaction: did a results list or "
    "panel appear, did a validation/error message show, did a loading indicator "
    "run, or did the page navigate? Give a short concrete 'detail' of the visible "
    "result. Use ONLY what is visible across the screenshots; if nothing "
    "observable changed, set outcome 'none' and leave detail empty. You MUST "
    "respond by calling the record_action_outcomes tool."
)


def _user_prompt(location: str, labels: list[str]) -> str:
    listed = "\n".join(f"- {lbl}" for lbl in labels)
    return (
        f"Page location: {location or '(unknown)'}.\n"
        f"Elements the user interacted with:\n{listed}\n\n"
        "For each, return the visible outcome via the record_action_outcomes tool."
    )


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


async def _extract_one(
    inp,
    labels: list[str],
    *,
    router: LLMRouter,
    fs_cfg,
    semaphore: asyncio.Semaphore,
    http_client: httpx.AsyncClient,
    auth_token: str,
) -> dict[str, dict]:
    images: list[ImageContent] = []
    for path in inp.frame_paths:
        raw = await _fetch_frame_bytes(http_client, path, auth_token=auth_token)
        if raw:
            raw, media_type = _maybe_downsize(raw, fs_cfg.image_max_dimension_px)
            images.append(ImageContent(data=raw, media_type=media_type))
    if not images or not labels:
        return {}

    tool = _outcome_tool()
    async with semaphore:
        request = CompletionRequest(
            system=_SYSTEM_PROMPT,
            prompt=_user_prompt(inp.location, labels),
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
            request_timeout_s=getattr(fs_cfg, "request_timeout_s", None),
            correlation_id=f"{inp.page_visit_id}:outcomes",
            images=tuple(images),
            tools=(tool,),
            tool_choice=tool.name,
            enable_prompt_cache=True,
            metadata={"task": _LLM_TASK, "image_count": len(images)},
        )
        response = await router.complete(task=_LLM_TASK, request=request)

    parsed: OutcomeResponse | None = None
    if response.tool_calls:
        for call in response.tool_calls:
            if call.name == tool.name:
                try:
                    parsed = OutcomeResponse.model_validate(call.arguments)
                    break
                except ValueError:
                    pass
    if parsed is None and response.text and response.finish_reason != FinishReason.ERROR:
        try:
            payload = _strip_code_fence(response.text)
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                inner = _extract_python_tool_args(response.text)
                data = json.loads(inner) if inner is not None else None
            if isinstance(data, dict):
                parsed = OutcomeResponse.model_validate(data)
            elif isinstance(data, list):
                parsed = OutcomeResponse(outcomes=[OutcomeItem.model_validate(o) for o in data])
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "test_factory.after_extractor.invalid_output visit=%s model=%s/%s",
                inp.page_visit_id, response.provider, response.model,
            )

    out: dict[str, dict] = {}
    if parsed:
        for o in parsed.outcomes:
            lbl = (o.element_label or "").strip()
            detail = (o.detail or "").strip()
            outcome = (o.outcome or "none").strip().lower()
            if outcome not in _OUTCOMES:
                outcome = "none"
            if not lbl or (outcome == "none" and not detail):
                continue
            out[_norm(lbl)] = {
                "outcome": outcome,
                "detail": detail[:300],
                "source": "vision",
                "extractor_version": AFTER_EXTRACTOR_VERSION,
            }
    return out


async def extract_outcomes_for_artifact(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    router: LLMRouter,
    auth_token: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Capture per-action outcomes into ``page_actions.evidence_signals['after']``.

    Idempotent (overwrites the ``after`` key).  Does NOT commit.
    """
    started = time.monotonic()
    fs_cfg = load_config().form_snapshot_extractor

    inputs = await _collect_snapshot_inputs(
        session, artifact_id=artifact_id, tenant_id=tenant_id, config=fs_cfg,
    )
    if not inputs:
        return {"visits_scanned": 0, "actions_with_outcome": 0}

    visit_ids = [inp.page_visit_id for inp in inputs]
    action_rows = (
        await session.execute(
            select(PageActionRow).where(
                PageActionRow.page_visit_id.in_(visit_ids),
                PageActionRow.tenant_id == tenant_id,
            )
        )
    ).scalars().all()
    actions_by_visit: dict[str, list[PageActionRow]] = {}
    for a in action_rows:
        if (a.verb or "").strip().lower() in _INTERACTIVE and (a.target_label or "").strip():
            actions_by_visit.setdefault(a.page_visit_id, []).append(a)

    semaphore = asyncio.Semaphore(max(1, getattr(fs_cfg, "concurrency", 2)))
    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(getattr(fs_cfg, "request_timeout_s", None) or 30.0),
            follow_redirects=True,
        )

    async def _run(inp):
        rows = actions_by_visit.get(inp.page_visit_id, [])
        labels = list(dict.fromkeys(r.target_label.strip() for r in rows))
        if not labels:
            return inp.page_visit_id, {}
        return inp.page_visit_id, await _extract_one(
            inp, labels, router=router, fs_cfg=fs_cfg, semaphore=semaphore,
            http_client=http_client, auth_token=auth_token,
        )

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(_run(inp) for inp in inputs)),
            timeout=getattr(fs_cfg, "total_timeout_s", 240.0),
        )
    except asyncio.TimeoutError:
        logger.warning("test_factory.after_extractor.total_timeout artifact=%s", artifact_id)
        results = []
    finally:
        if owns_client:
            try:
                await http_client.aclose()
            except Exception:  # pragma: no cover
                pass

    outcomes_by_visit = dict(results)  # all scanned visits (clears stale)

    actions_with_outcome = 0
    for visit_id, rows in actions_by_visit.items():
        outcomes = outcomes_by_visit.get(visit_id) or {}
        for row in rows:
            after = outcomes.get(_norm(row.target_label))
            merged = dict(row.evidence_signals or {})
            if after:
                if merged.get("after") == after:
                    continue  # unchanged
                merged["after"] = after
                actions_with_outcome += 1
            elif "after" in merged:
                merged.pop("after", None)  # clear stale outcome
            else:
                continue
            await session.execute(
                update(PageActionRow)
                .where(
                    PageActionRow.page_action_id == row.page_action_id,
                    PageActionRow.tenant_id == tenant_id,
                )
                .values(evidence_signals=merged)
            )

    return {
        "visits_scanned": len(inputs),
        "actions_with_outcome": actions_with_outcome,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "after_extractor_version": AFTER_EXTRACTOR_VERSION,
    }
