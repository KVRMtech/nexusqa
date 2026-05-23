"""Scene grouper — collapse adjacent same-screen raw scenes into storyboard panels.

The canonical pipeline emits one ``VisualSceneRow`` whenever the dHash
similarity falls below the pipeline's frame-diff threshold.  In practice
this is too granular for human consumption:

* A 2-hour form-fill produces dozens of scenes whose OCR text differs
  only by what the user typed into a single field.
* A "New Tab" page with the cursor in different positions becomes many
  scenes.
* Browser chrome (favicon updates, tab reorder, history dropdown) also
  triggers boundaries.

The grouper walks the raw scene timeline greedily and produces
``StoryboardPanel`` candidates — one per meaningful "screen the user
spent time on".  Each panel records the underlying scene range, the
representative frame, the in-scene action count drawn from
``evidence_steps``, and a noise flag for the UI to hide repetitive
chrome by default.

Key invariants:

* Idempotent: the same input scenes always produce the same panel set
  with the same ``panel_id`` values (uuid5 of artifact + first_scene_index).
* Tenant-isolated: every read and write filters on ``tenant_id``.
* Production-safe: a malformed input row (missing OCR, missing scene
  index) downgrades the panel to weak quality but never crashes the
  composer.
* Configurable: every threshold lives in ``SceneGrouperConfig``; the
  service itself contains no magic numbers.

Consumed by ``composer.run_for_artifact``.  Never called from a UI
route directly.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    EvidenceStepRow,
    StoryboardPanelRow,
    VisualFrameRow,
    VisualSceneRow,
)

from .config import SceneGrouperConfig


logger = logging.getLogger(__name__)


_NAMESPACE_STORYBOARD = uuid.UUID("d4f6c9a2-6d8b-4f5b-9a32-8f4b1c1a2d3e")
"""Stable UUIDv5 namespace for storyboard derivations.

A different namespace from the pipeline's own uuid5 namespaces so
collisions between derived and pipeline-owned IDs are impossible even
if the underlying tuple inputs coincide.
"""

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _panel_id(artifact_id: str, first_scene_index: int) -> str:
    """Deterministic panel ID — re-running the grouper produces the same row."""
    return str(uuid.uuid5(
        _NAMESPACE_STORYBOARD,
        f"panel:{artifact_id}:{first_scene_index}",
    ))


def _tokenise(text: str | None, *, lowercase: bool = True) -> set[str]:
    """Cheap word-set tokeniser for Jaccard similarity.

    Production-grade requires a robust tokeniser; this one is good
    enough for OCR-derived strings where punctuation is unreliable.
    """
    if not text:
        return set()
    tokens = _TOKEN_RE.findall(text)
    if lowercase:
        return {t.lower() for t in tokens}
    return set(tokens)


def _jaccard(left: set[str], right: set[str]) -> float:
    """Set Jaccard — handles empty inputs by returning 0.0 (no overlap)."""
    if not left and not right:
        # Two empty strings count as identical, but we treat them as
        # un-mergeable to avoid collapsing many "OCR failed" scenes into
        # one panel that hides real differences.
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _normalise_screen_title(title: str | None) -> str:
    """Lower-case + strip whitespace so cosmetic differences do not block merge."""
    if not title:
        return ""
    return " ".join(title.lower().split())


def _is_noise_screen(screen_name: str | None, patterns: Sequence[str]) -> tuple[bool, str]:
    """Return (is_noise, reason) when the screen name matches a noise pattern."""
    if not screen_name:
        return False, ""
    needle = screen_name.lower()
    for pattern in patterns:
        if pattern and pattern.lower() in needle:
            return True, f"matched_pattern:{pattern}"
    return False, ""


@dataclass
class _ScenePayload:
    """Internal view of a raw scene + its representative frame asset path.

    Loaded once from the DB so the grouper does not re-query per pair.
    """

    scene_id: str
    scene_index: int
    screen_name: str
    ocr_text: str
    state_summary: dict | None
    state_quality: str
    completeness_confidence: float
    duration_ms: int | None
    start_ms: int
    end_ms: int
    app_instance_id: str | None
    representative_frame_id: str | None
    representative_frame_asset_path: str
    representative_frame_ocr_confidence: float

    # Derived once at load time so the merge loop does not re-tokenise.
    ocr_token_set: set[str] = field(default_factory=set, repr=False)
    title_token_set: set[str] = field(default_factory=set, repr=False)
    title_normalised: str = ""


@dataclass
class PanelCandidate:
    """Result of merging one or more raw scenes into a single panel.

    The composer turns these into ``StoryboardPanelRow`` rows.  We keep
    the dataclass separate from the ORM row so unit tests can verify
    grouper logic without a database.
    """

    panel_id: str
    panel_index: int
    first_scene_id: str
    last_scene_id: str
    scene_id_members: list[str]
    scene_count: int
    representative_frame_id: str | None
    representative_frame_asset_path: str
    start_ms: int
    end_ms: int
    duration_ms: int
    in_scene_action_count: int
    panel_quality: str
    completeness_confidence: float
    is_noise: bool
    noise_reason: str
    grouper_signals: dict


_QUALITY_RANK = {"strong": 3, "degraded": 2, "weak": 1}


def _panel_quality(scenes: Sequence[_ScenePayload]) -> str:
    """Aggregate scene_quality across the merged scenes.

    "strong" wins if at least 50% of scenes are strong, "degraded" if
    no scene is weak, otherwise weak.
    """
    if not scenes:
        return "weak"
    counts = {"strong": 0, "degraded": 0, "weak": 0}
    for sc in scenes:
        counts[sc.state_quality if sc.state_quality in counts else "weak"] += 1
    total = len(scenes)
    if counts["strong"] * 2 >= total:
        return "strong"
    if counts["weak"] == 0:
        return "degraded"
    return "weak"


async def _load_scenes(
    session: AsyncSession, *, artifact_id: str, tenant_id: str,
) -> list[_ScenePayload]:
    """Load all scenes for an artifact, joined with their representative frame."""
    stmt = (
        select(
            VisualSceneRow,
            VisualFrameRow.frame_asset_path,
            VisualFrameRow.ocr_confidence,
        )
        .outerjoin(
            VisualFrameRow,
            VisualSceneRow.representative_frame_id == VisualFrameRow.frame_id,
        )
        .where(
            VisualSceneRow.artifact_id == artifact_id,
            VisualSceneRow.tenant_id == tenant_id,
        )
        .order_by(VisualSceneRow.scene_index.asc())
    )
    result = await session.execute(stmt)
    payloads: list[_ScenePayload] = []
    for scene, frame_path, frame_ocr in result.all():
        state_summary = scene.scene_state_summary or {}
        state_quality = (
            (state_summary.get("state_quality") or scene.scene_quality or "weak")
            if isinstance(state_summary, dict)
            else (scene.scene_quality or "weak")
        )
        screen_title = ""
        if isinstance(state_summary, dict):
            screen_title = (state_summary.get("screen_title") or "").strip()
        payload = _ScenePayload(
            scene_id=scene.scene_id,
            scene_index=int(scene.scene_index or 0),
            screen_name=(scene.screen_name or "").strip(),
            ocr_text=scene.ocr_text or "",
            state_summary=state_summary if isinstance(state_summary, dict) else None,
            state_quality=state_quality,
            completeness_confidence=float(scene.completeness_confidence or 0.0),
            duration_ms=int(scene.duration_ms) if scene.duration_ms is not None else None,
            start_ms=int(scene.start_ms or 0),
            end_ms=int(scene.end_ms or 0),
            app_instance_id=scene.app_instance_id,
            representative_frame_id=scene.representative_frame_id,
            representative_frame_asset_path=frame_path or "",
            representative_frame_ocr_confidence=float(frame_ocr or 0.0),
        )
        payload.ocr_token_set = _tokenise(payload.ocr_text)
        payload.title_normalised = _normalise_screen_title(
            screen_title or payload.screen_name,
        )
        payload.title_token_set = _tokenise(payload.title_normalised)
        payloads.append(payload)
    return payloads


async def _load_action_counts_per_scene(
    session: AsyncSession, *, artifact_id: str, tenant_id: str,
) -> dict[str, int]:
    """Aggregate evidence_steps per scene_id so panels show in-scene action counts."""
    stmt = (
        select(EvidenceStepRow.scene_id)
        .where(
            EvidenceStepRow.artifact_id == artifact_id,
            EvidenceStepRow.tenant_id == tenant_id,
        )
    )
    result = await session.execute(stmt)
    counts: dict[str, int] = {}
    for (scene_id,) in result.all():
        if not scene_id:
            continue
        counts[scene_id] = counts.get(scene_id, 0) + 1
    return counts


def _should_merge(
    candidate_scenes: Sequence[_ScenePayload],
    next_scene: _ScenePayload,
    *,
    config: SceneGrouperConfig,
) -> tuple[bool, str]:
    """Decide whether ``next_scene`` extends the current panel.

    Returns ``(merge, reason)`` so the grouper can persist a human
    readable explanation in ``grouper_signals.collapse_reason``.

    Decision is greedy and configurable:

    1. Reject if the current panel already absorbed the max scenes.
    2. Merge if normalised screen titles share enough tokens (title is
       the pipeline's own grouping signal and is usually reliable when
       present).
    3. Merge if OCR text has high Jaccard overlap (catches form fills
       and minor in-page state changes).
    4. Merge if the next scene is shorter than the configured floor
       (typing a single character produces 200ms scenes the pipeline
       never collapses).
    5. Otherwise start a new panel.
    """
    if not candidate_scenes:
        return False, "first_scene"

    if len(candidate_scenes) >= config.max_scenes_per_panel:
        return False, "max_scenes_per_panel"

    # App-instance boundary: if the previous and next scenes belong to
    # explicitly different app_instances we never collapse them, even
    # if OCR happens to look similar.
    last = candidate_scenes[-1]
    if last.app_instance_id and next_scene.app_instance_id:
        if last.app_instance_id != next_scene.app_instance_id:
            return False, "app_instance_boundary"

    # Title-driven merge — strongest signal when both titles are set.
    if next_scene.title_normalised and last.title_normalised:
        title_jaccard = _jaccard(last.title_token_set, next_scene.title_token_set)
        if title_jaccard >= config.same_screen_title_jaccard_threshold:
            return True, f"title_jaccard:{title_jaccard:.3f}"

    # OCR-driven merge — handles same-screen form fills where the title
    # is unchanged but OCR text has minor delta.  We tolerate weaker
    # overlap when title also agrees lexically.
    if last.ocr_token_set and next_scene.ocr_token_set:
        ocr_jaccard = _jaccard(last.ocr_token_set, next_scene.ocr_token_set)
        if ocr_jaccard >= config.same_screen_jaccard_threshold:
            return True, f"ocr_jaccard:{ocr_jaccard:.3f}"

    # Short-scene merge — absorb micro-scenes into the previous panel.
    if next_scene.duration_ms is not None and next_scene.duration_ms < config.merge_below_duration_ms:
        return True, f"short_duration:{next_scene.duration_ms}ms"

    return False, "no_merge_signal"


def _select_representative(
    scenes: Sequence[_ScenePayload],
) -> tuple[str | None, str, float]:
    """Pick the highest-quality frame across the merged scenes.

    Highest representative_frame_ocr_confidence wins.  Ties broken by
    earliest scene_index so the panel hero remains chronologically
    sensible.  Returns ``(frame_id, asset_path, ocr_confidence)``.
    """
    best_id: str | None = None
    best_path = ""
    best_conf = -1.0
    for sc in scenes:
        if sc.representative_frame_id is None:
            continue
        if sc.representative_frame_ocr_confidence > best_conf:
            best_id = sc.representative_frame_id
            best_path = sc.representative_frame_asset_path
            best_conf = sc.representative_frame_ocr_confidence
    if best_id is None:
        return None, "", 0.0
    return best_id, best_path, max(best_conf, 0.0)


def _build_candidates(
    payloads: Sequence[_ScenePayload],
    *,
    artifact_id: str,
    config: SceneGrouperConfig,
    action_counts: dict[str, int],
) -> list[PanelCandidate]:
    """Greedy walk that produces ordered ``PanelCandidate`` entries."""
    if not payloads:
        return []

    panels: list[PanelCandidate] = []
    current: list[_ScenePayload] = []
    collapse_reasons: list[str] = []

    def _flush() -> None:
        if not current:
            return
        first = current[0]
        last = current[-1]
        frame_id, frame_path, frame_conf = _select_representative(current)
        start_ms = min(sc.start_ms for sc in current)
        end_ms = max(sc.end_ms for sc in current)
        duration_ms = max(0, end_ms - start_ms)
        completeness = sum(sc.completeness_confidence for sc in current) / len(current)
        member_ids = [sc.scene_id for sc in current]
        action_count = sum(action_counts.get(sc.scene_id, 0) for sc in current)
        noise_flag, noise_reason = False, ""
        # Apply noise rules over the merged panel — if every member scene
        # matches a noise pattern, the panel is noise.  Mixed panels
        # (some real, some noise) are kept because the real scene is
        # informative.
        noise_hits = [
            _is_noise_screen(sc.screen_name, config.noise_screen_name_patterns)
            for sc in current
        ]
        if noise_hits and all(hit[0] for hit in noise_hits):
            noise_flag = True
            noise_reason = noise_hits[0][1]
        # OCR-confidence-floor noise: every member scene had very weak
        # OCR — the storyboard would just show a blurry thumbnail with
        # no readable caption.
        if not noise_flag and frame_conf < config.weak_ocr_confidence:
            # Only flag noise if NO scene contributed strong title
            # signal — otherwise we hide informative screens just
            # because OCR was poor.
            informative = any(sc.title_normalised for sc in current)
            if not informative:
                noise_flag = True
                noise_reason = (
                    f"frame_ocr_below_floor:{frame_conf:.2f}<{config.weak_ocr_confidence}"
                )

        panel = PanelCandidate(
            panel_id=_panel_id(artifact_id, first.scene_index),
            panel_index=len(panels),
            first_scene_id=first.scene_id,
            last_scene_id=last.scene_id,
            scene_id_members=member_ids,
            scene_count=len(current),
            representative_frame_id=frame_id,
            representative_frame_asset_path=frame_path,
            start_ms=start_ms,
            end_ms=end_ms,
            duration_ms=duration_ms,
            in_scene_action_count=action_count,
            panel_quality=_panel_quality(current),
            completeness_confidence=round(completeness, 4),
            is_noise=noise_flag,
            noise_reason=noise_reason,
            grouper_signals={
                "scene_id_members": member_ids,
                "collapse_reasons": list(collapse_reasons),
                "first_scene_index": first.scene_index,
                "last_scene_index": last.scene_index,
                "rep_frame_ocr_confidence": round(frame_conf, 3),
                "rep_frame_id": frame_id,
            },
        )
        panels.append(panel)

    for payload in payloads:
        merge, reason = _should_merge(current, payload, config=config)
        if merge:
            current.append(payload)
            collapse_reasons.append(f"{payload.scene_index}:{reason}")
            continue
        _flush()
        current = [payload]
        collapse_reasons = [f"{payload.scene_index}:{reason}"]
    _flush()
    return panels


async def _upsert_panels(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    session_id: str,
    panels: Sequence[PanelCandidate],
    config: SceneGrouperConfig,
) -> int:
    """UPSERT panels into ``storyboard_panels``.

    Returns the number of rows touched.  Uses Postgres ON CONFLICT so
    the operation is idempotent — re-running the grouper for the same
    artifact updates existing rows in place (no DELETE-then-INSERT race
    window where the storyboard endpoint sees a partial result).
    """
    if not panels:
        return 0

    payloads = []
    for panel in panels:
        payloads.append({
            "panel_id": panel.panel_id,
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "panel_index": panel.panel_index,
            "first_scene_id": panel.first_scene_id,
            "last_scene_id": panel.last_scene_id,
            "scene_count": panel.scene_count,
            "app_grouping_id": None,
            "representative_frame_id": panel.representative_frame_id,
            "representative_frame_asset_path": panel.representative_frame_asset_path,
            "start_ms": panel.start_ms,
            "end_ms": panel.end_ms,
            "duration_ms": panel.duration_ms,
            "caption_short": "",
            "caption_long": "",
            "in_scene_action_count": panel.in_scene_action_count,
            "panel_quality": panel.panel_quality,
            "completeness_confidence": panel.completeness_confidence,
            "is_noise": panel.is_noise,
            "noise_reason": panel.noise_reason,
            "grouper_version": config.version,
            "grouper_signals": panel.grouper_signals,
        })

    stmt = pg_insert(StoryboardPanelRow.__table__).values(payloads)
    update_columns = {
        col.name: stmt.excluded[col.name]
        for col in StoryboardPanelRow.__table__.columns
        if col.name not in {"panel_id", "created_at"}
    }
    # Refresh updated_at server-side rather than from the bound payload.
    update_columns["updated_at"] = stmt.excluded.updated_at
    upsert = stmt.on_conflict_do_update(
        index_elements=[StoryboardPanelRow.__table__.c.panel_id],
        set_=update_columns,
    )
    await session.execute(upsert)
    return len(payloads)


async def _delete_orphan_panels(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    keep_panel_ids: set[str],
) -> int:
    """Remove panels for this artifact that no longer exist in the new layout.

    Called after UPSERT so the table reflects exactly what the grouper
    produced for the configured version.  Without this, switching
    versions or shrinking the underlying scene set would leave stale
    rows.  Returns the count of deleted rows.
    """
    from sqlalchemy import delete

    stmt = delete(StoryboardPanelRow).where(
        StoryboardPanelRow.artifact_id == artifact_id,
        StoryboardPanelRow.tenant_id == tenant_id,
    )
    if keep_panel_ids:
        stmt = stmt.where(~StoryboardPanelRow.panel_id.in_(keep_panel_ids))
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


@dataclass(frozen=True)
class GrouperResult:
    """Summary returned to the composer (for observability and tests)."""

    artifact_id: str
    panels_written: int
    orphans_removed: int
    raw_scene_count: int
    noise_panel_count: int
    elapsed_ms: int


async def regroup_artifact(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    session_id: str,
    config: SceneGrouperConfig,
) -> GrouperResult:
    """Re-derive storyboard panels for one artifact.

    Caller is responsible for tenant-context configuration on the DB
    session (``SET LOCAL nexus.current_tenant_id`` so RLS works).  We do
    not commit — the composer batches related writes (panels + app
    groupings + captions) into one transaction.

    Idempotent: running this twice in a row yields the same rows.
    Safe to call concurrently for different artifacts; per-artifact
    serialization should be handled by the composer via an advisory
    lock when concurrent triggers are possible.
    """
    started_at = time.monotonic()

    payloads = await _load_scenes(
        session, artifact_id=artifact_id, tenant_id=tenant_id,
    )
    action_counts = await _load_action_counts_per_scene(
        session, artifact_id=artifact_id, tenant_id=tenant_id,
    )

    candidates = _build_candidates(
        payloads,
        artifact_id=artifact_id,
        config=config,
        action_counts=action_counts,
    )

    written = await _upsert_panels(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        session_id=session_id,
        panels=candidates,
        config=config,
    )
    keep_ids = {c.panel_id for c in candidates}
    orphans = await _delete_orphan_panels(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        keep_panel_ids=keep_ids,
    )

    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    noise_panels = sum(1 for c in candidates if c.is_noise)

    logger.info(
        "storyboard.scene_grouper.regroup",
        extra={
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "raw_scene_count": len(payloads),
            "panels_written": written,
            "orphans_removed": orphans,
            "noise_panel_count": noise_panels,
            "elapsed_ms": elapsed_ms,
            "grouper_version": config.version,
        },
    )

    return GrouperResult(
        artifact_id=artifact_id,
        panels_written=written,
        orphans_removed=orphans,
        raw_scene_count=len(payloads),
        noise_panel_count=noise_panels,
        elapsed_ms=elapsed_ms,
    )


def group_payloads(
    payloads: Iterable[_ScenePayload],
    *,
    artifact_id: str,
    config: SceneGrouperConfig,
    action_counts: dict[str, int] | None = None,
) -> list[PanelCandidate]:
    """Pure helper exposed for unit tests.

    Lets tests assert grouper behavior on hand-constructed scene
    payloads without standing up a database.
    """
    return _build_candidates(
        list(payloads),
        artifact_id=artifact_id,
        config=config,
        action_counts=action_counts or {},
    )
