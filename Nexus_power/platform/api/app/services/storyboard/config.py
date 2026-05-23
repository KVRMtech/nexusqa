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


def _env_str(name: str, default: str) -> str:
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


@dataclass(frozen=True)
class CaptionRewriterConfig:
    """Tunables for ``caption_rewriter``."""

    version: str = "v1"

    # Where the LLM lives.  Defaults to the Ollama instance running
    # next to platform-api in docker-compose.  Swap to an external API
    # by setting the URL + API key.
    llm_url: str = "http://ollama:11434"
    llm_api_key: str = ""

    # Small fast model — captions are short so we do not need the
    # large LLaVA model that runs the canonical pipeline.  Override
    # for customers that bring their own model.
    llm_model: str = "llama3.2:3b"

    # Hard cap on output length.  The LLM is prompted for "5-8 words"
    # but enforce truncation server-side so a runaway model cannot
    # break the storyboard layout.
    max_words: int = 8

    # Soft minimum — captions shorter than this fall back to weak quality
    # and the UI may de-emphasise them.
    min_words: int = 2

    # How many captions to generate in one batch.  Smaller = more
    # serial requests, larger = bigger memory pressure per call.
    batch_size: int = 16

    # Per-call timeout.  When the LLM stalls past this the rewriter
    # records the caption as weak quality and moves on.
    request_timeout_s: float = 30.0

    # Maximum total wall time for one storyboard's caption pass.
    # Beyond this the composer returns whatever has been generated and
    # schedules the rest for the next request.
    total_timeout_s: float = 180.0

    # When True, captions for noise panels are skipped entirely (saves
    # LLM tokens on filtered-out scenes).  Toggle off if you need
    # captions even on noise for audit.
    skip_noise: bool = True

    # When False, the rewriter falls back to a deterministic non-LLM
    # caption derived from scene_state_summary + primary_action_summary.
    # Useful when the LLM is down or for customers who require
    # deterministic outputs.
    use_llm: bool = True


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
class StoryboardConfig:
    """Aggregate config holder — one instance is created at startup."""

    scene_grouper: SceneGrouperConfig = field(default_factory=SceneGrouperConfig)
    app_deduper: AppDeduperConfig = field(default_factory=AppDeduperConfig)
    caption_rewriter: CaptionRewriterConfig = field(default_factory=CaptionRewriterConfig)
    frame_annotator: FrameAnnotatorConfig = field(default_factory=FrameAnnotatorConfig)
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
        ),
        caption_rewriter=CaptionRewriterConfig(
            version=_env_str("STORYBOARD_CAPTION_VERSION", "v1"),
            llm_url=_env_str(
                "STORYBOARD_CAPTION_LLM_URL",
                "http://ollama:11434",
            ),
            llm_api_key=_env_str("STORYBOARD_CAPTION_LLM_API_KEY", ""),
            llm_model=_env_str(
                "STORYBOARD_CAPTION_LLM_MODEL", "llama3.2:3b",
            ),
            max_words=_env_int(
                "STORYBOARD_CAPTION_MAX_WORDS", 8, min_value=1,
            ),
            min_words=_env_int(
                "STORYBOARD_CAPTION_MIN_WORDS", 2, min_value=1,
            ),
            batch_size=_env_int(
                "STORYBOARD_CAPTION_BATCH_SIZE", 16, min_value=1,
            ),
            request_timeout_s=_env_float(
                "STORYBOARD_CAPTION_REQUEST_TIMEOUT_S", 30.0, min_value=0.5,
            ),
            total_timeout_s=_env_float(
                "STORYBOARD_CAPTION_TOTAL_TIMEOUT_S", 180.0, min_value=1.0,
            ),
            skip_noise=_env_bool("STORYBOARD_CAPTION_SKIP_NOISE", True),
            use_llm=_env_bool("STORYBOARD_CAPTION_USE_LLM", True),
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
