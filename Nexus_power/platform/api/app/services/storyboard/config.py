"""Storyboard derivation configuration — every knob is env-driven.

No threshold, model name, batch size, prompt template or asset path
pattern is hardcoded inside the services.  Each parameter is read here
from the environment exactly once at startup, exposed as an immutable
dataclass, and passed to the services that need it.  This keeps:

* Production tuning a deploy-time concern (no code changes).
* Tests deterministic (override via the load_config kwargs).
* Re-derivation triggers explicit — bumping ``STORYBOARD_*_VERSION``
  envs forces fresh runs without DDL.

If an env var is missing the documented default is used.  If an env var
is set but unparseable a ``ConfigError`` is raised at module import so
the platform-api container refuses to start with a misconfiguration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Tuple


class ConfigError(ValueError):
    """Raised when an env var is set but cannot be parsed."""


def _env_str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def _env_int(name: str, default: int, *, min_value: int | None = None,
             max_value: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise ConfigError(f"{name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ConfigError(f"{name} must be <= {max_value}, got {value}")
    return value


def _env_float(name: str, default: float, *, min_value: float | None = None,
               max_value: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a float, got {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise ConfigError(f"{name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ConfigError(f"{name} must be <= {max_value}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    normalised = raw.strip().lower()
    if normalised in ("1", "true", "yes", "on"):
        return True
    if normalised in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean string, got {raw!r}")


def _env_tuple(name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class SceneGrouperConfig:
    """Tunables for ``scene_grouper``.

    Defaults are chosen for production traffic — they keep noise out of
    the storyboard while preserving every scene the SME treated as
    meaningfully distinct.  Adjust via env when onboarding a customer
    whose workflows behave differently (e.g. desktop apps with very
    little visible state change between actions).
    """

    # Stamped on every storyboard_panels row; bump to force re-grouping
    # of every artifact in the database without dropping rows.
    version: str = "v1"

    # Two adjacent scenes are considered "same screen" when their OCR
    # text Jaccard similarity is at least this high.  Lower → more
    # collapse (less noise, risk of merging legitimately different
    # states); higher → less collapse (more cards).
    same_screen_jaccard_threshold: float = 0.85

    # Same as above but for the scene_state_summary.screen_title
    # similarity.  When titles match we trust the merge even with lower
    # OCR overlap, because state_summary is the pipeline's own grouping
    # signal.
    same_screen_title_jaccard_threshold: float = 0.92

    # Hard floor: scenes shorter than this are merged into the next
    # scene regardless of similarity (typing a single character can
    # produce a 200ms "scene" the pipeline never collapses).
    merge_below_duration_ms: int = 600

    # OCR confidence below this floors the scene to weak quality and
    # makes it eligible for noise filtering.
    weak_ocr_confidence: float = 0.55

    # Noise classifiers — when the pipeline's screen_name matches any of
    # these patterns the panel gets is_noise=True (hidden from default
    # storyboard).  Substring match, case insensitive.  Add domain-
    # specific patterns via env for customer onboarding without code
    # changes.
    noise_screen_name_patterns: Tuple[str, ...] = (
        "new tab",
        "blank tab",
        "loading…",
        "loading...",
    )

    # Cap the maximum number of scenes a single panel can absorb.  Even
    # if 800 form-fill scenes are technically the same screen, we still
    # want the storyboard to show meaningful boundaries — one panel
    # absorbing 800 scenes will hide too much narrative.
    max_scenes_per_panel: int = 60


@dataclass(frozen=True)
class AppDeduperConfig:
    """Tunables for ``app_deduper``."""

    version: str = "v1"

    # Two app_instances are merged if the Levenshtein distance between
    # their normalised domains is at most this value.  Tuned for OCR
    # corruption ("Wivwquardianlife.com" → "guardianlife.com" requires
    # ~3 substitutions including a 1-char prefix).
    domain_levenshtein_max: int = 3

    # Minimum fraction of shared characters (after normalisation)
    # required for a fuzzy domain merge.  Guards against unrelated
    # short domains accidentally getting merged.
    domain_min_overlap_ratio: float = 0.6

    # When neither domain nor window title give a confident grouping,
    # fall back to the pipeline's app_instance boundaries verbatim.
    # Toggle off for customers whose workflows are exclusively web-based.
    allow_window_title_grouping: bool = True

    # Public-suffix-aware extraction is preferred (so ``co.uk`` domains
    # parse correctly).  When False, fall back to last-two-labels which
    # is good enough for ~95% of US/.com workflows but breaks on
    # country-code TLDs.
    use_public_suffix_list: bool = True

    # Phase 1.5 hardening — Tier 3 fuzzy match.
    # Strip leading [wvqiy]{1,5} prefix runs before retrying fuzzy
    # match.  Catches OCR errors at the start of the domain ("Wivw"
    # for "www.").  Safe in practice because the regex requires the
    # next char to be a consonant, which rules out legitimate domains
    # like ``wikipedia.org`` (next char is a vowel — won't strip).
    enable_ocr_prefix_strip: bool = True

    # Phase 1.5 hardening — Tier 4 shared-suffix merge.
    # When two candidate domains have the same TLD and share this many
    # trailing characters, merge them regardless of total Levenshtein
    # distance.  Defends against long OCR-corrupted prefixes (e.g.
    # ``Wivwquardianlife.com`` vs ``guardianlife.com`` share 15 chars
    # of suffix but Levenshtein is 5).
    enable_shared_suffix_merge: bool = True
    shared_suffix_min_chars: int = 10


@dataclass(frozen=True)
class CaptionRewriterConfig:
    """Tunables for ``caption_rewriter``.

    LLM-specific knobs (URL, API key, model name) intentionally live in
    the LLM router config — see ``platform/api/app/services/llm/config.py``.
    The caption rewriter only knows the *task name* and routing tier; it
    never touches an HTTP client itself.  This means swapping Ollama →
    OpenAI → Anthropic is a deploy-time env change with no caption-
    rewriter code change.
    """

    version: str = "v1"

    # Task name passed to ``LLMRouter.complete(task=...)``.  Operators
    # map this to a tier in env via ``LLM_TASK_CAPTION=tier_fast``.
    # When no LLM is configured (LLM_TIERS empty), the rewriter still
    # produces deterministic captions via ``primary_action_summary``
    # fallback — see ``deterministic_caption()``.
    llm_task_name: str = "caption"

    # Hard cap on output length.  The LLM is prompted for "5-8 words"
    # but enforce truncation server-side so a runaway model cannot
    # break the storyboard layout.
    max_words: int = 8

    # Soft minimum — captions shorter than this drop to weak quality
    # and the UI may de-emphasise them.
    min_words: int = 2

    # How many captions to run CONCURRENTLY against the LLM.  Bounded
    # by a semaphore so a slow LLM does not exhaust the event loop.
    # Smaller = more serial latency, larger = bigger memory pressure
    # on the LLM provider.  Ollama on shared GPU often does better
    # with low concurrency (2-4); paid APIs can take 16+.
    concurrency: int = 4

    # Per-call timeout override.  None defers to the LLM tier's own
    # timeout setting (LLM_TIER_<NAME>_TIMEOUT_S).
    request_timeout_s: float | None = None

    # Maximum total wall time for one storyboard's caption pass.
    # Beyond this the composer returns whatever has been generated and
    # schedules the rest for the next request.
    total_timeout_s: float = 180.0

    # When True, captions for noise panels are skipped entirely (saves
    # LLM tokens on filtered-out scenes).  Toggle off if you need
    # captions even on noise for audit.
    skip_noise: bool = True

    # When False, the rewriter skips the LLM entirely and uses only
    # deterministic captions derived from scene_state_summary +
    # primary_action_summary.  Useful when the LLM is down, for
    # customers who require deterministic outputs, or for cost
    # control during bulk backfills.
    use_llm: bool = True

    # Per-LLM-call temperature.  Captions need consistency, not
    # creativity — keep this low.
    llm_temperature: float = 0.1

    # Per-LLM-call max output tokens.  20 is plenty for a 5-8 word
    # caption plus stop-sequence headroom.
    llm_max_tokens: int = 24


@dataclass(frozen=True)
class FrameAnnotatorConfig:
    """Tunables for ``frame_annotator``."""

    version: str = "v1"

    # JPEG quality for the JPEG renders (when configured).  PNGs are
    # always lossless; this only matters when output_format=jpeg.
    jpeg_quality: int = 88

    # Output format.  PNG keeps text crisp at OCR overlay edges; JPEG
    # is ~5x smaller for the same visual fidelity on photographic
    # screenshots.  Default to PNG for trust; flip to JPEG for high-
    # volume sharing scenarios.
    output_format: str = "png"

    # Cursor marker styling — every pixel here is configurable so
    # operators can theme annotated PNGs per tenant later.
    cursor_radius_px: int = 18
    cursor_ring_width_px: int = 4
    cursor_color_rgba: Tuple[int, int, int, int] = (239, 68, 68, 230)  # red-500

    # Click marker — a brighter inner dot inside the cursor ring.
    click_inner_radius_px: int = 6
    click_color_rgba: Tuple[int, int, int, int] = (255, 255, 255, 255)

    # OCR bounding-box overlay styling.
    ocr_box_width_px: int = 2
    ocr_box_color_rgba: Tuple[int, int, int, int] = (16, 185, 129, 180)  # emerald-500
    ocr_label_color_rgba: Tuple[int, int, int, int] = (15, 23, 42, 230)  # slate-900
    ocr_max_boxes_per_frame: int = 24

    # Caption band rendered along the bottom of the annotated frame.
    caption_band_height_px: int = 56
    caption_background_rgba: Tuple[int, int, int, int] = (10, 37, 64, 232)  # nexus-navy
    caption_text_color_rgba: Tuple[int, int, int, int] = (255, 255, 255, 255)
    caption_font_size_px: int = 22
    caption_padding_x_px: int = 20

    # Maximum render time before falling back to the raw frame.
    # Keeps the API responsive when annotation upstream signals are
    # missing or malformed.
    render_timeout_s: float = 10.0

    # Path prefix inside the object store for annotated assets.
    # The final asset path is
    # ``{tenant}/{session}/wf/{workflow}/annotated/{version}/{frame_id}.{ext}``
    asset_path_prefix: str = "annotated"


@dataclass(frozen=True)
class ComposerConfig:
    """Tunables for ``storyboard_composer``."""

    # Hard cap on panels returned in one storyboard response.  At
    # 2-hour scale a single artifact can produce 200+ panels; clients
    # should paginate.
    max_panels_per_response: int = 200

    # Number of panels to surface as the "Visual Story" hero preview
    # on the canonical result page.  Picked evenly across the timeline
    # so the hero shows the narrative arc, not the first N seconds.
    hero_panel_count: int = 5

    # Derivation timeout — the composer aborts lazy derivation past
    # this and returns whatever it has, scheduling the rest.
    derivation_timeout_s: float = 120.0

    # When True, the composer kicks off a background re-derivation if
    # any derived row's version is older than the current configured
    # version.  Disable for read-only deployments / disaster recovery.
    enable_lazy_re_derivation: bool = True


@dataclass(frozen=True)
class ActionExtractorConfig:
    """Tunables for ``action_extractor``.

    The extractor calls a multimodal LLM per scene with before/after
    frame screenshots + OCR text + URL + any existing cursor/control
    signals.  The LLM returns a structured ``SceneAction`` Pydantic
    object that gets reconciled with the existing pipeline signals
    and written to the ``scene_actions`` table.

    Defaults target Claude Sonnet 4.6 with image input + prompt
    caching.  Per-recording cost averages ~$0.10-0.20 with caching;
    self-hosted vision models are supported via the LLM router tier
    abstraction for tenants with on-prem requirements.

    See ``platform/api/app/services/storyboard/action_extractor.py``.
    """

    # Stamped on every scene_actions row.  Bumping this in env
    # (STORYBOARD_ACTION_EXTRACTOR_VERSION) forces re-extraction of
    # every artifact in the database without dropping rows.
    version: str = "v1"

    # Task name passed to ``LLMRouter.complete(task=...)``.  Operators
    # map this to a vision-capable tier in env via
    # ``LLM_TASK_ACTION_EXTRACTION=tier_vision``.  When no LLM tier is
    # configured the extractor falls back to a deterministic shape
    # built from existing evidence_controls (action_kind + label only,
    # no value capture) so the storyboard stays usable offline.
    llm_task_name: str = "action_extraction"

    # How many scene extractions to run CONCURRENTLY.  Bounded by a
    # semaphore so a slow LLM does not exhaust the event loop.
    # Anthropic tier supports 5-10 reliably; self-hosted is usually 1-2.
    concurrency: int = 4

    # Per-call timeout override.  None defers to the LLM tier's own
    # timeout setting (LLM_TIER_<NAME>_TIMEOUT_S).  Multimodal calls
    # are typically 5-15s so the tier default of 30s is usually
    # adequate; bump only when self-hosted models are slow.
    request_timeout_s: float | None = None

    # Maximum total wall time for one artifact's action-extraction
    # pass.  Beyond this the composer returns whatever has been
    # generated and schedules the rest for the next request.
    total_timeout_s: float = 300.0

    # When True, action extraction for noise-flagged panels is skipped
    # (saves LLM tokens on filtered-out scenes).  Toggle off if you
    # need actions on noise scenes for audit.
    skip_noise: bool = True

    # Per-LLM-call temperature.  Action extraction needs deterministic
    # structured output — keep at 0 or near-0.
    llm_temperature: float = 0.0

    # Per-LLM-call max output tokens.  The structured JSON output is
    # typically 100-200 tokens; 512 leaves headroom for verbose
    # reasoning fields without blowing the budget.
    llm_max_tokens: int = 512

    # Confidence floor below which an extracted action is recorded
    # but marked automation_ready=False regardless of what the LLM
    # claimed.  Test exporters can opt out by reading these too.
    min_confidence_for_automation: float = 0.65

    # When True, sends BOTH the before-scene and after-scene
    # representative frames to the LLM (consumes ~2x tokens but is
    # the only way to detect transitions like dropdown selection).
    # Disable only when latency or cost pressure is severe AND a
    # before/after diff is not needed for the workflow type.
    include_before_frame: bool = True

    # Maximum dimensions for the frame screenshots sent to the LLM,
    # in pixels.  The Anthropic vision API has a 5 MB per-image hard
    # limit; UI screenshots at 1568x1568 routinely PNG-encode to
    # 3-5 MB which can silently 400 the request.  1024 with JPEG q85
    # encoding (see _maybe_downsize) typically lands at 150-400 KB
    # per image — well under the limit and fast.  0 = no resize.
    image_max_dimension_px: int = 1024


@dataclass(frozen=True)
class PageVisitExtractorConfig:
    """Tunables for ``page_visit_extractor`` (Phase 2.0).

    The page visit extractor scans every frame's ``extracted_text`` for
    URLs and produces a chronological log of distinct page visits.
    Falls back to ``visual_frames.screen_name`` and
    ``app_instances.window_title`` for non-web recordings.  When ALL
    fallbacks fail it can optionally call a multimodal LLM on the first
    representative frame to read the location off the pixels.

    Generic across all UI domains — no hardcoded host lists, no
    customer-specific URL patterns.  Canonicalisation reuses the same
    dominant-host logic the app_deduper applies to scene URLs.

    See ``platform/api/app/services/storyboard/page_visit_extractor.py``.
    """

    # Stamped on every page_visits row.  Bumping this in env
    # (STORYBOARD_PAGE_VISIT_EXTRACTOR_VERSION) forces re-extraction
    # of every artifact in the database without dropping rows.
    version: str = "v1"

    # URL detection regex.  Configurable so operators can tighten it
    # for tenants whose OCR is noisy (false-positive URLs).  Default
    # matches http(s):// and www. prefixes; bare domain names are not
    # matched because OCR routinely misreads UI strings as bare domains
    # ("login.tail" etc.).
    url_regex_pattern: str = (
        r"(?:https?://|www\.)"
        r"[A-Za-z0-9][A-Za-z0-9\-]{0,62}"
        r"(?:\.[A-Za-z0-9][A-Za-z0-9\-]{0,62}){1,4}"
        r"(?:/[^\s\"'<>]*)?"
    )

    # Minimum number of contiguous frames a URL must appear in before
    # it is recorded as a distinct page visit.  Filters OCR-flicker
    # where a URL appears for one frame mid-scroll and disappears.
    min_frames_for_visit: int = 1

    # When True, two adjacent visits to the SAME canonical URL with
    # the same query string are merged into one row even when their
    # frame_index runs are non-contiguous (e.g. a brief loading-tab
    # frame in the middle).  When False, every contiguous run is its
    # own row.  Default keeps the visit log clean for QA review.
    merge_adjacent_same_url: bool = True

    # When True, collapse consecutive same-host visits whose paths are
    # in a segment-prefix relationship (e.g. ``/insurance/life`` and
    # ``/insurance/life/iwa``).  This is the single-page-app (SPA)
    # defragmenter: SPAs keep one address-bar URL while the user moves
    # through many client-side sub-steps, and the OCR + scene-URL
    # sources disagree on the trailing segment frame-to-frame, so one
    # logical page fragments into a ping-pong run of short visits.
    # Merging adopts the LONGER (more specific) path and unions the
    # time window + frame count.
    merge_path_prefix_fragments: bool = True

    # Minimum number of leading path segments the SHORTER path must have
    # before a prefix-merge fires.  Guards the shallow landing page:
    # ``/insurance`` (1 segment) must NOT be merged into the quote SPA
    # ``/insurance/life/iwa``, but ``/insurance/life`` (2 segments) may.
    path_prefix_min_segments: int = 2

    # When True, collapse visits that refer to the SAME logical page but
    # whose paths differ only in representation — OCR's truncated tail
    # ``/personal-information/height-weight`` vs vision's full
    # ``/insurance/.../personal-information/height-weight`` (share the
    # SUFFIX, not a prefix), or OCR's run-together ``publicfestimate``
    # vs vision's clean ``estimate``.  Visits under the same canonical
    # host are grouped by their LAST path segment (with a substring
    # fallback for run-together OCR), keeping one row per logical page.
    # This is a GLOBAL merge (not adjacency-bound) so interspersed
    # fragments of one page collapse to a single step.
    merge_same_page_tail: bool = True

    # Minimum last-segment length for the substring fallback in the
    # same-page-tail merge (so short generic segments like "state" only
    # match exactly, while "estimate" can absorb "publicfestimate").
    same_page_tail_min_substring: int = 6

    # When True, fall back to ``visual_frames.screen_name`` when no
    # URL is detected on a frame.  Disable for pure web tenants where
    # screen_name fallback is always noise.
    enable_screen_name_fallback: bool = True

    # When True, fall back to ``app_instances.window_title`` (or
    # ``apps.window_title``) when neither URL nor screen_name is
    # available.  Disable for tenants whose pipeline does not record
    # window titles.
    enable_window_title_fallback: bool = True

    # When True, call a multimodal LLM on the first frame of any visit
    # whose location is still empty after all deterministic fallbacks.
    # Adds ~$0.001 per visit; defaults OFF because the deterministic
    # path covers ~99% of real recordings.  Enable for low-confidence
    # tenants.
    enable_llm_inference: bool = False

    # ── Vision page-identity pass (generic SPA sub-page recovery) ──────
    # When True, run a multimodal LLM over sampled frames to read each
    # frame's page identity (URL from the address bar + a short heading
    # label) DIRECTLY FROM PIXELS, then override the deterministic
    # per-frame location before grouping.  This recovers single-page-app
    # sub-pages that OCR cannot read — the address bar keeps one shell
    # URL while the heading changes (gender → birthdate → ...), and
    # Tesseract OCR frequently can't read the address bar at all.
    # Generic: no domain assumptions.  Costs one LLM call per sampled
    # frame, so it is bounded by ``vision_identity_max_frames`` and
    # cached after the first derivation.  OFF by default (cost); enable
    # per-deployment via STORYBOARD_PAGE_VISIT_VISION_IDENTITY.
    enable_vision_page_identity: bool = False

    # Hard cap on frames sent to the vision identity pass.  Frames are
    # sampled evenly across the artifact when the count exceeds this, so
    # a 2-hour recording stays affordable.  60-120 covers typical demos.
    vision_identity_max_frames: int = 120

    # Concurrency for the vision identity pass.  Kept low (2) because the
    # premium tier (Sonnet) rate-limits ~16-way concurrency; the pass
    # runs sequentially against the same Anthropic budget as the other
    # extractors.
    vision_identity_concurrency: int = 2

    # Minimum confidence a vision identity must report before it
    # overrides the deterministic location.  Guards against low-quality
    # reads polluting the page log.
    vision_identity_min_confidence: float = 0.5

    # Task name used when calling the LLM router for llm_inferred.
    llm_task_name: str = "page_visit_inference"

    # Task name for the vision page-identity pass.  Routes to a vision-
    # capable tier (e.g. tier_premium / Sonnet) via
    # LLM_TASK_PAGE_VISIT_IDENTITY.
    vision_identity_task_name: str = "page_visit_identity"

    # Per-LLM-call temperature.
    llm_temperature: float = 0.0

    # Per-LLM-call max output tokens.
    llm_max_tokens: int = 64

    # Image downsizing — see ``ActionExtractorConfig.image_max_dimension_px``.
    image_max_dimension_px: int = 1024

    # Hard wall-clock budget for the whole page_visit derivation (incl.
    # the vision page-identity pass).  The composer wraps the extractor
    # in ``asyncio.wait_for(total_timeout_s)`` so a slow vision pass
    # (e.g. the premium tier rate-limiting or failing over to a slow
    # local model) can never blow the request budget — matches the
    # bound the action/page_action/form_snapshot extractors already have.
    total_timeout_s: float = 240.0


@dataclass(frozen=True)
class PageActionExtractorConfig:
    """Tunables for ``page_action_extractor`` (Phase 2.1).

    Per-URL parallel of the scene-keyed ``action_extractor``.  Each
    page visit gets a multimodal LLM call with intra-visit sampled
    frames and produces 0..N PageAction rows keyed by
    (page_visit_id, extractor_version, subaction_index).

    The two extractors run side-by-side; ``scene_actions`` stays
    populated for backwards compat (existing 3D Journey UI reads it)
    while ``page_actions`` is the new source for test exporters.

    See ``platform/api/app/services/storyboard/page_action_extractor.py``.
    """

    version: str = "v1"

    # Task name passed to ``LLMRouter.complete(task=...)``.  Operators
    # map this to a vision-capable tier in env via
    # ``LLM_TASK_PAGE_ACTION_EXTRACTION=tier_premium``.  When no tier
    # is configured, falls back to no-LLM (page action rows still
    # written with verb=none + low confidence).
    llm_task_name: str = "page_action_extraction"

    concurrency: int = 4

    # Per-call timeout override.  None defers to the LLM tier's own
    # timeout setting.
    request_timeout_s: float | None = None

    # Maximum total wall time for one artifact's full page-action pass.
    total_timeout_s: float = 300.0

    # Per-LLM-call temperature.  Keep near zero for structured output.
    llm_temperature: float = 0.0

    # Per-LLM-call max output tokens.
    llm_max_tokens: int = 768

    # Confidence floor below which automation_ready is forced False.
    min_confidence_for_automation: float = 0.65

    # Max frames sampled per page visit.  Visits ≥ very-long
    # duration get the higher cap; short visits get the lower one.
    # Defaults match the scene-keyed extractor's intra-frame sampling
    # so token budgets are predictable.
    frames_per_visit_default: int = 4
    frames_per_visit_long: int = 6
    frames_per_visit_very_long: int = 8

    # Visit-duration thresholds (seconds) that pick which frame count
    # the extractor uses.  Mirror LONG / VERY_LONG in action_extractor.
    long_visit_threshold_s: float = 5.0
    very_long_visit_threshold_s: float = 10.0

    # Image downsizing budget — see ActionExtractorConfig.
    image_max_dimension_px: int = 1024


@dataclass(frozen=True)
class FormSnapshotExtractorConfig:
    """Tunables for ``form_snapshot_extractor`` (Phase 2.2).

    Captures the final state of every form field visible on a page
    visit.  Each visit triggers ONE multimodal LLM call on the LAST
    representative frame; the LLM returns a list of (label, value,
    kind, confidence) which gets flattened into the JSONB
    ``page_visits.form_snapshot`` column.

    Why per-visit (not per-action): the snapshot answers "what state
    did the user leave the page in" — useful for test assertions
    independent of how the user got there.  PageActions answer "how
    did the user fill the form".  Both are needed for high-fidelity
    QA export.

    See ``platform/api/app/services/storyboard/form_snapshot_extractor.py``.
    """

    version: str = "v1"

    llm_task_name: str = "form_snapshot_extraction"

    concurrency: int = 4

    # Skip pages with no form fields (the LLM rejects them with empty
    # output anyway, but this saves the call).  Heuristic: skip when
    # the page's last-frame OCR text has fewer than this many input-
    # candidate tokens (label-looking words ending in ':' or '?').
    skip_when_input_candidates_below: int = 2

    request_timeout_s: float | None = None
    total_timeout_s: float = 240.0

    llm_temperature: float = 0.0
    llm_max_tokens: int = 1024

    # When True, the extractor only runs on page visits with a non-
    # empty URL (web pages).  Disable to capture form state on
    # desktop / mainframe pages too.
    web_only: bool = False

    # Image downsizing budget.
    image_max_dimension_px: int = 1024


@dataclass(frozen=True)
class StoryboardConfig:
    """Aggregate config holder — one instance is created at startup."""

    scene_grouper: SceneGrouperConfig = field(default_factory=SceneGrouperConfig)
    app_deduper: AppDeduperConfig = field(default_factory=AppDeduperConfig)
    caption_rewriter: CaptionRewriterConfig = field(default_factory=CaptionRewriterConfig)
    frame_annotator: FrameAnnotatorConfig = field(default_factory=FrameAnnotatorConfig)
    action_extractor: ActionExtractorConfig = field(default_factory=ActionExtractorConfig)
    page_visit_extractor: PageVisitExtractorConfig = field(
        default_factory=PageVisitExtractorConfig,
    )
    page_action_extractor: PageActionExtractorConfig = field(
        default_factory=PageActionExtractorConfig,
    )
    form_snapshot_extractor: FormSnapshotExtractorConfig = field(
        default_factory=FormSnapshotExtractorConfig,
    )
    composer: ComposerConfig = field(default_factory=ComposerConfig)


def load_config() -> StoryboardConfig:
    """Read every env var once and return an immutable ``StoryboardConfig``.

    Called by ``platform/api/app/main.py`` at startup and by tests via
    the ``app.state.storyboard_config`` indirection.  Calls
    ``ConfigError`` on any invalid value so misconfigured deployments
    fail fast rather than silently returning bad data.
    """
    return StoryboardConfig(
        scene_grouper=SceneGrouperConfig(
            version=_env_str("STORYBOARD_SCENE_GROUPER_VERSION", "v1"),
            same_screen_jaccard_threshold=_env_float(
                "STORYBOARD_SCENE_SIMILARITY_THRESHOLD",
                0.85,
                min_value=0.0,
                max_value=1.0,
            ),
            same_screen_title_jaccard_threshold=_env_float(
                "STORYBOARD_SCENE_TITLE_SIMILARITY_THRESHOLD",
                0.92,
                min_value=0.0,
                max_value=1.0,
            ),
            merge_below_duration_ms=_env_int(
                "STORYBOARD_SCENE_MERGE_BELOW_DURATION_MS",
                600,
                min_value=0,
            ),
            weak_ocr_confidence=_env_float(
                "STORYBOARD_SCENE_WEAK_OCR_CONFIDENCE",
                0.55,
                min_value=0.0,
                max_value=1.0,
            ),
            noise_screen_name_patterns=_env_tuple(
                "STORYBOARD_SCENE_NOISE_PATTERNS",
                ("new tab", "blank tab", "loading…", "loading..."),
            ),
            max_scenes_per_panel=_env_int(
                "STORYBOARD_MAX_SCENES_PER_PANEL", 60, min_value=1,
            ),
        ),
        app_deduper=AppDeduperConfig(
            version=_env_str("STORYBOARD_APP_DEDUP_VERSION", "v1"),
            domain_levenshtein_max=_env_int(
                "STORYBOARD_APP_DEDUP_LEVENSHTEIN_MAX", 3, min_value=0,
            ),
            domain_min_overlap_ratio=_env_float(
                "STORYBOARD_APP_DEDUP_MIN_OVERLAP_RATIO",
                0.6,
                min_value=0.0,
                max_value=1.0,
            ),
            allow_window_title_grouping=_env_bool(
                "STORYBOARD_APP_DEDUP_WINDOW_TITLE", True,
            ),
            use_public_suffix_list=_env_bool(
                "STORYBOARD_APP_DEDUP_USE_PSL", True,
            ),
            enable_ocr_prefix_strip=_env_bool(
                "STORYBOARD_APP_DEDUP_OCR_PREFIX_STRIP", True,
            ),
            enable_shared_suffix_merge=_env_bool(
                "STORYBOARD_APP_DEDUP_SHARED_SUFFIX_MERGE", True,
            ),
            shared_suffix_min_chars=_env_int(
                "STORYBOARD_APP_DEDUP_SHARED_SUFFIX_MIN_CHARS",
                10,
                min_value=1,
            ),
        ),
        caption_rewriter=CaptionRewriterConfig(
            version=_env_str("STORYBOARD_CAPTION_VERSION", "v1"),
            llm_task_name=_env_str(
                "STORYBOARD_CAPTION_LLM_TASK_NAME", "caption",
            ),
            max_words=_env_int(
                "STORYBOARD_CAPTION_MAX_WORDS", 8, min_value=1,
            ),
            min_words=_env_int(
                "STORYBOARD_CAPTION_MIN_WORDS", 2, min_value=1,
            ),
            concurrency=_env_int(
                "STORYBOARD_CAPTION_CONCURRENCY", 4, min_value=1,
            ),
            request_timeout_s=(
                _env_float(
                    "STORYBOARD_CAPTION_REQUEST_TIMEOUT_S",
                    0.0,
                    min_value=0.0,
                )
                if os.environ.get("STORYBOARD_CAPTION_REQUEST_TIMEOUT_S")
                else None
            ),
            total_timeout_s=_env_float(
                "STORYBOARD_CAPTION_TOTAL_TIMEOUT_S", 180.0, min_value=1.0,
            ),
            skip_noise=_env_bool("STORYBOARD_CAPTION_SKIP_NOISE", True),
            use_llm=_env_bool("STORYBOARD_CAPTION_USE_LLM", True),
            llm_temperature=_env_float(
                "STORYBOARD_CAPTION_LLM_TEMPERATURE",
                0.1,
                min_value=0.0,
                max_value=2.0,
            ),
            llm_max_tokens=_env_int(
                "STORYBOARD_CAPTION_LLM_MAX_TOKENS", 24, min_value=1,
            ),
        ),
        frame_annotator=FrameAnnotatorConfig(
            version=_env_str("STORYBOARD_ANNOTATION_VERSION", "v1"),
            jpeg_quality=_env_int(
                "STORYBOARD_ANNOTATION_JPEG_QUALITY",
                88,
                min_value=10,
                max_value=100,
            ),
            output_format=_env_str(
                "STORYBOARD_ANNOTATION_OUTPUT_FORMAT", "png",
            ).lower(),
            cursor_radius_px=_env_int(
                "STORYBOARD_ANNOTATION_CURSOR_RADIUS_PX",
                18,
                min_value=1,
            ),
            cursor_ring_width_px=_env_int(
                "STORYBOARD_ANNOTATION_CURSOR_RING_WIDTH_PX",
                4,
                min_value=1,
            ),
            click_inner_radius_px=_env_int(
                "STORYBOARD_ANNOTATION_CLICK_INNER_RADIUS_PX",
                6,
                min_value=1,
            ),
            ocr_box_width_px=_env_int(
                "STORYBOARD_ANNOTATION_OCR_BOX_WIDTH_PX",
                2,
                min_value=1,
            ),
            ocr_max_boxes_per_frame=_env_int(
                "STORYBOARD_ANNOTATION_OCR_MAX_BOXES", 24, min_value=0,
            ),
            caption_band_height_px=_env_int(
                "STORYBOARD_ANNOTATION_CAPTION_BAND_HEIGHT_PX",
                56,
                min_value=0,
            ),
            caption_font_size_px=_env_int(
                "STORYBOARD_ANNOTATION_CAPTION_FONT_SIZE_PX",
                22,
                min_value=8,
            ),
            caption_padding_x_px=_env_int(
                "STORYBOARD_ANNOTATION_CAPTION_PADDING_X_PX",
                20,
                min_value=0,
            ),
            render_timeout_s=_env_float(
                "STORYBOARD_ANNOTATION_RENDER_TIMEOUT_S", 10.0, min_value=0.1,
            ),
            asset_path_prefix=_env_str(
                "STORYBOARD_ANNOTATION_ASSET_PATH_PREFIX", "annotated",
            ),
        ),
        action_extractor=ActionExtractorConfig(
            version=_env_str("STORYBOARD_ACTION_EXTRACTOR_VERSION", "v1"),
            llm_task_name=_env_str(
                "STORYBOARD_ACTION_EXTRACTOR_LLM_TASK_NAME", "action_extraction",
            ),
            concurrency=_env_int(
                "STORYBOARD_ACTION_EXTRACTOR_CONCURRENCY", 4, min_value=1,
            ),
            request_timeout_s=(
                _env_float(
                    "STORYBOARD_ACTION_EXTRACTOR_REQUEST_TIMEOUT_S",
                    0.0,
                    min_value=0.0,
                )
                if os.environ.get("STORYBOARD_ACTION_EXTRACTOR_REQUEST_TIMEOUT_S")
                else None
            ),
            total_timeout_s=_env_float(
                "STORYBOARD_ACTION_EXTRACTOR_TOTAL_TIMEOUT_S",
                300.0,
                min_value=1.0,
            ),
            skip_noise=_env_bool(
                "STORYBOARD_ACTION_EXTRACTOR_SKIP_NOISE", True,
            ),
            llm_temperature=_env_float(
                "STORYBOARD_ACTION_EXTRACTOR_LLM_TEMPERATURE",
                0.0,
                min_value=0.0,
                max_value=2.0,
            ),
            llm_max_tokens=_env_int(
                "STORYBOARD_ACTION_EXTRACTOR_LLM_MAX_TOKENS", 512, min_value=1,
            ),
            min_confidence_for_automation=_env_float(
                "STORYBOARD_ACTION_EXTRACTOR_MIN_AUTOMATION_CONFIDENCE",
                0.65,
                min_value=0.0,
                max_value=1.0,
            ),
            include_before_frame=_env_bool(
                "STORYBOARD_ACTION_EXTRACTOR_INCLUDE_BEFORE_FRAME", True,
            ),
            image_max_dimension_px=_env_int(
                "STORYBOARD_ACTION_EXTRACTOR_IMAGE_MAX_DIMENSION_PX",
                1568,
                min_value=0,
            ),
        ),
        page_visit_extractor=PageVisitExtractorConfig(
            version=_env_str("STORYBOARD_PAGE_VISIT_EXTRACTOR_VERSION", "v1"),
            url_regex_pattern=_env_str(
                "STORYBOARD_PAGE_VISIT_URL_REGEX",
                PageVisitExtractorConfig.url_regex_pattern,
            ),
            min_frames_for_visit=_env_int(
                "STORYBOARD_PAGE_VISIT_MIN_FRAMES", 1, min_value=1,
            ),
            merge_adjacent_same_url=_env_bool(
                "STORYBOARD_PAGE_VISIT_MERGE_ADJACENT_SAME_URL", True,
            ),
            merge_path_prefix_fragments=_env_bool(
                "STORYBOARD_PAGE_VISIT_MERGE_PATH_PREFIX", True,
            ),
            path_prefix_min_segments=_env_int(
                "STORYBOARD_PAGE_VISIT_PATH_PREFIX_MIN_SEGMENTS", 2, min_value=1,
            ),
            merge_same_page_tail=_env_bool(
                "STORYBOARD_PAGE_VISIT_MERGE_SAME_PAGE_TAIL", True,
            ),
            same_page_tail_min_substring=_env_int(
                "STORYBOARD_PAGE_VISIT_SAME_PAGE_TAIL_MIN_SUBSTRING", 6, min_value=2,
            ),
            enable_screen_name_fallback=_env_bool(
                "STORYBOARD_PAGE_VISIT_SCREEN_NAME_FALLBACK", True,
            ),
            enable_window_title_fallback=_env_bool(
                "STORYBOARD_PAGE_VISIT_WINDOW_TITLE_FALLBACK", True,
            ),
            enable_llm_inference=_env_bool(
                "STORYBOARD_PAGE_VISIT_LLM_INFERENCE", False,
            ),
            enable_vision_page_identity=_env_bool(
                "STORYBOARD_PAGE_VISIT_VISION_IDENTITY", False,
            ),
            vision_identity_max_frames=_env_int(
                "STORYBOARD_PAGE_VISIT_VISION_MAX_FRAMES", 120, min_value=1,
            ),
            vision_identity_concurrency=_env_int(
                "STORYBOARD_PAGE_VISIT_VISION_CONCURRENCY", 2, min_value=1,
            ),
            vision_identity_min_confidence=_env_float(
                "STORYBOARD_PAGE_VISIT_VISION_MIN_CONFIDENCE",
                0.5,
                min_value=0.0,
                max_value=1.0,
            ),
            llm_task_name=_env_str(
                "STORYBOARD_PAGE_VISIT_LLM_TASK_NAME", "page_visit_inference",
            ),
            vision_identity_task_name=_env_str(
                "STORYBOARD_PAGE_VISIT_VISION_TASK_NAME", "page_visit_identity",
            ),
            llm_temperature=_env_float(
                "STORYBOARD_PAGE_VISIT_LLM_TEMPERATURE",
                0.0,
                min_value=0.0,
                max_value=2.0,
            ),
            llm_max_tokens=_env_int(
                "STORYBOARD_PAGE_VISIT_LLM_MAX_TOKENS", 64, min_value=1,
            ),
            image_max_dimension_px=_env_int(
                "STORYBOARD_PAGE_VISIT_IMAGE_MAX_DIMENSION_PX",
                1024,
                min_value=0,
            ),
            total_timeout_s=_env_float(
                "STORYBOARD_PAGE_VISIT_EXTRACTOR_TOTAL_TIMEOUT_S",
                240.0,
                min_value=1.0,
            ),
        ),
        page_action_extractor=PageActionExtractorConfig(
            version=_env_str("STORYBOARD_PAGE_ACTION_EXTRACTOR_VERSION", "v1"),
            llm_task_name=_env_str(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_LLM_TASK_NAME",
                "page_action_extraction",
            ),
            concurrency=_env_int(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_CONCURRENCY",
                4,
                min_value=1,
            ),
            request_timeout_s=(
                _env_float(
                    "STORYBOARD_PAGE_ACTION_EXTRACTOR_REQUEST_TIMEOUT_S",
                    0.0,
                    min_value=0.0,
                )
                if os.environ.get(
                    "STORYBOARD_PAGE_ACTION_EXTRACTOR_REQUEST_TIMEOUT_S",
                )
                else None
            ),
            total_timeout_s=_env_float(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_TOTAL_TIMEOUT_S",
                300.0,
                min_value=1.0,
            ),
            llm_temperature=_env_float(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_LLM_TEMPERATURE",
                0.0,
                min_value=0.0,
                max_value=2.0,
            ),
            llm_max_tokens=_env_int(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_LLM_MAX_TOKENS",
                768,
                min_value=1,
            ),
            min_confidence_for_automation=_env_float(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_MIN_AUTOMATION_CONFIDENCE",
                0.65,
                min_value=0.0,
                max_value=1.0,
            ),
            frames_per_visit_default=_env_int(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_FRAMES_DEFAULT",
                4,
                min_value=1,
            ),
            frames_per_visit_long=_env_int(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_FRAMES_LONG",
                6,
                min_value=1,
            ),
            frames_per_visit_very_long=_env_int(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_FRAMES_VERY_LONG",
                8,
                min_value=1,
            ),
            long_visit_threshold_s=_env_float(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_LONG_THRESHOLD_S",
                5.0,
                min_value=0.0,
            ),
            very_long_visit_threshold_s=_env_float(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_VERY_LONG_THRESHOLD_S",
                10.0,
                min_value=0.0,
            ),
            image_max_dimension_px=_env_int(
                "STORYBOARD_PAGE_ACTION_EXTRACTOR_IMAGE_MAX_DIMENSION_PX",
                1024,
                min_value=0,
            ),
        ),
        form_snapshot_extractor=FormSnapshotExtractorConfig(
            version=_env_str("STORYBOARD_FORM_SNAPSHOT_EXTRACTOR_VERSION", "v1"),
            llm_task_name=_env_str(
                "STORYBOARD_FORM_SNAPSHOT_EXTRACTOR_LLM_TASK_NAME",
                "form_snapshot_extraction",
            ),
            concurrency=_env_int(
                "STORYBOARD_FORM_SNAPSHOT_EXTRACTOR_CONCURRENCY",
                4,
                min_value=1,
            ),
            skip_when_input_candidates_below=_env_int(
                "STORYBOARD_FORM_SNAPSHOT_SKIP_INPUT_CANDIDATES_BELOW",
                2,
                min_value=0,
            ),
            request_timeout_s=(
                _env_float(
                    "STORYBOARD_FORM_SNAPSHOT_EXTRACTOR_REQUEST_TIMEOUT_S",
                    0.0,
                    min_value=0.0,
                )
                if os.environ.get(
                    "STORYBOARD_FORM_SNAPSHOT_EXTRACTOR_REQUEST_TIMEOUT_S",
                )
                else None
            ),
            total_timeout_s=_env_float(
                "STORYBOARD_FORM_SNAPSHOT_EXTRACTOR_TOTAL_TIMEOUT_S",
                240.0,
                min_value=1.0,
            ),
            llm_temperature=_env_float(
                "STORYBOARD_FORM_SNAPSHOT_EXTRACTOR_LLM_TEMPERATURE",
                0.0,
                min_value=0.0,
                max_value=2.0,
            ),
            llm_max_tokens=_env_int(
                "STORYBOARD_FORM_SNAPSHOT_EXTRACTOR_LLM_MAX_TOKENS",
                1024,
                min_value=1,
            ),
            web_only=_env_bool(
                "STORYBOARD_FORM_SNAPSHOT_EXTRACTOR_WEB_ONLY", False,
            ),
            image_max_dimension_px=_env_int(
                "STORYBOARD_FORM_SNAPSHOT_EXTRACTOR_IMAGE_MAX_DIMENSION_PX",
                1024,
                min_value=0,
            ),
        ),
        composer=ComposerConfig(
            max_panels_per_response=_env_int(
                "STORYBOARD_MAX_PANELS_PER_RESPONSE",
                200,
                min_value=1,
                max_value=2000,
            ),
            hero_panel_count=_env_int(
                "STORYBOARD_HERO_PANEL_COUNT", 5, min_value=1, max_value=20,
            ),
            derivation_timeout_s=_env_float(
                "STORYBOARD_DERIVATION_TIMEOUT_S", 120.0, min_value=1.0,
            ),
            enable_lazy_re_derivation=_env_bool(
                "STORYBOARD_ENABLE_LAZY_RE_DERIVATION", True,
            ),
        ),
    )
