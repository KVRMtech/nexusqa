"""Unified vision enrichment — ONE LLM call per page (Phase 4.5).

Collapses the three separate vision passes (available options, "where it sits"
anchors, "what happened after" outcomes) into a single call per page: same
frames, one prompt, one response with all three. ~3x cheaper and faster than the
separate passes, with identical grounding rules.

Reuses the proven per-pass schemas (FieldOptionsItem / AnchorItem / OutcomeItem)
and write semantics:
  * options  -> page_visits.form_snapshot_signals  (merge; >=2 visible options)
  * anchors  -> page_actions.evidence_signals['anchor']  (repeated-only; clear-stale)
  * outcomes -> page_actions.evidence_signals['after']   (sticky; never cleared empty)
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

from nexus_sdk.db.models import PageActionRow, PageVisitRow

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
from .after_extractor import _OUTCOMES, OutcomeItem
from .anchor_extractor import AnchorItem, _norm
from .options_extractor import FieldOptionsItem

logger = logging.getLogger(__name__)

ENRICH_VERSION = "v1"
_LLM_TASK = os.environ.get("LLM_TASK_PAGE_ENRICH", "form_snapshot_extraction")
_MAX_TOKENS = int(os.environ.get("STORYBOARD_PAGE_ENRICH_MAX_TOKENS", "1100"))
_INTERACTIVE = {"click", "press", "tap", "submit", "type", "select", "check", "choose"}


class EnrichResponse(BaseModel):
    fields: list[FieldOptionsItem] = Field(default_factory=list)
    anchors: list[AnchorItem] = Field(default_factory=list)
    outcomes: list[OutcomeItem] = Field(default_factory=list)


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="record_page_enrichment",
        description=(
            "Record three things from the screenshots in one call: (1) every "
            "CHOICE control and its visible options, (2) anchors ONLY for repeated "
            "controls, (3) the visible outcome after each interacted element."
        ),
        input_schema=EnrichResponse.model_json_schema(),
    )


_SYSTEM_PROMPT = (
    "You are a meticulous UI analyst. From the screenshots of one page, produce "
    "THREE things via the record_page_enrichment tool:\n\n"
    "1. fields — every CHOICE control (dropdown/select/radio/segmented) VISIBLE in "
    "the screenshots, with its label, current selection, and the FULL list of "
    "options actually shown. Ignore free-text inputs, buttons, links. Do NOT "
    "invent options.\n\n"
    "2. anchors — for the listed interacted elements, an anchor is needed ONLY to "
    "tell apart REPEATED controls (the same label on several elements, e.g. a "
    "'Select' button in each results row). Give the SHORT identifying text of that "
    "element's own row/list-item/card (a few words). If an element is UNIQUE on "
    "the page, leave its anchor EMPTY — a unique element needs no anchor, and a "
    "wrong anchor is worse than none. Never use a field's value or an unrelated "
    "nearby label; never a sentence/paragraph.\n\n"
    "3. outcomes — for each interacted element, what VISIBLY changed right after: "
    "navigation | content_appeared | error | loading | none, plus a short concrete "
    "detail (e.g. 'a results list appears', 'a validation error is shown'). Use "
    "'none' with empty detail if nothing observable changed.\n\n"
    "Use ONLY what is visible in the screenshots. You MUST call the tool."
)


def _user_prompt(location: str, labels: list[str]) -> str:
    listed = "\n".join(f"- {lbl}" for lbl in labels) or "(none)"
    return (
        f"Page location: {location or '(unknown)'}.\n"
        f"Elements the user interacted with (for anchors + outcomes):\n{listed}\n\n"
        "Return fields, anchors, and outcomes via record_page_enrichment."
    )


async def _extract_one(
    inp, labels: list[str], *, router, fs_cfg, semaphore, http_client, auth_token,
) -> dict:
    images: list[ImageContent] = []
    for path in inp.frame_paths:
        raw = await _fetch_frame_bytes(http_client, path, auth_token=auth_token)
        if raw:
            raw, media_type = _maybe_downsize(raw, fs_cfg.image_max_dimension_px)
            images.append(ImageContent(data=raw, media_type=media_type))
    if not images:
        return {"fields": {}, "anchors": {}, "outcomes": {}}

    tool = _tool()
    async with semaphore:
        request = CompletionRequest(
            system=_SYSTEM_PROMPT,
            prompt=_user_prompt(inp.location, labels),
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
            request_timeout_s=getattr(fs_cfg, "request_timeout_s", None),
            correlation_id=f"{inp.page_visit_id}:enrich",
            images=tuple(images),
            tools=(tool,),
            tool_choice=tool.name,
            enable_prompt_cache=True,
            metadata={"task": _LLM_TASK, "image_count": len(images)},
        )
        response = await router.complete(task=_LLM_TASK, request=request)

    parsed: EnrichResponse | None = None
    if response.tool_calls:
        for call in response.tool_calls:
            if call.name == tool.name:
                try:
                    parsed = EnrichResponse.model_validate(call.arguments)
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
                parsed = EnrichResponse.model_validate(data)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "test_factory.enrich.invalid_output visit=%s model=%s/%s",
                inp.page_visit_id, response.provider, response.model,
            )
    if parsed is None:
        return {"fields": {}, "anchors": {}, "outcomes": {}}

    # ── normalize each sub-result (same rules as the individual extractors) ──
    fields: dict[str, dict] = {}
    for f in parsed.fields:
        label = (f.label or "").strip().rstrip("?:*").strip()
        opts = list(dict.fromkeys(o.strip() for o in (f.options or []) if o.strip()))
        if not label or len(opts) < 2:
            continue
        fields[label[:500]] = {
            "selected": (f.selected or "").strip()[:500],
            "options": opts[:50],
            "required": bool(f.required),
            "type": (f.control_type or "").strip()[:40] or "select",
            "source": "vision",
            "extractor_version": ENRICH_VERSION,
        }

    anchors: dict[str, dict] = {}
    for a in parsed.anchors:
        lbl = (a.element_label or "").strip()
        anchor = (a.anchor_text or "").strip()
        if not lbl or not anchor or len(anchor) > 60 or len(anchor.split()) > 8:
            continue
        anchors[_norm(lbl)] = {
            "label": anchor,
            "kind": (a.anchor_kind or "").strip()[:40] or "row",
            "source": "vision",
            "extractor_version": ENRICH_VERSION,
        }

    outcomes: dict[str, dict] = {}
    for o in parsed.outcomes:
        lbl = (o.element_label or "").strip()
        detail = (o.detail or "").strip()
        outcome = (o.outcome or "none").strip().lower()
        if outcome not in _OUTCOMES:
            outcome = "none"
        if not lbl or (outcome == "none" and not detail):
            continue
        outcomes[_norm(lbl)] = {
            "outcome": outcome,
            "detail": detail[:300],
            "source": "vision",
            "extractor_version": ENRICH_VERSION,
        }

    return {"fields": fields, "anchors": anchors, "outcomes": outcomes}


async def enrich_artifact(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    router: LLMRouter,
    auth_token: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """One vision call per page → options + anchors + outcomes. Does NOT commit."""
    started = time.monotonic()
    fs_cfg = load_config().form_snapshot_extractor

    inputs = await _collect_snapshot_inputs(
        session, artifact_id=artifact_id, tenant_id=tenant_id, config=fs_cfg,
    )
    if not inputs:
        return {"visits_scanned": 0, "fields_with_options": 0, "actions_anchored": 0, "actions_with_outcome": 0}

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
        logger.warning("test_factory.enrich.total_timeout artifact=%s", artifact_id)
        results = []
    finally:
        if owns_client:
            try:
                await http_client.aclose()
            except Exception:  # pragma: no cover
                pass

    by_visit = dict(results)

    # ── options → page_visits.form_snapshot_signals ──
    fields_total = 0
    for visit_id, res in by_visit.items():
        signals = res.get("fields") or {}
        if not signals:
            continue
        r = await session.execute(
            update(PageVisitRow)
            .where(PageVisitRow.page_visit_id == visit_id, PageVisitRow.tenant_id == tenant_id)
            .values(form_snapshot_signals=signals)
        )
        if r.rowcount:
            fields_total += len(signals)

    # ── anchors (clear-stale) + outcomes (sticky) → page_actions.evidence_signals ──
    anchored = 0
    outcomed = 0
    for visit_id, rows in actions_by_visit.items():
        res = by_visit.get(visit_id) or {}
        anchors = res.get("anchors") or {}
        outcomes = res.get("outcomes") or {}
        for row in rows:
            merged = dict(row.evidence_signals or {})
            changed = False

            anchor = anchors.get(_norm(row.target_label))
            if anchor:
                if merged.get("anchor") != anchor:
                    merged["anchor"] = anchor
                    anchored += 1
                    changed = True
            elif "anchor" in merged:
                merged.pop("anchor", None)  # clear-stale: wrong anchors are harmful
                changed = True

            after = outcomes.get(_norm(row.target_label))
            if after and merged.get("after") != after:
                merged["after"] = after  # sticky: only update on a new non-empty
                outcomed += 1
                changed = True

            if changed:
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
        "fields_with_options": fields_total,
        "actions_anchored": anchored,
        "actions_with_outcome": outcomed,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "enrich_version": ENRICH_VERSION,
    }
