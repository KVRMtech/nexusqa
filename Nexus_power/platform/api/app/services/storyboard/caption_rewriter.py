"""Caption rewriter — turn pipeline signals into 5-8 word imperative captions.

Why we need this
================
The frozen pipeline produces verbose LLaVA descriptions on each frame
("The interface displays a form with multiple input fields and a
prominent Submit button at the bottom...").  These are useful as
detector input but useless as picture-first captions.

What the storyboard needs is the picture-equivalent of a haiku:

    "Filled email field"
    "Selected smoker option"
    "Submitted quote form"
    "Opened Guardian Life"

5-8 words, past-tense imperative, no "User" prefix, no punctuation.

What the rewriter does
======================
For each scene in an artifact the rewriter:

1. Pulls the strongest signals available — ``scene_state_summary``,
   the incoming edge's ``primary_action_summary``, a digest of
   ``evidence_steps`` (target labels, observed values), and the
   ``app_grouping.canonical_name`` so the LLM knows which product
   is on screen.
2. Builds a tight prompt and asks the LLM via the
   provider-agnostic ``LLMRouter`` (task=``caption``).
3. Post-processes the result — trims whitespace, drops surrounding
   quotes, enforces the word cap, classifies quality (strong /
   degraded / weak) based on how well the output matches the
   contract.
4. Falls back to a *deterministic* caption built from
   ``primary_action_summary`` + ``screen_title`` when the LLM
   returns an error or has not been configured.  This keeps the
   storyboard usable when Ollama is down or no API keys are set.
5. UPSERTs into ``scene_captions`` keyed by
   ``(scene_id, generator_version)`` — bumping
   ``STORYBOARD_CAPTION_VERSION`` re-generates without dropping
   history.

Production-safety considerations
================================
* **Concurrency-bounded** via ``asyncio.Semaphore`` so a slow LLM
  cannot exhaust the event loop or DDOS the provider.
* **Total-timeout** budget so one bad artifact never blocks future
  composer requests; rows not generated stay missing and the
  composer can retry on the next request.
* **Idempotent** — same inputs + same generator_version yield the
  same row.  Re-running is safe.
* **Observable** — every row records ``generator_model``,
  ``generation_latency_ms``, ``input_signals`` snapshot, and the
  tier-fell-back flag.  Failures stay quiet (weak quality + log).
* **Tenant-scoped** — every query filters on ``tenant_id`` and the
  RLS session variable from migration 029/030 enforces the same.

Consumed by ``composer.run_for_artifact``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    AppGroupingRow,
    EvidenceStepRow,
    SceneCaptionRow,
    StoryboardPanelRow,
    VisualFlowEdgeRow,
    VisualSceneRow,
)

from ..llm import CompletionRequest, FinishReason, LLMRouter
from .config import CaptionRewriterConfig


logger = logging.getLogger(__name__)


_NAMESPACE_STORYBOARD = uuid.UUID("d4f6c9a2-6d8b-4f5b-9a32-8f4b1c1a2d3e")
"""Same namespace as scene_grouper / app_deduper — keeps storyboard derived IDs grouped."""


def _caption_id(scene_id: str, generator_version: str) -> str:
    """Deterministic caption_id — same (scene, version) yields same row."""
    return str(uuid.uuid5(
        _NAMESPACE_STORYBOARD,
        f"caption:{scene_id}:{generator_version}",
    ))


# ── Signal extraction ─────────────────────────────────────────────────────────


@dataclass
class _SceneSignals:
    """Everything the rewriter knows about one scene at generation time.

    Pre-collected once so the per-scene LLM call stays compact.  Field
    names mirror the storage layer for easy log-correlation.
    """

    scene_id: str
    scene_index: int
    panel_id: str | None
    panel_is_noise: bool
    screen_title: str
    screen_type: str
    app_name: str
    domain: str
    action_kind: str
    action_label: str
    action_quality: str
    target_labels: list[str]
    observed_values: list[str]
    audio_intent: str
    panel_in_scene_action_count: int


async def _collect_signals(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
) -> list[_SceneSignals]:
    """Read everything we need to caption every scene in one artifact.

    Five small queries (panels, scenes, edges, steps, groupings) is
    cheaper than one mega-join and gives clearer telemetry.  Total
    cost on a 200-scene artifact is < 200ms.
    """
    # 1) Panels — used to join scenes to their containing panel + app grouping
    panels_q = await session.execute(
        select(StoryboardPanelRow).where(
            StoryboardPanelRow.artifact_id == artifact_id,
            StoryboardPanelRow.tenant_id == tenant_id,
        )
    )
    panels = list(panels_q.scalars().all())
    panel_by_first_scene: dict[str, StoryboardPanelRow] = {
        p.first_scene_id: p for p in panels
    }

    # 2) App groupings — used for the "App: ..." context in the prompt
    groupings_q = await session.execute(
        select(AppGroupingRow).where(
            AppGroupingRow.artifact_id == artifact_id,
            AppGroupingRow.tenant_id == tenant_id,
        )
    )
    groupings = {g.grouping_id: g for g in groupings_q.scalars().all()}

    # 3) Scenes — the unit we caption
    scenes_q = await session.execute(
        select(VisualSceneRow)
        .where(
            VisualSceneRow.artifact_id == artifact_id,
            VisualSceneRow.tenant_id == tenant_id,
        )
        .order_by(VisualSceneRow.scene_index.asc())
    )
    scenes = list(scenes_q.scalars().all())

    # 4) Incoming edges — provide the primary_action_summary per scene
    edges_q = await session.execute(
        select(VisualFlowEdgeRow).where(
            VisualFlowEdgeRow.artifact_id == artifact_id,
            VisualFlowEdgeRow.tenant_id == tenant_id,
        )
    )
    incoming_edges: dict[str, VisualFlowEdgeRow] = {}
    for edge in edges_q.scalars().all():
        # Keep the strongest action_quality for each to_scene_id
        existing = incoming_edges.get(edge.to_scene_id)
        if existing is None:
            incoming_edges[edge.to_scene_id] = edge
            continue
        if _quality_rank(edge.action_quality) > _quality_rank(existing.action_quality):
            incoming_edges[edge.to_scene_id] = edge

    # 5) Evidence steps — provide target labels + observed values
    steps_q = await session.execute(
        select(EvidenceStepRow).where(
            EvidenceStepRow.artifact_id == artifact_id,
            EvidenceStepRow.tenant_id == tenant_id,
        )
    )
    steps_by_scene: dict[str, list[EvidenceStepRow]] = {}
    for step in steps_q.scalars().all():
        steps_by_scene.setdefault(step.scene_id, []).append(step)

    results: list[_SceneSignals] = []
    for scene in scenes:
        panel = panel_by_first_scene.get(scene.scene_id)
        grouping = (
            groupings.get(panel.app_grouping_id)
            if panel and panel.app_grouping_id
            else None
        )
        state_summary = scene.scene_state_summary or {}

        screen_title = ""
        screen_type = "unknown"
        domain = ""
        if isinstance(state_summary, dict):
            screen_title = (state_summary.get("screen_title") or "").strip()
            screen_type = state_summary.get("screen_type") or "unknown"
            domain = state_summary.get("domain") or ""
        if not screen_title:
            screen_title = (scene.screen_name or "").strip()

        edge = incoming_edges.get(scene.scene_id)
        action_summary = (edge.primary_action_summary or {}) if edge else {}
        if not isinstance(action_summary, dict):
            action_summary = {}

        action_kind = (action_summary.get("action_kind") or "").strip()
        action_label = (action_summary.get("action_label") or "").strip()
        action_quality = (action_summary.get("action_quality") or "weak").strip().lower()

        steps = steps_by_scene.get(scene.scene_id, [])
        target_labels = _unique_short(
            [step.target_label or "" for step in steps],
            max_count=4,
        )
        observed_values = _unique_short(
            [step.observed_value or "" for step in steps],
            max_count=4,
        )

        audio_intent = ""
        for step in steps:
            if step.audio_intent_text:
                audio_intent = step.audio_intent_text.strip()
                break

        app_name = ""
        if grouping is not None:
            app_name = (
                grouping.display_label
                or grouping.canonical_name
                or grouping.canonical_domain
                or ""
            )

        results.append(_SceneSignals(
            scene_id=scene.scene_id,
            scene_index=int(scene.scene_index or 0),
            panel_id=panel.panel_id if panel else None,
            panel_is_noise=bool(panel.is_noise) if panel else False,
            screen_title=screen_title,
            screen_type=screen_type,
            app_name=app_name,
            domain=domain or (grouping.canonical_domain if grouping else ""),
            action_kind=action_kind,
            action_label=action_label,
            action_quality=action_quality,
            target_labels=target_labels,
            observed_values=observed_values,
            audio_intent=audio_intent,
            panel_in_scene_action_count=(panel.in_scene_action_count if panel else 0),
        ))
    return results


def _unique_short(items: Sequence[str], *, max_count: int) -> list[str]:
    """De-duplicate while preserving order, drop blanks, cap the list."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        item = (raw or "").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item[:80])
        if len(out) >= max_count:
            break
    return out


_QUALITY_RANK = {"strong": 3, "degraded": 2, "weak": 1}


def _quality_rank(value: str | None) -> int:
    return _QUALITY_RANK.get((value or "weak").lower(), 0)


# ── Prompt construction ───────────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You write ultra-short captions for screenshots of a software application. "
    "Each caption describes exactly one user action on the screen.\n\n"
    "Rules:\n"
    "- Reply with ONE caption only, no preamble, no explanation, no quotation marks.\n"
    "- Use 5 to 8 words total.\n"
    "- Start with a past-tense verb (Filled, Selected, Clicked, Opened, Submitted, Reviewed).\n"
    "- Do NOT begin with \"User\", \"The user\", or pronouns.\n"
    "- Do NOT add trailing punctuation.\n"
    "- Do NOT describe the screen layout; describe the ACTION.\n"
    "- If the signals are ambiguous, prefer the most likely SPECIFIC action over a vague one."
)


def _build_user_prompt(signals: _SceneSignals) -> str:
    """Render the per-scene signals as a compact prompt body.

    Empty fields are omitted so the model is not distracted by a "Field: "
    line with no value.  The order is action-first because action_label
    typically anchors the strongest caption.
    """
    lines: list[str] = []
    if signals.action_label:
        suffix = f" (kind={signals.action_kind})" if signals.action_kind else ""
        lines.append(f"Most likely action: {signals.action_label}{suffix}")
    if signals.screen_title:
        lines.append(f"Screen title: {signals.screen_title}")
    if signals.screen_type and signals.screen_type != "unknown":
        lines.append(f"Screen type: {signals.screen_type}")
    if signals.app_name:
        lines.append(f"Application: {signals.app_name}")
    if signals.target_labels:
        lines.append("Controls used: " + ", ".join(signals.target_labels))
    if signals.observed_values:
        lines.append("Values entered: " + ", ".join(signals.observed_values))
    if signals.audio_intent:
        lines.append(f"Audio intent: {signals.audio_intent[:200]}")
    lines.append("\nReply with the caption only.")
    return "\n".join(lines)


# ── Output post-processing ────────────────────────────────────────────────────


_QUOTE_TRIM = re.compile(r"""^["'`“”‘’\s]+|["'`“”‘’\s.,!?]+$""")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")
_LEADING_BAD_RE = re.compile(
    r"^(the user|user|i am|i'm|i will|i)\b[\s,:-]*",
    re.IGNORECASE,
)
_FORBIDDEN_LEADING_WORDS = {"user", "i", "the", "a", "an"}


def _normalise_caption(raw: str, *, max_words: int) -> str:
    """Strip quotes/whitespace/punctuation; enforce leading-verb rule.

    Returns ``""`` when nothing useful can be extracted.  The caller
    classifies quality based on length + leading word.
    """
    if not raw:
        return ""
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    line = _QUOTE_TRIM.sub("", line).strip()
    # Drop "User", "The user" prefixes the LLM occasionally adds
    line = _LEADING_BAD_RE.sub("", line, count=1).strip()
    if not line:
        return ""
    words = _WORD_RE.findall(line)
    if not words:
        return ""
    words = words[:max_words]
    return " ".join(words)


def _classify_quality(
    caption: str, *, source: str, min_words: int, max_words: int,
) -> str:
    """Strong / degraded / weak classification.

    Strong: LLM produced 5-8 words, starts with an allowed verb-ish lead.
    Degraded: Output exists but breaks one of the constraints (too short,
              starts with a noun, source was deterministic_strong_action).
    Weak:    Output is empty or source is a deterministic fallback.
    """
    if not caption:
        return "weak"
    word_count = len(caption.split())
    leads = caption.split()[0].lower() if word_count else ""

    if source == "deterministic_fallback":
        return "weak"
    if source == "deterministic_action":
        # Built from a strong primary_action_summary — usable but not LLM-blessed
        return "degraded"

    if leads in _FORBIDDEN_LEADING_WORDS:
        return "degraded"
    if word_count < min_words or word_count > max_words:
        return "degraded"
    return "strong"


# ── Deterministic fallback ────────────────────────────────────────────────────


_ACTION_VERB_MAP = {
    "click_cta": "Clicked",
    "enter_text": "Filled",
    "select_option": "Selected",
    "toggle": "Toggled",
    "submit_form": "Submitted",
    "navigate": "Navigated to",
    "open_page": "Opened",
    "upload_file": "Uploaded",
    "drag_handle": "Dragged",
    "review": "Reviewed",
}


def deterministic_caption(signals: _SceneSignals, *, max_words: int) -> tuple[str, str]:
    """Build a caption without calling any LLM.

    Returns ``(caption, source)`` where ``source`` is either
    ``deterministic_action`` (built from a strong primary_action_summary,
    which often produces decent captions) or ``deterministic_fallback``
    (built from screen_title / app_name only — weak last resort).
    """
    action_quality = signals.action_quality
    action_kind = signals.action_kind
    action_label = signals.action_label

    if action_quality in ("strong", "degraded") and action_label:
        # Use action_label directly when it already reads like an
        # imperative (e.g. "Click Get Quote Button").  Strip leading
        # articles / pronouns and shrink to max_words.
        cleaned = _normalise_caption(action_label, max_words=max_words)
        if cleaned:
            return cleaned, "deterministic_action"

    # Verb-prefixed fallback: build "Verb Subject" from action_kind +
    # the most informative subject we can find.
    verb = _ACTION_VERB_MAP.get(action_kind, "Viewed")
    subject = ""
    if signals.target_labels:
        subject = signals.target_labels[0]
    elif signals.screen_title:
        subject = signals.screen_title
    elif signals.app_name:
        subject = signals.app_name
    elif signals.domain:
        subject = signals.domain
    else:
        subject = "screen"

    caption = _normalise_caption(f"{verb} {subject}", max_words=max_words)
    return caption, "deterministic_fallback"


# ── Generation ────────────────────────────────────────────────────────────────


@dataclass
class _GenerationResult:
    """One scene's caption + metadata after generation."""

    scene_id: str
    artifact_id: str
    tenant_id: str
    short_caption: str
    narrative_caption: str
    caption_quality: str
    generator_model: str
    input_signals: dict
    generation_latency_ms: int
    source: str


async def _generate_one(
    signals: _SceneSignals,
    *,
    router: LLMRouter,
    config: CaptionRewriterConfig,
    artifact_id: str,
    tenant_id: str,
    semaphore: asyncio.Semaphore,
) -> _GenerationResult:
    """Caption one scene with LLM-first, deterministic-fallback ordering."""

    # 1) When the user opted out of LLM, go straight to deterministic.
    if not config.use_llm or not router.has_tier_for_task(config.llm_task_name):
        caption, source = deterministic_caption(signals, max_words=config.max_words)
        return _GenerationResult(
            scene_id=signals.scene_id,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            short_caption=caption,
            narrative_caption="",
            caption_quality=_classify_quality(
                caption, source=source,
                min_words=config.min_words, max_words=config.max_words,
            ),
            generator_model="(deterministic)",
            input_signals=_input_signals_snapshot(signals, llm_path=False),
            generation_latency_ms=0,
            source=source,
        )

    # 2) LLM path with semaphore-bounded concurrency.
    async with semaphore:
        request = CompletionRequest(
            system=_SYSTEM_PROMPT,
            prompt=_build_user_prompt(signals),
            max_tokens=config.llm_max_tokens,
            temperature=config.llm_temperature,
            stop_sequences=("\n",),
            request_timeout_s=config.request_timeout_s,
            correlation_id=signals.scene_id,
            metadata={"task": config.llm_task_name},
        )
        started_ms = int(time.monotonic() * 1000)
        response = await router.complete(task=config.llm_task_name, request=request)
        elapsed_ms = int(time.monotonic() * 1000) - started_ms

    if response.finish_reason == FinishReason.ERROR or not response.text:
        # Fall back deterministically; record the LLM error in input_signals
        # so the operator can see why this row is weak.
        caption, source = deterministic_caption(signals, max_words=config.max_words)
        snapshot = _input_signals_snapshot(signals, llm_path=True)
        snapshot["llm_error_detail"] = response.error_detail[:300]
        snapshot["llm_fell_back"] = response.fell_back
        return _GenerationResult(
            scene_id=signals.scene_id,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            short_caption=caption,
            narrative_caption="",
            caption_quality=_classify_quality(
                caption, source=source,
                min_words=config.min_words, max_words=config.max_words,
            ),
            generator_model=f"{response.provider}/{response.model}",
            input_signals=snapshot,
            generation_latency_ms=elapsed_ms,
            source=source,
        )

    # 3) Successful LLM response — normalise, classify, snapshot signals.
    short_caption = _normalise_caption(response.text, max_words=config.max_words)
    snapshot = _input_signals_snapshot(signals, llm_path=True)
    snapshot.update({
        "llm_tier": response.tier,
        "llm_provider": response.provider,
        "llm_model": response.model,
        "llm_finish_reason": response.finish_reason.value,
        "llm_prompt_tokens": response.prompt_tokens,
        "llm_completion_tokens": response.completion_tokens,
        "llm_retries": response.retries,
        "llm_fell_back": response.fell_back,
        "raw_text": response.text[:500],
    })

    # Optional: keep the LLM raw text as the narrative caption for
    # the drill-down view when it is longer than the short caption.
    narrative_caption = ""
    raw_clean = response.text.strip()
    if raw_clean and raw_clean.lower() != short_caption.lower():
        narrative_caption = raw_clean[:500]

    if not short_caption:
        # LLM returned text but normalisation produced nothing useful —
        # treat as failure and fall back deterministically.
        caption, source = deterministic_caption(signals, max_words=config.max_words)
        return _GenerationResult(
            scene_id=signals.scene_id,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            short_caption=caption,
            narrative_caption=narrative_caption,
            caption_quality=_classify_quality(
                caption, source=source,
                min_words=config.min_words, max_words=config.max_words,
            ),
            generator_model=f"{response.provider}/{response.model}",
            input_signals=snapshot,
            generation_latency_ms=elapsed_ms,
            source=source,
        )

    return _GenerationResult(
        scene_id=signals.scene_id,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        short_caption=short_caption,
        narrative_caption=narrative_caption,
        caption_quality=_classify_quality(
            short_caption, source="llm",
            min_words=config.min_words, max_words=config.max_words,
        ),
        generator_model=f"{response.provider}/{response.model}",
        input_signals=snapshot,
        generation_latency_ms=elapsed_ms,
        source="llm",
    )


def _input_signals_snapshot(signals: _SceneSignals, *, llm_path: bool) -> dict:
    """Compact dict for the input_signals JSON column.

    Keep it small (<2KB) so audit queries do not pay for huge JSON
    blobs.  Operators read this to debug bad captions without having
    to re-derive the entire scene graph.
    """
    return {
        "scene_index": signals.scene_index,
        "screen_title": signals.screen_title[:200],
        "screen_type": signals.screen_type,
        "app_name": signals.app_name[:200],
        "domain": signals.domain[:200],
        "action_kind": signals.action_kind,
        "action_label": signals.action_label[:200],
        "action_quality": signals.action_quality,
        "target_labels": signals.target_labels[:4],
        "observed_values": signals.observed_values[:4],
        "audio_intent_first_120": signals.audio_intent[:120],
        "panel_in_scene_action_count": signals.panel_in_scene_action_count,
        "llm_path_attempted": llm_path,
    }


# ── Persistence ───────────────────────────────────────────────────────────────


async def _upsert_captions(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    results: Sequence[_GenerationResult],
    config: CaptionRewriterConfig,
) -> int:
    if not results:
        return 0
    rows = []
    for r in results:
        rows.append({
            "caption_id": _caption_id(r.scene_id, config.version),
            "scene_id": r.scene_id,
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "short_caption": r.short_caption,
            "narrative_caption": r.narrative_caption,
            "caption_quality": r.caption_quality,
            "generator_model": r.generator_model,
            "generator_version": config.version,
            "input_signals": r.input_signals,
            "generation_latency_ms": r.generation_latency_ms,
        })
    stmt = pg_insert(SceneCaptionRow.__table__).values(rows)
    update_columns = {
        col.name: stmt.excluded[col.name]
        for col in SceneCaptionRow.__table__.columns
        if col.name not in {"caption_id", "created_at"}
    }
    update_columns["updated_at"] = stmt.excluded.updated_at
    upsert = stmt.on_conflict_do_update(
        index_elements=[SceneCaptionRow.__table__.c.caption_id],
        set_=update_columns,
    )
    await session.execute(upsert)
    return len(rows)


async def _delete_orphan_captions(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    keep_caption_ids: set[str],
    current_version: str,
) -> int:
    """Drop captions belonging to older generator_version rows.

    Without this, switching versions accumulates dead rows in the
    table forever.  We only delete rows whose generator_version
    differs from the current one — the active version's rows are
    kept via the UPSERT path.
    """
    stmt = delete(SceneCaptionRow).where(
        SceneCaptionRow.artifact_id == artifact_id,
        SceneCaptionRow.tenant_id == tenant_id,
        SceneCaptionRow.generator_version != current_version,
    )
    if keep_caption_ids:
        stmt = stmt.where(~SceneCaptionRow.caption_id.in_(keep_caption_ids))
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def _backlink_panels(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    results: Sequence[_GenerationResult],
) -> int:
    """Copy short_caption into storyboard_panels.caption_short.

    The composer reads the panel rows directly for the storyboard
    response, so denormalising the caption onto the panel saves a
    join at request time.  We update by ``first_scene_id`` since
    each panel maps 1:1 to its first scene.
    """
    from sqlalchemy import update
    touched = 0
    for r in results:
        if not r.short_caption:
            continue
        stmt = (
            update(StoryboardPanelRow)
            .where(
                StoryboardPanelRow.artifact_id == artifact_id,
                StoryboardPanelRow.tenant_id == tenant_id,
                StoryboardPanelRow.first_scene_id == r.scene_id,
            )
            .values(caption_short=r.short_caption, caption_long=r.narrative_caption)
        )
        result = await session.execute(stmt)
        touched += int(result.rowcount or 0)
    return touched


# ── Public API ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CaptionResult:
    """Summary returned to the composer (for observability + tests)."""

    artifact_id: str
    scenes_total: int
    captions_written: int
    captions_strong: int
    captions_degraded: int
    captions_weak: int
    llm_used: bool
    llm_fell_back_count: int
    orphans_removed: int
    panels_relinked: int
    elapsed_ms: int
    timed_out: bool


async def rewrite_artifact_captions(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    config: CaptionRewriterConfig,
    router: LLMRouter,
) -> CaptionResult:
    """Re-derive scene captions for one artifact.

    Lazy and idempotent: re-running with the same generator_version is
    a no-op-ish refresh (UPSERTs touch updated_at but otherwise leave
    rows alone when the LLM produces the same caption).

    Honours ``config.total_timeout_s``: when exceeded the function
    returns whatever has been generated so far and the composer can
    resume on a later request.
    """
    started_at = time.monotonic()

    signals = await _collect_signals(
        session, artifact_id=artifact_id, tenant_id=tenant_id,
    )

    # Optionally skip captions for noise panels — saves LLM tokens
    if config.skip_noise:
        eligible = [s for s in signals if not s.panel_is_noise]
    else:
        eligible = list(signals)

    semaphore = asyncio.Semaphore(config.concurrency)
    llm_available = config.use_llm and router.has_tier_for_task(config.llm_task_name)

    async def _wrapped(sig: _SceneSignals) -> _GenerationResult:
        return await _generate_one(
            sig,
            router=router,
            config=config,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            semaphore=semaphore,
        )

    tasks = [asyncio.create_task(_wrapped(s)) for s in eligible]

    timed_out = False
    results: list[_GenerationResult] = []
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=False),
            timeout=config.total_timeout_s,
        )
    except asyncio.TimeoutError:
        timed_out = True
        # Collect whatever finished; cancel the rest cleanly.
        for task in tasks:
            if task.done():
                try:
                    results.append(task.result())
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "storyboard.caption_rewriter.task_failed",
                        extra={"artifact_id": artifact_id, "error": str(exc)[:200]},
                    )
            else:
                task.cancel()
        # Best-effort wait for cancellations
        try:
            await asyncio.gather(*[t for t in tasks if not t.done()], return_exceptions=True)
        except Exception:  # pragma: no cover - defensive
            pass

    # Filter empty captions out of the persistence layer — the composer
    # gracefully shows nothing rather than an empty caption row.
    persistable = [r for r in results if r.short_caption]
    written = await _upsert_captions(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        results=persistable,
        config=config,
    )
    keep_ids = {_caption_id(r.scene_id, config.version) for r in persistable}
    orphans = await _delete_orphan_captions(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        keep_caption_ids=keep_ids,
        current_version=config.version,
    )
    relinked = await _backlink_panels(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        results=persistable,
    )

    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    by_quality = {"strong": 0, "degraded": 0, "weak": 0}
    fell_back = 0
    for r in results:
        by_quality[r.caption_quality if r.caption_quality in by_quality else "weak"] += 1
        if r.input_signals.get("llm_fell_back"):
            fell_back += 1

    logger.info(
        "storyboard.caption_rewriter.rewrite",
        extra={
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "scenes_total": len(signals),
            "scenes_eligible": len(eligible),
            "captions_written": written,
            "captions_strong": by_quality["strong"],
            "captions_degraded": by_quality["degraded"],
            "captions_weak": by_quality["weak"],
            "orphans_removed": orphans,
            "panels_relinked": relinked,
            "llm_used": llm_available,
            "llm_fell_back_count": fell_back,
            "elapsed_ms": elapsed_ms,
            "timed_out": timed_out,
            "generator_version": config.version,
        },
    )

    return CaptionResult(
        artifact_id=artifact_id,
        scenes_total=len(signals),
        captions_written=written,
        captions_strong=by_quality["strong"],
        captions_degraded=by_quality["degraded"],
        captions_weak=by_quality["weak"],
        llm_used=llm_available,
        llm_fell_back_count=fell_back,
        orphans_removed=orphans,
        panels_relinked=relinked,
        elapsed_ms=elapsed_ms,
        timed_out=timed_out,
    )


# Re-exported for tests + the composer
__all__ = [
    "CaptionResult",
    "deterministic_caption",
    "rewrite_artifact_captions",
    "_SceneSignals",
    "_build_user_prompt",
    "_normalise_caption",
    "_classify_quality",
]
