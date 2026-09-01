"""Additive vision options-extractor (Phase 2c).

Populates the (empty) ``page_visits.form_snapshot_signals`` JSONB column with
the AVAILABLE OPTIONS of each choice control (dropdown / select / radio group)
the recording revealed — read directly from the frame pixels via the working
vision model.  This is the capture half of Phase 2: once options exist, the
combination engine generates grounded ``Business × Female``-style tests.

Additive by construction:
* It does NOT modify the frozen ``form_snapshot_extractor`` — it *imports* its
  frame-loading / fetch / parse helpers and reuses them read-only.
* It fills the empty ``form_snapshot_signals`` column (the path the product
  owner authorised: "new additive extractor that populates signals"); it never
  touches ``form_snapshot`` (the demonstrated selected values).

Grounding rule (unchanged): only options the model actually SEES in a frame are
recorded.  A closed dropdown the user never opened yields nothing — no guessing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import PageVisitRow

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

OPTIONS_EXTRACTOR_VERSION = "v1"
_LLM_TASK = os.environ.get("LLM_TASK_PAGE_OPTIONS", "form_snapshot_extraction")
_MAX_TOKENS = int(os.environ.get("STORYBOARD_PAGE_OPTIONS_MAX_TOKENS", "700"))


# ─── LLM response schema + tool ──────────────────────────────────────────────


class FieldOptionsItem(BaseModel):
    label: str = Field(description="The field's visible label.")
    selected: str = Field(default="", description="Currently-selected value, if any.")
    options: list[str] = Field(
        default_factory=list,
        description="ALL available options visible for this control.",
    )
    required: bool = Field(default=False)
    control_type: str = Field(
        default="", description="select | dropdown | radio | checkbox | segmented",
    )


class FieldOptionsResponse(BaseModel):
    fields: list[FieldOptionsItem] = Field(default_factory=list)


def _options_tool() -> ToolDefinition:
    return ToolDefinition(
        name="record_field_options",
        description=(
            "Record every CHOICE control (dropdown/select/radio group/segmented "
            "control) visible in the screenshots, with its full list of available "
            "options. Only include options that are actually visible."
        ),
        input_schema=FieldOptionsResponse.model_json_schema(),
    )


_SYSTEM_PROMPT = (
    "You are a meticulous UI analyst. From the screenshots, identify every form "
    "control that offers a CHOICE among multiple values — dropdowns/selects, "
    "radio-button groups, segmented controls, comboboxes. For each, record its "
    "label, the currently-selected value, and the FULL list of options that are "
    "VISIBLE in the screenshot (e.g. an opened dropdown list, or all radio "
    "buttons). Do NOT invent options that are not shown. Ignore free-text inputs, "
    "buttons, and links. You MUST respond by calling the record_field_options "
    "tool with a list of fields (empty list if no choice controls are visible)."
)


def _user_prompt(location: str) -> str:
    return (
        f"Page location: {location or '(unknown)'}.\n"
        "List the choice controls and their visible available options. "
        "Return them via the record_field_options tool."
    )


# ─── Per-visit extraction ────────────────────────────────────────────────────


async def _extract_one(
    inp,
    *,
    router: LLMRouter,
    fs_cfg,
    semaphore: asyncio.Semaphore,
    http_client: httpx.AsyncClient,
    auth_token: str,
) -> tuple[str, dict]:
    """Return (page_visit_id, signals_dict) for one visit's frames."""
    images: list[ImageContent] = []
    for path in inp.frame_paths:
        raw = await _fetch_frame_bytes(http_client, path, auth_token=auth_token)
        if raw:
            raw, media_type = _maybe_downsize(raw, fs_cfg.image_max_dimension_px)
            images.append(ImageContent(data=raw, media_type=media_type))
    if not images:
        return inp.page_visit_id, {}

    tool = _options_tool()
    async with semaphore:
        request = CompletionRequest(
            system=_SYSTEM_PROMPT,
            prompt=_user_prompt(inp.location),
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
            request_timeout_s=getattr(fs_cfg, "request_timeout_s", None),
            correlation_id=f"{inp.page_visit_id}:options",
            images=tuple(images),
            tools=(tool,),
            tool_choice=tool.name,
            enable_prompt_cache=True,
            metadata={"task": _LLM_TASK, "image_count": len(images)},
        )
        response = await router.complete(task=_LLM_TASK, request=request)

    parsed: FieldOptionsResponse | None = None
    if response.tool_calls:
        for call in response.tool_calls:
            if call.name == tool.name:
                try:
                    parsed = FieldOptionsResponse.model_validate(call.arguments)
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
            if isinstance(data, list):
                parsed = FieldOptionsResponse(
                    fields=[FieldOptionsItem.model_validate(f) for f in data],
                )
            elif isinstance(data, dict):
                parsed = FieldOptionsResponse.model_validate(data)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "test_factory.options_extractor.invalid_output visit=%s model=%s/%s",
                inp.page_visit_id, response.provider, response.model,
            )

    signals: dict[str, dict] = {}
    if parsed:
        for f in parsed.fields:
            label = (f.label or "").strip().rstrip("?:*").strip()
            opts = list(dict.fromkeys(o.strip() for o in (f.options or []) if o.strip()))
            if not label or len(opts) < 2:
                continue  # only real choices (>=2 visible options)
            signals[label[:500]] = {
                "selected": (f.selected or "").strip()[:500],
                "options": opts[:50],
                "required": bool(f.required),
                "type": (f.control_type or "").strip()[:40] or "select",
                "source": "vision",
                "extractor_version": OPTIONS_EXTRACTOR_VERSION,
            }
    return inp.page_visit_id, signals


# ─── Public entry point ──────────────────────────────────────────────────────


async def extract_field_options_for_artifact(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    router: LLMRouter,
    auth_token: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Capture available options into ``page_visits.form_snapshot_signals``.

    Idempotent (merges into the column).  Does NOT commit — caller controls the
    transaction.
    """
    started = time.monotonic()
    fs_cfg = load_config().form_snapshot_extractor

    inputs = await _collect_snapshot_inputs(
        session, artifact_id=artifact_id, tenant_id=tenant_id, config=fs_cfg,
    )
    if not inputs:
        return {"visits_scanned": 0, "fields_with_options": 0, "visits_updated": 0}

    semaphore = asyncio.Semaphore(max(1, getattr(fs_cfg, "concurrency", 2)))
    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(getattr(fs_cfg, "request_timeout_s", None) or 30.0),
            follow_redirects=True,
        )

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(
                _extract_one(
                    inp, router=router, fs_cfg=fs_cfg, semaphore=semaphore,
                    http_client=http_client, auth_token=auth_token,
                ) for inp in inputs
            )),
            timeout=getattr(fs_cfg, "total_timeout_s", 240.0),
        )
    except asyncio.TimeoutError:
        logger.warning(
            "test_factory.options_extractor.total_timeout artifact=%s", artifact_id,
        )
        results = []
    finally:
        if owns_client:
            try:
                await http_client.aclose()
            except Exception:  # pragma: no cover
                pass

    fields_total = 0
    visits_updated = 0
    for page_visit_id, signals in results:
        if not signals:
            continue
        existing = (
            await session.execute(
                update(PageVisitRow)
                .where(
                    PageVisitRow.page_visit_id == page_visit_id,
                    PageVisitRow.tenant_id == tenant_id,
                )
                .values(form_snapshot_signals=signals)
            )
        )
        if existing.rowcount:
            visits_updated += 1
            fields_total += len(signals)

    return {
        "visits_scanned": len(inputs),
        "fields_with_options": fields_total,
        "visits_updated": visits_updated,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "options_extractor_version": OPTIONS_EXTRACTOR_VERSION,
    }
