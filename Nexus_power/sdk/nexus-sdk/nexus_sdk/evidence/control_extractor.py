"""
Control Extractor — Phase 6 algorithm.

Extracts individual UI controls from a scene's representative frame
and generates Playwright selectors only from OCR-confirmed text.
``automation_ready`` is True only when the selector is grounded in
OCR evidence — never for vision-only (LLaVA-only) controls.

Control IDs are **deterministic**: uuid5(NAMESPACE_OID, "control:{artifact_id}:{scene_id}:{element_type}:{label_text}:{playwright_selector}")
so that retrying the same artifact produces identical IDs and DB upserts are
idempotent via ON CONFLICT DO NOTHING.

No external dependencies — pure Python computation over frame dicts.

Phase 2 (A3) — canonical action_kind taxonomy
---------------------------------------------
Behind ``EYES_PHASE2_GUARDS`` (default OFF), unmappable elements (no
``action_kind`` in ``properties`` and no entry in
:data:`_STRUCTURAL_KIND_MAP`) are DROPPED instead of emitting the
meaningless ``"interact"`` verb.  This is the contract that downstream
test-generation relies on: every emitted control has a verb that maps
1:1 to a Playwright action.

Phase 1 (Plan) — surface-pluggable dispatch
-------------------------------------------
At extraction time the scene's ``application_type`` is matched against
the surface registry in :mod:`nexus_sdk.evidence.surfaces`.  When a
surface claims the screen (mainframe 3270, SAP GUI, DB client, Office
desktop, …) the surface produces the entire control list and the
default web logic below is skipped.  Web (and unclassified) screens
fall through to the historical implementation untouched.
"""
from __future__ import annotations

import os
import re
import uuid
from typing import Optional

# Importing the surfaces package self-registers every built-in
# extractor.  Keeping the import here (rather than inside ``extract``)
# means the registry is warm before the first scene is processed.
from . import surfaces as _surfaces  # noqa: F401


# ── Phase 2 feature flag (mirrors build_scenes._phase2_guards_enabled) ──
def _phase2_guards_enabled() -> bool:
    return os.environ.get("EYES_PHASE2_GUARDS", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


# Canonical verbs that downstream test generation knows how to emit.
# This set is the contract — never expand without updating the
# Playwright emitter (Wave B B4) AND the UI verb map.
CANONICAL_ACTION_KINDS: frozenset[str] = frozenset({
    "click_cta",      # button, link
    "enter_text",     # text field, textarea, contenteditable
    "select_option",  # dropdown, radio (single value)
    "toggle",         # checkbox, switch
    "navigate",       # tab, step indicator, breadcrumb
    "upload_file",    # file input
    "drag_handle",    # slider, draggable
})


# Structural fallback used when ``properties.action_kind`` is missing.
# Maps element_type → canonical kind.  Anything not in this map is dropped
# under Phase 2 (returns the meaningless ``"interact"`` under Phase 1 so
# legacy behaviour stays byte-identical when the flag is off).
_STRUCTURAL_KIND_MAP: dict[str, str] = {
    "button": "click_cta",
    "link": "click_cta",
    "text_field": "enter_text",
    "input": "enter_text",
    "textarea": "enter_text",
    "dropdown": "select_option",
    "select": "select_option",
    "radio": "select_option",
    "checkbox": "toggle",
    "switch": "toggle",
    "tab": "navigate",
    "step_indicator": "navigate",
    "breadcrumb": "navigate",
    "file_input": "upload_file",
    "slider": "drag_handle",
}

# ── Browser chrome noise patterns ────────────────────────────
# OCR often reads browser tab bar / address bar text alongside form fields.
# These keywords appear in Chrome/Edge UI and should be stripped from labels.
# Also includes recording-software overlays (Zoom, Teams, Loom) that appear
# at the edges of screen recordings.
_CHROME_BREAKS = [
    "New Incognito Tab", "New Tab", "Private Browsing", "InPrivate",
    "Incognito Window", "Incognito Mode",
    "Ask Gemini", "Customize Chrome", "Show more", "Al Mode",
    "Mostly cloudy", "Partly cloudy", "Mostly sunny",
    "- Google Chrome", "- Microsoft Edge", "Mozilla Firefox",
    "ChatGPT", "Inbox (",
    "Zoom Meeting", "Teams Meeting", "Loom Recording",
    "is recording", "Recording in progress",
]

# Separators that delimit a meaningful label from trailing noise
_LABEL_SEPARATORS = [" — ", " – ", " - ", " | ", " · ", " › ", " > ", ": "]


def _clean_control_label(raw: str) -> str:
    """Strip browser chrome OCR noise from a raw element label.

    OCR scans a bounding box that may bleed into the browser title/tab bar
    above the form field.  This strips everything after the first chrome
    keyword and truncates at common separators to leave a short, clean label.

    Returns empty string if the cleaned result is too short to be meaningful.
    """
    if not raw:
        return ""
    text = raw.strip()

    # Step 1: chop at first browser chrome keyword.
    # pos == 0 means the entire string starts with chrome noise -> discard entirely.
    # pos > 0 means meaningful content precedes the chrome keyword -> keep that prefix.
    earliest = len(text)
    for kw in _CHROME_BREAKS:
        pos = text.find(kw)
        if pos == 0:
            return ""  # whole string is chrome noise
        if 0 < pos < earliest:
            earliest = pos
    text = text[:earliest].strip().rstrip(";:,.'\"")

    # Step 2: take only the first meaningful clause before a separator
    for sep in _LABEL_SEPARATORS:
        idx = text.find(sep)
        if 0 < idx <= 80:
            text = text[:idx].strip()
            break

    # Step 3: remove residual URL fragments that leaked in
    text = re.sub(r"https?://\S+", "", text).strip()
    text = re.sub(r"\bwww\.\S+", "", text).strip()

    # Step 4: truncate to 60 chars max; require at least 2 chars to be valid
    text = text[:60].strip()
    return text if len(text) >= 2 else ""

# Namespace for deterministic IDs (shared convention across SDK evidence modules)
_NS = uuid.NAMESPACE_OID

# Generic placeholder strings that appear as field text when a dropdown/input
# has no selected value yet.  When a control’s label matches one of these, the
# real label could not be resolved from OCR — mark as not automation-ready.
_GENERIC_PLACEHOLDERS: frozenset[str] = frozenset({
    "select one", "choose", "choose one", "-- select --", "- select -",
    "please select", "select...", "choose...", "pick one",
    "select an option", "select option", "select a value",
    "type here", "enter text", "enter here", "please enter", "please type",
    "search...", "search", "filter...",
    "n/a", "none", "not selected", "select all that apply",
})

# Words that appear in marketing copy, instructional sentences, and Zoom overlays
# but NEVER in genuine form field labels. A label containing any of these is
# ambient page text, not a field label.
_COPY_INDICATOR_WORDS: frozenset[str] = frozenset({
    # Pronouns / possessives used in copy
    "our", "their", "we", "us", "them",
    # Common marketing / instruction verbs / nouns
    "support", "dreams", "hopes", "inspired", "inspiring",
    "solutions", "customers", "clients", "users", "people",
    "please", "connect", "contact", "call", "visit",
    "ushers", "welcome", "thanks", "discover", "explore",
    "dedicated", "providing", "helping",
})

# Single words that look like candidate field labels but are actually instruction
# verbs, common prepositions, or article words — never genuine form field names.
# Used by the noise-prefix rescue to reject false candidates.
_NEVER_FIELD_LABEL_WORDS: frozenset[str] = frozenset({
    "take", "enter", "provide", "fill", "complete", "submit", "click",
    "review", "read", "check", "select", "choose",
    "information", "details", "field", "form", "step", "here", "more",
    "terms", "privacy", "note", "notice", "required", "optional",
    "all", "most", "some", "your", "the", "a", "an",
    "this", "that", "for", "from", "with", "into", "about",
})

# Element types eligible for automation selector generation
ELIGIBLE_ELEMENT_TYPES = {
    "button",
    "text_field",
    "dropdown",
    "checkbox",
    "radio",
    "link",
    "table_cell",
}


def _ocr_contains(label: str, ocr_text: str) -> bool:
    """Return True if label appears verbatim (case-insensitive) in ocr_text."""
    if not label or not ocr_text:
        return False
    return label.lower() in ocr_text.lower()


def _ocr_contains_word(label: str, ocr_text: str) -> bool:
    """Word-boundary aware OCR check.  Prevents 'age' matching inside 'encourage'."""
    if not label or not ocr_text:
        return False
    pattern = r'\b' + re.escape(label.lower()) + r'\b'
    return bool(re.search(pattern, ocr_text.lower()))


def _make_playwright_selector(element_type: str, label_text: str) -> str:
    """Generate a Playwright locator string.

    Only called when automation_ready=True (OCR-confirmed label).
    Returns empty string for unsupported element types.
    """
    safe = label_text.replace("'", "\\'")
    mapping = {
        "button": f"getByRole('button', {{ name: '{safe}' }})",
        "text_field": f"getByLabel('{safe}')",
        "dropdown": f"getByLabel('{safe}')",
        "checkbox": f"getByRole('checkbox', {{ name: '{safe}' }})",
        "radio": f"getByRole('radio', {{ name: '{safe}' }})",
        "link": f"getByRole('link', {{ name: '{safe}' }})",
        "table_cell": f"getByRole('cell', {{ name: '{safe}' }})",
    }
    return mapping.get(element_type, "")


def _resolve_app_type(scene: dict, frame: dict) -> str:
    """Resolve the active ``application_type`` for surface dispatch.

    The pipeline writes the app type to multiple places depending on
    the stage: the frame row carries it directly, the scene's
    ``scene_state_summary`` (Phase 8) has a higher-fidelity
    ``application_label``, and older code paths still write the raw
    classifier label onto the scene itself.  We consult all three so
    surface dispatch works regardless of which stage produced the dict.
    """
    state_summary = scene.get("scene_state_summary") or {}
    if isinstance(state_summary, dict):
        candidates = (
            state_summary.get("application_label"),
            state_summary.get("app_type"),
        )
        for c in candidates:
            if isinstance(c, str) and c.strip():
                return c.strip()
    for key in ("application_type", "app_type"):
        val = frame.get(key) or scene.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


class ControlExtractor:
    """Extract automation-ready UI controls from a scene + representative frame.

    Dispatch order:
      1. Resolve ``application_type`` from scene/frame.
      2. Ask the surface registry whether any non-web surface claims it
         (mainframe 3270, SAP GUI, DB client, Office desktop).
      3. If a surface matches, return its controls verbatim.
      4. Otherwise run the historical web-centric extractor below.

    The web fallback is unchanged for every existing recording, so
    behaviour on web demos is byte-identical pre- and post-Phase 1.
    """

    def extract(
        self,
        scene: dict,
        frame: dict,
        artifact_id: str = "",
        tenant_id: str = "",
        all_frames: list | None = None,
    ) -> list[dict]:
        """Extract controls from ``frame["ui_elements_json"]``.

        Selector source classification (web path):
        ┌──────────────────────────────────────────────┬────────────────┬──────────────────┐
        │ Condition                                    │ selector_source│ automation_ready │
        ├──────────────────────────────────────────────┼────────────────┼──────────────────┤
        │ Label verbatim in OCR text AND in LLaVA desc│ hybrid         │ True             │
        │ Label verbatim in OCR text (no LLaVA)       │ ocr            │ True             │
        │ Label from LLaVA only, no OCR match         │ vision         │ False            │
        │ No label text at all                        │ unknown        │ False            │
        └──────────────────────────────────────────────┴────────────────┴──────────────────┘

        selector_confidence:
          ocr    → frame.ocr_confidence * 0.9
          hybrid → frame.ocr_confidence * 0.9  (same — OCR-grounded)
          vision → 0.0
          unknown→ 0.0

        Surface paths (mainframe / SAP / DB / Office) populate
        ``selector_source`` with their own tokens (``terminal``, ``sap``,
        ``db_client``, ``office``) and use a confidence floor of 0.6
        because their OCR surfaces are monospaced / high-DPI text.
        """
        # ── Surface dispatch ────────────────────────────────────────
        # Resolve the screen's surface; the surface owns the scene
        # entirely if it matches.  Web / unclassified screens fall
        # through to the historical implementation below.
        app_type = _resolve_app_type(scene, frame)
        surface = _surfaces.find_surface(app_type) if app_type else None
        if surface is not None:
            return surface.extract(
                scene, frame,
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                all_frames=all_frames,
            )

        scene_id = scene.get("scene_id", "")
        ocr_text = frame.get("extracted_text", "") or ""
        llava_description = frame.get("description", "") or frame.get("llava_description", "") or ""
        ocr_confidence = float(frame.get("ocr_confidence", 0.0))
        frame_id: Optional[str] = frame.get("frame_id") or None

        ui_elements: list[dict] = frame.get("ui_elements_json") or frame.get("ui_elements") or []
        controls: list[dict] = []

        for element in ui_elements:
            if not isinstance(element, dict):
                continue

            element_type = (element.get("element_type") or "").lower()
            if element_type not in ELIGIBLE_ELEMENT_TYPES:
                continue

            raw_label = (
                element.get("text")
                or element.get("label")
                or element.get("label_text")
                or ""
            ).strip()
            # Clean browser chrome OCR noise (tab bar bleed-in) from label
            label_text = _clean_control_label(raw_label) or raw_label[:60].strip()

            raw_value = (
                element.get("value")
                or element.get("value_text")
                or element.get("properties", {}).get("value", "")
                or ""
            ).strip()
            # Apply noise-cleaning to value (OCR can bleed tab bar / page content into value).
            # Real user-entered values are short — a name, date, number, short phrase.
            # If the cleaned result still has more than 4 words, it is ambient OCR noise; discard.
            _cleaned_value = _clean_control_label(raw_value)
            if _cleaned_value and len(_cleaned_value.split()) > 4:
                _cleaned_value = ""
            # If the chrome-pattern cleaner wiped the whole string but raw is short, keep raw.
            # Exception: if the raw value itself contains a chrome keyword (e.g. "New Incognito Tab X")
            # do NOT restore it — that text is browser chrome noise, not a real field value.
            _raw_has_chrome = any(kw in raw_value for kw in _CHROME_BREAKS)
            if not _cleaned_value and raw_value and len(raw_value.split()) <= 4 and not _raw_has_chrome:
                _cleaned_value = raw_value[:60].strip()
            value_text = _cleaned_value

            # Action semantics — read from evidence (set by eyes-engine);
            # fall back to a structural derivation from element_type only when absent.
            #
            # Phase 1 path (flag OFF): preserves the legacy behaviour where
            # unmappable elements emit the meaningless verb "interact".
            #
            # Phase 2 path (flag ON):  enforces the canonical action_kind
            # taxonomy.  An element whose explicit action_kind is not
            # canonical AND whose element_type has no structural mapping is
            # DROPPED (the control is not emitted at all) — far better than
            # polluting the flow graph with "interact" noise that downstream
            # test generation cannot turn into runnable code.
            props = element.get("properties", {}) if isinstance(element.get("properties"), dict) else {}
            explicit_kind = (props.get("action_kind") or "").strip()
            structural_kind = _STRUCTURAL_KIND_MAP.get(element_type, "")

            if _phase2_guards_enabled():
                # Canonical-only path.
                if explicit_kind in CANONICAL_ACTION_KINDS:
                    action_kind = explicit_kind
                elif structural_kind in CANONICAL_ACTION_KINDS:
                    action_kind = structural_kind
                else:
                    # No canonical mapping — drop the control silently.
                    # The form field / element still appears via OCR text,
                    # but is not a first-class control row.
                    continue
            else:
                # Legacy fallback map (kept verbatim from Phase 1 for parity).
                _LEGACY_FALLBACK = {
                    "button": "click", "link": "click",
                    "text_field": "enter_text", "input": "enter_text",
                    "textarea": "enter_text",
                    "dropdown": "select_option", "select": "select_option",
                    "checkbox": "check", "radio": "select_option",
                    "tab": "navigate", "step_indicator": "navigate",
                }
                action_kind = explicit_kind or _LEGACY_FALLBACK.get(element_type, "interact")

            # observed_value: prefer explicit evidence value from eyes-engine; fall back to
            # cleaned value_text. Apply same word-count noise filter — if the evidence value
            # is still a multi-word ambient OCR fragment, discard it.
            _raw_obs = (props.get("observed_value") or "").strip()
            _cleaned_obs = _clean_control_label(_raw_obs)
            if _cleaned_obs and len(_cleaned_obs.split()) > 4:
                _cleaned_obs = ""
            if not _cleaned_obs and _raw_obs and len(_raw_obs.split()) <= 4:
                _cleaned_obs = _raw_obs[:60].strip()
            observed_value: str = _cleaned_obs or value_text

            # display_label: a human-readable action phrase for the UI.
            # Format: "<Verb>: <Field>" when we have a label; otherwise just the label.
            # When we also have an observed_value, append " = <value>".
            #
            # Verb map covers BOTH the canonical Phase 2 kinds and the legacy
            # Phase 1 kinds so flipping the flag does not require a UI rebuild.
            _VERB_MAP = {
                # Phase 2 canonical kinds
                "click_cta": "Click",
                "enter_text": "Fill",
                "select_option": "Select",
                "toggle": "Toggle",
                "navigate": "Open",
                "upload_file": "Upload",
                "drag_handle": "Drag",
                # Phase 1 legacy kinds
                "click": "Click",
                "check": "Check",
                "interact": "Interact",
                "review": "Review",
            }
            verb = _VERB_MAP.get(action_kind, action_kind.title())
            if label_text:
                display_label = f"{verb}: {label_text}"
                if observed_value:
                    display_label = f"{display_label} = {observed_value}"
            else:
                display_label = ""

            bbox_raw = element.get("bbox") or element.get("bounding_box") or []
            if isinstance(bbox_raw, list) and len(bbox_raw) == 4:
                bounding_box: dict = {
                    "x1": bbox_raw[0], "y1": bbox_raw[1],
                    "x2": bbox_raw[2], "y2": bbox_raw[3],
                }
            elif isinstance(bbox_raw, dict):
                bounding_box = bbox_raw
            else:
                bounding_box = {}

            # Generic placeholder check: the visible text is the field's default
            # placeholder (e.g. "Select one", "Choose") rather than its real label.
            # These cannot produce reliable Playwright selectors because the locator
            # would break as soon as the user selects a value.  Flag them clearly so
            # downstream test-generation skips selector generation but still knows the
            # field exists as a form element requiring interaction.
            _is_placeholder = label_text.lower().strip(".-\u2026 ") in _GENERIC_PLACEHOLDERS

            # Long-label noise filter: genuine field labels are short (≤ 4 words).
            # OCR bounding boxes often bleed into surrounding page text (marketing copy,
            # multi-field concatenations, Zoom participant name overlays, instructions).
            # Additionally, 3-4 word labels containing known marketing/copy indicator
            # words ("our", "support", "dreams", etc.) are treated as noise even if short.
            _label_words = label_text.split()
            _has_copy_word = any(w.lower() in _COPY_INDICATOR_WORDS for w in _label_words)
            _is_long_noise = len(_label_words) > 4 or (len(_label_words) > 2 and _has_copy_word)

            # Page-heading shape: 3+ all-Title-case tokens with no
            # connecting lowercase words. Real form-field labels are
            # short and often contain prepositions ("Date of birth", "How
            # old are you"); page headings are uniform Title-case panel
            # titles ("Term Life Insurance Calculator", "Personal
            # Information"). Reject these so they don't end up as form
            # controls in downstream evidence steps.
            if (
                len(_label_words) >= 3
                and all(w[:1].isupper() for w in _label_words if w)
                and not any(w.lower() in _COPY_INDICATOR_WORDS for w in _label_words)
            ):
                _is_long_noise = True

            # ── Noise-label prefix rescue ─────────────────────────────────────
            # When a noise label's FIRST WORD is a clean, short form field name
            # (not an instruction verb, not a copy word), rescue just that word.
            # Two OCR confirmation signals (either is sufficient):
            #   A. "<word> <number>" pattern: e.g. "Age 35" in a filled frame
            #   B. "Enter your <word>" pattern: standard placeholder in the same
            #      or neighbouring frames (e.g. "Enter your age" in frame 6 OCR)
            # Uses ALL scene frames if provided, otherwise just the rep frame.
            # Restriction: single word only; must not already be covered by an
            # existing control in this scene.
            if _is_long_noise and len(_label_words) >= 2:
                _first_word = _label_words[0]
                _fw_lower = _first_word.lower()
                _label_num_re = re.compile(
                    r'\b' + re.escape(_fw_lower) + r'\b\s+\d{1,5}\b',
                    re.IGNORECASE,
                )
                _enter_your_re = re.compile(
                    r'\bEnter your\b\s+' + re.escape(_fw_lower) + r'\b',
                    re.IGNORECASE,
                )
                _in_never = _fw_lower in _NEVER_FIELD_LABEL_WORDS
                _in_copy = _fw_lower in _COPY_INDICATOR_WORDS
                _already_covered = any(
                    _fw_lower in c.get("label_text", "").lower()
                    for c in controls
                )
                _all_ocr_for_rescue = [ocr_text] + [
                    _af.get("extracted_text", "") or ""
                    for _af in (all_frames or [])
                ]
                _confirmed_in_ocr = any(
                    _label_num_re.search(_t) or _enter_your_re.search(_t)
                    for _t in _all_ocr_for_rescue
                    if _t
                )
                if not _in_never and not _in_copy and not _already_covered and _confirmed_in_ocr:
                    label_text = _first_word.capitalize()
                    _label_words = label_text.split()
                    _has_copy_word = False
                    _is_long_noise = False

            in_ocr = _ocr_contains(label_text, ocr_text)
            in_llava = bool(label_text) and _ocr_contains(label_text, llava_description)

            if _is_placeholder:
                # Real label unresolved \u2014 the visible text is a placeholder
                selector_source = "placeholder"
                automation_ready = False
                playwright_selector = ""
                selector_confidence = 0.0
                # Override display_label to communicate the ambiguity clearly
                display_label = f"[{element_type.title()} field \u2014 label not resolved]"
            elif _is_long_noise:
                # Label is OCR row-overflow: ambient page text or multi-row
                # concatenation. Emitting it pollutes the triangulator
                # control-match path and the test-case generator. Skip.
                continue
            elif label_text and in_ocr and in_llava:
                selector_source = "hybrid"
                automation_ready = True
                playwright_selector = _make_playwright_selector(element_type, label_text)
                selector_confidence = round(ocr_confidence * 0.9, 4)
            elif label_text and in_ocr:
                selector_source = "ocr"
                automation_ready = True
                playwright_selector = _make_playwright_selector(element_type, label_text)
                selector_confidence = round(ocr_confidence * 0.9, 4)
            elif label_text and in_llava:
                selector_source = "vision"
                automation_ready = False
                playwright_selector = ""
                selector_confidence = 0.0
            else:
                selector_source = "unknown"
                automation_ready = False
                playwright_selector = ""
                selector_confidence = 0.0

            # Deterministic control_id: same artifact+scene+type+label+selector → same UUID
            control_id = str(uuid.uuid5(
                _NS,
                f"control:{artifact_id}:{scene_id}:{element_type}:{label_text}:{playwright_selector}",
            ))
            controls.append({
                "control_id": control_id,
                "scene_id": scene_id,
                "frame_id": frame_id,
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "element_type": element_type,
                "label_text": label_text,
                "value_text": value_text,
                "action_kind": action_kind,
                "observed_value": observed_value,
                "display_label": display_label,
                "bounding_box": bounding_box,
                "selector_source": selector_source,
                "playwright_selector": playwright_selector,
                "selector_confidence": selector_confidence,
                "automation_ready": automation_ready,
            })

        # ── OCR question-dropdown scan (second pass) ────────────────────────
        # Some question-format labels (e.g. "Do you use cigarettes or e-cigarettes?")
        # are not emitted by the LLaVA model in ui_elements_json.
        # Scan OCR text from ALL scene frames for the pattern:
        #   <question text>? <Select|Yes|No|Male|Female|Other>
        # Any discovered question not already covered gets added as a dropdown
        # with automation_ready=False (OCR may have merged words like "Doyou").
        _all_ocr_texts: list[str] = [ocr_text] if ocr_text else []
        for _af in (all_frames or []):
            _af_ocr = (_af.get("extracted_text") or "").strip()
            if _af_ocr and _af_ocr != ocr_text:
                _all_ocr_texts.append(_af_ocr)

        _existing_labels_lower: set[str] = {
            c["label_text"].lower() for c in controls if c["label_text"]
        }
        _question_re = re.compile(
            # Anchor on a known question-starter word (handles OCR-merged tokens
            # like "Doyou" → starts with "Do").  This prevents greedy leftward
            # expansion into surrounding form instructions ("marked optional:
            # Gender Select your gender...") which also end with "? Select".
            r'\b((?:Do|Did|Are|Is|Have|Can|Will|Would|Should|Does|How|What|When|Which)\w*'
            r'(?:\s+\S+){1,7})\?\s+(Select|Yes|No|Male|Female|Other)',
            re.IGNORECASE,
        )
        _seen_q_keys: set[str] = set()
        for _ocr_src in _all_ocr_texts:
            for _qm in _question_re.finditer(_ocr_src):
                _raw_q = _qm.group(1).strip()
                # Normalise for dedup: strip whitespace and lowercase
                _q_key = re.sub(r'\s+', '', _raw_q.lower())
                if _q_key in _seen_q_keys:
                    continue
                _seen_q_keys.add(_q_key)
                # Skip if the first 15 chars already appear in a known label
                _q_prefix = _q_key[:15]
                if any(_q_prefix in re.sub(r'\s+', '', _ex) for _ex in _existing_labels_lower):
                    continue
                # Normalize OCR word-merges in the question text.
                # OCR often fuses short words onto adjacent tokens.  Two passes:
                #   1. Question-word split: "Doyou" → "Do you"
                #      The regex anchors on known question-starters (Do, Are, etc.)
                #      followed immediately by a lowercase char (the merged next word).
                #   2. Conjunction-at-token-end: "cigarettesor" → "cigarettes or"
                #      Requires 5+ chars before the conjunction so common words like
                #      "for" or "color" are not split.  The lookahead (?=\s|\?) ensures
                #      the conjunction is at the end of the OCR token (followed by space
                #      or end of question mark), not embedded inside a real word.
                _norm_q = re.sub(
                    r'\b(Do|Did|Are|Is|Have|Can|Will|Would|Should|Does|How|What|When|Which)([a-z])',
                    r'\1 \2', _raw_q + "?",
                    flags=re.IGNORECASE,
                )
                _norm_q = re.sub(
                    r'([a-z]{5,}?)(or|and)(?=\s|\?)',
                    r'\1 \2', _norm_q,
                )
                _norm_q = re.sub(r'\s+', ' ', _norm_q).strip()
                _clean_q = _norm_q[:60].strip()
                _q_word_count = len(_clean_q.split())
                if _q_word_count < 3:
                    continue  # too short to be a meaningful question
                _q_obs_val = _qm.group(2) if _qm.group(2).lower() != "select" else ""
                _q_ctrl_id = str(uuid.uuid5(
                    _NS,
                    f"control:{artifact_id}:{scene_id}:dropdown:{_clean_q}:",
                ))
                controls.append({
                    "control_id": _q_ctrl_id,
                    "scene_id": scene_id,
                    "frame_id": frame_id,
                    "artifact_id": artifact_id,
                    "tenant_id": tenant_id,
                    "element_type": "dropdown",
                    "label_text": _clean_q,
                    "value_text": _q_obs_val,
                    "action_kind": "select_option",
                    "observed_value": _q_obs_val,
                    "display_label": f"Select: {_clean_q}",
                    "bounding_box": {},
                    "selector_source": "ocr_question",
                    "playwright_selector": "",
                    "selector_confidence": 0.0,
                    "automation_ready": False,
                })

        return controls
