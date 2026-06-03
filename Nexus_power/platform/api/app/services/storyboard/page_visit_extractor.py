"""Page-visit extractor — URL-keyed log of distinct pages the user visited.

Why we need this
================
The canonical pipeline's ``scene_grouper`` correctly merges visually-
similar consecutive frames into one scene (e.g. all USAA
``/personal-information/*`` sub-pages share the same layout, so they
collapse into one scene).  For QA test generation we need every distinct
URL captured, because each URL represents a separate page in the
journey the test must visit / assert against.

This extractor produces a parallel axis — ``page_visits`` — that does
NOT depend on scene boundaries:

  artifact
    ├─ canonical (FROZEN): visual_scenes, visual_frames, visual_flows
    ├─ storyboard (additive): scene_captions, app_groupings, scene_actions
    └─ page_visits (NEW): URL-keyed sequence of contiguous visits

What this service does
======================
For every recording the extractor:

1. **Reads every frame** of the artifact in chronological order
   (``visual_frames`` ORDER BY ``frame_index``).
2. **Detects a location** per frame using a tier ladder:
     * Tier 1 — regex match against ``frame.extracted_text`` / ``page_title`` /
       ``url_or_path``.  Highest confidence.
     * Tier 2 — copy ``visual_scenes.detected_url`` for the scene that
       owns the frame (when the canonical pipeline already attached a
       URL).
     * Tier 3 — fall back to ``visual_frames.page_title`` (frame-level
       UI title) or the parent scene's ``screen_name``.
     * Tier 4 — fall back to the parent ``app_instances.window_title``.
     * Tier 5 — optional multimodal LLM call against the first frame.
       Off by default; opt in via env.
3. **Canonicalises** the URL host using the artifact's dominant
   canonical_domain (same logic the ``/visual-flow`` router applies to
   scene URLs — kept in sync via a shared helper).
4. **Groups contiguous frames** with the same canonical (host, path,
   query) into ONE page_visit row.  Two visits to the SAME URL with a
   different URL in between are recorded as two distinct rows.
5. **UPSERTs** into ``page_visits`` keyed by
   ``(artifact_id, sequence_index, extractor_version)``.

Production-safety considerations
================================
* **Generic across UI domains.** No hardcoded host lists, no per-
  customer regex tweaks.  Operators can override the URL regex via env
  (``STORYBOARD_PAGE_VISIT_URL_REGEX``) for tenants with unusual URL
  formats but the default works on web / desktop / mainframe / mobile.
* **Idempotent.** Same artifact + same version produces the same row
  set.  Re-running is safe.
* **Tenant-scoped.** Every query filters on ``tenant_id``; the RLS
  session variable enforces the same at the DB layer.
* **Cost-controlled.** Tier 5 LLM inference is OFF by default — the
  deterministic tiers cover ~99% of real recordings.
* **Observable.** Returns a structured ``PageVisitExtractorResult``
  with per-tier counts so operators can monitor extraction quality.

Consumed by:
  * ``page_action_extractor`` — runs per page_visit.
  * ``form_snapshot_extractor`` — runs per page_visit.
  * ``platform/api/app/routers/artifacts.py`` — surfaces page_visits in
    the ``/visual-flow`` response so test exporters and the future UI
    can enumerate every URL.
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
from typing import Iterable, Sequence
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    AppGroupingRow,
    AppInstanceRow,
    PageVisitRow,
    VisualFrameRow,
    VisualSceneRow,
)

from ..llm import (
    CompletionRequest,
    FinishReason,
    ImageContent,
    LLMRouter,
    ToolDefinition,
)
from .config import PageVisitExtractorConfig
from .page_schemas import PageIdentity, PageVisit, PageVisitSource


try:  # pragma: no cover - optional dependency
    from PIL import Image as _PILImage  # type: ignore[import-untyped]

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False


_EYES_BASE_URL = os.environ.get(
    "STORYBOARD_EYES_BASE_URL", "http://nexus-eyes:8003",
).rstrip("/")


logger = logging.getLogger(__name__)


_NAMESPACE_PAGE_VISITS = uuid.UUID("6f8c4b53-2a5b-4a85-94a2-7b3f3a5e9d11")
"""Stable namespace — keeps page_visit_id derivation grouped under one root."""


def _page_visit_id(
    artifact_id: str, sequence_index: int, extractor_version: str,
) -> str:
    """Deterministic uuid5 — same (artifact, sequence, version) → same id."""
    seed = f"page_visit:{artifact_id}:{sequence_index}:{extractor_version}"
    return str(uuid.uuid5(_NAMESPACE_PAGE_VISITS, seed))


# ── Frame-level inputs ────────────────────────────────────────────────────────


@dataclass
class _FrameLocation:
    """Per-frame extracted location + provenance.

    Built once per visual_frames row during the first pass; the
    grouping pass walks this list and emits page_visits when the
    location changes.
    """

    frame_id: str
    frame_index: int
    timestamp_ms: int
    frame_asset_path: str
    scene_id: str

    # Raw extracted location (before canonicalisation).  Empty string
    # means no location could be derived — the frame is "homeless" and
    # will be merged into whichever neighbour visit absorbs it.
    raw_location: str
    source: PageVisitSource

    # Parsed URL parts (when applicable).
    url_host: str = ""
    url_path: str = ""
    url_query: str = ""


# ── URL detection ────────────────────────────────────────────────────────────


def _build_url_pattern(pattern: str) -> re.Pattern:
    """Compile the configured URL regex once per run.

    Raises a clear error on invalid patterns so a misconfigured env var
    fails fast rather than silently producing zero visits.
    """
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(
            f"STORYBOARD_PAGE_VISIT_URL_REGEX is not a valid regex: {exc}",
        ) from exc


# Tier 1.5 fallback — matches PATH-ONLY URL shapes with 2+ segments.
#
# OCR routinely mangles browser address-bar text by:
#   * Dropping the scheme (https://) entirely
#   * Mis-reading the dot in the host as a space ("Msdd com" instead of
#     "Msdd.com")
#   * Splitting words inside the path ("public" + space + "personal-")
#
# After all that, the deeper path segments often survive intact — e.g.
# the OCR text contains literal "/personal-information/military-status"
# even though the host portion was destroyed.  This fallback recovers
# those by matching path-shaped substrings of 2+ segments where each
# segment looks URL-shaped (lowercase letters / digits / hyphens).
#
# 2-segment minimum avoids matching things like trailing "/ok" in
# button labels or "/Yes" in unrelated UI text.
_PATH_ONLY_PATTERN = re.compile(
    r"/[a-z][a-z0-9\-]{2,}(?:/[a-z][a-z0-9\-]{2,}){1,}",
    re.IGNORECASE,
)


def _first_url_match(text: str, pattern: re.Pattern) -> str:
    """Return the strongest URL-shaped substring in ``text`` (or '').

    Tries the configured strict pattern first (scheme or www.).  When
    that misses, falls back to a path-only matcher that recovers OCR-
    corrupted URLs where the host was destroyed but the path survived.

    For path-only matches we pick the LONGEST hit — the deepest path
    is typically the page-specific URL we want to track, while shorter
    paths (e.g. "/insurance") are usually parent-section prefixes that
    appear in nav text alongside the real URL.
    """
    if not text:
        return ""
    match = pattern.search(text)
    if match:
        raw = match.group(0)
        # Strip surrounding punctuation that the regex doesn't consume
        # (trailing periods, commas, closing parens — OCR routinely picks
        # these up as part of the URL).
        return raw.rstrip(".,);:!?\"'>")

    # Tier 1.5 — path-only fallback (OCR-corrupted address bars).
    candidates = _PATH_ONLY_PATTERN.findall(text)
    if not candidates:
        return ""
    longest = max(candidates, key=len)
    return longest.rstrip(".,);:!?\"'>")


def _parse_url_parts(url: str) -> tuple[str, str, str]:
    """Return (host_lower, path, query) for an http(s) URL.

    Empty tuple values when parsing fails.  No exceptions propagate —
    the OCR layer routinely produces near-URL strings that urlparse
    handles but yields odd parts for.
    """
    if not url:
        return "", "", ""
    try:
        candidate = url if "://" in url else "https://" + url
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").strip().lower()
        path = parsed.path or ""
        query = parsed.query or ""
        return host, path, query
    except Exception:  # pragma: no cover - defensive
        return "", "", ""


# ── Per-artifact data assembly ───────────────────────────────────────────────


@dataclass
class _ArtifactSignals:
    """Pre-loaded artifact-wide context the extractor needs.

    Computed once per ``extract_page_visits_for_artifact`` call so the
    per-frame loop stays cheap.
    """

    artifact_id: str
    tenant_id: str
    session_id: str

    # All frames in the artifact, chronological order.
    frames: list[VisualFrameRow] = field(default_factory=list)

    # scene_id → VisualSceneRow lookup (for tier 2 fallback).
    scene_by_id: dict[str, VisualSceneRow] = field(default_factory=dict)

    # app_instance_id → AppInstanceRow lookup (for tier 4 fallback).
    app_instance_by_id: dict[str, AppInstanceRow] = field(default_factory=dict)

    # Dominant canonical_domain (from app_groupings) — used to rewrite
    # OCR-ghost hostnames to the canonical form.  Empty string means we
    # could not determine a dominant domain; the extractor preserves the
    # raw URL host in that case.
    dominant_canonical_host: str = ""

    # True when the artifact is effectively single-app (no rival clean
    # domain).  Gates the aggressive "rewrite every non-dominant host to
    # the dominant" rule — see _canonicalise_host.
    dominant_single_app: bool = False


async def _load_artifact_signals(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
) -> _ArtifactSignals | None:
    """Bulk-load every frame + scene + app_instance for one artifact.

    Five aggregate selects keep the per-frame loop O(N) without DB
    round-trips.  Returns ``None`` when the artifact has no frames yet
    (the extractor cannot produce anything useful in that case).
    """
    frames_q = await session.execute(
        select(VisualFrameRow)
        .where(
            VisualFrameRow.artifact_id == artifact_id,
            VisualFrameRow.tenant_id == tenant_id,
        )
        .order_by(VisualFrameRow.frame_index.asc())
    )
    frames = list(frames_q.scalars().all())
    if not frames:
        return None

    session_id = frames[0].session_id or ""

    scenes_q = await session.execute(
        select(VisualSceneRow).where(
            VisualSceneRow.artifact_id == artifact_id,
            VisualSceneRow.tenant_id == tenant_id,
        )
    )
    scene_by_id = {s.scene_id: s for s in scenes_q.scalars().all() if s.scene_id}

    insts_q = await session.execute(
        select(AppInstanceRow).where(
            AppInstanceRow.artifact_id == artifact_id,
            AppInstanceRow.tenant_id == tenant_id,
        )
    )
    app_instance_by_id = {
        i.instance_id: i for i in insts_q.scalars().all() if i.instance_id
    }

    dominant, single_app = await _resolve_dominant_canonical_host(
        session, artifact_id=artifact_id, tenant_id=tenant_id,
    )

    return _ArtifactSignals(
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        session_id=session_id,
        frames=frames,
        scene_by_id=scene_by_id,
        app_instance_by_id=app_instance_by_id,
        dominant_canonical_host=dominant,
        dominant_single_app=single_app,
    )


async def _resolve_dominant_canonical_host(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
) -> tuple[str, bool]:
    """Pick the artifact's dominant canonical_domain from app_groupings.

    Mirrors the heuristic used by ``_canonicalize_scene_urls`` in
    ``routers/artifacts.py``.  Returns ``(dominant_host, single_app)``:

      * ``dominant_host`` — lowercase canonical host of the largest
        grouping whose canonical_domain is "clean" (lowercase, has at
        least one dot, no single-letter labels).  Empty string when no
        dominant can be determined.
      * ``single_app`` — True when NO other clean domain has a scene
        count within 50% of the dominant's.  When single_app is True it
        is safe to rewrite every non-dominant host to the dominant
        (OCR ghosts like ``Msdd.com`` for ``usaa.com`` share no
        characters, so only the dominant-host rule can collapse them).
        When False the journey is genuinely multi-app and secondary
        hosts must be preserved.

    Kept self-contained here so the extractor remains testable in
    isolation; the router's heuristic is the authoritative single
    source for the live response, but both follow the same dominant-
    host rule.
    """
    groupings_q = await session.execute(
        select(
            AppGroupingRow.canonical_domain,
            AppGroupingRow.canonical_name,
            AppGroupingRow.total_scene_count,
        )
        .where(
            AppGroupingRow.artifact_id == artifact_id,
            AppGroupingRow.tenant_id == tenant_id,
        )
    )
    rows = list(groupings_q.all())
    if not rows:
        return "", False

    def _is_clean(s: str) -> bool:
        if not s or "." not in s:
            return False
        if s != s.lower():
            return False
        labels = s.split(".")
        return all(len(lbl) >= 2 for lbl in labels)

    def _name_is_ghost(name: str) -> bool:
        """Mirror the router's ``_name_looks_like_ocr_ghost``.

        Real domains render lowercase in the browser bar, so an
        uppercase letter in the canonical_name's head (before the first
        dot) is the dominant OCR-misread signature.  Critically, the
        ``canonical_domain`` is often already lower-cased (e.g.
        ``msdd.com``) so it looks clean — only the ``canonical_name``
        ("Msdd.com") preserves the uppercase tell.  Without this check
        a ghost grouping is mistaken for a legitimate second app and
        the artifact reads as multi-app.
        """
        if not name:
            return False
        head = name.split(".", 1)[0]
        return any(ch.isupper() for ch in head)

    # Candidates = clean domain AND non-ghost name.  A ghost grouping
    # can neither be the dominant nor count as a rival.
    candidates: list[tuple[str, int]] = []
    for domain, name, count in rows:
        d = (domain or "").strip()
        if not _is_clean(d):
            continue
        if _name_is_ghost((name or "").strip()):
            continue
        candidates.append((d.lower(), int(count or 0)))

    if candidates:
        candidates.sort(key=lambda x: x[1], reverse=True)
        dominant, best_count = candidates[0]
        # single_app when no OTHER candidate rivals the dominant
        # (>= 50% of its scene count).
        rival_threshold = max(1, int(best_count * 0.5))
        rivals = [c for _, c in candidates if c >= rival_threshold]
        single_app = len(rivals) <= 1
        return dominant, single_app

    # Fallback: no clean non-ghost candidate.  Accept any host-shaped
    # domain (including ghosts), picking the highest count.  Treat as
    # single-app only when there's exactly one such candidate.
    fallback_dominant = ""
    fallback_best = 0
    host_shaped = 0
    for domain, _name, count in rows:
        d = (domain or "").strip().lower()
        if not d or "." not in d:
            continue
        host_shaped += 1
        c = int(count or 0)
        if c > fallback_best:
            fallback_best = c
            fallback_dominant = d
    return fallback_dominant, host_shaped <= 1


# ── Frame-level location resolution ──────────────────────────────────────────


def _resolve_frame_location(
    frame: VisualFrameRow,
    *,
    scene: VisualSceneRow | None,
    app_instance: AppInstanceRow | None,
    url_pattern: re.Pattern,
    config: PageVisitExtractorConfig,
    dominant_host: str = "",
) -> _FrameLocation:
    """Apply the 4 deterministic tiers (LLM tier handled separately).

    Tier order:
      1. Frame OCR — strongest signal; the URL was literally read off
         the screen.  Also tries a path-only fallback that recovers
         OCR-corrupted URLs where the scheme + host were destroyed
         but path segments survived; in that case we synthesize the
         host from the artifact's dominant canonical_host.
      2. Scene detected_url — the canonical pipeline already attached
         a URL to the parent scene.
      3. Frame page_title / scene screen_name — non-URL UI title text
         when no URL is present.
      4. App instance window_title — desktop / native fallback.
    """
    ts_seconds = float(getattr(frame, "timestamp_seconds", 0.0) or 0.0)
    timestamp_ms = int(ts_seconds * 1000) if ts_seconds else 0

    base = _FrameLocation(
        frame_id=frame.frame_id,
        frame_index=int(frame.frame_index or 0),
        timestamp_ms=timestamp_ms,
        frame_asset_path=frame.frame_asset_path or "",
        scene_id=frame.scene_id or "",
        raw_location="",
        source=PageVisitSource.URL_REGEX,
    )

    # Tier 1 — regex over OCR text, page_title, then url_or_path.
    for candidate_text in (
        frame.extracted_text or "",
        frame.page_title or "",
        frame.url_or_path or "",
    ):
        url = _first_url_match(candidate_text, url_pattern)
        if url:
            host, path, query = _parse_url_parts(url)
            # Path-only match (Tier 1.5): _first_url_match's fallback
            # returned a bare /path/... string with no scheme.  Synthesize
            # the host from the artifact's dominant canonical_host so
            # downstream grouping + URL canonicalisation work uniformly.
            # Two visits with different paths still distinguish; visits
            # to the same path under the dominant host still merge.
            if not host and url.startswith("/") and dominant_host:
                host = dominant_host
                # The raw_location stays as the canonical full URL so
                # the API surface shows a clean https://host/path.
                synthesized = f"https://{dominant_host}{path}"
                if query:
                    synthesized += f"?{query}"
                base.raw_location = synthesized
            else:
                base.raw_location = url
            base.source = PageVisitSource.URL_REGEX
            base.url_host = host
            base.url_path = path
            base.url_query = query
            return base

    # Tier 2 — scene detected_url.
    if scene is not None:
        scene_url = (scene.detected_url or "").strip()
        if scene_url:
            host, path, query = _parse_url_parts(scene_url)
            base.raw_location = scene_url
            base.source = PageVisitSource.URL_SCENE
            base.url_host = host
            base.url_path = path
            base.url_query = query
            return base

    # Tier 3 — frame page_title / scene screen_name (no URL detected).
    if config.enable_screen_name_fallback:
        page_title = (frame.page_title or "").strip()
        screen_name = (scene.screen_name or "").strip() if scene else ""
        candidate = page_title or screen_name
        if candidate:
            base.raw_location = candidate[:2000]
            base.source = PageVisitSource.SCREEN_NAME_OCR
            return base

    # Tier 4 — app_instance window_title.
    if config.enable_window_title_fallback and app_instance is not None:
        wt = (app_instance.window_title or "").strip()
        if wt:
            base.raw_location = wt[:2000]
            base.source = PageVisitSource.WINDOW_TITLE
            return base

    # All tiers missed — leave raw_location empty.  Tier 5 (LLM) runs
    # separately on visits whose location is still empty after grouping.
    return base


# ── LLM tier 5 fallback (optional) ───────────────────────────────────────────


async def _fetch_frame_bytes(
    http_client: httpx.AsyncClient,
    asset_path: str,
    *,
    auth_token: str,
    max_retries: int = 4,
) -> bytes | None:
    """Fetch one frame from eyes-engine with retry-on-429 backoff.

    Identical contract to action_extractor — JWT goes as both Bearer
    header and ``?token=`` query param because eyes-engine only honours
    the query form on the frames route.

    Phase 2 v3 — eyes-engine enforces ~120 req/min and Phase 2 fires
    multiple extractors in parallel.  Backoff: 1s, 2s, 4s, 8s capped.
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
                f"storyboard.page_visit_extractor.frame_transport_error "
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
        f"storyboard.page_visit_extractor.frame_http_error "
        f"asset_path={asset_path!r} url={url!r} "
        f"status={last_status} body={last_body_preview!r} "
        f"after_retries={max_retries}"
    )
    return None


def _maybe_downsize(data: bytes, max_dimension_px: int) -> tuple[bytes, str]:
    """Re-encode to JPEG q85 within the max dimension budget.

    Same rationale as ``action_extractor._maybe_downsize`` — predictable
    payload size + matched media_type for the Anthropic vision API.
    """
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
            "storyboard.page_visit_extractor.image_resize_failed",
            extra={"error": str(exc)[:200]},
        )
        return data, "image/png"


_LLM_INFERENCE_SYSTEM = """You read a single screenshot from a screen recording and return the LOCATION of the page being shown.

If a URL is visible, return JUST the URL (https://... or www...).
If no URL but a clear app/window title is visible, return the title.
If neither is readable, return the empty string.

Return ONLY the location string — no quotes, no prose, no JSON."""


async def _llm_infer_location(
    *,
    router: LLMRouter,
    config: PageVisitExtractorConfig,
    http_client: httpx.AsyncClient,
    auth_token: str,
    frame_asset_path: str,
    correlation_id: str,
) -> str:
    """Run one multimodal LLM call to read the location off a frame.

    Returns an empty string on any failure — the caller treats that as
    "still no location" and the visit gets the deterministic-tier
    answer (which may itself be empty).
    """
    if not frame_asset_path:
        return ""
    raw = await _fetch_frame_bytes(
        http_client, frame_asset_path, auth_token=auth_token,
    )
    if not raw:
        return ""
    raw, media_type = _maybe_downsize(raw, config.image_max_dimension_px)
    image = ImageContent(data=raw, media_type=media_type)

    request = CompletionRequest(
        system=_LLM_INFERENCE_SYSTEM,
        prompt="What is the location of this page? Return only the URL or title.",
        max_tokens=config.llm_max_tokens,
        temperature=config.llm_temperature,
        request_timeout_s=config.request_timeout_s if hasattr(
            config, "request_timeout_s",
        ) else None,
        correlation_id=correlation_id,
        images=(image,),
        metadata={"task": config.llm_task_name},
    )
    try:
        response = await router.complete(
            task=config.llm_task_name, request=request,
        )
    except Exception as exc:  # pragma: no cover - router degrades on tier miss
        logger.info(
            "storyboard.page_visit_extractor.llm_inference_unavailable",
            extra={"error": str(exc)[:200]},
        )
        return ""

    if response.finish_reason == FinishReason.ERROR or not response.text:
        return ""
    candidate = response.text.strip().splitlines()[0].strip()
    # The model can be chatty — strip stray quotes / trailing punctuation.
    candidate = candidate.strip("\"' \t")
    return candidate[:2000]


# ── Vision page-identity pass (generic SPA sub-page recovery) ─────────────────


_VISION_IDENTITY_SYSTEM = """You read ONE screenshot from a screen recording of a web/desktop/mobile workflow and identify the page being shown.

Call the ``record_page_identity`` tool ONCE with:
  - url: the FULL URL from the browser address bar if ANY part is legible — include the deepest path you can read.  Empty string if there is no address bar or it cannot be read.  NEVER invent a URL.
  - page_title: a SHORT label from the page's MAIN heading or the question/field it is asking the user (e.g. "Birth gender", "Height and weight", "Coverage needs", "Military status", "Date of birth").  This is what tells two single-page-app sub-pages apart when the URL does not change.  Empty if there is no clear heading.
  - confidence: 0..1.

Read only what is actually visible.  Do not guess."""


def _page_identity_tool() -> ToolDefinition:
    """LLM tool forcing a PageIdentity-shaped response."""
    return ToolDefinition(
        name="record_page_identity",
        description=(
            "Record the page shown in this screenshot: its address-bar "
            "URL (if legible) and a short heading label."
        ),
        input_schema=PageIdentity.model_json_schema(),
    )


async def _vision_page_identity(
    *,
    router: LLMRouter,
    config: PageVisitExtractorConfig,
    http_client: httpx.AsyncClient,
    auth_token: str,
    frame_asset_path: str,
    correlation_id: str,
) -> PageIdentity | None:
    """Read one frame's page identity (URL + heading) via multimodal LLM.

    Returns ``None`` on any failure (frame fetch, router error, invalid
    output) so the caller keeps the deterministic location.  This is the
    pixel-reading recovery path for SPA sub-pages OCR cannot see.
    """
    if not frame_asset_path:
        return None
    raw = await _fetch_frame_bytes(
        http_client, frame_asset_path, auth_token=auth_token,
    )
    if not raw:
        return None
    raw, media_type = _maybe_downsize(raw, config.image_max_dimension_px)
    image = ImageContent(data=raw, media_type=media_type)
    tool = _page_identity_tool()

    request = CompletionRequest(
        system=_VISION_IDENTITY_SYSTEM,
        prompt=(
            "Identify this page. Read the address-bar URL if legible and "
            "a short heading label. Respond only via record_page_identity."
        ),
        max_tokens=256,
        temperature=0.0,
        correlation_id=correlation_id,
        images=(image,),
        tools=(tool,),
        tool_choice=tool.name,
        enable_prompt_cache=True,
        metadata={"task": config.vision_identity_task_name},
    )
    try:
        response = await router.complete(
            task=config.vision_identity_task_name, request=request,
        )
    except Exception as exc:  # pragma: no cover - router degrades on tier miss
        logger.info(
            f"storyboard.page_visit_extractor.vision_identity_unavailable "
            f"error={str(exc)[:200]}"
        )
        return None

    if response.finish_reason == FinishReason.ERROR:
        return None
    # Prefer the structured tool call; fall back to JSON-in-text.
    if response.tool_calls:
        for call in response.tool_calls:
            if call.name == tool.name:
                try:
                    return PageIdentity.model_validate(call.arguments)
                except ValueError:
                    pass
    if response.text:
        try:
            txt = response.text.strip()
            if txt.startswith("```"):
                nl = txt.find("\n")
                if nl > 0:
                    txt = txt[nl + 1:]
                if txt.endswith("```"):
                    txt = txt[:-3]
            return PageIdentity.model_validate(json.loads(txt))
        except (json.JSONDecodeError, ValueError):
            return None
    return None


async def _apply_vision_page_identity(
    frame_locations: list[_FrameLocation],
    *,
    router: LLMRouter,
    config: PageVisitExtractorConfig,
    http_client: httpx.AsyncClient,
    auth_token: str,
    artifact_id: str,
    dominant_host: str,
) -> int:
    """Override per-frame locations using a bounded vision identity pass.

    Samples up to ``vision_identity_max_frames`` frames evenly across
    the artifact, reads each one's page identity via the LLM, and makes
    the vision-read URL AUTHORITATIVE for that frame so the grouping pass
    separates SPA sub-pages OCR could not.

    URL-first by design (learned the hard way): an earlier version also
    promoted the vision HEADING to the grouping key when no deeper URL
    was found.  Headings vary frame-to-frame ("Birth gender" vs "What's
    your birth gender?") and mix with URL keys, which over-fragmented one
    artifact from 10 to 37 visits.  Now we group ONLY on vision URLs
    (stable) and fall back to a heading label ONLY for frames that have
    no URL from any source at all (genuinely location-less).

    Override rule per sampled frame (confidence >= min):
      * vision URL with >= 2 path segments AND at least as deep as the
        current path  → adopt it (host canonicalised by the grouping
        pass).  Replaces OCR-garbled paths (e.g. "publicfestimate" →
        the clean "/.../estimate") and recovers shell-masked sub-pages.
      * else, only if the frame currently has NO url and NO raw_location
        → use the heading as a last-resort label.

    Returns the number of frames whose location was overridden.
    """
    if not frame_locations:
        return 0

    # Sample frames evenly when over the cap.
    cap = max(1, config.vision_identity_max_frames)
    if len(frame_locations) > cap:
        step = (len(frame_locations) - 1) / max(1, cap - 1)
        idxs = sorted({
            min(len(frame_locations) - 1, int(round(i * step)))
            for i in range(cap)
        })
    else:
        idxs = list(range(len(frame_locations)))

    semaphore = asyncio.Semaphore(max(1, config.vision_identity_concurrency))
    min_conf = config.vision_identity_min_confidence

    # Internal wall-clock budget so this pass ALWAYS returns control to
    # the caller in time for the grouping + UPSERT step to run, even when
    # the vision tier is degraded (e.g. the premium provider is out of
    # credit and every call fails over to a slow local model).  Frames
    # not reached by the deadline simply keep their deterministic
    # location — partial vision enrichment, never a lost derivation.
    # Reserve a slice of the extractor's total budget for persistence.
    budget_s = max(10.0, float(config.total_timeout_s) - 30.0)
    deadline = time.monotonic() + budget_s
    timed_out = 0

    async def _one(i: int) -> tuple[int, PageIdentity | None]:
        nonlocal timed_out
        if time.monotonic() >= deadline:
            return i, None
        loc = frame_locations[i]
        async with semaphore:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return i, None
            try:
                ident = await asyncio.wait_for(
                    _vision_page_identity(
                        router=router,
                        config=config,
                        http_client=http_client,
                        auth_token=auth_token,
                        frame_asset_path=loc.frame_asset_path,
                        correlation_id=f"{artifact_id}:vid:{loc.frame_index}",
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                timed_out += 1
                return i, None
        return i, ident

    results = await asyncio.gather(*(_one(i) for i in idxs))
    if timed_out:
        logger.warning(
            f"storyboard.page_visit_extractor.vision_identity_budget_hit "
            f"artifact={artifact_id} budget_s={budget_s:.0f} "
            f"skipped={timed_out}/{len(idxs)} (deterministic locations kept)"
        )

    overridden = 0
    for i, ident in results:
        if ident is None or ident.confidence < min_conf:
            continue
        loc = frame_locations[i]
        vurl = (ident.url or "").strip()
        vtitle = (ident.page_title or "").strip()

        # Path 1 (primary) — vision read a real URL.  Adopt it when it
        # is a deep path (>= 2 segments) and at least as deep as what we
        # already have, so we never replace a good deep OCR URL with a
        # shallower vision read, but we DO replace shell/garbled paths.
        if vurl:
            vhost, vpath, vquery = _parse_url_parts(vurl)
            cur_depth = len(_path_segments(loc.url_path))
            new_depth = len(_path_segments(vpath))
            if vhost and new_depth >= 2 and new_depth >= cur_depth:
                loc.raw_location = vurl
                loc.url_host = vhost  # canonicalised later by _canonical_key
                loc.url_path = vpath
                loc.url_query = vquery
                loc.source = PageVisitSource.LLM_INFERRED
                overridden += 1
                continue

        # Path 2 (last resort) — only for frames with NO URL from any
        # source.  Use the heading as a coarse label so a genuinely
        # location-less screen still appears.  NOT used to split frames
        # that already have a URL (that was the over-fragmentation bug).
        if vtitle and not loc.url_host and not loc.raw_location:
            loc.raw_location = vtitle[:2000]
            loc.source = PageVisitSource.LLM_INFERRED
            overridden += 1

    if overridden:
        logger.info(
            f"storyboard.page_visit_extractor.vision_identity "
            f"artifact={artifact_id} sampled={len(idxs)} overridden={overridden}"
        )
    return overridden


# ── Grouping pass ────────────────────────────────────────────────────────────


def _canonical_key(
    loc: _FrameLocation, dominant_host: str, single_app: bool,
) -> tuple[str, str, str]:
    """Return the (canonical_host, path, query) key used to group frames.

    Two adjacent frames with the same key get merged into one
    page_visit row.  Non-URL fallbacks (window_title / screen_name)
    use ('', '', raw_location) so two identical window titles in a row
    still merge while differing titles split.
    """
    if loc.url_host:
        canonical_host = _canonicalise_host(loc.url_host, dominant_host, single_app)
        return (canonical_host, loc.url_path, loc.url_query)
    return ("", "", loc.raw_location.lower())


def _canonicalise_host(host: str, dominant_host: str, single_app: bool) -> str:
    """Apply the dominant-host substitution rule.

    Mirrors ``_canonicalize_scene_urls`` in the router so page_visit
    hosts match the hosts shown on scenes.  Rewrites to the dominant
    host in two cases:

      1. The OCR'd host looks like a ghost (mixed-case head, or a label
         shorter than 2 chars) — always rewritten.
      2. ``single_app`` is True and the host simply differs from the
         dominant — rewritten even when it's a clean lowercase domain.
         This is the case the old heuristic missed: an OCR misread like
         ``usaa.com`` → ``Msdd.com`` produces a clean lowercase domain
         that shares NO characters with the real one, so only the
         "single-app artifact → one canonical host" rule can collapse
         it.  Guarded by ``single_app`` so genuine multi-app journeys
         keep their distinct secondary hosts.
    """
    if not host or not dominant_host:
        return host

    h = host.lower()
    if h == dominant_host:
        return h

    head = host.split(".", 1)[0]
    is_dirty = (
        any(ch.isupper() for ch in head)
        or any(len(lbl) < 2 for lbl in host.split("."))
    )
    if is_dirty or single_app:
        return dominant_host
    return h


# ── SPA path-prefix defragmentation ───────────────────────────────────────────


def _path_segments(path: str) -> list[str]:
    """Split a URL path into non-empty segments."""
    return [s for s in (path or "").split("/") if s]


def _can_prefix_merge(a, b, min_segments: int) -> bool:
    """True when two adjacent visit drafts are the same logical SPA page.

    Both must be URL visits (non-empty canonical_host) on the SAME host,
    and the shorter path must be a leading-segment prefix of the longer
    (equal counts as a prefix).  The shorter path must have at least
    ``min_segments`` segments so a shallow landing page like
    ``/insurance`` is never absorbed into a deep flow like
    ``/insurance/life/iwa``.

    Operates by attribute access so it works on the grouping pass's
    local ``_VisitDraft`` dataclass without importing it.
    """
    if not getattr(a, "canonical_host", "") or not getattr(b, "canonical_host", ""):
        return False
    if a.canonical_host != b.canonical_host:
        return False
    sa, sb = _path_segments(a.path), _path_segments(b.path)
    if not sa or not sb:
        return False
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if len(shorter) < min_segments:
        return False
    return longer[: len(shorter)] == shorter


def _merge_path_prefix_fragments(drafts: list, min_segments: int) -> list:
    """Collapse consecutive same-host visits whose paths are in a
    segment-prefix relationship — the single-page-app defragmenter.

    An SPA keeps one address-bar URL while the user moves through many
    client-side steps; the OCR and scene-URL sources disagree on the
    trailing segment frame-to-frame, so one logical page fragments into
    a ping-pong run of short visits (e.g. ``/insurance/life`` ⇄
    ``/insurance/life/iwa`` five times).  This pass walks the draft list
    once and folds each draft into the previous when they are
    prefix-mergeable, adopting the LONGER (more specific) path and
    unioning the time window + frame count.

    Genuinely distinct consecutive pages (different leading segments, or
    a sub-1-segment shallow page) are left untouched.
    """
    if len(drafts) < 2:
        return drafts
    merged = [drafts[0]]
    for d in drafts[1:]:
        prev = merged[-1]
        if _can_prefix_merge(prev, d, min_segments):
            prev.last_seen_ms = max(prev.last_seen_ms, d.last_seen_ms)
            prev.frame_count += d.frame_count
            # Adopt the deeper path so the merged visit carries the most
            # specific URL the user actually reached.
            if len(_path_segments(d.path)) > len(_path_segments(prev.path)):
                prev.path = d.path
                prev.query = d.query
                prev.url_host = d.url_host or prev.url_host
                prev.source = d.source
                prev.raw_location = d.raw_location
            continue
        merged.append(d)
    return merged


def _last_segment(path: str) -> str:
    segs = _path_segments(path)
    return segs[-1] if segs else ""


def _same_page_tail(tail_a: str, tail_b: str, min_substring: int) -> bool:
    """True when two last-path-segments denote the same logical page.

    Exact match, or — to absorb OCR run-together garble like
    ``publicfestimate`` vs the clean ``estimate`` — one is a substring
    of the other and the shorter is at least ``min_substring`` chars
    (so short generic segments like ``state`` only match exactly).
    """
    if not tail_a or not tail_b:
        return False
    if tail_a == tail_b:
        return True
    short, long_ = sorted([tail_a, tail_b], key=len)
    return len(short) >= min_substring and short in long_


def _merge_same_page_tail(drafts: list, min_substring: int) -> list:
    """Collapse visits that are the SAME logical page in different
    representations — the cross-source deduplicator.

    The prefix defragmenter only merges adjacent visits whose paths are
    prefix-related.  But OCR and vision often disagree on the WHOLE
    prefix: OCR yields the truncated tail ``/personal-information/X``
    while vision yields the full ``/insurance/.../personal-information/X``
    — they share the SUFFIX, not a prefix — and these fragments are
    interspersed across the SPA stretch, not adjacent.  This GLOBAL pass
    groups URL visits by (canonical_host, last-path-segment) and keeps a
    single row per logical page (first-seen order preserved, window +
    frames unioned, the deepest/cleanest path adopted).

    Non-URL visits (window_title / screen_name) pass through untouched.
    """
    if len(drafts) < 2:
        return drafts
    result: list = []
    # Each entry: (canonical_host, tail, index_in_result)
    index: list[tuple[str, str]] = []
    for d in drafts:
        host = getattr(d, "canonical_host", "")
        if not host or not getattr(d, "path", ""):
            result.append(d)
            continue
        tail = _last_segment(d.path)
        matched = -1
        for ri, (rhost, rtail) in enumerate(index):
            if rhost == host and _same_page_tail(tail, rtail, min_substring):
                matched = ri
                break
        if matched < 0:
            index.append((host, tail))
            result.append(d)
        else:
            # result index for this logical page: the i-th URL entry maps
            # to its position in result.  Rebuild target by scanning.
            tgt = _nth_url_entry(result, matched)
            if tgt is None:
                index.append((host, tail))
                result.append(d)
                continue
            tgt.first_seen_ms = min(tgt.first_seen_ms, d.first_seen_ms)
            tgt.last_seen_ms = max(tgt.last_seen_ms, d.last_seen_ms)
            tgt.frame_count += d.frame_count
            # Prefer the deeper / cleaner representation.
            if len(_path_segments(d.path)) > len(_path_segments(tgt.path)):
                tgt.path = d.path
                tgt.query = d.query
                tgt.url_host = d.url_host or tgt.url_host
                tgt.source = d.source
                tgt.raw_location = d.raw_location
    return result


def _nth_url_entry(result: list, n: int):
    """Return the n-th URL-bearing draft in ``result`` (matches the
    ordering used to build the (host, tail) index)."""
    count = -1
    for d in result:
        if getattr(d, "canonical_host", "") and getattr(d, "path", ""):
            count += 1
            if count == n:
                return d
    return None


# ── Persistence ──────────────────────────────────────────────────────────────


def _confidence_for_source(source: PageVisitSource) -> float:
    """Default confidence by extraction tier.  Stored on every row."""
    return {
        PageVisitSource.URL_REGEX: 1.0,
        PageVisitSource.URL_SCENE: 0.85,
        PageVisitSource.WINDOW_TITLE: 0.6,
        PageVisitSource.SCREEN_NAME_OCR: 0.6,
        PageVisitSource.LLM_INFERRED: 0.85,
    }.get(source, 0.5)


def _build_page_visit(
    *,
    artifact_id: str,
    tenant_id: str,
    session_id: str,
    sequence_index: int,
    extractor_version: str,
    canonical_host: str,
    path: str,
    query: str,
    raw_location: str,
    source: PageVisitSource,
    first_seen_ms: int,
    last_seen_ms: int,
    frame_count: int,
    primary_scene_id: str | None,
    url_host: str,
) -> PageVisit:
    """Construct one PageVisit Pydantic model ready for persistence."""
    location = raw_location
    if canonical_host and path:
        # Compose a clean canonical URL for storage when the source was
        # URL-shaped — keeps downstream test exporters from re-parsing.
        scheme = "https"
        q = f"?{query}" if query else ""
        location = f"{scheme}://{canonical_host}{path}{q}"

    visit_id = _page_visit_id(artifact_id, sequence_index, extractor_version)
    duration = max(0, last_seen_ms - first_seen_ms)
    return PageVisit(
        page_visit_id=visit_id,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        session_id=session_id,
        sequence_index=sequence_index,
        location=location[:2000],
        url_host=url_host[:500],
        url_path=path[:2000],
        url_query=query[:2000],
        canonical_host=canonical_host[:500],
        first_seen_ms=first_seen_ms,
        last_seen_ms=last_seen_ms,
        duration_ms=duration,
        frame_count=frame_count,
        source=source,
        extraction_confidence=_confidence_for_source(source),
        primary_scene_id=primary_scene_id,
        extractor_version=extractor_version,
    )


async def _upsert_page_visits(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    visits: Sequence[PageVisit],
    extractor_version: str,
) -> int:
    """UPSERT one row per PageVisit; safe to call repeatedly.

    On conflict (artifact_id, sequence_index, extractor_version) we
    update every column EXCEPT the persistence-side metadata
    (page_visit_id is the same; created_at is preserved).  This keeps
    form_snapshot_extractor output intact when the visit extractor
    re-runs at the same version.
    """
    if not visits:
        return 0
    rows = [
        {
            "page_visit_id": v.page_visit_id,
            "artifact_id": v.artifact_id,
            "tenant_id": v.tenant_id,
            "session_id": v.session_id,
            "sequence_index": v.sequence_index,
            "location": v.location,
            "url_host": v.url_host,
            "url_path": v.url_path,
            "url_query": v.url_query,
            "canonical_host": v.canonical_host,
            "first_seen_ms": v.first_seen_ms,
            "last_seen_ms": v.last_seen_ms,
            "duration_ms": v.duration_ms,
            "frame_count": v.frame_count,
            "source": v.source.value,
            "extraction_confidence": v.extraction_confidence,
            "primary_scene_id": v.primary_scene_id,
            "extractor_version": v.extractor_version,
        }
        for v in visits
    ]
    stmt = pg_insert(PageVisitRow.__table__).values(rows)
    # Conflict on the explicit unique constraint, not the PK — the PK
    # is deterministic but the unique constraint represents the
    # logical idempotency key the route uses.
    update_columns = {
        "location": stmt.excluded.location,
        "url_host": stmt.excluded.url_host,
        "url_path": stmt.excluded.url_path,
        "url_query": stmt.excluded.url_query,
        "canonical_host": stmt.excluded.canonical_host,
        "first_seen_ms": stmt.excluded.first_seen_ms,
        "last_seen_ms": stmt.excluded.last_seen_ms,
        "duration_ms": stmt.excluded.duration_ms,
        "frame_count": stmt.excluded.frame_count,
        "source": stmt.excluded.source,
        "extraction_confidence": stmt.excluded.extraction_confidence,
        "primary_scene_id": stmt.excluded.primary_scene_id,
        "updated_at": stmt.excluded.updated_at,
    }
    upsert = stmt.on_conflict_do_update(
        constraint="uq_page_visits_artifact_sequence_version",
        set_=update_columns,
    )
    await session.execute(upsert)
    return len(rows)


# ── Stale-row cleanup ────────────────────────────────────────────────────────


async def _delete_stale_visits(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    extractor_version: str,
    keep_sequence_indices: Iterable[int],
) -> int:
    """Drop page_visits rows that this run's UPSERT did not touch.

    Two cases this guards against:
      * The previous run produced 15 visits; this run produces 12.
        The 3 trailing rows (sequence 12..14) would otherwise stick
        around forever as ghosts.
      * The extractor version was bumped and the previous version's
        rows still exist with a different version stamp.  We only
        delete rows AT THE CURRENT VERSION — older versions are kept
        for audit / rollback.
    """
    keep = set(int(i) for i in keep_sequence_indices)
    # SQLAlchemy 2.x lets us use a plain SQL DELETE with NOT IN.
    from sqlalchemy import and_, delete, not_

    stmt = (
        delete(PageVisitRow)
        .where(
            and_(
                PageVisitRow.artifact_id == artifact_id,
                PageVisitRow.tenant_id == tenant_id,
                PageVisitRow.extractor_version == extractor_version,
                not_(PageVisitRow.sequence_index.in_(keep)) if keep else True,
            )
        )
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


# ── Public entry point ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PageVisitExtractorResult:
    """Summary returned to the composer for observability."""

    artifact_id: str
    frames_scanned: int
    visits_written: int
    visits_via_url_regex: int
    visits_via_url_scene: int
    visits_via_screen_name: int
    visits_via_window_title: int
    visits_via_llm: int
    visits_with_empty_location: int
    stale_visits_deleted: int
    elapsed_ms: int


async def extract_page_visits_for_artifact(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    config: PageVisitExtractorConfig,
    router: LLMRouter | None = None,
    auth_token: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> PageVisitExtractorResult:
    """Run page-visit extraction for one artifact.

    Idempotent.  Safe to call after every pipeline run.  Caller must
    have set the tenant RLS context on the session.  Does NOT commit;
    the caller controls the transaction boundary.

    ``router`` is required only when ``config.enable_llm_inference`` is
    True; pass ``None`` to disable Tier 5 even if env says otherwise.
    """
    started = time.monotonic()

    signals = await _load_artifact_signals(
        session, artifact_id=artifact_id, tenant_id=tenant_id,
    )
    if signals is None:
        return PageVisitExtractorResult(
            artifact_id=artifact_id,
            frames_scanned=0,
            visits_written=0,
            visits_via_url_regex=0,
            visits_via_url_scene=0,
            visits_via_screen_name=0,
            visits_via_window_title=0,
            visits_via_llm=0,
            visits_with_empty_location=0,
            stale_visits_deleted=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    url_pattern = _build_url_pattern(config.url_regex_pattern)

    # ── Per-frame location resolution (tiers 1-4) ──────────────────
    frame_locations: list[_FrameLocation] = []
    for frame in signals.frames:
        scene = signals.scene_by_id.get(frame.scene_id) if frame.scene_id else None
        inst = (
            signals.app_instance_by_id.get(frame.app_instance_id)
            if frame.app_instance_id
            else None
        )
        loc = _resolve_frame_location(
            frame,
            scene=scene,
            app_instance=inst,
            url_pattern=url_pattern,
            config=config,
            dominant_host=signals.dominant_canonical_host,
        )
        frame_locations.append(loc)

    # ── Vision page-identity pass (generic SPA sub-page recovery) ───
    # Runs BEFORE grouping so vision-recovered locations split SPA
    # sub-pages OCR could not see (gender / birthdate / coverage-needs).
    # Bounded + concurrency-limited; cached after first derivation.
    if config.enable_vision_page_identity and router is not None:
        vis_owns_client = http_client is None
        vis_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                config.request_timeout_s
                if getattr(config, "request_timeout_s", None)
                else 60.0
            ),
            follow_redirects=True,
        )
        try:
            await _apply_vision_page_identity(
                frame_locations,
                router=router,
                config=config,
                http_client=vis_client,
                auth_token=auth_token,
                artifact_id=artifact_id,
                dominant_host=signals.dominant_canonical_host,
            )
        except Exception as exc:  # pragma: no cover - degrade gracefully
            logger.warning(
                f"storyboard.page_visit_extractor.vision_identity_failed "
                f"artifact={artifact_id} error={str(exc)[:200]}"
            )
        finally:
            if vis_owns_client:
                try:
                    await vis_client.aclose()
                except Exception:  # pragma: no cover
                    pass

    # ── Group contiguous frames with the same canonical key ────────
    @dataclass
    class _VisitDraft:
        canonical_host: str
        url_host: str
        path: str
        query: str
        raw_location: str
        source: PageVisitSource
        first_seen_ms: int
        last_seen_ms: int
        frame_count: int
        first_frame_asset_path: str
        primary_scene_id: str | None

    drafts: list[_VisitDraft] = []
    current: _VisitDraft | None = None
    current_key: tuple[str, str, str] | None = None
    dominant = signals.dominant_canonical_host
    single_app = signals.dominant_single_app

    for loc in frame_locations:
        # Frames with no detectable location get folded into the
        # PREVIOUS visit when one exists (homeless frames sit between
        # two visits to the same page during a load) and dropped when
        # nothing precedes them.  This is what produces a clean
        # chronological log: brief loading frames don't create
        # spurious empty-location visits.
        key = _canonical_key(loc, dominant, single_app)
        if key == ("", "", ""):
            if current is not None:
                current.last_seen_ms = max(
                    current.last_seen_ms, loc.timestamp_ms,
                )
                current.frame_count += 1
            # else: drop the leading homeless frame entirely.
            continue

        if current is None or key != current_key:
            # Start a new visit.  If config requested merge_adjacent_same_url
            # and the previous visit had the same canonical key (with
            # only a homeless-frame gap), absorb instead.  We've already
            # advanced `current` past the gap above so `current_key`
            # reflects the previous canonical value.
            if (
                config.merge_adjacent_same_url
                and current is not None
                and key == current_key
            ):
                current.last_seen_ms = max(current.last_seen_ms, loc.timestamp_ms)
                current.frame_count += 1
                continue

            current = _VisitDraft(
                canonical_host=key[0],
                url_host=loc.url_host,
                path=loc.url_path,
                query=loc.url_query,
                raw_location=loc.raw_location,
                source=loc.source,
                first_seen_ms=loc.timestamp_ms,
                last_seen_ms=loc.timestamp_ms,
                frame_count=1,
                first_frame_asset_path=loc.frame_asset_path,
                primary_scene_id=loc.scene_id or None,
            )
            current_key = key
            drafts.append(current)
        else:
            current.last_seen_ms = max(current.last_seen_ms, loc.timestamp_ms)
            current.frame_count += 1

    # ── Filter visits that fall below the per-visit minimum frame count
    if config.min_frames_for_visit > 1:
        drafts = [d for d in drafts if d.frame_count >= config.min_frames_for_visit]

    # ── SPA defragmentation — collapse path-prefix ping-pong runs ──
    # Runs BEFORE Tier-5 LLM inference and PageVisit construction so the
    # merged visit's wider time window feeds the downstream page_action
    # and form_snapshot extractors (better frame coverage of the form).
    if config.merge_path_prefix_fragments:
        before = len(drafts)
        drafts = _merge_path_prefix_fragments(
            drafts, config.path_prefix_min_segments,
        )
        if len(drafts) != before:
            logger.info(
                f"storyboard.page_visit_extractor.spa_defrag "
                f"artifact={artifact_id} merged={before}->{len(drafts)}"
            )

    # ── Same-page-tail dedup — collapse cross-source duplicate
    # representations of one logical page (OCR truncated tail vs vision
    # full path; OCR-garbled vs clean).  GLOBAL (not adjacency-bound).
    if config.merge_same_page_tail:
        before = len(drafts)
        drafts = _merge_same_page_tail(
            drafts, config.same_page_tail_min_substring,
        )
        if len(drafts) != before:
            logger.info(
                f"storyboard.page_visit_extractor.tail_dedup "
                f"artifact={artifact_id} merged={before}->{len(drafts)}"
            )

    # ── Tier 5 — optional LLM inference for visits with no location ─
    if (
        config.enable_llm_inference
        and router is not None
        and any(not d.raw_location for d in drafts)
    ):
        owns_client = http_client is None
        if http_client is None:
            http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    config.request_timeout_s
                    if getattr(config, "request_timeout_s", None)
                    else 30.0
                ),
                follow_redirects=True,
            )
        try:
            for d in drafts:
                if d.raw_location:
                    continue
                inferred = await _llm_infer_location(
                    router=router,
                    config=config,
                    http_client=http_client,
                    auth_token=auth_token,
                    frame_asset_path=d.first_frame_asset_path,
                    correlation_id=f"{artifact_id}:{d.first_seen_ms}",
                )
                if not inferred:
                    continue
                # If the LLM returned a URL, parse it.
                host, path, query = _parse_url_parts(inferred)
                if host:
                    d.url_host = host
                    d.path = path
                    d.query = query
                    d.canonical_host = _canonicalise_host(host, dominant, single_app)
                d.raw_location = inferred
                d.source = PageVisitSource.LLM_INFERRED
        finally:
            if owns_client and http_client is not None:
                try:
                    await http_client.aclose()
                except Exception:  # pragma: no cover
                    pass

    # ── Build PageVisit models ─────────────────────────────────────
    visits: list[PageVisit] = []
    for idx, d in enumerate(drafts):
        visits.append(_build_page_visit(
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            session_id=signals.session_id,
            sequence_index=idx,
            extractor_version=config.version,
            canonical_host=d.canonical_host,
            path=d.path,
            query=d.query,
            raw_location=d.raw_location,
            source=d.source,
            first_seen_ms=d.first_seen_ms,
            last_seen_ms=d.last_seen_ms,
            frame_count=d.frame_count,
            primary_scene_id=d.primary_scene_id,
            url_host=d.url_host,
        ))

    written = await _upsert_page_visits(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        visits=visits,
        extractor_version=config.version,
    )
    deleted = await _delete_stale_visits(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        extractor_version=config.version,
        keep_sequence_indices=(v.sequence_index for v in visits),
    )

    by_source = {s: 0 for s in PageVisitSource}
    empty = 0
    for v in visits:
        by_source[v.source] = by_source.get(v.source, 0) + 1
        if not v.location:
            empty += 1

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "storyboard.page_visit_extractor.run",
        extra={
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "frames_scanned": len(frame_locations),
            "visits_written": written,
            "visits_via_url_regex": by_source[PageVisitSource.URL_REGEX],
            "visits_via_url_scene": by_source[PageVisitSource.URL_SCENE],
            "visits_via_screen_name": by_source[PageVisitSource.SCREEN_NAME_OCR],
            "visits_via_window_title": by_source[PageVisitSource.WINDOW_TITLE],
            "visits_via_llm": by_source[PageVisitSource.LLM_INFERRED],
            "stale_visits_deleted": deleted,
            "elapsed_ms": elapsed_ms,
            "extractor_version": config.version,
            "dominant_host": dominant,
        },
    )

    return PageVisitExtractorResult(
        artifact_id=artifact_id,
        frames_scanned=len(frame_locations),
        visits_written=written,
        visits_via_url_regex=by_source[PageVisitSource.URL_REGEX],
        visits_via_url_scene=by_source[PageVisitSource.URL_SCENE],
        visits_via_screen_name=by_source[PageVisitSource.SCREEN_NAME_OCR],
        visits_via_window_title=by_source[PageVisitSource.WINDOW_TITLE],
        visits_via_llm=by_source[PageVisitSource.LLM_INFERRED],
        visits_with_empty_location=empty,
        stale_visits_deleted=deleted,
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    "PageVisitExtractorResult",
    "extract_page_visits_for_artifact",
]
