"""Pydantic schemas shared between the action_extractor service, the
LLM structured-output schema, the storyboard composer view types, and
the visual-flow API response.

Single source of truth for the action shape — when the LLM produces a
``SceneAction``, the same model validates the response.  The DB row
shape mirrors these fields (see ``SceneActionRow`` in models.py and
migration ``031_scene_actions``).

Why Pydantic over a plain dataclass: the Anthropic tool-use API (and
the OpenAI structured-output API) accepts a JSON Schema for output
constraint.  Pydantic's ``model_json_schema()`` emits that JSON Schema
directly, so the LLM call uses ONE schema definition end-to-end.
Invalid output is rejected by Pydantic before reaching the database —
no manual regex parsing, no silent type drift.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class SceneActionVerb(str, Enum):
    """The semantic action verb extracted from a scene transition.

    Tight closed set so:
      * LLM cannot hallucinate novel verbs that break exporters
      * Test exporters can dispatch on verb without string matching
      * Frontend can render verb-specific icons reliably

    Values:
      click     — single mouse/touch press on a button / link / tab.
      type      — keyboard input into a text field, with the typed value.
      select    — selection from a dropdown / radio / menu, with the
                  chosen value.
      scroll    — viewport movement, no specific target.
      navigate  — URL changed (back/forward/address bar).  Often
                  follows a click; we emit ``navigate`` only when no
                  identifiable click target exists.
      submit    — form submission (Enter / submit button).  Distinct
                  from ``click`` so exporters can attach wait-for-
                  navigation expectations.
      hover     — pointer paused on an element without click.  Triggers
                  tooltips, dropdowns, hover states.
      none      — no detectable user action between Before and After
                  (e.g. animation, autoplay, idle).  These rows are
                  recorded for completeness but excluded from
                  automation export.
    """

    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    SCROLL = "scroll"
    NAVIGATE = "navigate"
    SUBMIT = "submit"
    HOVER = "hover"
    NONE = "none"


class SceneActionTargetKind(str, Enum):
    """UI element kind the action targeted.

    Used by the frontend to pick a verb icon variant and by the test
    exporters to choose between ``cy.click()`` vs ``cy.select()`` etc.
    The ``other`` bucket is a catch-all so the LLM does not invent new
    kinds that downstream code doesn't know how to render.
    """

    BUTTON = "button"
    LINK = "link"
    DROPDOWN = "dropdown"
    TEXT_FIELD = "text_field"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    MENU_ITEM = "menu_item"
    TAB = "tab"
    OTHER = "other"


class SceneAction(BaseModel):
    """One semantic user action extracted from a scene.

    This is the structured output schema the multimodal LLM is asked
    to fill in.  The same shape is what gets persisted to
    ``scene_actions`` (minus the metadata columns the extractor adds
    on the persistence side: extractor_model, latency, tokens, etc.).

    Field constraints are intentionally tight so:
      * The LLM gets a precise JSON Schema (via Pydantic) and is less
        likely to drift
      * Invalid responses fail validation BEFORE reaching the DB
      * Strings are length-capped to match the DB column widths so a
        chatty LLM can never overflow a column
    """

    verb: SceneActionVerb = Field(
        ...,
        description=(
            "The semantic verb of what the user did between the Before "
            "and After frame.  Use 'none' when no user action is "
            "detectable (animations, autoplay, idle frames)."
        ),
    )
    target_label: str = Field(
        "",
        max_length=500,
        description=(
            "Human-readable label of the element the user interacted "
            "with.  For a State dropdown, this would be the dropdown's "
            "visible label like 'What state do you live in?', NOT the "
            "value the user selected.  Empty string when no clear "
            "target (e.g. scroll, generic navigation)."
        ),
    )
    target_kind: SceneActionTargetKind = Field(
        SceneActionTargetKind.OTHER,
        description=(
            "The kind of UI element the action targeted.  Used to "
            "dispatch downstream rendering and test-exporter logic."
        ),
    )
    value: Optional[str] = Field(
        None,
        max_length=1000,
        description=(
            "The value the user entered (verb=type) or selected "
            "(verb=select/checkbox/radio).  For 'TX' selected from a "
            "State dropdown, value='TX'.  Null when verb is click / "
            "submit / hover / scroll / navigate / none."
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "0.0-1.0 confidence in the extraction.  Use ~0.95 when the "
            "After frame unambiguously shows the action and value; ~0.7 "
            "when there are multiple plausible interpretations; ~0.4 or "
            "lower when the frames are blurry / ambiguous."
        ),
    )
    automation_ready: bool = Field(
        False,
        description=(
            "True only when verb + target_label + value (when applicable) "
            "provide enough information for a Playwright/Cypress test "
            "to reliably reproduce the action.  False when value is "
            "missing for verb=type/select, or when target_label is "
            "vague."
        ),
    )
    reasoning: str = Field(
        "",
        max_length=500,
        description=(
            "ONE concise sentence explaining the extraction.  Cite the "
            "specific visual evidence: 'Dropdown showed TX in After "
            "frame after being blank in Before frame.'  Helps QA "
            "engineers audit and override low-confidence extractions."
        ),
    )

    @field_validator("target_label", "reasoning", "value", mode="before")
    @classmethod
    def _strip(cls, v):
        """Trim incidental whitespace from LLM outputs — keeps strings tight."""
        if isinstance(v, str):
            return v.strip()
        return v


class ExtractionEvidence(BaseModel):
    """Reconciliation snapshot — which existing pipeline signals
    corroborated the LLM's call.

    Persisted to ``scene_actions.evidence_signals`` JSONB column.
    Used by:
      * QA review UI to show "this action is supported by N signals"
      * Confidence dashboard to score extraction quality at scale
      * Quality-gate computation in Phase 4
    """

    ocr_text_match: bool = Field(
        False,
        description=(
            "True when the LLM's value appears verbatim in the After "
            "frame's OCR text — strong signal that the value is real."
        ),
    )
    control_match: bool = Field(
        False,
        description=(
            "True when ``evidence_controls.observed_value`` for any "
            "control in this scene matches the LLM's value.  Lets us "
            "boost confidence when the canonical pipeline's value "
            "extractor agrees with the LLM."
        ),
    )
    cursor_event_match: bool = Field(
        False,
        description=(
            "True when a ``cursor_events`` row for this scene has a "
            "click within 200px of an OCR text region containing the "
            "LLM's target_label."
        ),
    )
    url_changed: bool = Field(
        False,
        description=(
            "True when the URL differs between Before and After.  "
            "Strong corroboration for verb=navigate or verb=submit; "
            "contradicts verb=type/select on a stable form."
        ),
    )
    audio_intent_match: bool = Field(
        False,
        description=(
            "True when the audio transcript for this scene contains a "
            "verb+target combo matching the LLM's call (e.g. SME says "
            "'now I select Texas' and LLM extracted verb=select, "
            "value=TX)."
        ),
    )
    sources: list[str] = Field(
        default_factory=list,
        description=(
            "Names of pipeline signals consumed by the extractor "
            "(e.g. 'before_frame', 'after_frame', 'ocr_text', "
            "'evidence_controls', 'audio_intent').  Audit-only."
        ),
    )


class SceneActionList(BaseModel):
    """Wrapper for the multimodal LLM's per-scene response.

    Each scene may contain multiple user actions — particularly the
    "long scenes" of 10+ seconds where the user fills several form
    fields on the same page (State / Gender / Age / Height / Weight /
    Tobacco etc. all on one quote page).  The canonical pipeline
    correctly emits ONE scene per logical page; this list lets us
    capture EACH action that happened within that scene.

    The list is ordered chronologically — first action first.  Empty
    list is valid (no detectable user action in this scene).  Each
    action gets its own row in the ``scene_actions`` table with a
    monotonic ``subaction_index``.

    Used as the LLM tool's ``input_schema`` (replaces ``SceneAction``
    as the top-level tool I/O shape from v8 onwards).
    """

    actions: list[SceneAction] = Field(
        default_factory=list,
        max_length=20,  # hard cap — a single scene shouldn't have >20 distinct actions
        description=(
            "Ordered list of distinct user actions visible across this "
            "scene's frames.  Use an EMPTY list when the user did not "
            "interact (idle, autoplay, etc.).  Use a SINGLE action when "
            "the scene shows one interaction (click, navigate, type).  "
            "Use MULTIPLE actions when the scene is a long form-fill "
            "page where the user changed several fields in sequence."
        ),
    )


__all__ = [
    "ExtractionEvidence",
    "SceneAction",
    "SceneActionList",
    "SceneActionTargetKind",
    "SceneActionVerb",
]
