"""Action extractor — multimodal LLM produces semantic user actions per scene.

Why we need this
================
The frozen pipeline's frame-action extractor (``build_scenes._annotate_scenes
_with_frame_actions``) produces OCR-text-diff signals like "text 'TX' appeared
on the page" but cannot interpret that as "user selected TX from a State
dropdown".  See memory/action_capture_extraction_limit.md for the full
diagnostic against artifact 72be675e on 2026-05-25:
  * 30 evidence_controls detected, only 1 with a value (an OCR-misread
    button label, not a user input)
  * 0 evidence_steps rows
  * 0 cursor_events rows
  * 80 frame_actions in JSON across 17 scenes — 75% empty placeholders,
    the rest OCR-noise fragments mislabeled as actions
  * quality_gate_outcome = 'pass', brain_quality_score = 1.0 — the gates
    measure OCR coverage, not action evidence

What this service does
======================
For each scene in an artifact the extractor:

1. Pulls the strongest signals — representative before/after frames,
   OCR text, URL, existing evidence_controls + cursor_events + audio
   intent.
2. Builds a multimodal LLM request with the BEFORE and AFTER frame
   screenshots and the structured-output schema (SceneAction Pydantic
   model from schemas.py).
3. Sends to the LLM via the provider-agnostic LLMRouter
   (task='action_extraction'); the router routes to a vision-capable
   tier configured by env (LLM_TASK_ACTION_EXTRACTION=tier_vision).
4. Validates the response through Pydantic — invalid output is rejected
   and retried up to the LLM router's retry budget.  Persistent
   failures fall back to a deterministic action built from the
   existing evidence_controls (action_kind + label only, no value).
5. Reconciles the LLM output with existing pipeline signals
   (OCR text, evidence_controls.observed_value, cursor_events, URL
   change, audio transcript).  Boosts confidence on agreement; clamps
   automation_ready when the LLM was overconfident.
6. UPSERTs into scene_actions keyed by (scene_id, extractor_version).

Production-safety considerations
================================
* **Concurrency-bounded** via asyncio.Semaphore so a slow vision LLM
  cannot exhaust the event loop or DDOS the provider.
* **Total-timeout** budget so one bad artifact never blocks future
  composer requests; rows not generated stay missing and the composer
  can retry on the next request.
* **Idempotent** — same inputs + same extractor_version yield the same
  row.  Re-running is safe.
* **Observable** — every row records extractor_model, generation_
  latency_ms, prompt/completion tokens, and the evidence_signals
  snapshot.  Failures stay quiet (None confidence + log).
* **Tenant-scoped** — every query filters on tenant_id and the RLS
  session variable from migration 029/030/031 enforces the same.
* **Cost-controlled** — noise scenes are skipped by default
  (skip_noise=True), reducing per-recording LLM calls by ~30-50%.
  Frame screenshots are downsized to image_max_dimension_px before
  upload so token cost stays predictable.

Consumed by composer.get_storyboard.  Exposed via the visual-flow API
response under each scene as ``scene_actions``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Sequence
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    CursorEventRow,
    EvidenceControlRow,
    SceneActionRow,
    StoryboardPanelRow,
    TranscriptSegmentRow,
    VisualFrameRow,
    VisualFlowEdgeRow,
    VisualSceneRow,
)

from ..llm import (
    CompletionRequest,
    FinishReason,
    ImageContent,
    LLMRouter,
    ToolDefinition,
)
from .config import ActionExtractorConfig
from .schemas import (
    ExtractionEvidence,
    SceneAction,
    SceneActionList,
    SceneActionTargetKind,
    SceneActionVerb,
)


# Long-scene threshold (seconds) — scenes ≥ this duration trigger
# multi-frame sampling AND multi-action LLM extraction.  Short scenes
# (clicks, navigations) stay at 2 frames + single-action.  Form-fill
# pages are typically 10s+ on a single page.
LONG_SCENE_THRESHOLD_S = 5.0

# Very-long-scene threshold — scenes ≥ this duration get DOUBLE the
# intra-frame samples (8 instead of 4).  These are typically multi-
# field form pages where the user spent 15+ seconds typing/selecting.
VERY_LONG_SCENE_THRESHOLD_S = 10.0

# How many INTRA-SCENE frames to sample for long scenes (in addition
# to the previous-scene rep used as "before context").  4 lets Sonnet
# see start / 1/3 / 2/3 / end of the scene — captures field-by-field
# transitions in a form-fill.
INTRA_SCENE_FRAME_COUNT = 4
INTRA_SCENE_FRAME_COUNT_VERY_LONG = 8


# Optional Pillow — used to downsize frames before sending to the LLM.
# When unavailable the raw bytes are sent through unchanged, which is
# fine for most artifacts but may push above token budgets on 4K
# screenshots.
try:  # pragma: no cover
    from PIL import Image as _PILImage  # type: ignore[import-untyped]

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False


_EYES_BASE_URL = os.environ.get(
    "STORYBOARD_EYES_BASE_URL", "http://nexus-eyes:8003",
).rstrip("/")


logger = logging.getLogger(__name__)


_NAMESPACE_STORYBOARD = uuid.UUID("d4f6c9a2-6d8b-4f5b-9a32-8f4b1c1a2d3e")
"""Same namespace as the rest of the storyboard layer — keeps derived IDs grouped."""


def _action_id(scene_id: str, extractor_version: str, subaction_index: int = 0) -> str:
    """Deterministic action_id — same (scene, version, subaction_index) yields same row.

    The subaction_index is included so the SAME scene at the same
    version can have multiple rows without colliding ids.  Existing
    v1-v7 rows used subaction_index=0 implicitly; their action_ids
    remain stable because uuid5(_NAMESPACE, f"action:scene:v") ==
    uuid5(_NAMESPACE, f"action:scene:v:0") is NOT true — so for
    BACK-COMPAT with v1-v7 rows, when subaction_index==0 we emit the
    short legacy form.  v8+ always uses the long form with explicit
    subaction_index so multi-row scenes stay deterministic.
    """
    if subaction_index == 0:
        seed = f"action:{scene_id}:{extractor_version}"
    else:
        seed = f"action:{scene_id}:{extractor_version}:{subaction_index}"
    return str(uuid.uuid5(_NAMESPACE_STORYBOARD, seed))


# ── Per-scene signal collection ───────────────────────────────────────────────


@dataclass
class _JourneyNeighbour:
    """One adjacent scene's text-only summary, given to the LLM as
    sequence context for the current scene's extraction."""

    scene_index: int
    url: str
    screen_title: str
    ocr_snippet: str  # short — keeps token budget tight on small LLMs


@dataclass
class _SceneInputs:
    """Everything the LLM call needs for one scene.

    Pre-collected once so the per-scene LLM call stays compact and
    each scene's inputs are independently testable.
    """

    scene_id: str
    scene_index: int
    panel_id: str | None
    panel_is_noise: bool

    # Scene duration in seconds — drives the multi-frame branch.
    # Scenes ≥ LONG_SCENE_THRESHOLD_S also fetch ``intra_scene_frame_paths``.
    duration_s: float = 0.0

    # Frame data
    before_frame_asset_path: str = ""  # representative frame of PREVIOUS scene
    after_frame_asset_path: str = ""   # representative frame of CURRENT scene
    before_ocr_text: str = ""
    after_ocr_text: str = ""

    # Phase 1.6 — additional frames sampled WITHIN this scene for
    # long-scene multi-action extraction.  Empty for short scenes;
    # populated with INTRA_SCENE_FRAME_COUNT evenly-spaced frame asset
    # paths for scenes ≥ LONG_SCENE_THRESHOLD_S.  Ordering matches the
    # temporal sequence (earliest first).
    intra_scene_frame_paths: list[str] = field(default_factory=list)

    # Context
    before_url: str = ""
    after_url: str = ""
    audio_intent: str = ""

    # Existing pipeline signals (for reconciliation, NOT for the LLM
    # prompt — we want the LLM to read the pixels independently).
    existing_control_values: list[tuple[str, str]] = field(default_factory=list)
    """List of (label, value) pairs from evidence_controls for this scene."""

    existing_cursor_click_count: int = 0
    """Number of cursor_events with is_click=True attributed to this scene."""

    # Phase 1.5 — journey sequence context (text-only).
    #
    # The 2-3 scenes BEFORE this one and AFTER this one, as URL+title
    # summaries.  Lets even a weak LLM infer "this scene's action
    # must have completed the state page, since the next scene is on
    # the gender page" — narrative continuity is often the strongest
    # signal when before/after frames look similar.
    prev_neighbours: list[_JourneyNeighbour] = field(default_factory=list)
    next_neighbours: list[_JourneyNeighbour] = field(default_factory=list)


async def _collect_scene_inputs(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
) -> list[_SceneInputs]:
    """Pull every scene's inputs in one batch.

    Avoids N+1 queries by loading scenes + frames + controls + cursor +
    transcript in five aggregate selects, then assembling per-scene
    inputs in Python.  Cheap even on 200+ scene artifacts.
    """
    # ── Scenes (ordered by scene_index)
    scenes_q = await session.execute(
        select(VisualSceneRow)
        .where(
            VisualSceneRow.artifact_id == artifact_id,
            VisualSceneRow.tenant_id == tenant_id,
        )
        .order_by(VisualSceneRow.scene_index.asc())
    )
    scenes = list(scenes_q.scalars().all())
    if not scenes:
        return []

    # ── Panels (for noise flag + caption short — used to skip noise scenes)
    panels_q = await session.execute(
        select(StoryboardPanelRow).where(
            StoryboardPanelRow.artifact_id == artifact_id,
            StoryboardPanelRow.tenant_id == tenant_id,
        )
    )
    panel_by_first_scene: dict[str, StoryboardPanelRow] = {
        p.first_scene_id: p for p in panels_q.scalars().all() if p.first_scene_id
    }

    # ── Representative frames for each scene
    frame_ids = {s.representative_frame_id for s in scenes if s.representative_frame_id}
    frames_q = await session.execute(
        select(VisualFrameRow).where(
            VisualFrameRow.tenant_id == tenant_id,
            VisualFrameRow.frame_id.in_(frame_ids) if frame_ids else False,
        )
    )
    frames_by_id: dict[str, VisualFrameRow] = {
        f.frame_id: f for f in frames_q.scalars().all()
    }

    # Phase 1.6 — all frames per scene, used for multi-frame sampling
    # on long-form-fill scenes.  We restrict the in-query to the
    # current artifact's scene_ids so a huge tenant doesn't blow up
    # the result set; ordering by frame_index keeps the per-scene
    # ordering deterministic.
    scene_ids = [s.scene_id for s in scenes if s.scene_id]
    intra_frames_by_scene: dict[str, list[str]] = {}
    if scene_ids:
        intra_q = await session.execute(
            select(
                VisualFrameRow.scene_id,
                VisualFrameRow.frame_asset_path,
                VisualFrameRow.frame_index,
            )
            .where(
                VisualFrameRow.tenant_id == tenant_id,
                VisualFrameRow.scene_id.in_(scene_ids),
            )
            .order_by(
                VisualFrameRow.scene_id.asc(),
                VisualFrameRow.frame_index.asc(),
            )
        )
        for sid, asset_path, _idx in intra_q.all():
            if not sid or not asset_path:
                continue
            intra_frames_by_scene.setdefault(str(sid), []).append(asset_path)

    # ── Edges (for URL before/after)
    edges_q = await session.execute(
        select(VisualFlowEdgeRow).where(
            VisualFlowEdgeRow.artifact_id == artifact_id,
            VisualFlowEdgeRow.tenant_id == tenant_id,
        )
    )
    edge_to_scene: dict[str, VisualFlowEdgeRow] = {}
    for e in edges_q.scalars().all():
        if e.to_scene_id:
            edge_to_scene[e.to_scene_id] = e

    # ── Evidence controls per scene
    ctrls_q = await session.execute(
        select(EvidenceControlRow).where(
            EvidenceControlRow.artifact_id == artifact_id,
            EvidenceControlRow.tenant_id == tenant_id,
        )
    )
    controls_by_scene: dict[str, list[EvidenceControlRow]] = {}
    for c in ctrls_q.scalars().all():
        if c.scene_id:
            controls_by_scene.setdefault(c.scene_id, []).append(c)

    # ── Cursor events per scene
    cursor_q = await session.execute(
        select(CursorEventRow).where(
            CursorEventRow.artifact_id == artifact_id,
            CursorEventRow.tenant_id == tenant_id,
        )
    )
    cursor_clicks_by_scene: dict[str, int] = {}
    for ev in cursor_q.scalars().all():
        if not ev.is_click:
            continue
        # Cursor events carry frame_id; resolve to scene via the frame.
        frame = frames_by_id.get(ev.frame_id) if ev.frame_id else None
        if frame and frame.scene_id:
            cursor_clicks_by_scene[frame.scene_id] = (
                cursor_clicks_by_scene.get(frame.scene_id, 0) + 1
            )

    # ── Audio intent (transcript-based signal) — DISABLED on this deployment.
    #
    # The ``TranscriptSegmentRow`` ORM model and the actual
    # ``transcript_segments`` DB table have diverged at this site (ORM
    # declares ``start_time``/``end_time``/``text``/``job_id``; DB has
    # ``start_ms``/``end_ms``/``text_redacted`` and lacks ``job_id``).
    # Any query through the ORM raises ``UndefinedColumnError`` and
    # crashes extraction.  Audio intent is a nice-to-have reconciliation
    # signal, not a required one — disabling it is safe.  Re-enable once
    # the ORM ↔ DB migration is reconciled (separate cleanup task).
    def _audio_intent_for_scene(scene: VisualSceneRow) -> str:
        """Stub — returns empty string until transcript ORM is reconciled."""
        return ""

    # ── Assemble per-scene inputs (with before frame = previous scene's after)
    inputs: list[_SceneInputs] = []
    prev_after_path = ""
    prev_after_ocr = ""
    prev_url = ""

    # First pass — build the bare _SceneInputs without neighbours.
    for s in scenes:
        rep = frames_by_id.get(s.representative_frame_id) if s.representative_frame_id else None
        after_path = (rep.frame_asset_path or "") if rep else ""
        after_ocr = (rep.extracted_text or "") if rep else ""
        edge = edge_to_scene.get(s.scene_id)
        after_url = (s.detected_url or "") if hasattr(s, "detected_url") else ""

        panel = panel_by_first_scene.get(s.scene_id)
        panel_id = panel.panel_id if panel else None
        is_noise = bool(panel.is_noise) if panel else False

        controls = controls_by_scene.get(s.scene_id, [])
        existing_values: list[tuple[str, str]] = []
        for c in controls:
            label = (c.label_text or c.display_label or "").strip()
            value = (c.observed_value or c.value_text or "").strip()
            if label and value:
                existing_values.append((label[:200], value[:200]))

        # Phase 1.6 — for long scenes, sample evenly-spaced frames so
        # the LLM sees intermediate states (between filling State and
        # Gender on the same form page).  Very-long scenes get more
        # samples (8) since they typically contain multiple form-fill
        # actions packed close together.
        duration_s = float(((s.end_ms or 0) - (s.start_ms or 0)) / 1000.0)
        intra_paths: list[str] = []
        if duration_s >= LONG_SCENE_THRESHOLD_S:
            sample_count = (
                INTRA_SCENE_FRAME_COUNT_VERY_LONG
                if duration_s >= VERY_LONG_SCENE_THRESHOLD_S
                else INTRA_SCENE_FRAME_COUNT
            )
            all_frames = intra_frames_by_scene.get(s.scene_id, [])
            if len(all_frames) > sample_count:
                # Evenly-spaced sampling across the scene.
                step = (len(all_frames) - 1) / max(1, sample_count - 1)
                indices = sorted({
                    min(len(all_frames) - 1, int(round(i * step)))
                    for i in range(sample_count)
                })
                intra_paths = [all_frames[i] for i in indices]
            else:
                intra_paths = list(all_frames)

        inputs.append(_SceneInputs(
            scene_id=s.scene_id,
            scene_index=int(s.scene_index or 0),
            panel_id=panel_id,
            panel_is_noise=is_noise,
            duration_s=duration_s,
            before_frame_asset_path=prev_after_path,
            after_frame_asset_path=after_path,
            before_ocr_text=prev_after_ocr[:4000],
            after_ocr_text=after_ocr[:4000],
            before_url=prev_url,
            after_url=after_url,
            audio_intent=_audio_intent_for_scene(s),
            existing_control_values=existing_values[:10],
            existing_cursor_click_count=cursor_clicks_by_scene.get(s.scene_id, 0),
            intra_scene_frame_paths=intra_paths,
        ))
        prev_after_path = after_path
        prev_after_ocr = after_ocr
        prev_url = after_url

    # Second pass — populate the journey-context neighbours.  Doing
    # this in a separate pass keeps the data structure simple (build
    # everything first, then cross-reference).  We attach up to 2
    # prev + 2 next scenes' summaries to each scene.
    NEIGHBOUR_RADIUS = 2

    # Build scene_id → screen_title lookup once (O(N) instead of O(N²)).
    scene_title_by_id: dict[str, str] = {}
    for s in scenes:
        summary = getattr(s, "scene_state_summary", None) or {}
        title = ""
        if isinstance(summary, dict):
            title = (summary.get("screen_title") or "").strip()
        scene_title_by_id[s.scene_id] = title

    def _to_neighbour(other: _SceneInputs) -> _JourneyNeighbour:
        return _JourneyNeighbour(
            scene_index=other.scene_index,
            url=(other.after_url or "")[:200],
            screen_title=scene_title_by_id.get(other.scene_id, "")[:120],
            ocr_snippet=(other.after_ocr_text or "")[:200],
        )

    for i, inp in enumerate(inputs):
        for j in range(max(0, i - NEIGHBOUR_RADIUS), i):
            inp.prev_neighbours.append(_to_neighbour(inputs[j]))
        for j in range(i + 1, min(len(inputs), i + 1 + NEIGHBOUR_RADIUS)):
            inp.next_neighbours.append(_to_neighbour(inputs[j]))

    return inputs


# ── LLM prompt building ───────────────────────────────────────────────────────


_SYSTEM_PROMPT = """You analyse a screen-recording scene and extract EVERY user action that happened in that scene.

You will receive:
  - Multiple screenshots IN CHRONOLOGICAL ORDER showing the scene over time.
    * For SHORT scenes (clicks, navigations) you'll get 2 frames: BEFORE and AFTER.
    * For LONG scenes (form-fill pages with several fields) you'll get 4-6 frames sampled across the scene's duration. EACH transition between adjacent frames may represent a separate user action — read them as a film strip, not as a single before/after pair.
  - OCR text from the first and last frames as a hint (OCR is often noisy; trust the pixels).
  - The URL on each screenshot.
  - JOURNEY CONTEXT: the URL + screen title of the 2 scenes BEFORE and 2 AFTER this one in the recording. Use this sequence to infer what actions this scene must have contained.
  - (Optional) a transcript snippet from the recording's audio narration.
  - (Optional) a list of UI controls the pipeline detected with their captured values.

Call the ``record_scene_actions`` tool ONCE with a LIST of actions, in chronological order. RULES:

  * **Single-action scenes** (a click that navigated, a single typed word, a hover): return a list of length 1.
  * **Long form-fill scenes** (multiple frames showing different fields being filled — State then Gender then Age etc.): return ONE action per field change. Example: for a Personal Information page where the user filled 5 fields, return a list of length 5 in order.
  * **Idle scenes**: return an empty list `[]` only when frames are visually identical AND the URL did not change AND journey shows no progression.
  * **Detect changes BETWEEN adjacent frames**: for each pair of adjacent frames (frame[i] → frame[i+1]), ask "what did the user do here?" — that's one action.  Different field values appearing across frames almost always means separate type/select actions, NOT one bulk action.
  * **The cursor may be INVISIBLE**. Infer actions from value changes, page transitions, and journey context — not from visible cursor.
  * For verb=select on a dropdown/menu, the value is the SELECTED option (e.g. 'TX'), target_label is the field's prompt (e.g. 'What state do you live in?'). NEVER put the selected value in target_label.
  * For verb=type, the value is the typed text. Mask passwords with value=null.
  * confidence MUST reflect actual certainty per action: ~0.95 when a frame unambiguously shows the value; drop to ~0.5 when ambiguous.
  * automation_ready is True only when verb + target + value are unambiguous enough for a Playwright test to reproduce.
  * reasoning is ONE short sentence per action citing the specific visual evidence.
  * Work generically across any UI domain — insurance, banking, healthcare, internal tools."""


def _build_user_prompt(inputs: _SceneInputs) -> str:
    """Compose the text portion of the multimodal LLM call.

    Image content (the before/after screenshots) is attached separately
    by the LLM router's vision tier; this function only renders the
    text scaffold.

    Phase 1.5 — adds JOURNEY CONTEXT section with up to 2 previous and
    2 next scenes' URLs + screen titles + OCR snippets.  This lets the
    LLM infer the action even when the cursor is invisible by noting
    that the destination URL is different (so the user MUST have done
    something to navigate).  Critical for radio/dropdown selections
    that produce minimal pixel change.
    """
    sections: list[str] = []
    sections.append(f"Scene index: {inputs.scene_index}")
    if inputs.before_url or inputs.after_url:
        sections.append(
            f"URL before: {inputs.before_url or '(none)'} -> "
            f"URL after: {inputs.after_url or '(none)'}"
        )

    # Journey context — text only, compact.
    if inputs.prev_neighbours or inputs.next_neighbours:
        ctx_lines: list[str] = ["JOURNEY CONTEXT (use to infer the action):"]
        for n in inputs.prev_neighbours:
            label = n.screen_title or n.ocr_snippet[:60] or "(no title)"
            ctx_lines.append(
                f"  - Scene {n.scene_index} (PREV): URL={n.url or '(none)'}, screen=\"{label}\""
            )
        ctx_lines.append(
            f"  > Scene {inputs.scene_index} (CURRENT): URL={inputs.after_url or '(none)'} ← this is the scene to analyse"
        )
        for n in inputs.next_neighbours:
            label = n.screen_title or n.ocr_snippet[:60] or "(no title)"
            ctx_lines.append(
                f"  - Scene {n.scene_index} (NEXT): URL={n.url or '(none)'}, screen=\"{label}\""
            )
        sections.append("\n".join(ctx_lines))

    if inputs.before_ocr_text:
        sections.append(f"BEFORE OCR text:\n{inputs.before_ocr_text[:2000]}")
    if inputs.after_ocr_text:
        sections.append(f"AFTER OCR text:\n{inputs.after_ocr_text[:2000]}")
    if inputs.audio_intent:
        sections.append(f"SME narration during this scene:\n{inputs.audio_intent}")
    if inputs.existing_control_values:
        pairs = "; ".join(
            f"{lbl}={val}" for lbl, val in inputs.existing_control_values[:6]
        )
        sections.append(f"Pipeline-detected controls (may be noisy): {pairs}")
    if inputs.existing_cursor_click_count > 0:
        sections.append(
            f"Pipeline detected {inputs.existing_cursor_click_count} click event(s) "
            "in this scene's frame range."
        )
    sections.append(
        "Extract EVERY distinct user action that happened in this scene, "
        "in chronological order.  For LONG scenes with multiple frames, "
        "compare ADJACENT frames pairwise and identify each transition as "
        "a separate action — a 24-second form-fill page typically shows "
        "3-7 distinct actions (e.g. select State, select Gender, type "
        "Age, type Height, type Weight, click Tobacco option, click "
        "Submit).  Return a LIST of actions via the record_scene_actions "
        "tool.  Empty list = no detectable user activity."
    )
    return "\n\n".join(sections)


# ── Reconciliation ────────────────────────────────────────────────────────────


def _reconcile(
    action: SceneAction, inputs: _SceneInputs,
) -> tuple[SceneAction, ExtractionEvidence]:
    """Cross-check the LLM output against existing pipeline signals.

    Adjusts confidence + automation_ready based on agreement:
      * Boost confidence ceiling when multiple signals corroborate.
      * Clamp automation_ready to False when the value the LLM claims
        cannot be found in any independent signal AND the verb is
        type/select (where the value is the whole point).

    Returns the (possibly adjusted) SceneAction and an ExtractionEvidence
    snapshot to persist alongside it.
    """
    evidence = ExtractionEvidence(sources=["before_frame", "after_frame", "ocr_text"])

    after_ocr_lower = inputs.after_ocr_text.lower()

    # OCR text match — verb-aware
    if action.value:
        evidence.ocr_text_match = action.value.lower() in after_ocr_lower
    elif action.target_label and len(action.target_label) > 4:
        evidence.ocr_text_match = action.target_label.lower() in after_ocr_lower

    # Control match
    for label, value in inputs.existing_control_values:
        if action.value and value.lower() == action.value.lower():
            evidence.control_match = True
            if "evidence_controls" not in evidence.sources:
                evidence.sources.append("evidence_controls")
            break
        if action.target_label and label.lower() in action.target_label.lower():
            evidence.control_match = True
            if "evidence_controls" not in evidence.sources:
                evidence.sources.append("evidence_controls")
            break

    # Cursor event corroboration
    if inputs.existing_cursor_click_count > 0 and action.verb in {
        SceneActionVerb.CLICK,
        SceneActionVerb.SELECT,
        SceneActionVerb.SUBMIT,
    }:
        evidence.cursor_event_match = True
        if "cursor_events" not in evidence.sources:
            evidence.sources.append("cursor_events")

    # URL change
    if inputs.before_url and inputs.after_url and inputs.before_url != inputs.after_url:
        evidence.url_changed = True

    # Audio intent
    if inputs.audio_intent and action.target_label:
        if action.target_label.lower() in inputs.audio_intent.lower():
            evidence.audio_intent_match = True
            if "audio_intent" not in evidence.sources:
                evidence.sources.append("audio_intent")

    # Adjust the SceneAction based on reconciliation
    corroboration_count = sum([
        evidence.ocr_text_match,
        evidence.control_match,
        evidence.cursor_event_match,
        evidence.url_changed,
        evidence.audio_intent_match,
    ])

    adjusted_confidence = action.confidence
    adjusted_automation = action.automation_ready
    adjusted_verb = action.verb
    adjusted_target = action.target_label
    adjusted_kind = action.target_kind
    adjusted_reasoning = action.reasoning

    # Phase 1.5 — verb=none override.
    #
    # When the LLM returns verb=none but the URL clearly changed
    # between Before and After, the user MUST have done something to
    # cause the navigation.  A literal "I see no UI change" is wrong
    # in this case; the navigation IS the change.  Promote to
    # verb=navigate so the journey doesn't look broken downstream.
    # This is the dominant failure mode for weaker LLMs (llava) on
    # subtle radio/dropdown selections where the value rendering
    # changes minimally but the next URL proves an action happened.
    if action.verb is SceneActionVerb.NONE and evidence.url_changed:
        adjusted_verb = SceneActionVerb.NAVIGATE
        # Use the AFTER URL's last path segment as the target label
        # when the LLM didn't supply one — gives the journey a
        # human-readable step ("/personal-information/state").
        if not adjusted_target and inputs.after_url:
            from urllib.parse import urlparse
            try:
                path = urlparse(
                    inputs.after_url if "://" in inputs.after_url
                    else "https://" + inputs.after_url
                ).path or ""
                tail = [seg for seg in path.split("/") if seg]
                if tail:
                    adjusted_target = tail[-1].replace("-", " ").title()[:200]
            except Exception:  # pragma: no cover
                pass
        adjusted_kind = SceneActionTargetKind.OTHER
        if not adjusted_reasoning or "no user action" in adjusted_reasoning.lower():
            adjusted_reasoning = (
                "URL changed between Before and After frames — "
                "navigation occurred even though the on-page "
                "interaction wasn't visible in the screenshots."
            )
        # Confidence: moderate — we know SOMETHING happened, but the
        # specifics are unknown.  Floor at 0.55.
        adjusted_confidence = max(adjusted_confidence, 0.55)

    # When verb requires a value but no signal corroborates the value,
    # clamp automation_ready.  The LLM may be hallucinating.
    needs_value = adjusted_verb in {SceneActionVerb.TYPE, SceneActionVerb.SELECT}
    if needs_value and not action.value:
        adjusted_automation = False
    if needs_value and action.value and not (
        evidence.ocr_text_match or evidence.control_match
    ):
        adjusted_automation = False
        adjusted_confidence = min(adjusted_confidence, 0.5)

    # When 2+ signals corroborate, lift confidence floor.
    if corroboration_count >= 2:
        adjusted_confidence = max(adjusted_confidence, 0.85)

    if (adjusted_confidence != action.confidence
            or adjusted_automation != action.automation_ready
            or adjusted_verb is not action.verb
            or adjusted_target != action.target_label
            or adjusted_kind is not action.target_kind
            or adjusted_reasoning != action.reasoning):
        action = action.model_copy(update={
            "verb": adjusted_verb,
            "target_label": adjusted_target,
            "target_kind": adjusted_kind,
            "confidence": adjusted_confidence,
            "automation_ready": adjusted_automation,
            "reasoning": adjusted_reasoning,
        })

    return action, evidence


# ── Fallback: deterministic action from existing controls ─────────────────────


def _deterministic_action(inputs: _SceneInputs) -> SceneAction | None:
    """Build a low-confidence action from existing evidence_controls.

    Used when the LLM is unavailable or returns an unparseable response,
    so the row stays populated and the scene is auditable.  Limited:
    can only produce verb=click/type with target from controls — no
    value extraction without the LLM.
    """
    if not inputs.existing_control_values:
        return None
    label, value = inputs.existing_control_values[0]
    if value:
        verb = SceneActionVerb.TYPE
        target_kind = SceneActionTargetKind.TEXT_FIELD
    else:
        verb = SceneActionVerb.CLICK
        target_kind = SceneActionTargetKind.BUTTON
    return SceneAction(
        verb=verb,
        target_label=label[:500],
        target_kind=target_kind,
        value=value[:1000] if value else None,
        confidence=0.4,
        automation_ready=False,
        reasoning=(
            "Deterministic fallback from evidence_controls — LLM unavailable "
            "or returned no structured output."
        ),
    )


# ── LLM invocation ────────────────────────────────────────────────────────────


@dataclass
class _ExtractionResult:
    """One scene's extracted actions (1 or more) plus metadata for persistence.

    Phase 1.6 — ``actions`` is a LIST so long-form-fill scenes can
    produce multiple rows.  Single-action scenes still return a list
    of length 1 (or 0 when no action was detected).
    """

    scene_id: str
    actions: list[SceneAction]
    evidence: ExtractionEvidence
    model: str
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int


# ── Frame fetching + downsizing ───────────────────────────────────────────────


async def _fetch_frame_bytes(
    http_client: httpx.AsyncClient,
    asset_path: str,
    *,
    auth_token: str,
) -> bytes | None:
    """GET a raw frame PNG from eyes-engine.

    Uses the same HTTP endpoint and auth model as FrameAnnotator.

    Auth: eyes-engine accepts the JWT as a query-string ``?token=``
    parameter (matches the format the frontend uses for annotated.png
    URLs).  Bearer headers are NOT accepted on this route — sending
    via ``Authorization: Bearer ...`` results in 401.  Send token as
    BOTH a Bearer header AND a query param for defense in depth.

    Returns ``None`` on any transport/HTTP error so the caller can
    degrade.
    """
    if not asset_path:
        return None
    safe_path = quote(asset_path.lstrip("/"), safe="/")
    url = f"{_EYES_BASE_URL}/api/v1/eyes/frames/{safe_path}"
    params: dict[str, str] = {}
    headers: dict[str, str] = {}
    if auth_token:
        params["token"] = auth_token            # what eyes-engine actually checks
        headers["Authorization"] = f"Bearer {auth_token}"  # belt + suspenders
    try:
        response = await http_client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        logger.warning(
            "storyboard.action_extractor.frame_transport_error",
            extra={
                "asset_path": asset_path,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            },
        )
        return None
    if response.status_code >= 400:
        logger.warning(
            "storyboard.action_extractor.frame_http_error",
            extra={
                "asset_path": asset_path,
                "status": response.status_code,
            },
        )
        return None
    return response.content


def _maybe_downsize(data: bytes, max_dimension_px: int) -> tuple[bytes, str]:
    """Normalize an image to JPEG bytes, downsizing if needed.

    Returns ``(bytes, media_type)`` so the caller can attach the right
    ``ImageContent.media_type``.

    Why JPEG instead of PNG: PNG of a 1568x1568 UI screenshot is
    routinely 3-5 MB.  Anthropic's vision API has a 5 MB per-image
    limit and a soft aggregate-payload limit — two large PNGs can
    silently 400 the request without a clear error.  JPEG q85 of the
    same screenshot is typically 150-400 KB with negligible UI-text
    fidelity loss (Sonnet reads JPEG just as well as PNG).

    Also caps the larger dimension at ``max_dimension_px``.  When
    Pillow is unavailable the bytes pass through and we fall back to
    declaring image/png (best guess; downstream provider may reject).
    """
    if not data or not _PIL_AVAILABLE:
        return data, "image/png"
    try:
        with _PILImage.open(io.BytesIO(data)) as img:
            # Convert to RGB — JPEG can't encode RGBA / palette modes.
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
    except Exception as exc:  # pragma: no cover - Pillow decode failure
        logger.warning(
            "storyboard.action_extractor.image_resize_failed",
            extra={"error": str(exc)[:200]},
        )
        return data, "image/png"


# ── LLM tool definition (structured output) ───────────────────────────────────


def _scene_action_tool() -> ToolDefinition:
    """Build the LLM tool that forces a SceneActionList-shaped response.

    The tool's ``input_schema`` is generated from the SceneActionList
    Pydantic model so changes to either ``SceneAction`` or the list
    wrapper propagate to the LLM contract automatically.

    Single-action scenes (clicks, navigations) return a list of length
    1.  Long form-fill scenes return a list of N where N is the number
    of distinct user actions the LLM identified across the sampled
    frames.  Empty list = no detectable user action.
    """
    return ToolDefinition(
        name="record_scene_actions",
        description=(
            "Record EVERY distinct user action visible across the "
            "scene's frames, in chronological order.  Single click "
            "/ navigation scenes return a list of length 1.  Long "
            "form-fill scenes (10+ seconds where the user changes "
            "several fields on the same page) return a list with one "
            "entry per field change.  Return an empty list when no "
            "user action is detectable."
        ),
        input_schema=SceneActionList.model_json_schema(),
    )


# ── LLM invocation ────────────────────────────────────────────────────────────


async def _extract_one_scene(
    inputs: _SceneInputs,
    *,
    router: LLMRouter,
    config: ActionExtractorConfig,
    semaphore: asyncio.Semaphore,
    http_client: httpx.AsyncClient,
    auth_token: str,
) -> _ExtractionResult:
    """Run one LLM call + reconciliation for a single scene.

    Multimodal flow:
      1. Fetch before/after frame bytes from eyes-engine.
      2. Downsize to fit Anthropic's 1568x1568 tile budget.
      3. Build CompletionRequest with images + SceneAction tool +
         prompt caching enabled.
      4. Call router.complete; expect a tool_use response.
      5. Validate the tool arguments through Pydantic.
      6. Reconcile against existing pipeline signals.

    Falls back to deterministic action (no value capture) on any LLM
    failure so the row is always populated.
    """
    # Skip noise scenes per config — they would burn tokens for no value.
    if inputs.panel_is_noise and config.skip_noise:
        return _ExtractionResult(
            scene_id=inputs.scene_id,
            actions=[],
            evidence=ExtractionEvidence(sources=["skipped_noise"]),
            model="(skipped:noise)",
            latency_ms=0,
            prompt_tokens=0,
            completion_tokens=0,
        )

    # ── Fetch + downsize frame images.
    #
    # For short scenes we send 2 frames (before + after).  For long
    # scenes (≥ LONG_SCENE_THRESHOLD_S) we ALSO send up to
    # INTRA_SCENE_FRAME_COUNT intra-scene frames so Sonnet can detect
    # field-by-field changes in form-fill pages.  Image order is
    # chronological (before → intra-frames → after) so the LLM reads
    # the sequence naturally.
    images: list[ImageContent] = []
    if config.include_before_frame and inputs.before_frame_asset_path:
        raw = await _fetch_frame_bytes(
            http_client, inputs.before_frame_asset_path, auth_token=auth_token,
        )
        if raw:
            raw, media_type = _maybe_downsize(raw, config.image_max_dimension_px)
            images.append(ImageContent(data=raw, media_type=media_type))
    # Intra-scene frames — only populated for long scenes by
    # _collect_scene_inputs.  Empty list for short scenes.
    for path in inputs.intra_scene_frame_paths:
        raw = await _fetch_frame_bytes(http_client, path, auth_token=auth_token)
        if raw:
            raw, media_type = _maybe_downsize(raw, config.image_max_dimension_px)
            images.append(ImageContent(data=raw, media_type=media_type))
    if inputs.after_frame_asset_path:
        raw = await _fetch_frame_bytes(
            http_client, inputs.after_frame_asset_path, auth_token=auth_token,
        )
        if raw:
            raw, media_type = _maybe_downsize(raw, config.image_max_dimension_px)
            images.append(ImageContent(data=raw, media_type=media_type))

    # When ALL frame fetches failed we can't do meaningful vision
    # extraction — fall back to deterministic + log so ops sees the
    # frame-fetch gap.
    if not images:
        logger.warning(
            "storyboard.action_extractor.no_frames_available",
            extra={
                "scene_id": inputs.scene_id,
                "before_path": inputs.before_frame_asset_path,
                "after_path": inputs.after_frame_asset_path,
                "intra_count": len(inputs.intra_scene_frame_paths),
            },
        )
        fallback = _deterministic_action(inputs)
        if fallback is None:
            return _ExtractionResult(
                scene_id=inputs.scene_id,
                actions=[],
                evidence=ExtractionEvidence(sources=["no_frames"]),
                model="(no_frames)",
                latency_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
            )
        action, evidence = _reconcile(fallback, inputs)
        evidence.sources.append("no_frames_fallback")
        return _ExtractionResult(
            scene_id=inputs.scene_id,
            actions=[action],
            evidence=evidence,
            model="(deterministic:no_frames)",
            latency_ms=0,
            prompt_tokens=0,
            completion_tokens=0,
        )

    tool = _scene_action_tool()

    async with semaphore:
        started_ms = int(time.monotonic() * 1000)

        request = CompletionRequest(
            system=_SYSTEM_PROMPT,
            prompt=_build_user_prompt(inputs),
            max_tokens=config.llm_max_tokens,
            temperature=config.llm_temperature,
            request_timeout_s=config.request_timeout_s,
            correlation_id=inputs.scene_id,
            images=tuple(images),
            tools=(tool,),
            tool_choice=tool.name,
            enable_prompt_cache=True,
            metadata={
                "task": config.llm_task_name,
                "scene_index": inputs.scene_index,
                "image_count": len(images),
            },
        )
        response = await router.complete(task=config.llm_task_name, request=request)
        latency_ms = int(time.monotonic() * 1000) - started_ms

    # ── Provider errored or returned nothing usable.
    if response.finish_reason == FinishReason.ERROR or (
        not response.tool_calls and not response.text
    ):
        # Emit to BOTH structlog and root logger so the detail is
        # visible in `docker logs` regardless of formatter config.
        msg = (
            f"storyboard.action_extractor.llm_error scene={inputs.scene_id} "
            f"provider={response.provider} model={response.model} "
            f"finish={response.finish_reason.value} "
            f"prompt_tokens={response.prompt_tokens} "
            f"completion_tokens={response.completion_tokens} "
            f"image_count={len(images)} "
            f"image_bytes_total={sum(len(i.data) for i in images)} "
            f"fell_back={response.fell_back} "
            f"error_detail={response.error_detail[:400]!r}"
        )
        logger.warning(msg)
        return _fallback_result(
            inputs, response, latency_ms, evidence_marker="llm_failed_fallback",
        )

    # ── Parse the tool_use response into a SceneActionList.
    action_list: SceneActionList | None = None
    if response.tool_calls:
        for call in response.tool_calls:
            if call.name == tool.name:
                try:
                    action_list = SceneActionList.model_validate(call.arguments)
                    break
                except ValueError as exc:
                    logger.warning(
                        "storyboard.action_extractor.invalid_tool_args",
                        extra={
                            "scene_id": inputs.scene_id,
                            "args_preview": json.dumps(call.arguments)[:300],
                            "error": str(exc)[:200],
                        },
                    )

    # ── Fallback: tool_use missing but text returned — try JSON in text.
    if action_list is None and response.text:
        try:
            payload = _strip_code_fence(response.text)
            payload_dict = json.loads(payload)
            # Accept either {"actions": [...]} or a bare list / single action.
            if isinstance(payload_dict, list):
                action_list = SceneActionList(actions=[
                    SceneAction.model_validate(a) for a in payload_dict
                ])
            elif isinstance(payload_dict, dict) and "actions" in payload_dict:
                action_list = SceneActionList.model_validate(payload_dict)
            else:
                action_list = SceneActionList(actions=[
                    SceneAction.model_validate(payload_dict),
                ])
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "storyboard.action_extractor.invalid_text_output",
                extra={
                    "scene_id": inputs.scene_id,
                    "raw_text": response.text[:300],
                    "error": str(exc)[:200],
                },
            )

    if action_list is None:
        return _fallback_result(
            inputs, response, latency_ms, evidence_marker="llm_invalid_fallback",
        )

    # Reconcile each action against existing pipeline signals.  The
    # evidence record is computed once per scene (shared across actions
    # since they all share the same scene context) — individual confidence
    # / automation_ready adjustments happen per action.
    reconciled_actions: list[SceneAction] = []
    aggregate_evidence: ExtractionEvidence | None = None
    for action in action_list.actions:
        adjusted, evidence = _reconcile(action, inputs)
        if adjusted.confidence < config.min_confidence_for_automation:
            adjusted = adjusted.model_copy(update={"automation_ready": False})
        reconciled_actions.append(adjusted)
        if aggregate_evidence is None:
            aggregate_evidence = evidence

    # Phase 1.6 — when the LLM returned an empty list, write at least
    # one placeholder row so the scene is still visible in the journey
    # (a missing scene in the bottom panel looks like a UI bug to the
    # user).  The placeholder is a verb=none action that may still get
    # promoted to verb=navigate by the reconciliation override when
    # the URL changed.
    if not reconciled_actions:
        placeholder = SceneAction(
            verb=SceneActionVerb.NONE,
            target_label="",
            target_kind=SceneActionTargetKind.OTHER,
            value=None,
            confidence=0.40,
            automation_ready=False,
            reasoning=(
                "LLM returned an empty action list for this scene — "
                "frames likely look similar.  Surfaced as a placeholder "
                "so the journey is not visually broken."
            ),
        )
        adjusted, evidence = _reconcile(placeholder, inputs)
        reconciled_actions.append(adjusted)
        if aggregate_evidence is None:
            aggregate_evidence = evidence
            aggregate_evidence.sources.append("empty_list_placeholder")

    if aggregate_evidence is None:
        aggregate_evidence = ExtractionEvidence(
            sources=["before_frame", "after_frame", "ocr_text"],
        )

    return _ExtractionResult(
        scene_id=inputs.scene_id,
        actions=reconciled_actions,
        evidence=aggregate_evidence,
        model=f"{response.provider}/{response.model}",
        latency_ms=latency_ms,
        prompt_tokens=response.prompt_tokens or 0,
        completion_tokens=response.completion_tokens or 0,
    )


def _fallback_result(
    inputs: _SceneInputs,
    response,
    latency_ms: int,
    *,
    evidence_marker: str,
) -> _ExtractionResult:
    """Build a fallback result when the LLM fails or returns garbage.

    Centralised so all failure paths look the same — same evidence
    marker pattern, same model string, same metadata.  Phase 1.6 —
    ALWAYS returns at least one action so the scene is never silently
    dropped from the journey.  Order of preference:
      1. Deterministic action from evidence_controls (when available)
      2. verb=none placeholder (reconciled — may promote to navigate
         when URL changed)
    """
    fallback = _deterministic_action(inputs)
    if fallback is None:
        # Phase 1.6 safety net: emit a verb=none placeholder so every
        # non-noise scene gets at least one row in the journey.  The
        # reconciliation step may promote verb=none → verb=navigate
        # when the URL changed, giving the scene a useful label even
        # though the LLM call itself produced nothing.
        fallback = SceneAction(
            verb=SceneActionVerb.NONE,
            target_label="",
            target_kind=SceneActionTargetKind.OTHER,
            value=None,
            confidence=0.30,
            automation_ready=False,
            reasoning=(
                "LLM call failed and no controls were available for a "
                "deterministic fallback — emitting placeholder so the "
                "scene is still visible in the journey."
            ),
        )
    action, evidence = _reconcile(fallback, inputs)
    evidence.sources.append(evidence_marker)
    return _ExtractionResult(
        scene_id=inputs.scene_id,
        actions=[action],
        evidence=evidence,
        model=f"{response.provider}/{response.model}",
        latency_ms=latency_ms,
        prompt_tokens=response.prompt_tokens or 0,
        completion_tokens=response.completion_tokens or 0,
    )


def _strip_code_fence(text: str) -> str:
    """Strip optional ```json ... ``` markdown fences from LLM output.

    Some providers wrap JSON in code fences despite being told not to.
    Be lenient on input, strict on validation.
    """
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text[: -3].rstrip()
    return text


# ── Persistence ───────────────────────────────────────────────────────────────


async def _upsert_actions(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    results: Sequence[_ExtractionResult],
    config: ActionExtractorConfig,
) -> int:
    """UPSERT one row per extracted action.

    Phase 1.6 — a scene may contribute MULTIPLE rows (one per
    SceneAction in its ``actions`` list).  Each row gets a distinct
    ``action_id`` (uuid5 of scene_id + version + subaction_index) so
    multi-action scenes don't collide and re-runs at the same version
    UPSERT cleanly.

    The latency / token attributes are stamped on every row of the
    same scene — they describe the SCENE's LLM call, not a per-action
    cost.  Operators summing tokens by scene must DISTINCT on action_id
    to avoid double-counting.
    """
    rows: list[dict] = []
    for r in results:
        for sub_idx, action in enumerate(r.actions):
            rows.append({
                "action_id": _action_id(r.scene_id, config.version, sub_idx),
                "scene_id": r.scene_id,
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "subaction_index": sub_idx,
                "verb": action.verb.value,
                "target_label": action.target_label[:500],
                "target_kind": action.target_kind.value,
                "value": (action.value[:1000] if action.value else None),
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

    stmt = pg_insert(SceneActionRow.__table__).values(rows)
    update_columns = {
        col.name: stmt.excluded[col.name]
        for col in SceneActionRow.__table__.columns
        if col.name not in {"action_id", "created_at"}
    }
    update_columns["updated_at"] = stmt.excluded.updated_at
    upsert = stmt.on_conflict_do_update(
        index_elements=[SceneActionRow.__table__.c.action_id],
        set_=update_columns,
    )
    await session.execute(upsert)
    return len(rows)


# ── Public entry point ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActionExtractorResult:
    """Summary returned to the composer."""

    artifact_id: str
    scenes_attempted: int
    scenes_extracted: int
    scenes_skipped_noise: int
    scenes_failed: int
    actions_written: int
    elapsed_ms: int


async def extract_actions_for_artifact(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    config: ActionExtractorConfig,
    router: LLMRouter,
    auth_token: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> ActionExtractorResult:
    """Run action extraction for one artifact.

    Idempotent.  Safe to call after every pipeline run.  Caller must
    have set the tenant RLS context on the session.  Does not commit.

    ``auth_token`` is the caller's JWT, forwarded to eyes-engine so
    frame fetches respect tenant isolation.

    ``http_client`` is optional — when None a short-lived client is
    created for this run.  Callers that handle many artifacts (the
    composer) should pass a long-lived client to amortise the TCP
    handshake cost.
    """
    started = time.monotonic()

    inputs = await _collect_scene_inputs(
        session, artifact_id=artifact_id, tenant_id=tenant_id,
    )
    if not inputs:
        return ActionExtractorResult(
            artifact_id=artifact_id,
            scenes_attempted=0,
            scenes_extracted=0,
            scenes_skipped_noise=0,
            scenes_failed=0,
            actions_written=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    semaphore = asyncio.Semaphore(max(1, config.concurrency))

    # Create a short-lived client when the caller didn't supply one.
    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                config.request_timeout_s
                if config.request_timeout_s
                else 30.0
            ),
            follow_redirects=True,
        )

    try:
        gathered: list[_ExtractionResult] = await asyncio.wait_for(
            asyncio.gather(*(
                _extract_one_scene(
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
            "storyboard.action_extractor.total_timeout",
            extra={
                "artifact_id": artifact_id,
                "timeout_s": config.total_timeout_s,
                "scenes_pending": len(inputs),
            },
        )
        if owns_client:
            await http_client.aclose()
        return ActionExtractorResult(
            artifact_id=artifact_id,
            scenes_attempted=len(inputs),
            scenes_extracted=0,
            scenes_skipped_noise=0,
            scenes_failed=len(inputs),
            actions_written=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        if owns_client and http_client is not None:
            try:
                await http_client.aclose()
            except Exception:  # pragma: no cover - cleanup
                pass

    skipped = sum(
        1 for r in gathered
        if not r.actions and "skipped_noise" in r.evidence.sources
    )
    failed = sum(
        1 for r in gathered
        if not r.actions and "skipped_noise" not in r.evidence.sources
    )
    extracted = sum(1 for r in gathered if r.actions)

    actions_written = await _upsert_actions(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        results=gathered,
        config=config,
    )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "storyboard.action_extractor.run",
        extra={
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "scenes_attempted": len(inputs),
            "scenes_extracted": extracted,
            "scenes_skipped_noise": skipped,
            "scenes_failed": failed,
            "actions_written": actions_written,
            "elapsed_ms": elapsed_ms,
            "extractor_version": config.version,
        },
    )

    return ActionExtractorResult(
        artifact_id=artifact_id,
        scenes_attempted=len(inputs),
        scenes_extracted=extracted,
        scenes_skipped_noise=skipped,
        scenes_failed=failed,
        actions_written=actions_written,
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    "ActionExtractorResult",
    "extract_actions_for_artifact",
]
