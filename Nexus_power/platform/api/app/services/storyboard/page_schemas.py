"""Pydantic schemas for the Phase 2 URL-keyed extractors.

Three new extractor pipelines operate alongside the scene-keyed
storyboard layer:

* ``page_visit_extractor`` — produces a chronological log of distinct
  pages (URL for web; window_title or screen_name for desktop / native
  / mobile) the user visited during one recording.  Solves the
  "scene_grouper merged 9 distinct URL visits into one scene" failure
  mode that the scene-keyed ``action_extractor`` cannot recover from
  on its own (every USAA ``/personal-information/*`` sub-page renders
  with the same layout, so the canonical pipeline collapses them).

* ``page_action_extractor`` — given a sequence of page visits, runs a
  multimodal LLM per visit to extract every distinct user action that
  occurred on that page.  Schema parity with ``SceneAction`` so test
  exporters can swap input source with a one-line change.

* ``form_snapshot_extractor`` — given a page visit, runs a multimodal
  LLM to capture the final state of every form field on the page
  (label → value pairs).  Persisted to ``page_visits.form_snapshot``
  JSONB so test exporters can synthesise per-page assertions
  ("page should end with State=TX, Gender=Male, ...").

These schemas serve THREE purposes simultaneously:

1. Pydantic validation of the LLM tool outputs (rejects invalid
   responses before reaching the DB).
2. JSON Schema generation for the LLM tool ``input_schema`` field —
   one source of truth for the LLM contract and the persistence layer.
3. API response serialisation when the Phase 2 data is surfaced via
   ``/artifacts/{id}/visual-flow``.

The action shape itself is identical to ``SceneAction`` (verb /
target_label / target_kind / value / confidence / automation_ready /
reasoning); ``PageAction`` is a subclass that ADDS provenance fields
(``subaction_index``) without re-declaring the action vocabulary.
Reusing ``SceneActionVerb`` / ``SceneActionTargetKind`` keeps the
verb / kind enums centralised so a future restriction (e.g. adding
``verb=upload``) only needs to change one place.

See also:
  * migration 034 (page_visits) — DB shape that PageVisit mirrors.
  * migration 035 (page_actions) — DB shape that PageAction mirrors.
  * ``schemas.py`` — scene-keyed equivalents (SceneAction / SceneActionList /
    ExtractionEvidence).  PageActionEvidence intentionally reuses
    ExtractionEvidence verbatim — the reconciliation signal set is the
    same regardless of whether the action is scene- or URL-keyed.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .schemas import (
    ExtractionEvidence,
    SceneActionTargetKind,
    SceneActionVerb,
)


# ── Page-visit source enumeration ────────────────────────────────────────────


class PageVisitSource(str, Enum):
    """How a page_visits row's ``location`` was discovered.

    Recorded on every row so downstream consumers (test exporters,
    confidence dashboards, RCA tooling) can tell the difference between
    a high-confidence regex-extracted URL and a fallback screen-name
    inference.  Used to weight extraction_confidence too.

    Values:
      url_regex        — extracted via the regex sweep over
                         ``visual_frames.extracted_text``.  Highest
                         confidence path (~1.0); the URL was literally
                         OCR'd off the address bar of a frame.
      url_scene        — copied from ``visual_scenes.detected_url``
                         when no frame OCR contained a URL.  The
                         canonical pipeline already attached a URL to
                         the scene; we just adopt it.
      window_title     — non-web app.  Fell back to
                         ``app_instances.window_title`` because no URL
                         was OCR'd.  Confidence ~0.6.
      screen_name_ocr  — non-web mobile / native.  Fell back to
                         ``visual_frames.screen_name``.  Confidence ~0.6.
      llm_inferred     — none of the above worked.  Ran a multimodal LLM
                         over the first frame to extract whatever
                         location string the LLM could read off the
                         pixels.  Confidence ~0.85 (the LLM read it but
                         we lack independent corroboration).
      ground_truth     — an INSTRUMENTED recorder (CDP/DOM/a11y) emitted
                         the REAL URL + navigation event for this frame.
                         Not pixel-derived — confidence 1.0, PROVEN.  This
                         is the Tier-0 overlay; absent it, behaviour is
                         identical to the pixel tiers above.
      missing_page     — a run of location-less ("homeless") frames sat
                         between two DIFFERENT pages, likely hiding a page
                         whose URL could not be read.  Surfaced as a
                         low-confidence honest placeholder (NEVER asserted
                         as a real page) so a gap is visible, not silent.
    """

    URL_REGEX = "url_regex"
    URL_SCENE = "url_scene"
    WINDOW_TITLE = "window_title"
    SCREEN_NAME_OCR = "screen_name_ocr"
    LLM_INFERRED = "llm_inferred"
    GROUND_TRUTH = "ground_truth"
    MISSING_PAGE = "missing_page"


# ── Page visit ───────────────────────────────────────────────────────────────


class PageVisit(BaseModel):
    """One contiguous visit to a single location (URL / window_title /
    screen_name).

    Mirrors ``PageVisitRow`` in ``models.py`` minus the persistence
    metadata.  Used as the API response model when page_visits are
    surfaced via ``/visual-flow`` and as the in-memory representation
    passed between the three Phase 2 extractors.

    ``page_visit_id`` is a deterministic uuid5 of
    ``(artifact_id, sequence_index, extractor_version)`` so re-runs at
    the same extractor version produce identical IDs.  Two visits to
    the same URL get DIFFERENT page_visit_ids because their
    sequence_index differs — re-entry is its own row.
    """

    page_visit_id: str = Field(
        ...,
        max_length=64,
        description="Deterministic uuid5 — stable across re-runs at the same version.",
    )
    artifact_id: str = Field(..., max_length=64)
    tenant_id: str = Field(..., max_length=64)
    session_id: str = Field("", max_length=64)
    sequence_index: int = Field(
        ...,
        ge=0,
        description=(
            "0-based monotonic position within the artifact.  Two visits "
            "to the same URL get different sequence_index values."
        ),
    )

    # ── The "where was the user" payload ─────────────────────────────
    location: str = Field(
        "",
        max_length=2000,
        description=(
            "Canonical location string.  URL for web, window_title for "
            "desktop / mainframe, screen_name for mobile / native."
        ),
    )
    url_host: str = Field("", max_length=500)
    url_path: str = Field("", max_length=2000)
    url_query: str = Field("", max_length=2000)
    canonical_host: str = Field(
        "",
        max_length=500,
        description=(
            "Mirrors the URL canonicalisation applied to scene URLs "
            "(e.g. ``Msdd.com`` → ``usaa.com`` when the artifact's "
            "dominant host is ``usaa.com``).  Empty for non-URL sources."
        ),
    )

    # ── Temporal extent within the recording ────────────────────────
    first_seen_ms: int = Field(0, ge=0)
    last_seen_ms: int = Field(0, ge=0)
    duration_ms: int = Field(0, ge=0)
    frame_count: int = Field(0, ge=0)

    # ── How the location was discovered ─────────────────────────────
    source: PageVisitSource = Field(
        PageVisitSource.URL_REGEX,
        description="Extraction method — see PageVisitSource for semantics.",
    )
    extraction_confidence: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description=(
            "0..1 confidence in the extraction.  url_regex defaults to 1.0; "
            "screen_name fallback ~0.6; llm_inferred ~0.85."
        ),
    )

    # ── Optional join to the canonical scene this visit fell within
    primary_scene_id: Optional[str] = Field(
        None,
        max_length=64,
        description=(
            "Best-effort join to the canonical scene this visit fell "
            "within.  None when the visit spans multiple scenes or no "
            "scene boundary matched."
        ),
    )

    # ── Form snapshot (populated by form_snapshot_extractor) ────────
    form_snapshot: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "label → value mapping of every form field visible on this "
            "page.  Empty {} when no form was visible or the extractor "
            "has not yet run."
        ),
    )
    form_snapshot_signals: dict = Field(
        default_factory=dict,
        description=(
            "Confidence + evidence for the form snapshot itself "
            "(visible_field_count, overall_confidence, source_frame_index)."
        ),
    )

    # ── Provenance ──────────────────────────────────────────────────
    extractor_version: str = Field("v1", max_length=50)
    form_snapshot_extractor_version: str = Field("", max_length=50)

    @field_validator(
        "location", "url_host", "url_path", "url_query", "canonical_host",
        mode="before",
    )
    @classmethod
    def _strip_strings(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v


# ── Page action ──────────────────────────────────────────────────────────────


class PageAction(BaseModel):
    """One semantic user action that occurred on a single page visit.

    Mirrors ``PageActionRow`` minus persistence metadata.  Action
    vocabulary (verb / target_kind) is reused from the scene-keyed
    layer (``SceneActionVerb`` / ``SceneActionTargetKind``) so the
    two layers stay in lockstep — adding ``verb=upload`` only needs
    to change one enum, not two.

    A single page visit may produce MULTIPLE PageActions (one per
    field changed during the visit).  ``subaction_index`` is the
    chronological ordinal within that visit.
    """

    verb: SceneActionVerb = Field(
        ...,
        description=(
            "Semantic verb of the user's action on this page.  Use "
            "``none`` ONLY for visits with no detectable action (idle "
            "frames, autoplay)."
        ),
    )
    target_label: str = Field(
        "",
        max_length=500,
        description=(
            "Human-readable label of the element acted upon.  For "
            "verb=select, this is the FIELD label (e.g. 'What state "
            "do you live in?'), NOT the value (e.g. 'TX')."
        ),
    )
    target_kind: SceneActionTargetKind = Field(
        SceneActionTargetKind.OTHER,
        description="Kind of UI element the action targeted.",
    )
    value: Optional[str] = Field(
        None,
        max_length=1000,
        description=(
            "Value entered (verb=type) or selected (verb=select / radio / "
            "checkbox).  Null for click / submit / hover / scroll / "
            "navigate / none."
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="0..1 confidence in this action's extraction.",
    )
    automation_ready: bool = Field(
        False,
        description=(
            "True only when verb + target + value (when applicable) are "
            "sufficient for a Playwright / Cypress test to reproduce."
        ),
    )
    reasoning: str = Field(
        "",
        max_length=500,
        description="One short sentence citing visual evidence.",
    )

    @field_validator("target_label", "reasoning", "value", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v


class PageActionList(BaseModel):
    """Wrapper for the page_action_extractor LLM tool output.

    Same shape as SceneActionList — a list of PageAction in
    chronological order — but distinguished so the page-keyed and
    scene-keyed LLM tools can have different prompts / descriptions
    without collision.

    Empty list is valid (no detectable action on this page visit).
    """

    actions: list[PageAction] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Ordered list of distinct user actions visible across the "
            "frames for this page visit.  Empty list = no detectable "
            "interaction (the user viewed the page without acting)."
        ),
    )


# ── Vision page identity (generic SPA sub-page recovery) ─────────────────────


class PageIdentity(BaseModel):
    """One frame's page identity as read by a multimodal LLM from pixels.

    The deterministic tiers (OCR regex, scene URL, screen_name) fail on
    single-page apps: the address bar keeps one shell URL while the user
    moves through many client-side sub-pages, and Tesseract OCR often
    can't read the address bar at all.  A vision model reading the
    actual screenshot recovers what OCR missed — both the URL (when any
    of it is legible) and a short page label from the on-screen heading
    (which distinguishes sub-pages even when the URL is identical).

    Used as the ``record_page_identity`` tool's output schema.  Generic:
    no domain assumptions — works for any web / desktop / mobile UI.
    """

    url: str = Field(
        "",
        max_length=2000,
        description=(
            "The FULL URL from the browser address bar if ANY part is "
            "legible — best effort, include the deepest path you can "
            "read.  Empty string when there is no address bar or it is "
            "unreadable.  Do NOT invent a URL."
        ),
    )
    page_title: str = Field(
        "",
        max_length=200,
        description=(
            "A SHORT label for this page taken from its main heading or "
            "the question/field it asks (e.g. 'Birth gender', 'Height "
            "and weight', 'Coverage needs', 'Military status').  This is "
            "what distinguishes one SPA sub-page from the next when the "
            "URL does not change.  Empty when no clear heading."
        ),
    )
    confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="0..1 confidence in the page identification.",
    )

    @field_validator("url", "page_title", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v


# ── Form snapshot ────────────────────────────────────────────────────────────


class FormField(BaseModel):
    """One captured form field — label + final value + meta.

    Used as the per-field entry in the form_snapshot LLM tool output.
    The persistence layer flattens to ``{label: value}`` for storage in
    the ``page_visits.form_snapshot`` JSONB column; metadata
    (target_kind / confidence) goes to ``form_snapshot_signals``.
    """

    label: str = Field(
        ...,
        max_length=500,
        description=(
            "Human-readable label of the form field as the user would "
            "read it on screen.  Trimmed and de-punctuated by the "
            "extractor before persistence."
        ),
    )
    value: str = Field(
        "",
        max_length=1000,
        description=(
            "The final value visible in the field on the last frame of "
            "this page visit.  Empty string when the field is blank.  "
            "PASSWORD fields MUST be returned as empty string."
        ),
    )
    kind: SceneActionTargetKind = Field(
        SceneActionTargetKind.OTHER,
        description=(
            "What kind of input the field is.  Helps test exporters "
            "choose the right Playwright assertion (toHaveValue vs "
            "toBeChecked vs hasText)."
        ),
    )
    confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="0..1 confidence in this field's label+value extraction.",
    )

    @field_validator("label", "value", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v


class FormSnapshotLLMResponse(BaseModel):
    """LLM tool output for one page visit's form snapshot.

    The extractor calls Sonnet with the last representative frame of
    the page visit and asks it to enumerate every visible form field
    + its final value.  Pydantic validates the response; invalid
    responses are dropped (snapshot stays empty {}).
    """

    fields: list[FormField] = Field(
        default_factory=list,
        max_length=80,
        description=(
            "Every form field visible on the page, with the user's "
            "final value.  Empty list when no form is visible (a pure "
            "navigation / read-only page)."
        ),
    )
    overall_confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Aggregate 0..1 confidence in the whole snapshot.  Use the "
            "MIN of per-field confidences when individual fields vary; "
            "use a single value when all fields are equally readable."
        ),
    )
    page_intent: str = Field(
        "",
        max_length=200,
        description=(
            "ONE short phrase describing what the page is for, used by "
            "test exporters as a comment header (e.g. 'Personal "
            "information — state selection').  Empty string when the "
            "page intent is ambiguous."
        ),
    )

    @field_validator("page_intent", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v


class FormSnapshot(BaseModel):
    """Persisted shape of one page visit's form snapshot.

    What lives in ``page_visits.form_snapshot`` (the flat label→value
    dict) plus what lives in ``page_visits.form_snapshot_signals`` (the
    metadata sidecar).  Exposed via the API as ONE object so callers do
    not have to assemble it themselves.
    """

    fields: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Flat label → value mapping.  Direct surface for test "
            "exporters: ``for k, v in snapshot.fields.items(): "
            "page.get_by_label(k).fill(v)``."
        ),
    )
    field_kinds: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional label → target_kind mapping, when the extractor "
            "could determine the input type.  Lets exporters choose "
            "between fill / check / select.  Missing keys default to "
            "``other``."
        ),
    )
    visible_field_count: int = Field(
        0,
        ge=0,
        description="Number of form fields the LLM saw on the page.",
    )
    overall_confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="Aggregate 0..1 confidence in the whole snapshot.",
    )
    source_frame_asset_path: str = Field(
        "",
        max_length=500,
        description=(
            "Asset path of the frame the snapshot was extracted from "
            "(typically the last representative frame of the page "
            "visit).  Lets the QA reviewer click through to the "
            "evidence."
        ),
    )
    page_intent: str = Field("", max_length=200)
    extractor_model: str = Field("", max_length=100)
    extractor_version: str = Field("", max_length=50)


# ── Public surface ───────────────────────────────────────────────────────────


__all__ = [
    "ExtractionEvidence",
    "FormField",
    "FormSnapshot",
    "FormSnapshotLLMResponse",
    "PageAction",
    "PageActionList",
    "PageIdentity",
    "PageVisit",
    "PageVisitSource",
]
