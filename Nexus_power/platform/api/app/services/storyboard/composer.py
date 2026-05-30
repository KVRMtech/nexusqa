"""Storyboard composer — orchestrates the 4 derivation services + serves panels.

The composer is the only Phase 1 entry point that application code (the
``GET /storyboard`` and ``GET /annotated.png`` routes) interacts with.
Everything else — scene grouping, app dedup, caption rewriting, frame
annotation — is internal plumbing.

Flow (lazy-by-default)
======================

For every incoming ``GET /storyboard`` request the composer:

1. **Checks freshness.**  Compares the configured versions
   (``scene_grouper.version``, ``app_deduper.version``,
   ``caption_rewriter.version``, ``frame_annotator.version``) against
   the most recent rows in each derived table.  When any version is
   stale OR no rows exist for that step yet, that step is re-run.
2. **Runs missing or stale derivations** in dependency order:
       scene_grouper → app_deduper → caption_rewriter → frame_annotator
   Each is idempotent and re-running with the same version is cheap.
3. **Returns the assembled storyboard** — every panel joined with its
   app grouping + caption + annotated frame URL.

This avoids both the "must regenerate before viewing" delay of an
eager strategy and the staleness of a purely background strategy.
Re-derivation also runs in a thread-bounded asyncio pipeline, so a
slow LLM does not block multiple concurrent storyboard requests for
different artifacts.

Composer outputs are dataclasses (not Pydantic models) so the router
can format them per response shape.  See ``routers/storyboard.py`` for
the HTTP-level Pydantic conversion.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    AnnotatedFrameCacheRow,
    AppGroupingRow,
    CanonicalArtifactRow,
    PageActionRow,
    PageVisitRow,
    SceneActionRow,
    SceneCaptionRow,
    StoryboardPanelRow,
    VisualFrameRow,
)

from ..llm import LLMRouter
from .action_extractor import extract_actions_for_artifact
from .app_deduper import rededuplicate_artifact
from .caption_rewriter import rewrite_artifact_captions
from .config import StoryboardConfig
from .form_snapshot_extractor import extract_form_snapshots_for_artifact
from .frame_annotator import FrameAnnotator, precompute_panel_frames
from .page_action_extractor import extract_page_actions_for_artifact
from .page_visit_extractor import extract_page_visits_for_artifact
from .scene_grouper import regroup_artifact

logger = logging.getLogger(__name__)


# ── Output view types ────────────────────────────────────────────────────────


@dataclass
class AppGroupingView:
    """Storyboard-ready representation of one dedup'd application."""

    grouping_id: str
    display_label: str
    canonical_name: str
    canonical_domain: str
    app_type: str
    scene_count: int
    first_scene_index: int
    last_scene_index: int
    confidence: float
    dedup_basis: str


@dataclass
class StoryboardPanelView:
    """One comic-strip panel as the UI wants to render it."""

    panel_id: str
    panel_index: int
    scene_count: int
    in_scene_action_count: int
    start_ms: int
    end_ms: int
    duration_ms: int
    caption_short: str
    caption_long: str
    caption_quality: str
    panel_quality: str
    completeness_confidence: float
    is_noise: bool
    noise_reason: str
    representative_frame_id: str | None
    representative_frame_path: str
    annotated_frame_available: bool
    app: AppGroupingView | None


@dataclass
class DerivationStatus:
    """Per-step run status — included in the response for observability."""

    scene_grouper_version: str
    app_deduper_version: str
    caption_rewriter_version: str
    frame_annotator_version: str
    action_extractor_version: str
    page_visit_extractor_version: str
    page_action_extractor_version: str
    form_snapshot_extractor_version: str
    ran_scene_grouper: bool
    ran_app_deduper: bool
    ran_caption_rewriter: bool
    ran_frame_annotator: bool
    ran_action_extractor: bool
    ran_page_visit_extractor: bool
    ran_page_action_extractor: bool
    ran_form_snapshot_extractor: bool
    derivation_elapsed_ms: int
    storyboard_total_ms: int


@dataclass
class StoryboardResponse:
    """Top-level storyboard payload returned to the router."""

    artifact_id: str
    panels: list[StoryboardPanelView]
    apps: list[AppGroupingView]
    visual_e2e_status: str | None
    summary: Mapping[str, object]
    derivation: DerivationStatus


# ── Freshness checks ─────────────────────────────────────────────────────────


async def _max_version(
    session: AsyncSession,
    *,
    table: object,
    artifact_id: str,
    tenant_id: str,
    version_column: object,
) -> str | None:
    """Return the highest-ordered (lexically largest) version for an artifact.

    NULL when no rows exist.  We do not assume a particular version
    naming scheme; lexical max is sufficient because operators bump
    versions monotonically (``v1`` → ``v2`` → ``v3``).
    """
    artifact_column = getattr(table, "artifact_id")
    tenant_column = getattr(table, "tenant_id")
    stmt = (
        select(func.max(version_column))
        .where(artifact_column == artifact_id, tenant_column == tenant_id)
    )
    result = await session.execute(stmt)
    return result.scalar()


async def _needs_scene_grouper(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    target_version: str,
) -> bool:
    current = await _max_version(
        session,
        table=StoryboardPanelRow,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        version_column=StoryboardPanelRow.grouper_version,
    )
    return current is None or current != target_version


async def _needs_app_deduper(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    target_version: str,
) -> bool:
    current = await _max_version(
        session,
        table=AppGroupingRow,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        version_column=AppGroupingRow.deduper_version,
    )
    return current is None or current != target_version


async def _needs_caption_rewriter(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    target_version: str,
) -> bool:
    current = await _max_version(
        session,
        table=SceneCaptionRow,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        version_column=SceneCaptionRow.generator_version,
    )
    return current is None or current != target_version


async def _needs_action_extractor(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    target_version: str,
) -> bool:
    current = await _max_version(
        session,
        table=SceneActionRow,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        version_column=SceneActionRow.extractor_version,
    )
    return current is None or current != target_version


async def _needs_page_visit_extractor(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    target_version: str,
) -> bool:
    current = await _max_version(
        session,
        table=PageVisitRow,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        version_column=PageVisitRow.extractor_version,
    )
    return current is None or current != target_version


async def _needs_page_action_extractor(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    target_version: str,
) -> bool:
    current = await _max_version(
        session,
        table=PageActionRow,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        version_column=PageActionRow.extractor_version,
    )
    return current is None or current != target_version


async def _needs_form_snapshot_extractor(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    target_version: str,
) -> bool:
    current = await _max_version(
        session,
        table=PageVisitRow,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        version_column=PageVisitRow.form_snapshot_extractor_version,
    )
    return current is None or current != target_version


async def _needs_frame_annotator(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    target_version: str,
) -> bool:
    current = await _max_version(
        session,
        table=AnnotatedFrameCacheRow,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        version_column=AnnotatedFrameCacheRow.annotation_version,
    )
    # Frame annotator is optional — when missing, the composer still
    # returns panels with the raw representative_frame_path.  Only
    # re-run when the configured version is non-empty and the existing
    # version doesn't match.
    if not target_version:
        return False
    return current != target_version


# ── Reading the assembled storyboard ─────────────────────────────────────────


async def _read_storyboard(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    config: StoryboardConfig,
) -> tuple[list[StoryboardPanelView], list[AppGroupingView]]:
    """Load panels + their captions + their app groupings, joined for the UI."""

    panels_q = await session.execute(
        select(StoryboardPanelRow)
        .where(
            StoryboardPanelRow.artifact_id == artifact_id,
            StoryboardPanelRow.tenant_id == tenant_id,
        )
        .order_by(StoryboardPanelRow.panel_index.asc())
        .limit(config.composer.max_panels_per_response)
    )
    panels = list(panels_q.scalars().all())

    captions_q = await session.execute(
        select(SceneCaptionRow).where(
            SceneCaptionRow.artifact_id == artifact_id,
            SceneCaptionRow.tenant_id == tenant_id,
            SceneCaptionRow.generator_version == config.caption_rewriter.version,
        )
    )
    captions_by_scene: dict[str, SceneCaptionRow] = {
        c.scene_id: c for c in captions_q.scalars().all()
    }

    apps_q = await session.execute(
        select(AppGroupingRow).where(
            AppGroupingRow.artifact_id == artifact_id,
            AppGroupingRow.tenant_id == tenant_id,
        )
        .order_by(AppGroupingRow.first_scene_index.asc())
    )
    app_rows = list(apps_q.scalars().all())
    apps_by_id: dict[str, AppGroupingView] = {}
    apps: list[AppGroupingView] = []
    for row in app_rows:
        view = AppGroupingView(
            grouping_id=row.grouping_id,
            display_label=row.display_label or row.canonical_name or row.canonical_domain or "",
            canonical_name=row.canonical_name or "",
            canonical_domain=row.canonical_domain or "",
            app_type=row.app_type or "unknown",
            scene_count=int(row.total_scene_count or 0),
            first_scene_index=int(row.first_scene_index or 0),
            last_scene_index=int(row.last_scene_index or 0),
            confidence=float(row.confidence or 0.0),
            dedup_basis=row.dedup_basis or "single_instance",
        )
        apps_by_id[row.grouping_id] = view
        apps.append(view)

    # Annotated cache hits — used to drive the ``annotated_frame_available`` flag
    annot_q = await session.execute(
        select(AnnotatedFrameCacheRow.frame_id)
        .where(
            AnnotatedFrameCacheRow.artifact_id == artifact_id,
            AnnotatedFrameCacheRow.tenant_id == tenant_id,
            AnnotatedFrameCacheRow.annotation_version
            == config.frame_annotator.version,
        )
    )
    annotated_frame_ids = {fid for (fid,) in annot_q.all()}

    panel_views: list[StoryboardPanelView] = []
    for panel in panels:
        caption_row = (
            captions_by_scene.get(panel.first_scene_id)
            if panel.first_scene_id
            else None
        )
        caption_short = (
            caption_row.short_caption
            if caption_row and caption_row.short_caption
            else panel.caption_short
        )
        caption_long = caption_row.narrative_caption if caption_row else panel.caption_long
        caption_quality = caption_row.caption_quality if caption_row else "weak"
        panel_views.append(StoryboardPanelView(
            panel_id=panel.panel_id,
            panel_index=int(panel.panel_index),
            scene_count=int(panel.scene_count or 1),
            in_scene_action_count=int(panel.in_scene_action_count or 0),
            start_ms=int(panel.start_ms or 0),
            end_ms=int(panel.end_ms or 0),
            duration_ms=int(panel.duration_ms or 0),
            caption_short=caption_short or "",
            caption_long=caption_long or "",
            caption_quality=caption_quality or "weak",
            panel_quality=panel.panel_quality or "weak",
            completeness_confidence=float(panel.completeness_confidence or 0.0),
            is_noise=bool(panel.is_noise),
            noise_reason=panel.noise_reason or "",
            representative_frame_id=panel.representative_frame_id,
            representative_frame_path=panel.representative_frame_asset_path or "",
            annotated_frame_available=(
                panel.representative_frame_id in annotated_frame_ids
                if panel.representative_frame_id
                else False
            ),
            app=apps_by_id.get(panel.app_grouping_id) if panel.app_grouping_id else None,
        ))
    return panel_views, apps


# ── Composer ─────────────────────────────────────────────────────────────────


class StoryboardComposer:
    """Run the four derivation services then assemble the response.

    A single composer instance lives on ``app.state.storyboard_composer``
    and is shared across requests.  It holds references to the LLM
    router + frame annotator (both stateful) but creates new DB
    sessions per call.
    """

    def __init__(
        self,
        *,
        config: StoryboardConfig,
        llm_router: LLMRouter,
        frame_annotator: FrameAnnotator | None,
    ) -> None:
        self._config = config
        self._llm_router = llm_router
        self._frame_annotator = frame_annotator

    @property
    def config(self) -> StoryboardConfig:
        return self._config

    async def get_storyboard(
        self,
        session: AsyncSession,
        *,
        artifact_id: str,
        tenant_id: str,
        force_regenerate: bool = False,
        auth_token: str = "",
    ) -> StoryboardResponse | None:
        """Return the storyboard for one artifact, deriving on miss/stale.

        Returns ``None`` only when the artifact does not exist or is
        cross-tenant.  Otherwise always returns a payload (possibly
        with empty panels if the pipeline has not yet produced visual
        evidence).

        ``auth_token`` is the caller's JWT.  It is forwarded to the
        frame annotator so eyes-engine accepts service-to-service
        fetches under the same tenant isolation as the user request.
        """
        started = time.monotonic()

        artifact = await self._load_artifact(session, artifact_id, tenant_id)
        if artifact is None:
            return None

        derivation_started = time.monotonic()
        ran = await self._maybe_derive(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            force=force_regenerate,
            auth_token=auth_token,
        )
        derivation_elapsed_ms = int((time.monotonic() - derivation_started) * 1000)

        panels, apps = await _read_storyboard(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            config=self._config,
        )

        summary = self._summary(panels, apps, artifact=artifact)
        total_ms = int((time.monotonic() - started) * 1000)

        return StoryboardResponse(
            artifact_id=artifact_id,
            panels=panels,
            apps=apps,
            visual_e2e_status=(
                artifact.quality_gate_outcome if artifact else None
            ),
            summary=summary,
            derivation=DerivationStatus(
                scene_grouper_version=self._config.scene_grouper.version,
                app_deduper_version=self._config.app_deduper.version,
                caption_rewriter_version=self._config.caption_rewriter.version,
                frame_annotator_version=self._config.frame_annotator.version,
                action_extractor_version=self._config.action_extractor.version,
                page_visit_extractor_version=(
                    self._config.page_visit_extractor.version
                ),
                page_action_extractor_version=(
                    self._config.page_action_extractor.version
                ),
                form_snapshot_extractor_version=(
                    self._config.form_snapshot_extractor.version
                ),
                ran_scene_grouper=ran["scene_grouper"],
                ran_app_deduper=ran["app_deduper"],
                ran_caption_rewriter=ran["caption_rewriter"],
                ran_frame_annotator=ran["frame_annotator"],
                ran_action_extractor=ran["action_extractor"],
                ran_page_visit_extractor=ran["page_visit_extractor"],
                ran_page_action_extractor=ran["page_action_extractor"],
                ran_form_snapshot_extractor=ran["form_snapshot_extractor"],
                derivation_elapsed_ms=derivation_elapsed_ms,
                storyboard_total_ms=total_ms,
            ),
        )

    # ─── Helpers ─────────────────────────────────────────────

    async def _load_artifact(
        self,
        session: AsyncSession,
        artifact_id: str,
        tenant_id: str,
    ) -> CanonicalArtifactRow | None:
        q = await session.execute(
            select(CanonicalArtifactRow).where(
                CanonicalArtifactRow.artifact_id == artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
        )
        return q.scalar_one_or_none()

    async def _maybe_derive(
        self,
        session: AsyncSession,
        *,
        artifact_id: str,
        tenant_id: str,
        force: bool,
        auth_token: str = "",
    ) -> dict[str, bool]:
        """Run any missing-or-stale derivation step.

        Steps run sequentially because each depends on the previous
        (panels → app groupings → captions → annotated frames).  The
        timeout budget is enforced per-step by the underlying services
        (caption_rewriter has total_timeout_s; frame_annotator has
        render_timeout_s per frame).
        """
        ran = {
            "scene_grouper": False,
            "app_deduper": False,
            "caption_rewriter": False,
            "frame_annotator": False,
            "action_extractor": False,
            "page_visit_extractor": False,
            "page_action_extractor": False,
            "form_snapshot_extractor": False,
        }

        session_id = await self._session_id_for_artifact(session, artifact_id, tenant_id)
        if not session_id:
            return ran  # artifact has no session_id → nothing to derive

        scene_grouper_due = force or await _needs_scene_grouper(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            target_version=self._config.scene_grouper.version,
        )
        if scene_grouper_due:
            await regroup_artifact(
                session,
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                session_id=session_id,
                config=self._config.scene_grouper,
            )
            ran["scene_grouper"] = True

        app_deduper_due = force or await _needs_app_deduper(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            target_version=self._config.app_deduper.version,
        )
        if app_deduper_due or scene_grouper_due:
            await rededuplicate_artifact(
                session,
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                session_id=session_id,
                config=self._config.app_deduper,
            )
            ran["app_deduper"] = True

        caption_due = force or await _needs_caption_rewriter(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            target_version=self._config.caption_rewriter.version,
        )
        # Captions also re-run when panels changed (scene grouper ran)
        # so the new panels get fresh captions tied to their new
        # first_scene_id back-link.
        if caption_due or scene_grouper_due:
            await rewrite_artifact_captions(
                session,
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                config=self._config.caption_rewriter,
                router=self._llm_router,
            )
            ran["caption_rewriter"] = True

        action_extractor_due = force or await _needs_action_extractor(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            target_version=self._config.action_extractor.version,
        )
        # Action extractor also re-runs when scene groupings changed,
        # since new scenes need new actions extracted.
        if action_extractor_due or scene_grouper_due:
            try:
                await asyncio.wait_for(
                    extract_actions_for_artifact(
                        session,
                        artifact_id=artifact_id,
                        tenant_id=tenant_id,
                        config=self._config.action_extractor,
                        router=self._llm_router,
                        auth_token=auth_token,
                    ),
                    timeout=self._config.action_extractor.total_timeout_s,
                )
                ran["action_extractor"] = True
            except asyncio.TimeoutError:
                logger.warning(
                    "storyboard.composer.action_extractor_timeout",
                    extra={
                        "artifact_id": artifact_id,
                        "timeout_s": self._config.action_extractor.total_timeout_s,
                    },
                )

        # ── Phase 2.0 — page_visits.  URL-keyed log independent of
        # scene boundaries.  Always re-runs when a new artifact_version
        # is configured or when scenes were regrouped (the canonical
        # pipeline's url discovery feeds the visit fallbacks).
        page_visit_due = force or await _needs_page_visit_extractor(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            target_version=self._config.page_visit_extractor.version,
        )
        if page_visit_due or scene_grouper_due:
            try:
                await extract_page_visits_for_artifact(
                    session,
                    artifact_id=artifact_id,
                    tenant_id=tenant_id,
                    config=self._config.page_visit_extractor,
                    router=(
                        self._llm_router
                        if self._config.page_visit_extractor.enable_llm_inference
                        else None
                    ),
                    auth_token=auth_token,
                )
                ran["page_visit_extractor"] = True
            except Exception as exc:  # pragma: no cover - degrade gracefully
                logger.warning(
                    "storyboard.composer.page_visit_extractor_failed",
                    extra={
                        "artifact_id": artifact_id,
                        "error": str(exc)[:200],
                    },
                )

        # ── Phase 2.1 — page_actions.  Per-URL action breakdown.  Runs
        # after page_visits so the visit set is fresh.
        page_action_due = force or await _needs_page_action_extractor(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            target_version=self._config.page_action_extractor.version,
        )
        if page_action_due or page_visit_due or scene_grouper_due:
            try:
                await asyncio.wait_for(
                    extract_page_actions_for_artifact(
                        session,
                        artifact_id=artifact_id,
                        tenant_id=tenant_id,
                        config=self._config.page_action_extractor,
                        router=self._llm_router,
                        auth_token=auth_token,
                    ),
                    timeout=self._config.page_action_extractor.total_timeout_s,
                )
                ran["page_action_extractor"] = True
            except asyncio.TimeoutError:
                logger.warning(
                    "storyboard.composer.page_action_extractor_timeout",
                    extra={
                        "artifact_id": artifact_id,
                        "timeout_s": (
                            self._config.page_action_extractor.total_timeout_s
                        ),
                    },
                )
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "storyboard.composer.page_action_extractor_failed",
                    extra={
                        "artifact_id": artifact_id,
                        "error": str(exc)[:200],
                    },
                )

        # ── Phase 2.2 — form_snapshots.  Per-URL final form state.
        # Independent of page_actions; runs in parallel-equivalent
        # sequence (sequential here because composer is single-threaded
        # per artifact request).
        form_snapshot_due = force or await _needs_form_snapshot_extractor(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            target_version=self._config.form_snapshot_extractor.version,
        )
        if form_snapshot_due or page_visit_due or scene_grouper_due:
            try:
                await asyncio.wait_for(
                    extract_form_snapshots_for_artifact(
                        session,
                        artifact_id=artifact_id,
                        tenant_id=tenant_id,
                        config=self._config.form_snapshot_extractor,
                        router=self._llm_router,
                        auth_token=auth_token,
                    ),
                    timeout=self._config.form_snapshot_extractor.total_timeout_s,
                )
                ran["form_snapshot_extractor"] = True
            except asyncio.TimeoutError:
                logger.warning(
                    "storyboard.composer.form_snapshot_extractor_timeout",
                    extra={
                        "artifact_id": artifact_id,
                        "timeout_s": (
                            self._config.form_snapshot_extractor.total_timeout_s
                        ),
                    },
                )
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "storyboard.composer.form_snapshot_extractor_failed",
                    extra={
                        "artifact_id": artifact_id,
                        "error": str(exc)[:200],
                    },
                )

        if self._frame_annotator and self._frame_annotator.pil_available:
            annotator_due = force or await _needs_frame_annotator(
                session,
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                target_version=self._config.frame_annotator.version,
            )
            if annotator_due or scene_grouper_due or caption_due:
                try:
                    await asyncio.wait_for(
                        precompute_panel_frames(
                            session,
                            artifact_id=artifact_id,
                            tenant_id=tenant_id,
                            annotator=self._frame_annotator,
                            concurrency=4,
                            auth_token=auth_token,
                        ),
                        timeout=self._config.composer.derivation_timeout_s,
                    )
                    ran["frame_annotator"] = True
                except asyncio.TimeoutError:
                    logger.warning(
                        "storyboard.composer.frame_annotator_timeout",
                        extra={
                            "artifact_id": artifact_id,
                            "timeout_s": self._config.composer.derivation_timeout_s,
                        },
                    )
        return ran

    async def _session_id_for_artifact(
        self,
        session: AsyncSession,
        artifact_id: str,
        tenant_id: str,
    ) -> str:
        q = await session.execute(
            select(CanonicalArtifactRow.session_id).where(
                CanonicalArtifactRow.artifact_id == artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
        )
        return q.scalar() or ""

    def _summary(
        self,
        panels: list[StoryboardPanelView],
        apps: list[AppGroupingView],
        *,
        artifact: CanonicalArtifactRow | None,
    ) -> dict[str, object]:
        non_noise = [p for p in panels if not p.is_noise]
        total_actions = sum(p.in_scene_action_count for p in non_noise)
        duration_ms = (
            (non_noise[-1].end_ms - non_noise[0].start_ms) if non_noise else 0
        )
        strong = sum(1 for p in non_noise if p.panel_quality == "strong")
        degraded = sum(1 for p in non_noise if p.panel_quality == "degraded")
        weak = sum(1 for p in non_noise if p.panel_quality == "weak")

        return {
            "panel_count": len(panels),
            "non_noise_panel_count": len(non_noise),
            "noise_panel_count": len(panels) - len(non_noise),
            "app_count": len(apps),
            "total_in_scene_actions": total_actions,
            "duration_ms": max(duration_ms, 0),
            "strong_panels": strong,
            "degraded_panels": degraded,
            "weak_panels": weak,
            "source_filename": getattr(artifact, "source_filename", "") if artifact else "",
            "brain_quality_score": (
                float(artifact.brain_quality_score)
                if artifact and artifact.brain_quality_score is not None
                else None
            ),
        }


# ── Hero preview (canonical result page) ─────────────────────────────────────


async def get_hero_preview(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    config: StoryboardConfig,
    composer: StoryboardComposer,
    panel_count: int | None = None,
) -> list[StoryboardPanelView]:
    """Pick N evenly-spaced non-noise panels for the canonical result hero.

    Reuses the composer's lazy derivation path so calling this from
    the canonical result page also primes the storyboard cache.
    Returns at most ``panel_count`` (default from config) panels
    distributed across the storyboard's timeline so the user sees
    beginning, middle and end at a glance.
    """
    target = panel_count or config.composer.hero_panel_count

    response = await composer.get_storyboard(
        session, artifact_id=artifact_id, tenant_id=tenant_id, force_regenerate=False,
    )
    if response is None:
        return []

    non_noise = [p for p in response.panels if not p.is_noise]
    if not non_noise:
        return response.panels[:target]
    if len(non_noise) <= target:
        return non_noise

    # Evenly spaced picks across the timeline, always including first + last.
    step = (len(non_noise) - 1) / max(1, target - 1)
    indices: list[int] = []
    for i in range(target):
        idx = round(i * step)
        idx = min(idx, len(non_noise) - 1)
        if idx not in indices:
            indices.append(idx)
    return [non_noise[i] for i in indices]


__all__ = [
    "AppGroupingView",
    "DerivationStatus",
    "StoryboardComposer",
    "StoryboardPanelView",
    "StoryboardResponse",
    "get_hero_preview",
]
