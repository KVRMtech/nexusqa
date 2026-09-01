"""Page-action extractor — semantic user actions keyed by page_visit.

URL-keyed parallel of ``action_extractor`` (which is scene-keyed).
Solves the merged-scene problem: when the canonical pipeline's
``scene_grouper`` collapses 9 distinct URL visits into ONE scene
because their layout is similar, the scene-keyed extractor can only
emit actions for that one merged scene.  This extractor iterates the
``page_visits`` table instead, so every distinct URL gets its own
action breakdown regardless of how the canonical pipeline grouped
its frames.

Architecture
============
For every ``page_visits`` row produced by ``page_visit_extractor``:

1. Sample N representative frames evenly across the visit's
   ``first_seen_ms`` → ``last_seen_ms`` range.  N is config-driven
   (4 / 6 / 8 by duration bucket).
2. Build a multimodal LLM request — system prompt asks the LLM to
   emit a chronological list of distinct user actions visible across
   the frames (same contract as the scene-keyed extractor's
   ``SceneActionList`` tool).
3. Validate the response through Pydantic (``PageActionList``).
4. UPSERT one row per action into ``page_actions`` keyed by
   ``(page_visit_id, extractor_version, subaction_index)``.

Production-safety considerations
================================
* **Schema parity** with scene_actions — downstream consumers only
  need a trivial adapter to switch sources.
* **Idempotent** — same inputs + same version yield the same rows.
* **Tenant-scoped** — every query filters on ``tenant_id`` and the
  RLS session variable enforces the same at DB level.
* **Cost-controlled** — visits with empty location are skipped (no
  point asking the LLM about a frame whose URL we couldn't read).
* **Concurrency-bounded** via asyncio.Semaphore to prevent stampedes
  against the LLM provider.

Consumed by:
  * ``composer`` — runs after ``page_visit_extractor`` and
    ``scene_grouper``.
  * ``routers/artifacts.py`` — surfaces page_actions in the
    ``/visual-flow`` response.
  * Test exporters — generate per-URL Playwright/Cypress assertions.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Sequence
from urllib.parse import quote

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    PageActionRow,
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
from .config import PageActionExtractorConfig
from .page_schemas import (
    ExtractionEvidence,
    PageAction,
    PageActionList,
)
from .schemas import SceneActionTargetKind, SceneActionVerb


try:  # pragma: no cover - optional dependency
    from PIL import Image as _PILImage  # type: ignore[import-untyped]

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False


_EYES_BASE_URL = os.environ.get(
    "STORYBOARD_EYES_BASE_URL", "http://nexus-eyes:8003",
).rstrip("/")


logger = logging.getLogger(__name__)


_NAMESPACE_PAGE_ACTIONS = uuid.UUID("a7d3e9f1-5c8b-4a32-bd14-2e5f6a8d9c44")
"""Stable namespace — keeps page_action_id derivation grouped."""


def _page_action_id(
    page_visit_id: str, extractor_version: str, subaction_index: int,
) -> str:
    """Deterministic action_id — same (visit, version, sub) → same row."""
    seed = f"page_action:{page_visit_id}:{extractor_version}:{subaction_index}"
    return str(uuid.uuid5(_NAMESPACE_PAGE_ACTIONS, seed))


# ── Per-visit inputs ─────────────────────────────────────────────────────────


@dataclass
class _VisitInputs:
    """Everything the LLM call needs for one page_visit."""

    page_visit_id: str
    sequence_index: int
    canonical_host: str
    url_path: str
    url_query: str
    location: str  # full canonical URL or window title
    duration_s: float
    frame_count: int

    # Frames sampled across the visit's first_seen → last_seen window.
    sampled_frame_paths: list[str] = field(default_factory=list)

    # OCR text from the LAST frame of the visit — used in the prompt as
    # a hint and in the reconciliation step.
    last_frame_ocr: str = ""

    # Journey neighbours — the URLs immediately before and after this
    # visit.  Lets the LLM infer transitions even when on-page changes
    # are subtle (e.g. dropdown selection produces minimal pixel diff
    # but the next URL is a different page → user MUST have submitted).
    prev_visit_location: str = ""
    next_visit_location: str = ""


async def _collect_visit_inputs(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    config: PageActionExtractorConfig,
) -> list[_VisitInputs]:
    """Bulk-load visits + their candidate frames in a fixed number of selects."""
    # Pull visits at the CURRENT page_visit extractor version only.
    #
    # page_visits keeps prior versions for audit (the page_visit
    # extractor's _delete_stale_visits only prunes within a version).
    # If we processed every version we'd waste the LLM budget — and the
    # per-artifact total_timeout — re-deriving actions for stale rows,
    # starving the current visits (observed live: 11 stale v1 rows +
    # 14 current v2 rows meant the form pages near the end never got
    # processed before the timeout).  Resolve the current version first,
    # then filter to it.
    #
    # NOTE: "current" = the version of the MOST RECENTLY WRITTEN row, not the
    # lexical max.  extractor_version is a ``vN`` *string* column, so
    # ``func.max`` orders lexically — once the version reaches ``v10`` the
    # lexical max wrongly returns ``v9`` (``'v9' > 'v10'``), binding actions to
    # a stale visit set.  Ordering on ``created_at`` (the same pattern as
    # ``test_factory.service._latest_version``) is numeric-safe and keeps
    # visit/action/form bound to the same version.
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

    # All frames in the artifact, indexed by timestamp_ms for
    # window-based sampling.
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

    # Build per-visit frame windows in O(N + V) instead of O(N*V).
    visit_inputs: list[_VisitInputs] = []
    frame_cursor = 0

    for idx, visit in enumerate(visits):
        # Collect frames whose timestamp falls inside the visit window.
        window: list[VisualFrameRow] = []
        # Advance cursor to the first frame ≥ first_seen_ms.
        while (
            frame_cursor < len(all_frames)
            and _ts_ms(all_frames[frame_cursor]) < visit.first_seen_ms
        ):
            frame_cursor += 1
        local_cursor = frame_cursor
        while (
            local_cursor < len(all_frames)
            and _ts_ms(all_frames[local_cursor]) <= visit.last_seen_ms
        ):
            window.append(all_frames[local_cursor])
            local_cursor += 1

        # Pick sampling cap based on visit duration.
        duration_s = float(visit.duration_ms or 0) / 1000.0
        if duration_s >= config.very_long_visit_threshold_s:
            sample_count = config.frames_per_visit_very_long
        elif duration_s >= config.long_visit_threshold_s:
            sample_count = config.frames_per_visit_long
        else:
            sample_count = config.frames_per_visit_default

        if not window:
            sampled_paths: list[str] = []
        elif len(window) <= sample_count:
            sampled_paths = [f.frame_asset_path for f in window if f.frame_asset_path]
        else:
            step = (len(window) - 1) / max(1, sample_count - 1)
            indices = sorted({
                min(len(window) - 1, int(round(i * step)))
                for i in range(sample_count)
            })
            sampled_paths = [
                window[i].frame_asset_path
                for i in indices
                if window[i].frame_asset_path
            ]

        last_ocr = ""
        if window:
            last_frame = window[-1]
            last_ocr = (last_frame.extracted_text or "")[:4000]

        prev_loc = visits[idx - 1].location if idx > 0 else ""
        next_loc = visits[idx + 1].location if idx + 1 < len(visits) else ""

        visit_inputs.append(_VisitInputs(
            page_visit_id=visit.page_visit_id,
            sequence_index=int(visit.sequence_index or 0),
            canonical_host=visit.canonical_host or "",
            url_path=visit.url_path or "",
            url_query=visit.url_query or "",
            location=visit.location or "",
            duration_s=duration_s,
            frame_count=int(visit.frame_count or 0),
            sampled_frame_paths=sampled_paths,
            last_frame_ocr=last_ocr,
            prev_visit_location=prev_loc,
            next_visit_location=next_loc,
        ))

    return visit_inputs


# ── LLM prompt building ──────────────────────────────────────────────────────


_SYSTEM_PROMPT = """You analyse a sequence of screenshots from ONE PAGE in a screen recording and extract EVERY user action that happened on that page.

You will receive:
  - 1..8 screenshots IN CHRONOLOGICAL ORDER showing this page over time.
    * For SHORT pages (a click that navigated, a single typed word) you'll get 1-2 frames.
    * For LONG form-fill pages (10+ seconds where the user changed several fields on the same URL) you'll get 4-8 frames sampled across the page's duration. EACH transition between adjacent frames may represent a separate user action — read them as a film strip, not as a single before/after pair.
  - The page's URL or location.
  - The location of the PREVIOUS page and NEXT page in the user's journey (JOURNEY CONTEXT — use this to infer what actions this page must have contained, especially when on-page changes are subtle).
  - OCR text from the last frame as a hint (OCR is often noisy; trust the pixels).

You MUST respond by calling the ``record_page_actions`` tool ONCE with a LIST of actions, in chronological order. Do NOT respond with prose, JSON in plaintext, or anything other than a tool call.

RULES:

  * **Single-action pages** (a click that navigated, a single typed word, a hover): return a list of length 1.
  * **Long form-fill pages** (multiple frames show different fields being filled — State then Gender then Age etc.): return ONE action per field change. Example: for a Personal Information page where the user filled 5 fields, return a list of length 5 in order.
  * **Idle pages**: return an empty list `[]` only when frames are visually identical AND the URL did not change AND journey shows no progression.
  * **Detect changes BETWEEN adjacent frames**: for each pair of adjacent frames (frame[i] → frame[i+1]), ask "what did the user do here?" — that's one action.  Different field values appearing across frames almost always means separate type/select actions, NOT one bulk action.
  * **The cursor may be INVISIBLE**.  Infer actions from value changes, page transitions, and journey context — not from a visible cursor.
  * For verb=select on a dropdown/menu, the value is the SELECTED option (e.g. 'TX'), target_label is the field's prompt (e.g. 'What state do you live in?'). NEVER put the selected value in target_label.
  * For verb=type, the value is the typed text. Mask passwords with value=null.
  * confidence MUST reflect actual certainty per action: ~0.95 when a frame unambiguously shows the value; drop to ~0.5 when ambiguous.
  * automation_ready is True only when verb + target + value are unambiguous enough for a Playwright test to reproduce.
  * reasoning is ONE short sentence per action citing the specific visual evidence.
  * Work generically across any UI domain — insurance, banking, healthcare, internal tools."""


def _build_user_prompt(inputs: _VisitInputs) -> str:
    """Compose the text portion of the multimodal LLM call."""
    sections: list[str] = []
    sections.append(f"Page sequence index: {inputs.sequence_index}")
    sections.append(f"Page location: {inputs.location or '(unknown)'}")
    if inputs.prev_visit_location or inputs.next_visit_location:
        sections.append(
            "JOURNEY CONTEXT (use to infer actions when on-page changes are subtle):\n"
            f"  - PREV page: {inputs.prev_visit_location or '(none)'}\n"
            f"  > THIS page: {inputs.location or '(unknown)'} ← analyse this\n"
            f"  - NEXT page: {inputs.next_visit_location or '(none)'}"
        )
    if inputs.last_frame_ocr:
        sections.append(
            f"LAST-FRAME OCR text (noisy hint):\n{inputs.last_frame_ocr[:2000]}"
        )
    sections.append(
        f"Page duration: ~{inputs.duration_s:.1f}s across {inputs.frame_count} frames."
    )
    sections.append(
        "Extract EVERY distinct user action visible across the frames, "
        "in chronological order.  For LONG pages with multiple frames, "
        "compare ADJACENT frames pairwise and identify each transition as "
        "a separate action — a 24-second form-fill page typically shows "
        "3-7 distinct actions (e.g. select State, select Gender, type "
        "Age, type Height, type Weight, click Tobacco option, click "
        "Submit).  You MUST respond by calling the record_page_actions "
        "tool — do not return prose or text JSON.  Empty list = no "
        "detectable user activity."
    )
    return "\n\n".join(sections)


# ── Reconciliation ───────────────────────────────────────────────────────────


def _reconcile(
    action: PageAction, inputs: _VisitInputs,
) -> tuple[PageAction, ExtractionEvidence]:
    """Cross-check the LLM output against existing signals.

    Lighter-weight than the scene-keyed reconciliation — page visits
    do not carry their own evidence_controls / cursor_events binding
    (those are scene-keyed).  We rely on OCR + URL change + journey
    transition.
    """
    evidence = ExtractionEvidence(sources=["page_visit_frames", "last_frame_ocr"])

    last_ocr_lower = inputs.last_frame_ocr.lower()

    # OCR text corroboration.
    if action.value:
        evidence.ocr_text_match = action.value.lower() in last_ocr_lower
    elif action.target_label and len(action.target_label) > 4:
        evidence.ocr_text_match = action.target_label.lower() in last_ocr_lower

    # URL change — if the next visit's location differs from this one,
    # something on this page caused navigation.
    if (
        inputs.next_visit_location
        and inputs.location
        and inputs.next_visit_location != inputs.location
    ):
        evidence.url_changed = True

    # Adjust verb / confidence based on reconciliation.
    adjusted_verb = action.verb
    adjusted_target = action.target_label
    adjusted_kind = action.target_kind
    adjusted_confidence = action.confidence
    adjusted_automation = action.automation_ready
    adjusted_reasoning = action.reasoning

    # verb=none + URL changed → record an HONEST INFERRED transition (never green-wash).
    # The URL changed but NO on-page action was observed (verb=none). We do NOT fabricate
    # a demonstrated navigate: (1) we never invent a control label from the URL segment
    # (that manufactured a real-looking target that the recording never showed — the
    # verb=none→NAVIGATE@0.55 fabrication); (2) we mark it NOT automation-ready; (3) we keep
    # confidence LOW so downstream provenance is INFERRED (a soft, honestly-UNPROVEN
    # transition — handled by the proven-nav oracle + nav-recovery), never a demonstrated
    # action. The transition itself (the URL) is preserved for grounded recovery.
    if action.verb is SceneActionVerb.NONE and evidence.url_changed:
        adjusted_verb = SceneActionVerb.NAVIGATE
        adjusted_kind = SceneActionTargetKind.OTHER
        adjusted_target = ""            # do NOT fabricate a control label from the URL
        adjusted_automation = False     # inferred, not observed → never automation-ready
        adjusted_confidence = min(adjusted_confidence, 0.5)  # low → provenance = inferred
        if not adjusted_reasoning:
            adjusted_reasoning = (
                "INFERRED: the URL changed between this page and the next, so a navigation "
                "occurred, but the on-page action that caused it was NOT observed. Recorded "
                "as an inferred transition — not a demonstrated action."
            )

    # verb requires a value but no OCR signal corroborates → clamp.
    needs_value = adjusted_verb in {SceneActionVerb.TYPE, SceneActionVerb.SELECT}
    if needs_value and not action.value:
        adjusted_automation = False
    if needs_value and action.value and not evidence.ocr_text_match:
        adjusted_automation = False
        adjusted_confidence = min(adjusted_confidence, 0.5)

    # SELF-VERIFICATION ORACLE (Phase-2 — consensus, DEMOTE-ONLY, never green-wash).
    # Confidence should mean something: a claim is PROVEN only when INDEPENDENT signals
    # agree — here the LLM's own confidence + OCR text corroboration + a recorded URL
    # change. A high-confidence, automation-ready NON-navigation claim that NO independent
    # signal corroborates (its label/value is not visible in the frame OCR and no URL
    # change occurred) is unsupported by consensus → demote it to inferred
    # (automation_ready=False, confidence capped). This can ONLY LOWER confidence, never
    # raise it, so it cannot manufacture a green — it only makes the extraction more
    # conservative. Controls whose label IS visible in the frame OCR stay corroborated and
    # are untouched (evidence.ocr_text_match covers label OR value).
    corroborations = int(bool(evidence.ocr_text_match)) + int(bool(evidence.url_changed))
    if (adjusted_verb is not SceneActionVerb.NAVIGATE and adjusted_automation
            and adjusted_confidence >= 0.8 and corroborations == 0):
        adjusted_automation = False
        adjusted_confidence = min(adjusted_confidence, 0.5)
        if adjusted_reasoning:
            adjusted_reasoning += (
                " [self-verification: no independent signal (OCR / URL) corroborated this "
                "high-confidence claim — demoted to inferred.]")

    if (
        adjusted_confidence != action.confidence
        or adjusted_automation != action.automation_ready
        or adjusted_verb is not action.verb
        or adjusted_target != action.target_label
        or adjusted_kind is not action.target_kind
        or adjusted_reasoning != action.reasoning
    ):
        action = action.model_copy(update={
            "verb": adjusted_verb,
            "target_label": adjusted_target,
            "target_kind": adjusted_kind,
            "confidence": adjusted_confidence,
            "automation_ready": adjusted_automation,
            "reasoning": adjusted_reasoning,
        })

    return action, evidence


# ── Frame fetching ──────────────────────────────────────────────────────────


async def _fetch_frame_bytes(
    http_client: httpx.AsyncClient,
    asset_path: str,
    *,
    auth_token: str,
    max_retries: int = 4,
) -> bytes | None:
    """Fetch one frame from eyes-engine.

    Same contract as action_extractor — JWT goes as both Bearer header
    and ``?token=`` query param.

    Adds retry-with-backoff on HTTP 429 (eyes-engine enforces ~120 req/min)
    and other transient 5xx codes.  Phase 2 fires multiple extractors
    in parallel (page_action + form_snapshot + scene action_extractor)
    against the same eyes-engine; without retry, the second extractor to
    hit eyes-engine in a saturated minute returns 0 frames and produces
    empty rows.

    Backoff schedule: 1s, 2s, 4s, 8s (caps the worst-case slow visit
    at ~15s of waiting before giving up).
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
                f"storyboard.page_action_extractor.frame_transport_error "
                f"asset_path={asset_path!r} url={url!r} attempt={attempt} "
                f"error={type(exc).__name__}: {str(exc)[:200]}"
            )
            return None
        if response.status_code < 400:
            return response.content
        last_status = response.status_code
        last_body_preview = response.text[:200] if response.text else "(empty)"

        # Retry on rate-limit + transient 5xx; bail immediately on 4xx
        # that aren't 429 (auth issues won't fix themselves).
        retryable = response.status_code == 429 or 500 <= response.status_code < 600
        if not retryable or attempt >= max_retries:
            break

        # 1s, 2s, 4s, 8s — capped.  Honor Retry-After when eyes-engine
        # gives us a concrete hint.
        retry_after_header = response.headers.get("Retry-After")
        if retry_after_header and retry_after_header.isdigit():
            delay = min(int(retry_after_header), 8)
        else:
            delay = min(2 ** attempt, 8)
        await asyncio.sleep(delay)

    logger.warning(
        f"storyboard.page_action_extractor.frame_http_error "
        f"asset_path={asset_path!r} url={url!r} "
        f"status={last_status} body={last_body_preview!r} "
        f"after_retries={max_retries}"
    )
    return None


def _maybe_downsize(data: bytes, max_dimension_px: int) -> tuple[bytes, str]:
    """JPEG q85 re-encode with optional max-dimension cap."""
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
            "storyboard.page_action_extractor.image_resize_failed",
            extra={"error": str(exc)[:200]},
        )
        return data, "image/png"


# ── LLM tool ─────────────────────────────────────────────────────────────────


def _page_action_tool() -> ToolDefinition:
    """Build the LLM tool that forces a PageActionList response."""
    return ToolDefinition(
        name="record_page_actions",
        description=(
            "Record EVERY distinct user action visible on this page, "
            "in chronological order.  Empty list when the user did not "
            "interact (idle view, autoplay)."
        ),
        input_schema=PageActionList.model_json_schema(),
    )


# ── LLM invocation ──────────────────────────────────────────────────────────


@dataclass
class _ExtractionResult:
    page_visit_id: str
    actions: list[PageAction]
    evidence: ExtractionEvidence
    model: str
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int


def _strip_code_fence(text: str) -> str:
    """Strip optional ```json ... ``` markdown fences from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl > 0:
            text = text[nl + 1:]
    if text.endswith("```"):
        text = text[:-3].rstrip()
    return text


# Pattern for OpenAI/Gemini code-block responses that simulate a tool
# call instead of using the tool_use API.  GPT-4o has been observed
# returning:
#   ```python
#   record_page_actions([{...}, {...}])
#   ```
# After ``_strip_code_fence`` removes the fences, the remaining text is
# ``record_page_actions([{...}, {...}])`` — not valid JSON.  This regex
# extracts the bracketed argument list so the JSON parser can consume it.
_PYTHON_TOOL_CALL_PATTERN = re.compile(
    r"(?:record_page_actions|record_scene_actions)\s*\(\s*(\[.*\])\s*\)",
    re.DOTALL,
)


def _extract_python_tool_args(text: str) -> str | None:
    """Pull the bracketed arg list out of a ``record_page_actions(...)`` call.

    Returns the inner ``[...]`` substring (still raw text, JSON-shaped)
    or ``None`` when no such pattern is present.  The caller passes the
    result to ``json.loads`` after fixing Python-isms like ``None`` →
    ``null`` and ``True`` → ``true``.
    """
    match = _PYTHON_TOOL_CALL_PATTERN.search(text)
    if not match:
        return None
    inner = match.group(1)
    # Best-effort Python-to-JSON conversion.  Won't handle every edge
    # case (e.g. raw f-strings) but covers the dominant GPT-4o pattern.
    inner = re.sub(r"\bNone\b", "null", inner)
    inner = re.sub(r"\bTrue\b", "true", inner)
    inner = re.sub(r"\bFalse\b", "false", inner)
    return inner


def _normalize_action_dict(raw: object) -> dict:
    """Normalise a loosely-shaped action dict from a text-fallback parse.

    Fallback tiers (notably GPT-4o) drift from the PageAction schema in
    small ways even when the structure is sound.  Observed live on
    artifact 72be675e:
      * Uses ``target`` instead of ``target_label`` — copy it across
        when ``target_label`` is absent so the field isn't lost.

    Pydantic ignores unknown keys by default, so the stray ``target``
    is harmless once we've lifted its value.  Tool-use responses
    (the primary path) never hit this — they're already schema-valid.
    """
    if not isinstance(raw, dict):
        return raw  # let Pydantic raise a clear error downstream
    d = dict(raw)
    if not d.get("target_label"):
        for alt in ("target", "label", "element", "field"):
            if d.get(alt):
                d["target_label"] = d[alt]
                break
    return d


# Verb-phrase → (verb, target_kind).  Weak / local models (e.g. ollama
# llava) frequently ignore the structured tool schema and return a bare
# list of ACTION LABEL STRINGS like "Enter Departure City" / "Choose
# Travel Class".  The label's leading word IS the verb, so we recover a
# valid PageAction deterministically instead of dropping the whole visit
# to a verb=none placeholder.  Order matters (most specific first).
_VERB_PHRASE_HINTS: tuple[tuple[tuple[str, ...], SceneActionVerb, SceneActionTargetKind], ...] = (
    (("enter", "type", "fill", "input", "write", "provide"), SceneActionVerb.TYPE, SceneActionTargetKind.TEXT_FIELD),
    (("select", "choose", "pick", "set"), SceneActionVerb.SELECT, SceneActionTargetKind.DROPDOWN),
    (("check", "tick", "mark"), SceneActionVerb.SELECT, SceneActionTargetKind.CHECKBOX),
    (("toggle",), SceneActionVerb.SELECT, SceneActionTargetKind.RADIO),
    (("submit", "confirm", "search", "continue", "find", "proceed", "save", "apply", "send"), SceneActionVerb.SUBMIT, SceneActionTargetKind.BUTTON),
    (("click", "press", "tap", "go", "open", "sign", "log", "next", "add", "view"), SceneActionVerb.CLICK, SceneActionTargetKind.BUTTON),
    (("navigate", "visit", "browse"), SceneActionVerb.NAVIGATE, SceneActionTargetKind.OTHER),
)


def _label_to_action_dict(label: str) -> dict:
    """Recover a PageAction-shaped dict from a single action LABEL STRING by
    inferring the verb from the phrase's leading word (e.g. 'Enter Departure
    City' → verb=type, target_label='Departure City')."""
    text = (label or "").strip()
    low = text.lower()
    verb, kind, target = SceneActionVerb.NONE, SceneActionTargetKind.OTHER, text
    for words, v, k in _VERB_PHRASE_HINTS:
        hit = next((w for w in words if low == w or low.startswith(w + " ")), None)
        if hit:
            verb, kind = v, k
            target = text[len(hit):].strip().lstrip(":–-").strip() or text
            break
    return {
        "verb": verb.value,
        "target_label": target[:500],
        "target_kind": kind.value,
        "value": None,
        "confidence": 0.55,
        "automation_ready": False,
        "reasoning": "Recovered from a label-only model response; verb inferred from the action phrase.",
    }


def _coerce_actions_payload(data: object) -> list | None:
    """Normalise a loosely-shaped JSON payload into a list of action dicts.

    Handles every shape weak/local models have produced live:
      * a bare list of action dicts            → as-is (normalised)
      * a bare list of action LABEL STRINGS    → verb inferred per label
      * ``{"actions": [...]}``                 → unwrapped
      * a TOOL-NAME-keyed wrapper, e.g.
        ``{"record_page_actions": [...]}``     → unwrapped (llava:7b live)
      * a single action dict                   → wrapped to a 1-list
    Returns None when nothing list-shaped can be recovered.
    """
    if isinstance(data, dict):
        if isinstance(data.get("actions"), list):
            data = data["actions"]
        else:
            list_vals = [v for v in data.values() if isinstance(v, list)]
            if len(list_vals) == 1:
                data = list_vals[0]
    if isinstance(data, list):
        out: list = []
        for item in data:
            if isinstance(item, str):
                out.append(_label_to_action_dict(item))
            elif isinstance(item, dict):
                out.append(_normalize_action_dict(item))
        return out
    if isinstance(data, dict):
        return [_normalize_action_dict(data)]
    return None


def _empty_visit_result(visit_id: str) -> _ExtractionResult:
    """Empty result for visits we deliberately skip (e.g. unreadable location)."""
    return _ExtractionResult(
        page_visit_id=visit_id,
        actions=[],
        evidence=ExtractionEvidence(sources=["skipped_no_location"]),
        model="(skipped)",
        latency_ms=0,
        prompt_tokens=0,
        completion_tokens=0,
    )


def _fallback_result(
    inputs: _VisitInputs,
    response,
    latency_ms: int,
    *,
    evidence_marker: str,
) -> _ExtractionResult:
    """Emit at least one placeholder action so the visit is never silently dropped.

    Mirrors the scene-keyed extractor's safety-net behaviour.  The
    reconciliation step may promote verb=none → verb=navigate when the
    next visit's URL differs.
    """
    placeholder = PageAction(
        verb=SceneActionVerb.NONE,
        target_label="",
        target_kind=SceneActionTargetKind.OTHER,
        value=None,
        confidence=0.30,
        automation_ready=False,
        reasoning=(
            "LLM call failed for this page; emitting a placeholder so the "
            "page is not silently absent from the action log."
        ),
    )
    action, evidence = _reconcile(placeholder, inputs)
    evidence.sources.append(evidence_marker)
    return _ExtractionResult(
        page_visit_id=inputs.page_visit_id,
        actions=[action],
        evidence=evidence,
        model=f"{response.provider}/{response.model}",
        latency_ms=latency_ms,
        prompt_tokens=response.prompt_tokens or 0,
        completion_tokens=response.completion_tokens or 0,
    )


def _no_frames_fallback(inputs: _VisitInputs) -> _ExtractionResult:
    """Emit a placeholder for visits whose every frame fetch failed.

    Before this fallback existed, no_frames visits returned ``actions=[]``
    which got filtered out at persistence — silently dropping the visit
    from page_actions.  v3 — emit a placeholder so the visit is always
    represented downstream; the reconciliation step may even promote
    verb=none → navigate when the next visit's URL differs.
    """
    placeholder = PageAction(
        verb=SceneActionVerb.NONE,
        target_label="",
        target_kind=SceneActionTargetKind.OTHER,
        value=None,
        confidence=0.20,
        automation_ready=False,
        reasoning=(
            "All frame fetches failed for this page; emitting a "
            "placeholder so the visit is not silently dropped."
        ),
    )
    action, evidence = _reconcile(placeholder, inputs)
    evidence.sources.append("no_frames_fallback")
    return _ExtractionResult(
        page_visit_id=inputs.page_visit_id,
        actions=[action],
        evidence=evidence,
        model="(no_frames)",
        latency_ms=0,
        prompt_tokens=0,
        completion_tokens=0,
    )


async def _extract_one_visit(
    inputs: _VisitInputs,
    *,
    router: LLMRouter,
    config: PageActionExtractorConfig,
    semaphore: asyncio.Semaphore,
    http_client: httpx.AsyncClient,
    auth_token: str,
) -> _ExtractionResult:
    """Run one LLM call + reconciliation for a single page visit."""
    # Visits with no frames OR no readable location → skip (no point
    # asking the LLM about an empty window).
    if not inputs.sampled_frame_paths or not inputs.location:
        return _empty_visit_result(inputs.page_visit_id)

    images: list[ImageContent] = []
    for path in inputs.sampled_frame_paths:
        raw = await _fetch_frame_bytes(
            http_client, path, auth_token=auth_token,
        )
        if raw:
            raw, media_type = _maybe_downsize(raw, config.image_max_dimension_px)
            images.append(ImageContent(data=raw, media_type=media_type))

    if not images:
        logger.warning(
            f"storyboard.page_action_extractor.no_frames_available "
            f"visit={inputs.page_visit_id} "
            f"sampled_count={len(inputs.sampled_frame_paths)} "
            f"first_path={inputs.sampled_frame_paths[0] if inputs.sampled_frame_paths else '(none)'}"
        )
        # v3 — emit a placeholder so the visit is always represented
        # downstream.  Previously this returned actions=[] which got
        # silently filtered out at persistence.
        return _no_frames_fallback(inputs)

    tool = _page_action_tool()

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
            f"storyboard.page_action_extractor.llm_error "
            f"visit={inputs.page_visit_id} "
            f"provider={response.provider} model={response.model} "
            f"finish={response.finish_reason.value} "
            f"error_detail={response.error_detail[:400]!r}"
        )
        return _fallback_result(
            inputs, response, latency_ms, evidence_marker="llm_failed_fallback",
        )

    action_list: PageActionList | None = None
    if response.tool_calls:
        for call in response.tool_calls:
            if call.name == tool.name:
                try:
                    action_list = PageActionList.model_validate(call.arguments)
                    break
                except ValueError as exc:
                    logger.warning(
                        f"storyboard.page_action_extractor.invalid_tool_args "
                        f"visit={inputs.page_visit_id} "
                        f"error={str(exc)[:200]!r} "
                        f"args_preview={json.dumps(call.arguments)[:300]!r}"
                    )

    if action_list is None and response.text:
        try:
            payload = _strip_code_fence(response.text)
            data = None
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                # Fallback tiers (GPT-4o) sometimes simulate the tool
                # call as a Python expression: record_page_actions([...]).
                # Extract the bracketed arg list and retry.
                inner = _extract_python_tool_args(response.text)
                if inner is not None:
                    data = json.loads(inner)
                else:
                    raise
            coerced = _coerce_actions_payload(data)
            valid: list[PageAction] = []
            for a in (coerced or []):
                # Validate per-item so one malformed entry can't sink the
                # whole visit (weak models mix good + bad items).
                try:
                    valid.append(PageAction.model_validate(a))
                except ValueError:
                    continue
            if valid:
                action_list = PageActionList(actions=valid)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                f"storyboard.page_action_extractor.invalid_text_output "
                f"visit={inputs.page_visit_id} "
                f"provider={response.provider} model={response.model} "
                f"finish={response.finish_reason.value} "
                f"error={str(exc)[:200]!r} "
                f"raw_text={response.text[:300]!r}"
            )

    if action_list is None:
        return _fallback_result(
            inputs, response, latency_ms, evidence_marker="llm_invalid_fallback",
        )

    reconciled: list[PageAction] = []
    aggregate_evidence: ExtractionEvidence | None = None
    for action in action_list.actions:
        adjusted, evidence = _reconcile(action, inputs)
        if adjusted.confidence < config.min_confidence_for_automation:
            adjusted = adjusted.model_copy(update={"automation_ready": False})
        reconciled.append(adjusted)
        if aggregate_evidence is None:
            aggregate_evidence = evidence

    if not reconciled:
        placeholder = PageAction(
            verb=SceneActionVerb.NONE,
            target_label="",
            target_kind=SceneActionTargetKind.OTHER,
            value=None,
            confidence=0.40,
            automation_ready=False,
            reasoning=(
                "LLM returned an empty action list for this page; surfaced "
                "as a placeholder so the visit appears in the action log."
            ),
        )
        adjusted, evidence = _reconcile(placeholder, inputs)
        reconciled.append(adjusted)
        if aggregate_evidence is None:
            aggregate_evidence = evidence
            aggregate_evidence.sources.append("empty_list_placeholder")

    if aggregate_evidence is None:
        aggregate_evidence = ExtractionEvidence(
            sources=["page_visit_frames", "last_frame_ocr"],
        )

    return _ExtractionResult(
        page_visit_id=inputs.page_visit_id,
        actions=reconciled,
        evidence=aggregate_evidence,
        model=f"{response.provider}/{response.model}",
        latency_ms=latency_ms,
        prompt_tokens=response.prompt_tokens or 0,
        completion_tokens=response.completion_tokens or 0,
    )


# ── Persistence ──────────────────────────────────────────────────────────────


async def _upsert_page_actions(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    results: Sequence[_ExtractionResult],
    config: PageActionExtractorConfig,
    visit_to_artifact_id: dict[str, str],
) -> int:
    """UPSERT one row per extracted action; safe to re-run at the same version."""
    rows: list[dict] = []
    for r in results:
        # Skip empty results — UPSERT-of-empty is a noop but pre-filter
        # keeps the row count metric honest.
        if not r.actions:
            continue
        for sub_idx, action in enumerate(r.actions):
            rows.append({
                "page_action_id": _page_action_id(
                    r.page_visit_id, config.version, sub_idx,
                ),
                "page_visit_id": r.page_visit_id,
                "artifact_id": visit_to_artifact_id.get(
                    r.page_visit_id, artifact_id,
                ),
                "tenant_id": tenant_id,
                "subaction_index": sub_idx,
                "verb": action.verb.value,
                "target_label": action.target_label[:500],
                "target_kind": action.target_kind.value,
                "value": action.value[:1000] if action.value else None,
                "confidence": float(action.confidence),
                "automation_ready": bool(action.automation_ready),
                "reasoning": action.reasoning[:500],
                "evidence_signals": r.evidence.model_dump(),
                "extractor_model": r.model[:100],
                "extractor_version": config.version,
                "generation_latency_ms": int(r.latency_ms),
                "prompt_tokens": int(r.prompt_tokens),
                "completion_tokens": int(r.completion_tokens),
            })
    if not rows:
        return 0

    stmt = pg_insert(PageActionRow.__table__).values(rows)
    update_columns = {
        col.name: stmt.excluded[col.name]
        for col in PageActionRow.__table__.columns
        if col.name not in {"page_action_id", "created_at"}
    }
    update_columns["updated_at"] = stmt.excluded.updated_at
    upsert = stmt.on_conflict_do_update(
        index_elements=[PageActionRow.__table__.c.page_action_id],
        set_=update_columns,
    )
    await session.execute(upsert)
    return len(rows)


# ── Stale-row cleanup ───────────────────────────────────────────────────────


async def _delete_stale_actions(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    extractor_version: str,
    keep_ids: list[str],
) -> int:
    """Drop page_actions rows at the current version whose page_action_id is
    not in the freshly-written set.  Defends against shrinking action
    lists across re-runs (e.g. prompt v1 returned 5 actions; prompt v2
    returns 3 — the 2 obsolete rows must be removed)."""
    if not keep_ids:
        return 0
    from sqlalchemy import and_, delete, not_

    # Chunk the NOT IN by 500 to stay under Postgres param limits on
    # extremely chatty re-runs.
    deleted_total = 0
    chunk = 500
    for offset in range(0, len(keep_ids), chunk):
        kept = set(keep_ids[offset:offset + chunk])
        stmt = (
            delete(PageActionRow)
            .where(
                and_(
                    PageActionRow.artifact_id == artifact_id,
                    PageActionRow.tenant_id == tenant_id,
                    PageActionRow.extractor_version == extractor_version,
                    not_(PageActionRow.page_action_id.in_(kept)),
                )
            )
        )
        result = await session.execute(stmt)
        deleted_total += int(result.rowcount or 0)
    return deleted_total


# ── Public entry point ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PageActionExtractorResult:
    artifact_id: str
    visits_attempted: int
    visits_with_actions: int
    visits_skipped: int
    actions_written: int
    stale_actions_deleted: int
    elapsed_ms: int


async def extract_page_actions_for_artifact(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    config: PageActionExtractorConfig,
    router: LLMRouter,
    auth_token: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> PageActionExtractorResult:
    """Run page-action extraction for one artifact.

    Caller has already run ``extract_page_visits_for_artifact`` so the
    ``page_visits`` table is populated.  Idempotent.  Does NOT commit.
    """
    started = time.monotonic()

    inputs = await _collect_visit_inputs(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        config=config,
    )
    if not inputs:
        return PageActionExtractorResult(
            artifact_id=artifact_id,
            visits_attempted=0,
            visits_with_actions=0,
            visits_skipped=0,
            actions_written=0,
            stale_actions_deleted=0,
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
            "storyboard.page_action_extractor.total_timeout",
            extra={
                "artifact_id": artifact_id,
                "timeout_s": config.total_timeout_s,
                "visits_pending": len(inputs),
            },
        )
        if owns_client:
            await http_client.aclose()
        return PageActionExtractorResult(
            artifact_id=artifact_id,
            visits_attempted=len(inputs),
            visits_with_actions=0,
            visits_skipped=0,
            actions_written=0,
            stale_actions_deleted=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        if owns_client and http_client is not None:
            try:
                await http_client.aclose()
            except Exception:  # pragma: no cover
                pass

    # Cross-visit duplicate demotion: when the vision pass splits one wizard
    # page into adjacent visits, both re-observe the same already-filled state
    # (same verb+label+VALUE) and the generated script re-fills it. The user
    # demonstrated the action ONCE: keep the FIRST observation, demote the
    # immediately-following visit's identical VALUE-carrying claim to an
    # honestly-labeled duplicate (evidence stays; nothing is deleted).
    # Value-less clicks (Continue on every wizard page) are exempt — those are
    # genuinely distinct actions. Demote-only + guarded: any failure leaves
    # the extraction untouched.
    try:
        _prev_sigs: set = set()
        for r in gathered:
            _cur_sigs: set = set()
            for _i, _act in enumerate(list(r.actions)):
                _val = " ".join(str(getattr(_act, "value", "") or "").split()).casefold()
                if not _val:
                    continue
                _sig = (
                    str(getattr(_act, "verb", "") or ""),
                    " ".join(str(getattr(_act, "target_label", "") or "").split()).casefold(),
                    _val,
                )
                if _sig in _prev_sigs and bool(getattr(_act, "automation_ready", False)):
                    r.actions[_i] = _act.model_copy(update={
                        "automation_ready": False,
                        "reasoning": (str(getattr(_act, "reasoning", "") or "") +
                            " [duplicate: the immediately-preceding visit already "
                            "demonstrated this exact action — this is a re-observation "
                            "of persisted form state, not a second user action.]"),
                    })
                _cur_sigs.add(_sig)
            _prev_sigs = _cur_sigs
    except Exception:  # pragma: no cover — demote-only pass must never break extraction
        logger.warning(
            "storyboard.page_action_extractor.dup_demote_failed",
            extra={"artifact_id": artifact_id},
        )

    # All visits in this artifact share artifact_id (the join key), but
    # we keep the dict shape so the persistence helper stays generic.
    visit_to_artifact = {r.page_visit_id: artifact_id for r in gathered}

    actions_written = await _upsert_page_actions(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        results=gathered,
        config=config,
        visit_to_artifact_id=visit_to_artifact,
    )

    # Build the keep-set for the stale cleanup (every action_id we just
    # wrote).
    keep_ids: list[str] = []
    for r in gathered:
        for sub_idx in range(len(r.actions)):
            keep_ids.append(_page_action_id(r.page_visit_id, config.version, sub_idx))

    stale_deleted = await _delete_stale_actions(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        extractor_version=config.version,
        keep_ids=keep_ids,
    )

    with_actions = sum(1 for r in gathered if r.actions)
    skipped = sum(
        1 for r in gathered
        if not r.actions and "skipped_no_location" in r.evidence.sources
    )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "storyboard.page_action_extractor.run",
        extra={
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "visits_attempted": len(inputs),
            "visits_with_actions": with_actions,
            "visits_skipped": skipped,
            "actions_written": actions_written,
            "stale_actions_deleted": stale_deleted,
            "elapsed_ms": elapsed_ms,
            "extractor_version": config.version,
        },
    )

    return PageActionExtractorResult(
        artifact_id=artifact_id,
        visits_attempted=len(inputs),
        visits_with_actions=with_actions,
        visits_skipped=skipped,
        actions_written=actions_written,
        stale_actions_deleted=stale_deleted,
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    "PageActionExtractorResult",
    "extract_page_actions_for_artifact",
]
