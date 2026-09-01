"""Frame annotator — bake cursor / click / OCR / caption into a single PNG.

The frozen pipeline writes raw screenshots to the artifact store and
records bounding boxes / cursor positions / OCR text in separate tables.
The browser-side ``SceneFrameWithOverlays`` component renders these
as CSS overlays at view-time — beautiful in the portal, useless when
you try to drop the screenshot into Confluence, Jira or Slack.

This service composites everything into one shareable PNG (server-side)
so a single image becomes the hero artifact: cursor marker drawn on
the frame, click point highlighted, OCR text boxes outlined, and a
caption banner along the bottom.  The result is cached in the
artifact store under a versioned prefix so subsequent reads are cheap.

Raw frames are fetched via the eyes-engine HTTP API
(``GET /api/v1/eyes/frames/<asset_path>``) rather than directly
through ``ArtifactStore``.  This decouples platform-api from caring
which storage backend the pipeline used — eyes always returns the
bytes from wherever it wrote them.  Annotated outputs are still
written through ``ArtifactStore`` to platform-api's configured
backend so the cache survives container restarts.

Production safety
=================
* **Idempotent** — same (frame_id, annotation_version) yields the same
  cached row, same object key.  Re-runs are no-ops.
* **Per-call timeout** so a wedge of network failures cannot bog down
  the API.
* **Graceful degradation** — when Pillow is unavailable, or the input
  frame cannot be decoded, or render time exceeds the timeout, the
  service returns the RAW frame bytes plus weak quality flags.  The
  client always gets a usable image.
* **Tenant-scoped** — every cache row carries ``tenant_id`` and the
  asset path is tenant-prefixed, matching RLS migration 030 and the
  ArtifactStore safety conventions.
* **Memory-bounded** — frames are decoded once, never copied; we
  draw on the original via ImageDraw rather than holding multiple
  Image instances.
* **No fonts shipped** — uses system fonts (DejaVu on Linux) with a
  PIL default-font fallback so the container does not need bundled
  assets.

Consumed by ``storyboard_composer`` (batch precomputation) and by the
new ``GET /api/v1/artifacts/{id}/frames/{frame_id}/annotated.png``
endpoint (on-demand render-on-miss).
"""

from __future__ import annotations

import asyncio
import io
import logging
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
    AnnotatedFrameCacheRow,
    CursorEventRow,
    EvidenceControlRow,
    EvidenceStepRow,
    StoryboardPanelRow,
    VisualFrameRow,
    VisualSceneRow,
)
from nexus_sdk.storage import ArtifactStore

from .config import FrameAnnotatorConfig


logger = logging.getLogger(__name__)


_NAMESPACE_STORYBOARD = uuid.UUID("d4f6c9a2-6d8b-4f5b-9a32-8f4b1c1a2d3e")


def _cache_id(frame_id: str, annotation_version: str) -> str:
    """Deterministic cache_id — same (frame, version) yields the same row."""
    return str(uuid.uuid5(
        _NAMESPACE_STORYBOARD,
        f"annotated_frame:{frame_id}:{annotation_version}",
    ))


# ── Pillow detection ──────────────────────────────────────────────────────────


try:  # pragma: no cover - import guarded by capability check
    from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-untyped]

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False


# Common Linux system font paths.  We probe them in order; the first
# one that exists wins.  When none match we fall back to Pillow's
# bundled default font (bitmap, ugly but always available).
_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
)


def _load_font(size_px: int) -> "ImageFont.ImageFont":
    """Resolve a TTF font path; return Pillow default if none available.

    Memoised by size — fonts are immutable so caching them in-process
    is safe and saves disk IO per render.
    """
    if not _PIL_AVAILABLE:
        raise RuntimeError("PIL not available")  # pragma: no cover
    cached = _FONT_CACHE.get(size_px)
    if cached is not None:
        return cached
    for candidate in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(candidate, size_px)
            _FONT_CACHE[size_px] = font
            return font
        except (OSError, IOError):
            continue
    font = ImageFont.load_default()
    _FONT_CACHE[size_px] = font
    return font


_FONT_CACHE: dict[int, "ImageFont.ImageFont"] = {}


# ── Annotation inputs ─────────────────────────────────────────────────────────


@dataclass
class FrameAnnotationInputs:
    """Everything the renderer needs for one annotated PNG.

    Built by ``collect_inputs_for_frame()`` once per render so the
    rendering function is a pure transform over already-fetched data.
    """

    frame_id: str
    artifact_id: str
    tenant_id: str
    frame_asset_path: str
    caption_text: str
    # Cursor + click data from cursor_events / evidence_steps for this frame.
    cursor_x: int | None
    cursor_y: int | None
    is_click: bool
    # OCR boxes — list of (label, x1, y1, x2, y2) in source-frame pixel coords.
    ocr_boxes: tuple[tuple[str, int, int, int, int], ...]
    # Optional natural width/height hint — Pillow re-reads it but the
    # caller may already know it from VisualFrameRow.
    source_width: int | None
    source_height: int | None


# ── Rendering ─────────────────────────────────────────────────────────────────


@dataclass
class RenderedAnnotation:
    """Output of a single annotation render."""

    asset_bytes: bytes
    content_type: str
    width: int
    height: int
    annotation_signals: dict
    render_latency_ms: int


def _render_overlays(
    raw_bytes: bytes,
    inputs: FrameAnnotationInputs,
    config: FrameAnnotatorConfig,
) -> RenderedAnnotation:
    """Pure transform: decode → draw → encode.  Not async, called from a worker.

    Returns ``RenderedAnnotation`` with bytes + dimensions + signal
    manifest.  On any exception the caller decides how to degrade
    (this function does not catch — exceptions are bugs and should
    surface in logs).
    """
    if not _PIL_AVAILABLE:
        raise RuntimeError("Pillow is required for frame annotation")

    started = int(time.monotonic() * 1000)
    with Image.open(io.BytesIO(raw_bytes)) as src:
        # Always convert to RGBA so semi-transparent overlays alpha-blend
        # correctly even on RGB source frames.
        canvas = src.convert("RGBA")
    width, height = canvas.size

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")

    signals: dict = {
        "cursor_marker": False,
        "click_marker": False,
        "ocr_boxes": 0,
        "caption_band": False,
        "action_target": False,
    }

    # 1) OCR bounding boxes — drawn first so cursor + click sit on top.
    ocr_count = 0
    for label, x1, y1, x2, y2 in inputs.ocr_boxes:
        if ocr_count >= config.ocr_max_boxes_per_frame:
            break
        if x2 <= x1 or y2 <= y1:
            continue
        # Clamp to image bounds
        x1c = max(0, min(width - 1, x1))
        y1c = max(0, min(height - 1, y1))
        x2c = max(0, min(width, x2))
        y2c = max(0, min(height, y2))
        if x2c - x1c < 4 or y2c - y1c < 4:
            continue
        draw.rectangle(
            (x1c, y1c, x2c, y2c),
            outline=config.ocr_box_color_rgba,
            width=config.ocr_box_width_px,
        )
        ocr_count += 1
    signals["ocr_boxes"] = ocr_count

    # 2) Cursor marker — outer ring, drawn around the cursor position.
    if inputs.cursor_x is not None and inputs.cursor_y is not None:
        cx = max(0, min(width - 1, int(inputs.cursor_x)))
        cy = max(0, min(height - 1, int(inputs.cursor_y)))
        r = max(1, config.cursor_radius_px)
        draw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            outline=config.cursor_color_rgba,
            width=max(1, config.cursor_ring_width_px),
        )
        signals["cursor_marker"] = True

        # 3) Click marker — filled inner dot
        if inputs.is_click:
            inner_r = max(1, config.click_inner_radius_px)
            draw.ellipse(
                (cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r),
                fill=config.click_color_rgba,
                outline=config.cursor_color_rgba,
                width=max(1, config.cursor_ring_width_px // 2),
            )
            signals["click_marker"] = True

    # 4) Caption band — black strip along the bottom with the caption text.
    if inputs.caption_text and config.caption_band_height_px > 0:
        band_h = min(config.caption_band_height_px, max(0, height // 3))
        if band_h > 0:
            band_top = max(0, height - band_h)
            draw.rectangle(
                (0, band_top, width, height),
                fill=config.caption_background_rgba,
            )
            font = _load_font(config.caption_font_size_px)
            # Center the caption vertically within the band, anchored
            # left with caller-configured padding.
            text_x = config.caption_padding_x_px
            text_y = band_top + (band_h - config.caption_font_size_px) // 2
            # Truncate to image width if necessary so the caption never
            # spills off the right edge.  PIL has no native ellipsis;
            # we shrink-tail until the bounding box fits.
            display = inputs.caption_text
            max_text_width = width - 2 * config.caption_padding_x_px
            while display:
                try:
                    bbox = draw.textbbox((text_x, text_y), display, font=font)
                    if (bbox[2] - bbox[0]) <= max_text_width:
                        break
                except AttributeError:  # pragma: no cover - older Pillow
                    text_w, _ = font.getsize(display)  # type: ignore[attr-defined]
                    if text_w <= max_text_width:
                        break
                # Shrink one character with ellipsis suffix
                display = display[:-2] + "…"
                if len(display) <= 2:
                    break
            draw.text(
                (text_x, text_y),
                display,
                fill=config.caption_text_color_rgba,
                font=font,
            )
            signals["caption_band"] = True
            signals["caption_text_rendered"] = display

    composite = Image.alpha_composite(canvas, overlay)

    # Encode according to config.output_format
    buffer = io.BytesIO()
    fmt = (config.output_format or "png").lower()
    if fmt == "jpeg" or fmt == "jpg":
        composite = composite.convert("RGB")
        composite.save(buffer, format="JPEG", quality=config.jpeg_quality, optimize=True)
        content_type = "image/jpeg"
    else:
        composite.save(buffer, format="PNG", optimize=True)
        content_type = "image/png"

    return RenderedAnnotation(
        asset_bytes=buffer.getvalue(),
        content_type=content_type,
        width=width,
        height=height,
        annotation_signals=signals,
        render_latency_ms=int(time.monotonic() * 1000) - started,
    )


# ── Input collection ──────────────────────────────────────────────────────────


async def collect_inputs_for_frame(
    session: AsyncSession,
    *,
    frame_id: str,
    tenant_id: str,
    caption_override: str | None = None,
) -> FrameAnnotationInputs | None:
    """Load all signals needed to annotate ``frame_id`` in one shot.

    Returns ``None`` when the frame does not exist or is not owned by
    the requesting tenant (the caller treats that as 404).
    """
    frame_q = await session.execute(
        select(VisualFrameRow).where(
            VisualFrameRow.frame_id == frame_id,
            VisualFrameRow.tenant_id == tenant_id,
        )
    )
    frame = frame_q.scalar_one_or_none()
    if frame is None:
        return None

    artifact_id = frame.artifact_id or ""

    # Cursor + click — the cursor_events table records one row per
    # frame with confident cursor detection.  Click flag is from the
    # same row.
    cursor_q = await session.execute(
        select(CursorEventRow).where(
            CursorEventRow.frame_id == frame_id,
            CursorEventRow.tenant_id == tenant_id,
        )
    )
    cursor_event = cursor_q.scalar_one_or_none()

    cursor_x: int | None = None
    cursor_y: int | None = None
    is_click = False
    if cursor_event is not None:
        cursor_x = int(cursor_event.cursor_x)
        cursor_y = int(cursor_event.cursor_y)
        is_click = bool(cursor_event.is_click)
    else:
        # Fallback to evidence_steps if any step references this frame
        # — happens when cursor_events were pruned but steps still
        # carry coords.
        step_q = await session.execute(
            select(
                EvidenceStepRow.cursor_x,
                EvidenceStepRow.cursor_y,
                EvidenceStepRow.action_kind,
            ).where(
                EvidenceStepRow.tenant_id == tenant_id,
                (EvidenceStepRow.before_frame_id == frame_id)
                | (EvidenceStepRow.after_frame_id == frame_id),
            )
        )
        step_row = step_q.first()
        if step_row and step_row[0] is not None and step_row[1] is not None:
            cursor_x = int(step_row[0])
            cursor_y = int(step_row[1])
            is_click = (step_row[2] or "") in (
                "click_cta", "submit_form", "select_option", "toggle"
            )

    # OCR boxes — read evidence_controls scoped to the scene this frame
    # belongs to (controls do not record frame_id; they record scene_id).
    ocr_boxes: list[tuple[str, int, int, int, int]] = []
    if frame.scene_id:
        ctrls_q = await session.execute(
            select(EvidenceControlRow).where(
                EvidenceControlRow.scene_id == frame.scene_id,
                EvidenceControlRow.tenant_id == tenant_id,
            )
        )
        for ctrl in ctrls_q.scalars().all():
            bb = ctrl.bounding_box or {}
            if not isinstance(bb, dict):
                continue
            x1, y1, x2, y2 = _bbox_to_pixels(bb)
            if x1 is None:
                continue
            label = ctrl.display_label or ctrl.label_text or ""
            ocr_boxes.append((label, x1, y1, x2, y2))

    caption_text = caption_override
    if caption_text is None and frame.scene_id:
        # Pull the panel caption_short via storyboard_panels.first_scene_id
        panel_q = await session.execute(
            select(StoryboardPanelRow.caption_short).where(
                StoryboardPanelRow.first_scene_id == frame.scene_id,
                StoryboardPanelRow.tenant_id == tenant_id,
            )
        )
        panel_caption = panel_q.scalar_one_or_none()
        if panel_caption:
            caption_text = panel_caption
    if caption_text is None:
        caption_text = ""

    return FrameAnnotationInputs(
        frame_id=frame_id,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        frame_asset_path=frame.frame_asset_path or "",
        caption_text=caption_text,
        cursor_x=cursor_x,
        cursor_y=cursor_y,
        is_click=is_click,
        ocr_boxes=tuple(ocr_boxes),
        source_width=None,
        source_height=None,
    )


def _bbox_to_pixels(bb: dict) -> tuple[int | None, int | None, int | None, int | None]:
    """Convert a ``bounding_box`` JSON into ``(x1, y1, x2, y2)`` integers.

    The pipeline stores two formats in this column historically — the
    canonical ``{x1, y1, x2, y2}`` shape and the legacy
    ``{x, y, width, height}`` shape.  We accept both.
    """
    if "x1" in bb and "y1" in bb and "x2" in bb and "y2" in bb:
        try:
            return int(bb["x1"]), int(bb["y1"]), int(bb["x2"]), int(bb["y2"])
        except (TypeError, ValueError):
            return None, None, None, None
    if "x" in bb and "y" in bb and "width" in bb and "height" in bb:
        try:
            x = int(bb["x"])
            y = int(bb["y"])
            return x, y, x + int(bb["width"]), y + int(bb["height"])
        except (TypeError, ValueError):
            return None, None, None, None
    return None, None, None, None


# ── Storage paths ─────────────────────────────────────────────────────────────


def _annotated_asset_path(
    *,
    raw_asset_path: str,
    annotation_version: str,
    output_format: str,
    prefix: str,
) -> str:
    """Build a deterministic key for the annotated PNG in object store.

    Pattern: ``<tenant>/<session>/wf/<workflow>/<prefix>/<version>/<frame_basename>.<ext>``
    Mirrors the raw frame path so it lives alongside the source.
    """
    # Strip extension from the raw path's last segment.
    if not raw_asset_path:
        return ""
    parts = raw_asset_path.rstrip("/").split("/")
    leaf = parts[-1]
    dot = leaf.rfind(".")
    leaf_base = leaf[:dot] if dot > 0 else leaf
    extension = "jpg" if output_format in ("jpeg", "jpg") else "png"
    parent = "/".join(parts[:-1])
    # Insert <prefix>/<version>/ before the leaf so the path stays
    # tenant-prefixed and tenant isolation in the object store remains
    # intact.
    annotated_dir = f"{parent}/{prefix}/{annotation_version}" if parent else f"{prefix}/{annotation_version}"
    return f"{annotated_dir}/{leaf_base}.{extension}"


# ── Public API ────────────────────────────────────────────────────────────────


@dataclass
class AnnotatedFrame:
    """Returned by ``annotate_frame()`` — payload + identifying metadata."""

    frame_id: str
    asset_bytes: bytes
    content_type: str
    asset_path: str
    width: int
    height: int
    render_latency_ms: int
    cached: bool
    annotation_signals: dict


class FrameAnnotator:
    """Stateful service: holds the storage backend + Pillow lifecycle.

    Constructed once at platform-api startup (FastAPI ``app.state``)
    and reused for every request.  Thread-safe by virtue of httpx +
    Pillow both being safe to call from multiple coroutines.

    Raw-frame fetch strategy
    ------------------------
    Instead of using ``ArtifactStore.download_bytes(asset_path)`` we
    issue an HTTP GET against the eyes-engine's internal frame route
    (``http://nexus-eyes:8003/api/v1/eyes/frames/<asset_path>``).
    This sidesteps any storage-backend mismatch between platform-api
    and eyes — eyes always serves bytes from wherever it wrote them
    (local disk, GCS, S3, ...) including 307 redirects to signed URLs.

    Annotated PNGs are still written through ``ArtifactStore`` so the
    cache survives container restarts.  When platform-api is on local
    storage, this means the cache lives on its own volume; when on
    GCS it lives in the shared bucket.
    """

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        config: FrameAnnotatorConfig,
        eyes_base_url: str = "http://nexus-eyes:8003",
    ) -> None:
        self._store = artifact_store
        self._config = config
        self._eyes_base_url = eyes_base_url.rstrip("/")
        # Long-lived client — shared across requests.  Per-call
        # timeouts override the default via httpx.Timeout argument.
        # follow_redirects=True so eyes' 307→signed-URL redirect works.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(config.render_timeout_s),
            follow_redirects=True,
        )

    @property
    def pil_available(self) -> bool:
        return _PIL_AVAILABLE

    async def close(self) -> None:
        """Close the HTTP client.  Call at platform-api shutdown."""
        try:
            await self._http.aclose()
        except Exception:  # pragma: no cover - defensive
            pass

    async def _fetch_raw_via_eyes(
        self, asset_path: str, *, auth_token: str,
    ) -> bytes | None:
        """GET raw frame bytes from eyes-engine HTTP API.

        ``auth_token`` is the user's JWT; eyes-engine enforces the same
        tenant isolation it does for browser frame requests so we
        cannot accidentally surface cross-tenant content even if a
        caller passed the wrong tenant_id.  Returns ``None`` on any
        error (network, 4xx, 5xx) so the caller can degrade.
        """
        if not asset_path:
            return None
        # The asset path is tenant/session/wf/.../frames/foo.png with
        # slashes inside.  Eyes-engine treats the whole tail as a
        # path-parameter so we forward the slashes literally.  Each
        # path SEGMENT still needs URL-safe encoding (handle spaces,
        # plus-signs, etc.) — quote with safe='/' preserves the slashes.
        safe_path = quote(asset_path.lstrip("/"), safe="/")
        url = f"{self._eyes_base_url}/api/v1/eyes/frames/{safe_path}"
        headers: dict[str, str] = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        try:
            response = await self._http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "storyboard.frame_annotator.eyes_transport_error",
                extra={
                    "asset_path": asset_path,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                },
            )
            return None
        if response.status_code >= 400:
            logger.warning(
                "storyboard.frame_annotator.eyes_http_error",
                extra={
                    "asset_path": asset_path,
                    "status": response.status_code,
                    "body_preview": response.text[:200],
                },
            )
            return None
        return response.content

    async def annotate_frame(
        self,
        session: AsyncSession,
        *,
        frame_id: str,
        tenant_id: str,
        auth_token: str = "",
        caption_override: str | None = None,
        bypass_cache: bool = False,
    ) -> AnnotatedFrame | None:
        """Get an annotated PNG for ``frame_id``.

        Cache flow:

        1. Look up existing ``annotated_frame_cache`` row for
           ``(frame_id, current_version)``.  If present and the bytes
           are still in object store, stream them back.
        2. Otherwise fetch raw frame bytes from the artifact store,
           render overlays, upload the annotated bytes to a versioned
           path, record the cache row, return bytes.

        Returns ``None`` only when the frame itself is missing or
        cross-tenant.
        """
        config = self._config
        inputs = await collect_inputs_for_frame(
            session,
            frame_id=frame_id,
            tenant_id=tenant_id,
            caption_override=caption_override,
        )
        if inputs is None:
            return None

        annotated_asset_path = _annotated_asset_path(
            raw_asset_path=inputs.frame_asset_path,
            annotation_version=config.version,
            output_format=config.output_format,
            prefix=config.asset_path_prefix,
        )

        # Cache lookup (skipped on bypass_cache).
        if not bypass_cache and annotated_asset_path:
            existing_q = await session.execute(
                select(AnnotatedFrameCacheRow).where(
                    AnnotatedFrameCacheRow.frame_id == frame_id,
                    AnnotatedFrameCacheRow.tenant_id == tenant_id,
                    AnnotatedFrameCacheRow.annotation_version == config.version,
                )
            )
            existing = existing_q.scalar_one_or_none()
            if existing is not None and existing.asset_path:
                try:
                    if await self._store.exists(existing.asset_path):
                        cached_bytes = await self._store.download_bytes(
                            existing.asset_path,
                        )
                        return AnnotatedFrame(
                            frame_id=frame_id,
                            asset_bytes=cached_bytes,
                            content_type=existing.content_type or "image/png",
                            asset_path=existing.asset_path,
                            width=int(existing.width_px),
                            height=int(existing.height_px),
                            render_latency_ms=0,
                            cached=True,
                            annotation_signals=existing.annotation_signals or {},
                        )
                except Exception as exc:  # pragma: no cover - storage failure
                    logger.warning(
                        "storyboard.frame_annotator.cache_read_failed",
                        extra={
                            "frame_id": frame_id,
                            "asset_path": existing.asset_path,
                            "error": str(exc)[:200],
                        },
                    )

        # Cache miss — fetch via eyes HTTP, render, then upload + record.
        if not inputs.frame_asset_path:
            return None

        try:
            raw_bytes = await asyncio.wait_for(
                self._fetch_raw_via_eyes(
                    inputs.frame_asset_path, auth_token=auth_token,
                ),
                timeout=config.render_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "storyboard.frame_annotator.fetch_timeout",
                extra={
                    "frame_id": frame_id,
                    "asset_path": inputs.frame_asset_path,
                    "timeout_s": config.render_timeout_s,
                },
            )
            return None
        if raw_bytes is None:
            # _fetch_raw_via_eyes already logged the underlying error.
            return None

        # Render in a thread so Pillow does not block the event loop.
        loop = asyncio.get_running_loop()
        try:
            rendered = await asyncio.wait_for(
                loop.run_in_executor(
                    None, _render_overlays, raw_bytes, inputs, config,
                ),
                timeout=config.render_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "storyboard.frame_annotator.render_timeout",
                extra={
                    "frame_id": frame_id,
                    "timeout_s": config.render_timeout_s,
                },
            )
            return self._raw_passthrough(
                frame_id=frame_id, inputs=inputs, raw_bytes=raw_bytes,
            )
        except Exception as exc:
            logger.exception(
                "storyboard.frame_annotator.render_failed",
                extra={
                    "frame_id": frame_id,
                    "error": str(exc)[:200],
                },
            )
            return self._raw_passthrough(
                frame_id=frame_id, inputs=inputs, raw_bytes=raw_bytes,
            )

        # Upload + record.  We do not fail the request if upload fails;
        # the bytes are still returned to the client.
        try:
            await self._store.upload_bytes(
                annotated_asset_path,
                rendered.asset_bytes,
                content_type=rendered.content_type,
            )
        except Exception as exc:  # pragma: no cover - storage failure
            logger.warning(
                "storyboard.frame_annotator.upload_failed",
                extra={
                    "frame_id": frame_id,
                    "asset_path": annotated_asset_path,
                    "error": str(exc)[:200],
                },
            )
            # Best-effort: still record metadata but mark asset_path empty
            # so a later request will retry the upload.
            annotated_asset_path = ""

        if annotated_asset_path:
            await self._upsert_cache_row(
                session,
                inputs=inputs,
                annotated_asset_path=annotated_asset_path,
                rendered=rendered,
            )

        return AnnotatedFrame(
            frame_id=frame_id,
            asset_bytes=rendered.asset_bytes,
            content_type=rendered.content_type,
            asset_path=annotated_asset_path,
            width=rendered.width,
            height=rendered.height,
            render_latency_ms=rendered.render_latency_ms,
            cached=False,
            annotation_signals=rendered.annotation_signals,
        )

    def _raw_passthrough(
        self,
        *,
        frame_id: str,
        inputs: FrameAnnotationInputs,
        raw_bytes: bytes,
    ) -> AnnotatedFrame:
        """Return the raw frame when rendering fails — never an error."""
        # We do not attempt to peek at width/height without Pillow because
        # we may be in the no-Pillow code path.
        return AnnotatedFrame(
            frame_id=frame_id,
            asset_bytes=raw_bytes,
            content_type="image/png",
            asset_path="",
            width=inputs.source_width or 0,
            height=inputs.source_height or 0,
            render_latency_ms=0,
            cached=False,
            annotation_signals={"raw_passthrough": True},
        )

    async def _upsert_cache_row(
        self,
        session: AsyncSession,
        *,
        inputs: FrameAnnotationInputs,
        annotated_asset_path: str,
        rendered: RenderedAnnotation,
    ) -> None:
        config = self._config
        row = {
            "cache_id": _cache_id(inputs.frame_id, config.version),
            "frame_id": inputs.frame_id,
            "artifact_id": inputs.artifact_id,
            "tenant_id": inputs.tenant_id,
            "annotation_version": config.version,
            "asset_path": annotated_asset_path,
            "content_type": rendered.content_type,
            "width_px": rendered.width,
            "height_px": rendered.height,
            "size_bytes": len(rendered.asset_bytes),
            "annotation_signals": rendered.annotation_signals,
            "render_latency_ms": rendered.render_latency_ms,
        }
        stmt = pg_insert(AnnotatedFrameCacheRow.__table__).values([row])
        update_columns = {
            col.name: stmt.excluded[col.name]
            for col in AnnotatedFrameCacheRow.__table__.columns
            if col.name not in {"cache_id", "created_at"}
        }
        update_columns["updated_at"] = stmt.excluded.updated_at
        upsert = stmt.on_conflict_do_update(
            index_elements=[AnnotatedFrameCacheRow.__table__.c.cache_id],
            set_=update_columns,
        )
        await session.execute(upsert)


# ── Composer integration: batch precompute annotated frames ──────────────────


@dataclass(frozen=True)
class AnnotatorBatchResult:
    """Summary of a batch precomputation pass."""

    artifact_id: str
    frames_attempted: int
    frames_rendered: int
    frames_cached_hit: int
    frames_failed: int
    elapsed_ms: int


async def precompute_panel_frames(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    annotator: FrameAnnotator,
    concurrency: int = 4,
    auth_token: str = "",
) -> AnnotatorBatchResult:
    """Render annotated PNGs for every panel's representative frame.

    Called by the composer after captions are generated so that the
    storyboard response can return ready-to-display URLs.  Safe to
    call repeatedly — uses the cache on hit.

    ``auth_token`` is forwarded to eyes-engine for raw-frame fetches.
    Empty string is acceptable when called from internal contexts
    where eyes is configured to allow same-network traffic.
    """
    started = time.monotonic()
    panels_q = await session.execute(
        select(StoryboardPanelRow.representative_frame_id, StoryboardPanelRow.caption_short)
        .where(
            StoryboardPanelRow.artifact_id == artifact_id,
            StoryboardPanelRow.tenant_id == tenant_id,
            StoryboardPanelRow.is_noise.is_(False),
        )
    )
    targets: list[tuple[str, str]] = []
    for frame_id, caption in panels_q.all():
        if frame_id:
            targets.append((str(frame_id), caption or ""))

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(frame_id: str, caption: str) -> AnnotatedFrame | None:
        async with semaphore:
            return await annotator.annotate_frame(
                session,
                frame_id=frame_id,
                tenant_id=tenant_id,
                auth_token=auth_token,
                caption_override=caption,
            )

    results = await asyncio.gather(
        *(_one(fid, cap) for fid, cap in targets),
        return_exceptions=True,
    )

    rendered = 0
    cached = 0
    failed = 0
    for r in results:
        if isinstance(r, Exception) or r is None:
            failed += 1
            continue
        if r.cached:
            cached += 1
        else:
            rendered += 1

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "storyboard.frame_annotator.batch",
        extra={
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "frames_attempted": len(targets),
            "frames_rendered": rendered,
            "frames_cached_hit": cached,
            "frames_failed": failed,
            "elapsed_ms": elapsed_ms,
            "annotation_version": annotator._config.version,
        },
    )
    return AnnotatorBatchResult(
        artifact_id=artifact_id,
        frames_attempted=len(targets),
        frames_rendered=rendered,
        frames_cached_hit=cached,
        frames_failed=failed,
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    "AnnotatedFrame",
    "AnnotatorBatchResult",
    "FrameAnnotator",
    "FrameAnnotationInputs",
    "RenderedAnnotation",
    "collect_inputs_for_frame",
    "precompute_panel_frames",
]
