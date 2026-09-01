"""
Flow Segmenter — Phase 5b algorithm.

Splits a flat list of scenes into independent ``FlowGroup`` objects, each
representing one testable business process (e.g., "Guardian Life Insurance
Quote Journey").  Scenes within a flow share a coherent domain or app context.

Flow IDs are **deterministic**: uuid5(NAMESPACE_OID, "flow:{artifact_id}:{flow_index}")
so that repeated runs for the same artifact produce identical IDs and DB
upserts are idempotent via ON CONFLICT DO NOTHING.

No external dependencies — pure Python computation over scene dicts.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

# Namespace for deterministic IDs (shared convention across SDK evidence modules)
_NS = uuid.NAMESPACE_OID

_URL_DOMAIN_RE = re.compile(r"https?://(?:www\.)?([^/\s]+)")

# ── Configuration ──────────────────────────────────────────────

# Signal weights for boundary scoring
_W_DOMAIN = 0.40
_W_APP_TYPE = 0.25
_W_TEMPORAL = 0.20
_W_CONTENT = 0.15

# Threshold for combined boundary score → new flow
_BOUNDARY_THRESHOLD = 0.50

# Temporal gap above which a new flow may start (ms)
_GAP_THRESHOLD_MS = 15_000

# Jaccard thresholds for content discontinuity signal
_CONTENT_BREAK_THRESHOLD = 0.10   # below → supports break
_CONTENT_SAME_THRESHOLD = 0.40    # above → supports same flow

# Noise detection
_MIN_OCR_WORDS_FOR_FLOW = 5

# Human-readable app type labels
_APP_TYPE_LABELS: dict[str, str] = {
    "web_ui": "Web Application",
    "email_client": "Email",
    "mainframe_3270": "Mainframe",
    "pdf_document": "PDF Document",
    "excel_spreadsheet": "Spreadsheet",
    "terminal": "Terminal",
    "database_ui": "Database",
    "desktop_app": "Desktop Application",
    "unknown": "Application",
}


# ── Data class ─────────────────────────────────────────────────

@dataclass
class FlowGroup:
    """One testable business process extracted from a recording."""
    flow_id: str
    artifact_id: str
    session_id: str
    tenant_id: str
    flow_index: int
    flow_label: str
    domain: str
    app_type: str
    scene_ids: list[str] = field(default_factory=list)
    first_scene_index: int = 0
    last_scene_index: int = 0
    is_noise: bool = False
    confidence: float = 1.0
    # Set True when user returned to this app after visiting another (interleaved session)
    is_interleaved: bool = False
    # How many distinct time-contiguous visit segments this flow contains
    visit_count: int = 1


# ── Helpers ────────────────────────────────────────────────────

def _registered_domain(url: str) -> str:
    """Cheap eTLD+1 from a URL."""
    m = _URL_DOMAIN_RE.match(url)
    if not m:
        return ""
    host = m.group(1)
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _ocr_words(scene: dict) -> frozenset[str]:
    """Return meaningful word-set from scene OCR text."""
    text = (scene.get("ocr_text") or "").lower()
    return frozenset(re.findall(r"\b[a-z0-9]{2,}\b", text))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _scene_domain(scene: dict) -> str:
    url = (scene.get("detected_url") or "").strip()
    return _registered_domain(url)


def _scene_app_type(scene: dict) -> str:
    return (
        scene.get("app_type")
        or scene.get("application_type")
        or "unknown"
    ).strip().lower()


def _scene_end_ms(scene: dict) -> int:
    return int(scene.get("end_ms", 0))


def _scene_start_ms(scene: dict) -> int:
    return int(scene.get("start_ms", 0))


# ── Boundary scoring ──────────────────────────────────────────

def _compute_boundary_score(prev: dict, curr: dict) -> float:
    """Compute weighted boundary score between two consecutive scenes.

    Returns a float in [0, 1].  Score > _BOUNDARY_THRESHOLD means new flow.
    """
    score = 0.0

    # Signal 1: Domain clustering
    prev_domain = _scene_domain(prev)
    curr_domain = _scene_domain(curr)
    if prev_domain and curr_domain:
        if prev_domain != curr_domain:
            score += _W_DOMAIN  # different domain → strong boundary
        # Same domain → no contribution (supports same flow)
    elif prev_domain or curr_domain:
        # One has domain, other doesn't → mild boundary signal
        score += _W_DOMAIN * 0.5

    # Signal 2: App type boundary
    prev_type = _scene_app_type(prev)
    curr_type = _scene_app_type(curr)
    if prev_type != curr_type:
        score += _W_APP_TYPE
    elif prev_type == "web_ui" and curr_type == "web_ui":
        # Same app_type=web_ui but different domains → boundary
        if prev_domain and curr_domain and prev_domain != curr_domain:
            score += _W_APP_TYPE * 0.8

    # Signal 3: Temporal gap
    gap_ms = _scene_start_ms(curr) - _scene_end_ms(prev)
    if gap_ms > _GAP_THRESHOLD_MS:
        score += _W_TEMPORAL

    # Signal 4: Content discontinuity
    prev_words = _ocr_words(prev)
    curr_words = _ocr_words(curr)
    sim = _jaccard(prev_words, curr_words)
    if sim < _CONTENT_BREAK_THRESHOLD:
        score += _W_CONTENT  # very different content → supports break
    elif sim < _CONTENT_SAME_THRESHOLD:
        score += _W_CONTENT * 0.5  # moderate difference
    # sim >= _CONTENT_SAME_THRESHOLD → no contribution (similar content)

    return score


# ── Flow label derivation ─────────────────────────────────────

def _derive_flow_label(
    domain: str, app_type: str, first_screen_name: str, flow_index: int,
) -> str:
    """Produce a human-readable flow label.

    Keeps labels short and meaningful — max ~60 chars visible in tabs.
    """
    name_part = first_screen_name.strip()
    # Skip template screen names (e.g. "web_ui screen with 3 buttons")
    if re.match(r"^(\w+_\w+\s+)?screen (with|showing) ", name_part, re.IGNORECASE):
        name_part = ""

    # Truncate name_part to first meaningful phrase (up to 50 chars)
    if name_part:
        # Take only the first clause (before common separators)
        for sep in [" — ", " - ", " | ", ". ", ", "]:
            idx = name_part.find(sep)
            if 5 <= idx <= 50:
                name_part = name_part[:idx]
                break
        name_part = name_part[:50].strip()

    if domain:
        if name_part:
            return f"{domain} - {name_part}"
        return domain

    type_label = _APP_TYPE_LABELS.get(app_type, "")
    if type_label and type_label != "Application":
        if name_part:
            return f"{type_label} - {name_part}"
        return type_label

    if name_part:
        return name_part

    return f"Flow {flow_index + 1}"


# ── Noise detection ───────────────────────────────────────────

def _is_noise_flow(scenes: list[dict], controls_by_scene: dict[str, list] | None = None) -> bool:
    """Detect noise flows (desktop wallpaper, single transitional frames, etc.)."""
    if not scenes:
        return True

    # Single-scene flows with no controls, no URL, and desktop_app type → noise
    if len(scenes) == 1:
        s = scenes[0]
        app_type = _scene_app_type(s)
        has_url = bool((s.get("detected_url") or "").strip())
        sid = s.get("scene_id", "")
        has_controls = bool(controls_by_scene and controls_by_scene.get(sid))
        if app_type == "desktop_app" and not has_url and not has_controls:
            return True

    # Multi-scene flows where ALL scenes are desktop/unknown type AND no scene has
    # a detected URL AND no scene has any business controls.
    # This catches "session setup" recordings: user opening the browser / OS before
    # navigating to the actual application under test.  Such flows add visual noise
    # to the E2E graph without contributing any testable steps.
    _SETUP_TYPES = {"desktop_app", "unknown"}
    if all(_scene_app_type(s) in _SETUP_TYPES for s in scenes):
        any_url = any(bool((s.get("detected_url") or "").strip()) for s in scenes)
        any_controls = any(
            bool(controls_by_scene and controls_by_scene.get(s.get("scene_id", "")))
            for s in scenes
        )
        if not any_url and not any_controls:
            return True

    # Flows with very few OCR words across all scenes → noise
    total_words: set[str] = set()
    for s in scenes:
        total_words.update(_ocr_words(s))
    if len(total_words) < _MIN_OCR_WORDS_FOR_FLOW:
        return True

    return False


# ── Main segmenter ─────────────────────────────────────────────

class FlowSegmenter:
    """Segment scenes into independent business process flows."""

    def segment(
        self,
        scenes: list[dict],
        artifact_id: str = "",
        session_id: str = "",
        tenant_id: str = "",
        controls: list[dict] | None = None,
    ) -> list[FlowGroup]:
        """Walk scenes chronologically and split into FlowGroups.

        Parameters
        ----------
        scenes:      Scene dicts (with app_instance_id from Phase 5).
        artifact_id: Canonical artifact scope.
        session_id:  Session scope.
        tenant_id:   Tenant scope.
        controls:    Optional evidence controls for noise detection.

        Returns
        -------
        List of FlowGroup objects, each with a deterministic flow_id.
        """
        if not scenes:
            return []

        sorted_scenes = sorted(scenes, key=lambda s: int(s.get("scene_index", 0)))

        # Build controls lookup for noise detection
        controls_by_scene: dict[str, list[dict]] = {}
        if controls:
            for c in controls:
                sid = c.get("scene_id", "")
                controls_by_scene.setdefault(sid, []).append(c)

        # ── Phase 1: identify flow boundaries with app-return detection ──
        #
        # "App Identity Pool" tracks (app_type, domain, path_family) → group_index.
        # path_family = first 2 meaningful URL path segments, so that two distinct
        # business flows on the same domain (e.g. usaa.com/checking vs
        # usaa.com/insurance/life) are NOT merged into a single interleaved group.
        # Falls back to (app_type, domain) when URL is absent so that desktop
        # apps (Outlook, mainframe) and URL-less web pages still merge correctly.
        #
        # Example: Outlook→USAA-insurance→Mainframe→USAA-insurance→Outlook→Mainframe
        #   → 3 flows (Outlook, USAA insurance, Mainframe), each with 2 visit
        #     segments and app_switch edges connecting them.
        # Example: USAA-checking→USAA-insurance (boundary, different path_family)
        #   → 2 separate flows, NOT merged.
        groups: list[list[dict]] = []
        # app_identity_pool: (app_type, domain, path_family) → index in groups list
        app_identity_pool: dict[tuple[str, str, str], int] = {}

        # Segments that carry no business meaning and should be ignored in path_family
        _GENERIC_SEGMENTS = {
            "", "index", "home", "default", "main", "app", "portal",
            "www", "web", "page", "pages", "view", "static", "assets",
        }

        def _path_family(url: str) -> str:
            """First 1-2 meaningful URL path segments, normalised.

            usaa.com/insurance/life/quote  → "insurance/life"
            usaa.com/checking/overview     → "checking"
            usaa.com/                      → ""          (same as no-url)
            "" (no URL)                    → ""          (domain-only key)
            """
            if not url:
                return ""
            try:
                parsed = urlparse(url if url.startswith("http") else "https://" + url)
                parts = [p.lower() for p in parsed.path.split("/")
                         if p and p.lower() not in _GENERIC_SEGMENTS]
                return "/".join(parts[:2])
            except Exception:
                return ""

        def _scene_identity(s: dict) -> tuple[str, str, str]:
            """Canonical (app_type, domain, path_family) key for a scene."""
            url = s.get("detected_url") or ""
            return (_scene_app_type(s), _scene_domain(s), _path_family(url))

        def _identity_matches(key: tuple[str, str, str]) -> Optional[int]:
            """Find the best matching group for a return visit.

            Matching priority:
              1. Exact (app_type, domain, path_family) — same business flow.
              2. URL-less / path-less scene that is NOT a web_ui app: desktop
                 and native apps have no meaningful URL path, so matching on
                 (app_type, domain) alone is safe.  For web_ui apps the tier-2
                 fallback is skipped because two different path families on the
                 same domain are likely different tasks, not a return visit.
            """
            # Tier 1: exact match — same app + same domain + same path area
            if key in app_identity_pool:
                return app_identity_pool[key]
            # Tier 2: URL-less non-web scene — path_family is empty and
            # app_type is not web_ui, so safe to merge on (app_type, domain)
            if key[2] == "" and key[0] not in ("web_ui", "unknown"):
                for (at, dom, _pf), idx in app_identity_pool.items():
                    if at == key[0] and dom == key[1]:
                        return idx
            return None

        def _register_identity(s: dict, group_idx: int) -> None:
            key = _scene_identity(s)
            # Register only when we have at least one meaningful signal
            if key[0] not in ("unknown",) or key[1]:
                app_identity_pool.setdefault(key, group_idx)

        # Seed with first scene
        groups.append([sorted_scenes[0]])
        _register_identity(sorted_scenes[0], 0)

        for i in range(1, len(sorted_scenes)):
            prev = sorted_scenes[i - 1]
            curr = sorted_scenes[i]

            score = _compute_boundary_score(prev, curr)
            if score > _BOUNDARY_THRESHOLD:
                # Boundary detected — check if this app was seen before (return visit)
                curr_identity = _scene_identity(curr)
                existing_idx = _identity_matches(curr_identity)
                if existing_idx is not None and (curr_identity[0] != "unknown" or curr_identity[1]):
                    # User returned to a previously seen application — append to its group
                    groups[existing_idx].append(curr)
                else:
                    # Genuinely new application — start a new group
                    new_idx = len(groups)
                    groups.append([curr])
                    _register_identity(curr, new_idx)
            else:
                # No boundary — append to the last group
                groups[-1].append(curr)
                _register_identity(curr, len(groups) - 1)

        # Phase 2: build FlowGroup objects
        flows: list[FlowGroup] = []
        for flow_index, group in enumerate(groups):
            # Deterministic flow_id — keyed on first scene's index so that
            # return-visit merges (same group_index) produce the same UUID
            first_scene_idx = int(group[0].get("scene_index", flow_index))
            flow_id = str(uuid.uuid5(_NS, f"flow:{artifact_id}:{first_scene_idx}"))

            # Primary domain: most common domain across scenes in this group
            domain_counts: dict[str, int] = {}
            for s in group:
                d = _scene_domain(s)
                if d:
                    domain_counts[d] = domain_counts.get(d, 0) + 1
            domain = max(domain_counts, key=domain_counts.get) if domain_counts else ""

            # Primary app_type: most common across scenes
            type_counts: dict[str, int] = {}
            for s in group:
                t = _scene_app_type(s)
                type_counts[t] = type_counts.get(t, 0) + 1
            app_type = max(type_counts, key=type_counts.get) if type_counts else "unknown"

            first_screen_name = (group[0].get("screen_name") or "")
            flow_label = _derive_flow_label(domain, app_type, first_screen_name, flow_index)

            scene_indices = [int(s.get("scene_index", 0)) for s in group]
            scene_ids = [s.get("scene_id", "") for s in group if s.get("scene_id")]

            noise = _is_noise_flow(group, controls_by_scene)

            # Confidence: average boundary scores (inverted, since low score = high confidence)
            confidence = 1.0 - (_compute_boundary_score(group[0], group[-1]) if len(group) > 1 else 0.0)
            confidence = max(0.0, min(1.0, confidence))

            # Detect interleaved/return visits: count how many contiguous segments
            # exist in this group (gaps between non-consecutive scene indices = interruption)
            sorted_indices = sorted(scene_indices)
            visit_count = 1
            for si in range(1, len(sorted_indices)):
                # A gap > 1 between consecutive scene_indices means another flow
                # interrupted this one (user switched away and came back)
                if sorted_indices[si] - sorted_indices[si - 1] > 1:
                    visit_count += 1
            is_interleaved = visit_count > 1

            # Annotate flow label with visit count when interleaved
            if is_interleaved:
                flow_label = f"{flow_label} ({visit_count} visits)"

            flows.append(FlowGroup(
                flow_id=flow_id,
                artifact_id=artifact_id,
                session_id=session_id,
                tenant_id=tenant_id,
                flow_index=flow_index,
                flow_label=flow_label,
                domain=domain,
                app_type=app_type,
                scene_ids=scene_ids,
                first_scene_index=min(scene_indices),
                last_scene_index=max(scene_indices),
                is_noise=noise,
                confidence=confidence,
                is_interleaved=is_interleaved,
                visit_count=visit_count,
            ))

        return flows


def flow_groups_to_dicts(flows: list[FlowGroup]) -> list[dict]:
    """Convert FlowGroup dataclasses to plain dicts for DB insertion."""
    return [
        {
            "flow_id": f.flow_id,
            "artifact_id": f.artifact_id,
            "session_id": f.session_id,
            "tenant_id": f.tenant_id,
            "flow_index": f.flow_index,
            "flow_label": f.flow_label,
            "domain": f.domain,
            "app_type": f.app_type,
            "first_scene_index": f.first_scene_index,
            "last_scene_index": f.last_scene_index,
            "scene_count": len(f.scene_ids),
            "is_noise": f.is_noise,
            "confidence": f.confidence,
            "is_interleaved": f.is_interleaved,
            "visit_count": f.visit_count,
            # Used by caller to update visual_scenes.flow_id
            "scene_ids": f.scene_ids,
        }
        for f in flows
    ]
