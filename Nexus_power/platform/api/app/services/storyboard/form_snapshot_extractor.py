"""Form-snapshot extractor — captures every form field's final value per page.

Per-page-visit extractor that runs ONE multimodal LLM call on the
LAST representative frame of each page visit and asks the LLM to
enumerate every form field visible on the page along with the user's
final value.  The output is flattened into a label→value dict and
persisted to the JSONB ``page_visits.form_snapshot`` column.

Why this is a separate service (not folded into page_action_extractor)
======================================================================
The two extractors answer different QA questions:

* ``page_action_extractor`` — "WHAT did the user DO on this page?"
  (a chronological list of interactions, useful for replaying the
  flow).
* ``form_snapshot_extractor`` — "WHAT did the user LEAVE the page in?"
  (a static snapshot of field values, useful for state-based test
  assertions independent of how the user got there).

For a Personal Information form, the action extractor returns
``[type Age=23, select Gender=Male, select State=TX, ...]`` and the
snapshot extractor returns ``{Age: 23, Gender: Male, State: TX, ...}``.
Both are needed — actions tell exporters how to fill the form,
snapshots tell exporters what to assert at the end.

Architecture
============
1. Iterate page_visits for the artifact.
2. Skip visits that the heuristic flags as having no form (fewer than
   N label-shaped tokens in OCR text).
3. For each remaining visit, sample the LAST 1-2 frames (the page's
   final state).
4. Multimodal LLM call returns a ``FormSnapshotLLMResponse`` Pydantic
   model.
5. Flatten to ``{label: value}``; persist to JSONB.

Production-safety considerations
================================
* **Idempotent** — same visit + same version yield the same snapshot.
* **Tenant-scoped** — every query filters on tenant_id; RLS enforces
  the same.
* **Cost-controlled** — heuristic skip on visits with no form; only
  1-2 frames per call (vs 4-8 for action extractor).
* **Generic** — works on any UI domain; no hardcoded label lists.
* **Lossless** — when the LLM returns a non-empty snapshot, every
  field is persisted with its declared kind for exporter dispatch.

Consumed by:
  * ``composer`` — runs after ``page_visit_extractor``.
  * ``routers/artifacts.py`` — surfaces snapshots in /visual-flow
    response as part of the page_visits payload.
  * Test exporters — emit per-page assertion blocks.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import quote

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    PageVisitRow,
    VisualFrameRow,
)

from ..llm import (
    CompletionRequest,
    FinishReason,
    ImageContent,
    LLMRouter,
    ToolDefinition,
)
from .config import FormSnapshotExtractorConfig
from .page_schemas import (
    FormField,
    FormSnapshot,
    FormSnapshotLLMResponse,
)


try:  # pragma: no cover - optional dependency
    from PIL import Image as _PILImage  # type: ignore[import-untyped]

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False


_EYES_BASE_URL = os.environ.get(
    "STORYBOARD_EYES_BASE_URL", "http://nexus-eyes:8003",
).rstrip("/")


def _pii_egress_blocked() -> bool:
    """True when this deployment is configured as a PII vertical and must NOT
    send raw form-snapshot frames (which carry typed SSN/DOB) to a cloud LLM.

    Conservative + env-gated: defaults OFF, so existing tenants are unchanged.
    Set ``NEXUS_PII_VERTICAL`` (any non-empty value other than 0/false/no) to
    refuse cloud vision for form snapshots. This is a hard egress guard, not a
    redaction — the snapshot is returned EMPTY and visibly tagged so nothing is
    green-washed as 'extracted' when extraction was deliberately skipped.
    """
    val = (os.environ.get("NEXUS_PII_VERTICAL", "") or "").strip().lower()
    return val not in ("", "0", "false", "no", "off")


logger = logging.getLogger(__name__)


# Token pattern used by the "is there a form here" heuristic.  Counts
# label-shaped strings: words followed by ':' or '?' or '*' — the
# typographic markers most forms use to indicate prompts.
_LABEL_TOKEN_PATTERN = re.compile(
    r"\b[A-Za-z][A-Za-z0-9 ]{2,40}\s*(?:[:?*]|is required)",
    re.IGNORECASE,
)


# ── Per-visit inputs ─────────────────────────────────────────────────────────


@dataclass
class _SnapshotInputs:
    """Everything the LLM call needs for one page visit's snapshot."""

    page_visit_id: str
    sequence_index: int
    location: str
    canonical_host: str
    url_path: str

    # Frame paths to send to the LLM — typically the LAST 1-2 frames
    # of the visit so the model sees the page in its final state.
    frame_paths: list[str]

    # OCR text from the last frame — drives the "skip when no form"
    # heuristic and provides a noisy hint to the LLM.
    last_frame_ocr: str = ""


async def _collect_snapshot_inputs(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    config: FormSnapshotExtractorConfig,
) -> list[_SnapshotInputs]:
    """Build per-visit input bundles in O(V + F).

    Frame sampling picks the last frame whose timestamp falls within
    the visit window plus the immediately-preceding frame (for visits
    longer than 1 frame).  Two frames gives the LLM enough context to
    distinguish "value already there from a prior page" vs "value just
    typed".
    """
    # Filter to the CURRENT page_visit version only (page_visits keeps
    # prior versions for audit).  Without this we'd attempt snapshots
    # for stale rows and exhaust the per-artifact timeout before the
    # current form pages are reached — see the matching note in
    # page_action_extractor._collect_visit_inputs.
    #
    # "current" = the MOST RECENTLY WRITTEN version, not the lexical max.
    # extractor_version is a ``vN`` *string* column, so ``func.max`` orders
    # lexically and returns the wrong row once the version reaches ``v10``
    # (``'v9' > 'v10'``).  Order on ``created_at`` (same pattern as
    # ``test_factory.service._latest_version``) so form snapshots bind to the
    # same visit version that page_action_extractor + the artifacts router use.
    max_version_q = await session.execute(
        select(PageVisitRow.extractor_version)
        .where(
            PageVisitRow.artifact_id == artifact_id,
            PageVisitRow.tenant_id == tenant_id,
        )
        .order_by(PageVisitRow.created_at.desc())
        .limit(1)
    )
    current_visit_version = max_version_q.scalar()
    if current_visit_version is None:
        return []

    visits_q = await session.execute(
        select(PageVisitRow)
        .where(
            PageVisitRow.artifact_id == artifact_id,
            PageVisitRow.tenant_id == tenant_id,
            PageVisitRow.extractor_version == current_visit_version,
        )
        .order_by(PageVisitRow.sequence_index.asc())
    )
    visits = list(visits_q.scalars().all())
    if not visits:
        return []

    frames_q = await session.execute(
        select(VisualFrameRow)
        .where(
            VisualFrameRow.artifact_id == artifact_id,
            VisualFrameRow.tenant_id == tenant_id,
        )
        .order_by(VisualFrameRow.frame_index.asc())
    )
    all_frames = list(frames_q.scalars().all())

    def _ts_ms(frame: VisualFrameRow) -> int:
        ts = float(frame.timestamp_seconds or 0.0)
        return int(ts * 1000) if ts else 0

    out: list[_SnapshotInputs] = []
    frame_cursor = 0
    for visit in visits:
        # Optional web-only gate.
        if config.web_only and not visit.canonical_host:
            continue

        # Find the last frame within the window + the one before it.
        while (
            frame_cursor < len(all_frames)
            and _ts_ms(all_frames[frame_cursor]) < visit.first_seen_ms
        ):
            frame_cursor += 1
        local = frame_cursor
        window: list[VisualFrameRow] = []
        while (
            local < len(all_frames)
            and _ts_ms(all_frames[local]) <= visit.last_seen_ms
        ):
            window.append(all_frames[local])
            local += 1
        if not window:
            continue

        # Pick last frame (final state) and the previous one (for
        # before/after context when available).
        last_frame = window[-1]
        last_ocr = (last_frame.extracted_text or "")[:6000]
        frame_paths: list[str] = []
        if len(window) >= 2 and window[-2].frame_asset_path:
            frame_paths.append(window[-2].frame_asset_path)
        if last_frame.frame_asset_path:
            frame_paths.append(last_frame.frame_asset_path)

        if not frame_paths:
            continue

        # Heuristic skip: pages with too few label-shaped tokens
        # in OCR are unlikely to host a form.  Always allow when the
        # threshold is 0.
        label_hits = (
            len(_LABEL_TOKEN_PATTERN.findall(last_ocr))
            if config.skip_when_input_candidates_below > 0
            else config.skip_when_input_candidates_below
        )
        if (
            config.skip_when_input_candidates_below > 0
            and label_hits < config.skip_when_input_candidates_below
        ):
            continue

        out.append(_SnapshotInputs(
            page_visit_id=visit.page_visit_id,
            sequence_index=int(visit.sequence_index or 0),
            location=visit.location or "",
            canonical_host=visit.canonical_host or "",
            url_path=visit.url_path or "",
            frame_paths=frame_paths,
            last_frame_ocr=last_ocr,
        ))

    return out


# ── LLM prompt ───────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """You analyse the FINAL STATE of a page from a screen recording and enumerate EVERY form field visible on it.

You will receive:
  - 1-2 screenshots showing the page in its final state.
  - The page's URL or location.
  - OCR text from the last frame as a hint (noisy — trust the pixels).

You MUST respond by calling the ``record_form_snapshot`` tool — do NOT respond with prose, plaintext JSON, or anything other than a tool call.  Pass a LIST of FormField objects, ONE per visible form field:

  * label — the field's visible prompt as the user reads it (e.g. 'What state do you live in?').  Strip trailing colons / question marks for cleanliness.
  * value — the final value visible in the field.  Empty string when the field is blank.  PASSWORD fields MUST be returned with value="" (never reveal masked text).
  * kind — one of: button | link | dropdown | text_field | checkbox | radio | menu_item | tab | other.  Use ``text_field`` for text inputs, ``dropdown`` for selects, ``checkbox`` / ``radio`` as appropriate.
  * confidence — 0..1 confidence in your label+value extraction for this field.

Also set:
  * overall_confidence — min of per-field confidences (or single value when fields are equally clear).
  * page_intent — ONE short phrase describing the page's purpose (e.g. "Personal information — state selection").  Empty string when the intent is ambiguous.

RULES:
  * Return an EMPTY ``fields`` list when no form is visible on the page (read-only / pure navigation page).
  * Do NOT include buttons / links the user did not interact with — only ACTUAL FORM FIELDS that the page collects data into.
  * Include checkboxes and radios with their CURRENT STATE (checked = 'true', unchecked = 'false').
  * SEGMENTED CONTROLS / BUTTON-GROUP single-choice questions (e.g. a gender or plan picker rendered as side-by-side buttons where exactly one can be active): return ONE field per group -- label = the group's question text, kind = 'radio', value = the SELECTED option's visible text. If no option is visibly selected, value = '' (empty -- do NOT guess).
  * Work generically across any UI domain — insurance, banking, healthcare, internal tools."""


def _build_user_prompt(inputs: _SnapshotInputs) -> str:
    sections: list[str] = []
    sections.append(f"Page location: {inputs.location or '(unknown)'}")
    if inputs.last_frame_ocr:
        sections.append(
            f"LAST-FRAME OCR text (noisy hint):\n{inputs.last_frame_ocr[:3000]}"
        )
    sections.append(
        "Enumerate EVERY form field visible on the page in its FINAL state.  "
        "Return them via the record_form_snapshot tool.  Return an empty list "
        "when no form is visible (e.g. a marketing page, a list view, a "
        "loading screen)."
    )
    return "\n\n".join(sections)


# ── Frame fetching ──────────────────────────────────────────────────────────


async def _fetch_frame_bytes(
    http_client: httpx.AsyncClient,
    asset_path: str,
    *,
    auth_token: str,
    max_retries: int = 4,
) -> bytes | None:
    """Fetch one frame from eyes-engine with retry-on-429 backoff.

    Phase 2 v3 — eyes-engine enforces ~120 req/min and Phase 2 fires
    multiple extractors in parallel.  Without retry, the extractor
    that runs LAST in the composer chain (form_snapshot) consistently
    hit 429 and produced zero rows.  Backoff schedule: 1s, 2s, 4s, 8s
    capped, honoring Retry-After when eyes-engine provides it.
    """
    if not asset_path:
        return None
    safe_path = quote(asset_path.lstrip("/"), safe="/")
    url = f"{_EYES_BASE_URL}/api/v1/eyes/frames/{safe_path}"
    params: dict[str, str] = {}
    headers: dict[str, str] = {}
    if auth_token:
        params["token"] = auth_token
        headers["Authorization"] = f"Bearer {auth_token}"

    last_status: int | None = None
    last_body_preview = ""
    for attempt in range(max_retries + 1):
        try:
            response = await http_client.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            logger.warning(
                f"storyboard.form_snapshot_extractor.frame_transport_error "
                f"asset_path={asset_path!r} url={url!r} attempt={attempt} "
                f"error={type(exc).__name__}: {str(exc)[:200]}"
            )
            return None
        if response.status_code < 400:
            return response.content
        last_status = response.status_code
        last_body_preview = response.text[:200] if response.text else "(empty)"

        retryable = response.status_code == 429 or 500 <= response.status_code < 600
        if not retryable or attempt >= max_retries:
            break

        retry_after_header = response.headers.get("Retry-After")
        if retry_after_header and retry_after_header.isdigit():
            delay = min(int(retry_after_header), 8)
        else:
            delay = min(2 ** attempt, 8)
        await asyncio.sleep(delay)

    logger.warning(
        f"storyboard.form_snapshot_extractor.frame_http_error "
        f"asset_path={asset_path!r} url={url!r} "
        f"status={last_status} body={last_body_preview!r} "
        f"after_retries={max_retries}"
    )
    return None


def _maybe_downsize(data: bytes, max_dimension_px: int) -> tuple[bytes, str]:
    if not data or not _PIL_AVAILABLE:
        return data, "image/png"
    try:
        with _PILImage.open(io.BytesIO(data)) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            w, h = img.size
            target = img
            if max_dimension_px > 0 and max(w, h) > max_dimension_px:
                scale = max_dimension_px / max(w, h)
                new_w = max(1, int(w * scale))
                new_h = max(1, int(h * scale))
                target = img.resize((new_w, new_h), _PILImage.Resampling.LANCZOS)
            buf = io.BytesIO()
            target.save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "storyboard.form_snapshot_extractor.image_resize_failed",
            extra={"error": str(exc)[:200]},
        )
        return data, "image/png"


# ── LLM tool ────────────────────────────────────────────────────────────────


def _form_snapshot_tool() -> ToolDefinition:
    return ToolDefinition(
        name="record_form_snapshot",
        description=(
            "Record every form field visible on the page along with the "
            "user's final value.  Return an empty list when no form is "
            "visible on the page."
        ),
        input_schema=FormSnapshotLLMResponse.model_json_schema(),
    )


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl > 0:
            text = text[nl + 1:]
    if text.endswith("```"):
        text = text[:-3].rstrip()
    return text


# Fallback tiers (GPT-4o) sometimes simulate the tool call as a Python
# expression instead of using tool_use:
#   record_form_snapshot([{...}, {...}])           # bare list of fields
#   record_form_snapshot({"fields": [...], ...})   # full dict
# This pulls the inner argument (list or dict literal) so the JSON
# parser can consume it.
_PYTHON_TOOL_CALL_PATTERN = re.compile(
    r"record_form_snapshot\s*\(\s*([\[{].*[\]}])\s*\)",
    re.DOTALL,
)


def _extract_python_tool_args(text: str) -> str | None:
    """Pull the inner literal out of a ``record_form_snapshot(...)`` call."""
    match = _PYTHON_TOOL_CALL_PATTERN.search(text)
    if not match:
        return None
    inner = match.group(1)
    inner = re.sub(r"\bNone\b", "null", inner)
    inner = re.sub(r"\bTrue\b", "true", inner)
    inner = re.sub(r"\bFalse\b", "false", inner)
    return inner


# ── Per-visit extraction ────────────────────────────────────────────────────


@dataclass
class _ExtractionResult:
    page_visit_id: str
    snapshot: FormSnapshot
    model: str
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int


def _empty_snapshot(extractor_version: str, model: str = "") -> FormSnapshot:
    return FormSnapshot(
        fields={},
        field_kinds={},
        visible_field_count=0,
        overall_confidence=0.0,
        source_frame_asset_path="",
        page_intent="",
        extractor_model=model,
        extractor_version=extractor_version,
    )


async def _extract_one_visit(
    inputs: _SnapshotInputs,
    *,
    router: LLMRouter,
    config: FormSnapshotExtractorConfig,
    semaphore: asyncio.Semaphore,
    http_client: httpx.AsyncClient,
    auth_token: str,
) -> _ExtractionResult:
    """Run one multimodal LLM call → FormSnapshot."""
    # PII egress guard: in a configured PII vertical, raw form-snapshot frames
    # carry typed SSN/DOB. Refuse to ship them to the cloud LLM entirely and
    # return an EMPTY, visibly-tagged snapshot (never green-washed as extracted).
    if _pii_egress_blocked():
        logger.info(
            "storyboard.form_snapshot_extractor.pii_egress_skipped "
            f"visit={inputs.page_visit_id}"
        )
        return _ExtractionResult(
            page_visit_id=inputs.page_visit_id,
            snapshot=_empty_snapshot(config.version, model="(pii_egress_blocked)"),
            model="(pii_egress_blocked)",
            latency_ms=0,
            prompt_tokens=0,
            completion_tokens=0,
        )

    images: list[ImageContent] = []
    for path in inputs.frame_paths:
        raw = await _fetch_frame_bytes(
            http_client, path, auth_token=auth_token,
        )
        if raw:
            raw, media_type = _maybe_downsize(raw, config.image_max_dimension_px)
            images.append(ImageContent(data=raw, media_type=media_type))

    if not images:
        return _ExtractionResult(
            page_visit_id=inputs.page_visit_id,
            snapshot=_empty_snapshot(config.version, model="(no_frames)"),
            model="(no_frames)",
            latency_ms=0,
            prompt_tokens=0,
            completion_tokens=0,
        )

    tool = _form_snapshot_tool()

    async with semaphore:
        started_ms = int(time.monotonic() * 1000)
        request = CompletionRequest(
            system=_SYSTEM_PROMPT,
            prompt=_build_user_prompt(inputs),
            max_tokens=config.llm_max_tokens,
            temperature=config.llm_temperature,
            request_timeout_s=config.request_timeout_s,
            correlation_id=inputs.page_visit_id,
            images=tuple(images),
            tools=(tool,),
            tool_choice=tool.name,
            enable_prompt_cache=True,
            metadata={
                "task": config.llm_task_name,
                "sequence_index": inputs.sequence_index,
                "image_count": len(images),
            },
        )
        response = await router.complete(task=config.llm_task_name, request=request)
        latency_ms = int(time.monotonic() * 1000) - started_ms

    if response.finish_reason == FinishReason.ERROR or (
        not response.tool_calls and not response.text
    ):
        logger.warning(
            f"storyboard.form_snapshot_extractor.llm_error "
            f"visit={inputs.page_visit_id} "
            f"provider={response.provider} model={response.model} "
            f"finish={response.finish_reason.value} "
            f"error_detail={response.error_detail[:400]!r}"
        )
        return _ExtractionResult(
            page_visit_id=inputs.page_visit_id,
            snapshot=_empty_snapshot(
                config.version,
                model=f"{response.provider}/{response.model}",
            ),
            model=f"{response.provider}/{response.model}",
            latency_ms=latency_ms,
            prompt_tokens=response.prompt_tokens or 0,
            completion_tokens=response.completion_tokens or 0,
        )

    parsed: FormSnapshotLLMResponse | None = None
    if response.tool_calls:
        for call in response.tool_calls:
            if call.name == tool.name:
                try:
                    parsed = FormSnapshotLLMResponse.model_validate(call.arguments)
                    break
                except ValueError as exc:
                    logger.warning(
                        f"storyboard.form_snapshot_extractor.invalid_tool_args "
                        f"visit={inputs.page_visit_id} "
                        f"error={str(exc)[:200]!r} "
                        f"args_preview={json.dumps(call.arguments)[:300]!r}"
                    )

    if parsed is None and response.text:
        try:
            payload = _strip_code_fence(response.text)
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                # GPT-4o code-block fallback: record_form_snapshot([...]).
                inner = _extract_python_tool_args(response.text)
                if inner is not None:
                    data = json.loads(inner)
                else:
                    raise
            # Weak/local models (ollama llava) ignore the tool schema: they
            # return a tool-name-keyed wrapper, a flat {label: value} dict, or
            # a bare list of label strings. Salvage all three so the snapshot
            # isn't silently emptied (which leaves test steps value-less).
            if isinstance(data, dict) and "fields" not in data:
                _lists = [v for v in data.values() if isinstance(v, list)]
                if len(_lists) == 1:
                    data = _lists[0]                       # {"record_form_snapshot": [...]}
                elif data and all(not isinstance(v, (list, dict)) for v in data.values()):
                    data = [{"label": str(k), "value": "" if v is None else str(v)}
                            for k, v in data.items()]      # flat {label: value} — keeps VALUES
            if isinstance(data, list):
                _fields: list[FormField] = []
                for f in data:
                    item = {"label": f.strip(), "value": ""} if isinstance(f, str) else f
                    if not isinstance(item, dict):
                        continue
                    try:
                        _fields.append(FormField.model_validate(item))
                    except ValueError:
                        continue
                parsed = FormSnapshotLLMResponse(fields=_fields)
            elif isinstance(data, dict):
                parsed = FormSnapshotLLMResponse.model_validate(data)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                f"storyboard.form_snapshot_extractor.invalid_text_output "
                f"visit={inputs.page_visit_id} "
                f"provider={response.provider} model={response.model} "
                f"finish={response.finish_reason.value} "
                f"error={str(exc)[:200]!r} "
                f"raw_text={response.text[:300]!r}"
            )

    if parsed is None:
        return _ExtractionResult(
            page_visit_id=inputs.page_visit_id,
            snapshot=_empty_snapshot(
                config.version,
                model=f"{response.provider}/{response.model}",
            ),
            model=f"{response.provider}/{response.model}",
            latency_ms=latency_ms,
            prompt_tokens=response.prompt_tokens or 0,
            completion_tokens=response.completion_tokens or 0,
        )

    fields_dict: dict[str, str] = {}
    kinds_dict: dict[str, str] = {}
    for f in parsed.fields:
        label = (f.label or "").strip().rstrip("?:*").strip()
        if not label:
            continue
        # When the LLM returns duplicate labels (rare but possible),
        # keep the last value — typically the more complete one.
        fields_dict[label[:500]] = (f.value or "")[:1000]
        kinds_dict[label[:500]] = f.kind.value

    snapshot = FormSnapshot(
        fields=fields_dict,
        field_kinds=kinds_dict,
        visible_field_count=len(fields_dict),
        overall_confidence=float(parsed.overall_confidence),
        source_frame_asset_path=inputs.frame_paths[-1][:500],
        page_intent=parsed.page_intent[:200],
        extractor_model=f"{response.provider}/{response.model}"[:100],
        extractor_version=config.version,
    )

    return _ExtractionResult(
        page_visit_id=inputs.page_visit_id,
        snapshot=snapshot,
        model=f"{response.provider}/{response.model}",
        latency_ms=latency_ms,
        prompt_tokens=response.prompt_tokens or 0,
        completion_tokens=response.completion_tokens or 0,
    )


# ── Persistence ──────────────────────────────────────────────────────────────


async def _persist_snapshots(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    results: Sequence[_ExtractionResult],
    config: FormSnapshotExtractorConfig,
) -> int:
    """Write each snapshot back to its page_visits row.

    We update by primary key inside the (artifact_id, tenant_id) WHERE
    — RLS already filters by tenant, but the explicit clause keeps the
    statement self-describing and works under both RLS and direct
    superuser sessions.

    UPDATEs (not INSERTs) — the page_visits row exists already, we
    are only filling in the form_snapshot JSONB columns.
    """
    if not results:
        return 0
    written = 0
    for r in results:
        signals = {
            "visible_field_count": r.snapshot.visible_field_count,
            "overall_confidence": r.snapshot.overall_confidence,
            "source_frame_asset_path": r.snapshot.source_frame_asset_path,
            "page_intent": r.snapshot.page_intent,
            "extractor_model": r.snapshot.extractor_model,
            "field_kinds": r.snapshot.field_kinds,
            "generation_latency_ms": r.latency_ms,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
        }
        stmt = (
            update(PageVisitRow)
            .where(
                PageVisitRow.page_visit_id == r.page_visit_id,
                PageVisitRow.tenant_id == tenant_id,
                PageVisitRow.artifact_id == artifact_id,
            )
            .values(
                form_snapshot=r.snapshot.fields,
                form_snapshot_signals=signals,
                form_snapshot_extractor_version=config.version,
            )
        )
        result = await session.execute(stmt)
        written += int(result.rowcount or 0)
    try:
        async with session.begin_nested():
            await _reconcile_action_corroboration(
                session,
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                results=results,
            )
    except Exception:  # savepoint rolled back -- snapshots themselves are safe
        logger.warning(
            "storyboard.form_snapshot_extractor.action_reconcile_failed",
            extra={"artifact_id": artifact_id},
        )
    return written


def _norm_snapshot_text(v: object) -> str:
    return " ".join(str(v or "").split()).strip().strip(".").casefold()


async def _reconcile_action_corroboration(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    results: Sequence[_ExtractionResult],
) -> int:
    """Cross-extractor consensus: promote a DEMOTED page_action when the form
    snapshot -- an INDEPENDENT vision read of the visit's final frame --
    re-observes the same value. The action-level self-verification oracle
    demotes uncorroborated values, but it runs before snapshots exist; this
    closes the loop with real evidence. Raise-only-WITH-evidence: >=0.85 +
    automation_ready + explicit provenance note. Values the snapshot does not
    confirm keep their demotion untouched. Generic -- no field/app knowledge."""
    from nexus_sdk.db.models import PageActionRow

    promoted = 0
    truthy_tokens = {"true", "yes", "checked", "selected", "on"}
    for r in results:
        fields = dict(getattr(r.snapshot, "fields", None) or {})
        if not fields:
            continue
        nf = {_norm_snapshot_text(k): _norm_snapshot_text(v) for k, v in fields.items()}
        values = {v for v in nf.values() if len(v) >= 2}
        truthy_keys = {k for k, v in nf.items() if v in truthy_tokens}
        acts = (await session.execute(
            select(PageActionRow).where(
                PageActionRow.page_visit_id == r.page_visit_id,
                PageActionRow.artifact_id == artifact_id,
                PageActionRow.tenant_id == tenant_id,
            )
        )).scalars().all()
        for act in acts:
            if bool(getattr(act, "automation_ready", False)):
                continue
            if "[duplicate:" in str(getattr(act, "reasoning", "") or ""):
                # A demoted DUPLICATE stays demoted: the value matching the
                # snapshot is exactly what makes it a duplicate — corroboration
                # must never resurrect a re-observation as a second action.
                continue
            verb = str(getattr(act, "verb", "") or "").casefold()
            if verb not in ("type", "select", "check", "click"):
                continue
            v = _norm_snapshot_text(getattr(act, "value", ""))
            lbl = _norm_snapshot_text(getattr(act, "target_label", ""))
            corroborated = bool(
                (len(v) >= 2 and v in values)
                or (len(v) >= 2 and v in truthy_keys)
                or (verb in ("select", "check") and lbl and lbl in truthy_keys)
                or (v and lbl and lbl in nf and nf[lbl] == v)
            )
            if not corroborated:
                continue
            act.confidence = max(float(getattr(act, "confidence", 0.0) or 0.0), 0.85)
            act.automation_ready = True
            act.reasoning = (str(getattr(act, "reasoning", "") or "") +
                " [cross-verified: the form snapshot -- an independent vision read of "
                "this page's final frame -- re-observed this exact value; two "
                "independent extractions agree.]")
            promoted += 1
    if promoted:
        logger.info(
            "storyboard.form_snapshot_extractor.action_reconcile",
            extra={"artifact_id": artifact_id, "promoted": promoted},
        )
    return promoted


# ── Public entry point ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class FormSnapshotExtractorResult:
    artifact_id: str
    visits_attempted: int
    visits_with_snapshot: int
    visits_skipped: int
    fields_captured: int
    elapsed_ms: int


async def extract_form_snapshots_for_artifact(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    config: FormSnapshotExtractorConfig,
    router: LLMRouter,
    auth_token: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> FormSnapshotExtractorResult:
    """Run form-snapshot extraction for one artifact.

    Caller has already run ``extract_page_visits_for_artifact``.
    Idempotent.  Does NOT commit; the caller controls the transaction.
    """
    started = time.monotonic()

    inputs = await _collect_snapshot_inputs(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        config=config,
    )
    if not inputs:
        return FormSnapshotExtractorResult(
            artifact_id=artifact_id,
            visits_attempted=0,
            visits_with_snapshot=0,
            visits_skipped=0,
            fields_captured=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    semaphore = asyncio.Semaphore(max(1, config.concurrency))

    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                config.request_timeout_s if config.request_timeout_s else 30.0,
            ),
            follow_redirects=True,
        )

    try:
        gathered: list[_ExtractionResult] = await asyncio.wait_for(
            asyncio.gather(*(
                _extract_one_visit(
                    inp,
                    router=router,
                    config=config,
                    semaphore=semaphore,
                    http_client=http_client,
                    auth_token=auth_token,
                )
                for inp in inputs
            )),
            timeout=config.total_timeout_s,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "storyboard.form_snapshot_extractor.total_timeout",
            extra={
                "artifact_id": artifact_id,
                "timeout_s": config.total_timeout_s,
                "visits_pending": len(inputs),
            },
        )
        if owns_client:
            await http_client.aclose()
        return FormSnapshotExtractorResult(
            artifact_id=artifact_id,
            visits_attempted=len(inputs),
            visits_with_snapshot=0,
            visits_skipped=0,
            fields_captured=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        if owns_client and http_client is not None:
            try:
                await http_client.aclose()
            except Exception:  # pragma: no cover
                pass

    written = await _persist_snapshots(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        results=gathered,
        config=config,
    )

    with_snapshot = sum(1 for r in gathered if r.snapshot.visible_field_count > 0)
    fields_captured = sum(r.snapshot.visible_field_count for r in gathered)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "storyboard.form_snapshot_extractor.run",
        extra={
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "visits_attempted": len(inputs),
            "visits_with_snapshot": with_snapshot,
            "visits_persisted": written,
            "fields_captured": fields_captured,
            "elapsed_ms": elapsed_ms,
            "extractor_version": config.version,
        },
    )

    return FormSnapshotExtractorResult(
        artifact_id=artifact_id,
        visits_attempted=len(inputs),
        visits_with_snapshot=with_snapshot,
        visits_skipped=0,  # heuristic-skipped visits are dropped before this point
        fields_captured=fields_captured,
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    "FormSnapshotExtractorResult",
    "extract_form_snapshots_for_artifact",
]
