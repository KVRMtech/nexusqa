"""Additive vision anchor-extractor (Phase 3 — "where it sits").

For each interactive action the recording captured, this reads the frame pixels
and records the nearest STABLE visible landmark that locates the target element —
the table row it sits in, the section heading above it, the card it belongs to.
That anchor is what disambiguates repeated controls (five identical "Select"
buttons) without ever needing the DOM: a human uses the same visual context.

Additive by construction (mirrors the options-extractor):
* Reuses the frozen form_snapshot_extractor's frame-loading helpers read-only.
* Writes ONLY into the existing ``page_actions.evidence_signals`` JSONB column
  under the ``anchor`` key — no schema change, no touch to demonstrated values.

Grounding rule: only landmarks actually VISIBLE in a frame are recorded. If an
element has no distinguishing context, no anchor is written — never guessed.
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

ANCHOR_EXTRACTOR_VERSION = "v1"
_LLM_TASK = os.environ.get("LLM_TASK_PAGE_ANCHORS", "form_snapshot_extraction")
_MAX_TOKENS = int(os.environ.get("STORYBOARD_PAGE_ANCHORS_MAX_TOKENS", "700"))
_INTERACTIVE = {"click", "press", "tap", "type", "select", "check", "choose"}


# ─── LLM response schema + tool ──────────────────────────────────────────────


class AnchorItem(BaseModel):
    element_label: str = Field(description="The visible label/text of the element the user interacted with.")
    anchor_text: str = Field(
        default="",
        description="ONLY set this when the SAME label appears on MULTIPLE elements "
        "on the page (a control repeated across table rows / list items / cards). "
        "Then give the SHORT identifying text of that element's own row/card/section "
        "(a few words — e.g. a row's key value or a section title). If the element is "
        "UNIQUE on the page, leave this EMPTY. Never use a field's value or an "
        "unrelated nearby label, and never a full sentence or paragraph.",
    )
    anchor_kind: str = Field(
        default="", description="row | list_item | card | column | group",
    )


class AnchorResponse(BaseModel):
    anchors: list[AnchorItem] = Field(default_factory=list)


def _anchor_tool() -> ToolDefinition:
    return ToolDefinition(
        name="record_element_anchors",
        description=(
            "For each listed element, record the nearest STABLE visible landmark "
            "that uniquely locates it on the page (the row, section, card, or "
            "heading it sits within). Only use context actually visible in the "
            "screenshots; leave the anchor empty if there is none."
        ),
        input_schema=AnchorResponse.model_json_schema(),
    )


_SYSTEM_PROMPT = (
    "You are a meticulous UI analyst. You are given screenshots of a page and a "
    "list of elements the user interacted with (by their visible label). An anchor "
    "is ONLY needed to tell apart REPEATED controls — the same label appearing on "
    "several elements, e.g. a 'Select' button in every row of a results table, or "
    "an 'Edit' link in each list item. For such an element, give the SHORT "
    "identifying text of its OWN row / list item / card (a few words).\n"
    "CRITICAL RULES:\n"
    "- If an element is UNIQUE on the page (only one element has that label, like a "
    "single 'Find flights' or 'Accept cookies' button), you MUST leave its anchor "
    "EMPTY. A unique element needs no anchor, and a wrong anchor is worse than none.\n"
    "- NEVER use a form field's value (e.g. a selected 'Economy') or an unrelated "
    "nearby label as an anchor.\n"
    "- The anchor must be SHORT (a few words) — never a full sentence or paragraph.\n"
    "You MUST respond by calling the record_element_anchors tool."
)


def _user_prompt(location: str, labels: list[str]) -> str:
    listed = "\n".join(f"- {lbl}" for lbl in labels)
    return (
        f"Page location: {location or '(unknown)'}.\n"
        f"Elements the user interacted with:\n{listed}\n\n"
        "For each, return its nearest stable locating landmark via the "
        "record_element_anchors tool."
    )


# ─── Per-visit extraction ────────────────────────────────────────────────────


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
    """Return {normalized_label: {"label","kind"}} for one visit's frames."""
    images: list[ImageContent] = []
    for path in inp.frame_paths:
        raw = await _fetch_frame_bytes(http_client, path, auth_token=auth_token)
        if raw:
            raw, media_type = _maybe_downsize(raw, fs_cfg.image_max_dimension_px)
            images.append(ImageContent(data=raw, media_type=media_type))
    if not images or not labels:
        return {}

    tool = _anchor_tool()
    async with semaphore:
        request = CompletionRequest(
            system=_SYSTEM_PROMPT,
            prompt=_user_prompt(inp.location, labels),
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
            request_timeout_s=getattr(fs_cfg, "request_timeout_s", None),
            correlation_id=f"{inp.page_visit_id}:anchors",
            images=tuple(images),
            tools=(tool,),
            tool_choice=tool.name,
            enable_prompt_cache=True,
            metadata={"task": _LLM_TASK, "image_count": len(images)},
        )
        response = await router.complete(task=_LLM_TASK, request=request)

    parsed: AnchorResponse | None = None
    if response.tool_calls:
        for call in response.tool_calls:
            if call.name == tool.name:
                try:
                    parsed = AnchorResponse.model_validate(call.arguments)
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
                parsed = AnchorResponse.model_validate(data)
            elif isinstance(data, list):
                parsed = AnchorResponse(anchors=[AnchorItem.model_validate(a) for a in data])
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "test_factory.anchor_extractor.invalid_output visit=%s model=%s/%s",
                inp.page_visit_id, response.provider, response.model,
            )

    out: dict[str, dict] = {}
    if parsed:
        for a in parsed.anchors:
            lbl = (a.element_label or "").strip()
            anchor = (a.anchor_text or "").strip()
            # A real locating landmark is short. Drop empties and anything
            # sentence/paragraph-length (e.g. a whole cookie-banner blurb) — those
            # are not usable locators and only add noise.
            if not lbl or not anchor or len(anchor) > 60 or len(anchor.split()) > 8:
                continue
            out[_norm(lbl)] = {
                "label": anchor,
                "kind": (a.anchor_kind or "").strip()[:40] or "row",
                "source": "vision",
                "extractor_version": ANCHOR_EXTRACTOR_VERSION,
            }
    return out


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


# ─── Public entry point ──────────────────────────────────────────────────────


async def extract_anchors_for_artifact(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    router: LLMRouter,
    auth_token: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Capture per-action anchors into ``page_actions.evidence_signals['anchor']``.

    Idempotent (overwrites the ``anchor`` key).  Does NOT commit — caller controls
    the transaction.
    """
    started = time.monotonic()
    fs_cfg = load_config().form_snapshot_extractor

    inputs = await _collect_snapshot_inputs(
        session, artifact_id=artifact_id, tenant_id=tenant_id, config=fs_cfg,
    )
    if not inputs:
        return {"visits_scanned": 0, "actions_anchored": 0}

    # Interactive actions for these visits, grouped by visit, keyed by label.
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
        anchors = await _extract_one(
            inp, labels, router=router, fs_cfg=fs_cfg, semaphore=semaphore,
            http_client=http_client, auth_token=auth_token,
        )
        return inp.page_visit_id, anchors

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(_run(inp) for inp in inputs)),
            timeout=getattr(fs_cfg, "total_timeout_s", 240.0),
        )
    except asyncio.TimeoutError:
        logger.warning("test_factory.anchor_extractor.total_timeout artifact=%s", artifact_id)
        results = []
    finally:
        if owns_client:
            try:
                await http_client.aclose()
            except Exception:  # pragma: no cover
                pass

    # All scanned visits (anchors dict may be empty → clears stale anchors).
    anchors_by_visit = dict(results)

    actions_anchored = 0
    for visit_id, rows in actions_by_visit.items():
        anchors = anchors_by_visit.get(visit_id) or {}
        for row in rows:
            anchor = anchors.get(_norm(row.target_label))
            merged = dict(row.evidence_signals or {})
            if anchor:
                if merged.get("anchor") == anchor:
                    continue  # unchanged
                merged["anchor"] = anchor
                actions_anchored += 1
            elif "anchor" in merged:
                merged.pop("anchor", None)  # stricter pass: clear a stale anchor
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
        "actions_anchored": actions_anchored,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "anchor_extractor_version": ANCHOR_EXTRACTOR_VERSION,
    }
