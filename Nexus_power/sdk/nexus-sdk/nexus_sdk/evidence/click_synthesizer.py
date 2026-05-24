"""Best-effort click synthesizer for videos with no visible mouse cursor.

The :class:`nexus_sdk.cursor.CursorTracker` is motion-based — it needs a
visible cursor in the recording to produce events.  Many SME screen
recordings strip the cursor (Windows accessibility settings, some
Loom/Zoom captures, mobile-mirror tools).  For those videos the tracker
returns zero events, which then cascades into zero evidence_steps and a
detail panel that cannot show what the user clicked.

This module is the additive fallback: given the same frame / scene /
control / ui_elements_json data the rest of the pipeline already
produced, it synthesizes :class:`CursorEvent` records (``is_click=True``)
that the existing triangulator picks up unchanged.  No frozen pipeline
code is modified — the synthesizer is a sibling input to the tracker.

Each synthesized event sets ``detection_method`` to one of the values
below so consumers can distinguish real from inferred clicks and the UI
can render an "inferred" badge:

  * ``url_delta``        — url_or_path changed between consecutive
                            frames; the user navigated, which almost
                            always implies a click
  * ``ocr_new_block``    — a meaningful token cluster appeared in the
                            OCR text that wasn't present in the prior
                            frame (overlay / dialog / tab opened)
  * ``ocr_removed_block``— a meaningful token cluster disappeared
                            (overlay closed, dropdown collapsed)
  * ``ui_element_change``— a button/link/dropdown that was in
                            ``ui_elements_json`` at frame N is no
                            longer present at frame N+1
  * ``control_value``    — an evidence_control row exposes an
                            ``observed_value`` for this frame, which
                            means the user filled / selected it

Cursor coordinates default to ``(0, 0)`` because we have no pixel
position for synthesized events; consumers must inspect
``detection_method`` before treating coordinates as authoritative.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from nexus_sdk.cursor import CursorEvent


# ─── Configuration ───────────────────────────────────────────────────────────


@dataclass
class ClickSynthesizerConfig:
    """Tunable thresholds.  Defaults err on the side of recall — for an
    SDLC-evidence product, missing an action is worse than flagging an
    inferred one with a "weak" confidence badge.
    """

    # Per-strategy confidence ceilings.  Each synthesized event's
    # confidence is min(strategy_max, signal-derived score).
    url_delta_confidence: float = 0.78
    ocr_new_block_confidence: float = 0.55
    ocr_removed_block_confidence: float = 0.5
    ui_element_change_confidence: float = 0.6
    control_value_confidence: float = 0.72

    # Minimum number of "meaningful" tokens (alphabetic, length >= 3,
    # not in a generic stopword list) in an OCR delta required to emit
    # an event.  Higher values trade recall for precision: on noisy
    # OCRs every adjacent frame has 1-2 token re-recognitions that
    # aren't real UI changes.  Empirical sweet spot: require ≥ 4 new
    # tokens OR (1+ new tokens AND a CTA hint word in the delta).
    min_ocr_delta_tokens: int = 4
    # When set, the OCR delta strategy will fire on a 1-token delta if
    # any of those tokens is in the CTA hint vocabulary.  Lets us still
    # catch a single-word "Submit" click while suppressing OCR-jitter
    # noise on dense forms.
    ocr_cta_short_circuit: bool = True

    # Cap the OCR delta to this many words on either side before
    # diffing.  Prevents 1000-word page OCRs from producing huge
    # diff sets that match every adjacent frame.
    max_ocr_tokens_per_frame: int = 240

    # Bias multiplier when the new-OCR delta contains words from
    # ``ACTION_HINT_WORDS`` (Submit, Continue, Next, Apply, ...).  Such
    # tokens are strong CTA signals, so confidence is boosted.
    cta_hint_multiplier: float = 1.15


# Words that, when they appear in an OCR delta, indicate a UI element
# that's typically clicked.  Used to slightly boost confidence of
# ``ocr_new_block`` events on plausibly-clickable text.
_ACTION_HINT_WORDS = frozenset({
    "submit", "save", "continue", "next", "back", "apply",
    "calculate", "quote", "get", "start", "begin", "sign",
    "login", "logout", "logoff", "register", "search", "find",
    "buy", "purchase", "add", "remove", "delete", "edit",
    "confirm", "ok", "yes", "no", "cancel", "close", "exit",
    "select", "choose", "pick", "review", "verify",
})

# Generic noise tokens dropped before diffing OCR text.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at",
    "for", "with", "by", "from", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "should", "can", "could", "may",
    "this", "that", "these", "those", "it", "its", "as",
    "ask", "gemini",  # browser address-bar prompts are noise
})

# OCR garbage filter — exclude tokens that are clearly not real words.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{1,30}")


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _ocr_tokens(text: str, max_tokens: int) -> set[str]:
    """Extract meaningful lowercase tokens from raw OCR text.

    The OCR string is often messy (mis-recognised letters, glued words,
    line breaks).  We:
      * regex-extract alphabetic-prefixed runs
      * lowercase
      * drop stopwords + 2-character tokens
      * cap to max_tokens to keep set-diff cost bounded
    """
    if not text:
        return set()
    out: set[str] = set()
    for m in _TOKEN_RE.findall(text)[:max_tokens]:
        tok = m.lower()
        if len(tok) < 3:
            continue
        if tok in _STOPWORDS:
            continue
        if tok.isdigit():
            continue
        out.add(tok)
        if len(out) >= max_tokens:
            break
    return out


def _ui_element_signature(elem: dict) -> str:
    """Stable identity for a single UI element across frames.

    Combines element_type + text (lower-cased, whitespace-collapsed) so a
    "Sign in" button is the same element regardless of which frame
    observed it.  Used to detect element disappearance ("the button was
    here, now it's gone → likely clicked").
    """
    et = (elem.get("element_type") or "").lower()
    text = re.sub(r"\s+", " ", (elem.get("text") or "")).strip().lower()
    return f"{et}::{text}"[:120]


def _normalize_url(url: str) -> str:
    """Strip noise so trivial URL differences don't trigger events.

    Lowercases scheme + host, drops trailing slash, drops anchor (`#...`)
    because client-side hash routing changes shouldn't count when the
    path is otherwise identical.
    """
    if not url:
        return ""
    u = url.strip()
    # Drop fragment identifier
    if "#" in u:
        u = u.split("#", 1)[0]
    # Trailing slash normalization
    if u.endswith("/"):
        u = u[:-1]
    return u.lower()


# ─── Synthesis strategies ────────────────────────────────────────────────────


def _synthesize_url_delta(
    frames: list[dict],
    config: ClickSynthesizerConfig,
) -> list[CursorEvent]:
    """Emit one click event for every URL transition between adjacent frames.

    A URL change is the highest-confidence signal we can extract without a
    cursor — it means the browser actually navigated, which essentially
    requires a user action.  We attribute the click to the AFTER frame.
    """
    events: list[CursorEvent] = []
    prev_url = ""
    for frame in frames:
        url = _normalize_url(frame.get("url_or_path") or "")
        if url and prev_url and url != prev_url:
            events.append(CursorEvent(
                event_id=str(uuid.uuid4()),
                frame_id=frame.get("frame_id", ""),
                frame_index=int(frame.get("frame_index", 0)),
                timestamp_ms=int(round(float(frame.get("timestamp_seconds", 0.0)) * 1000.0)),
                cursor_x=0,
                cursor_y=0,
                velocity=0.0,
                is_click=True,
                is_drag=False,
                detection_method="url_delta",
                confidence=config.url_delta_confidence,
                metadata={
                    "synthetic": True,
                    "synthesis_kind": "url_delta",
                    "from_url": prev_url,
                    "to_url": url,
                    "target_label": f"navigate to {url}",
                },
            ))
        if url:
            prev_url = url
    return events


def _synthesize_ocr_block_delta(
    frames: list[dict],
    config: ClickSynthesizerConfig,
) -> list[CursorEvent]:
    """Emit a click event when meaningful OCR tokens appear or disappear
    between adjacent frames.

    Strategy:
      * Tokenize each frame's OCR (set of meaningful words)
      * Compute symmetric difference vs prior frame
      * If ≥ ``min_ocr_delta_tokens`` tokens are NEW → emit click
        (detection_method="ocr_new_block") attributing to the after-frame
      * If ≥ 2× threshold tokens are REMOVED → emit click
        (detection_method="ocr_removed_block")

    The "NEW" path catches CTA clicks that open an overlay / load a
    new page section; the "REMOVED" path catches close/back actions.
    """
    events: list[CursorEvent] = []
    prev_tokens: set[str] = set()
    prev_frame: Optional[dict] = None
    for frame in frames:
        text = frame.get("extracted_text") or ""
        curr_tokens = _ocr_tokens(text, config.max_ocr_tokens_per_frame)

        if prev_frame is not None and prev_tokens:
            new_tokens = curr_tokens - prev_tokens
            removed_tokens = prev_tokens - curr_tokens

            has_cta = bool(new_tokens & _ACTION_HINT_WORDS)
            # Fire on either a substantial delta OR a single CTA hint
            # token (Submit / Continue / Apply etc).
            fire_new = (
                len(new_tokens) >= config.min_ocr_delta_tokens
                or (config.ocr_cta_short_circuit and has_cta and len(new_tokens) >= 1)
            )
            if fire_new:
                # Confidence: scales with how many new tokens, boosted
                # when CTA-like words are in the new set.
                base = min(
                    config.ocr_new_block_confidence,
                    0.30 + 0.05 * min(len(new_tokens), 8),
                )
                conf = min(0.85, base * (config.cta_hint_multiplier if has_cta else 1.0))
                sample = sorted(new_tokens)[:6]
                events.append(CursorEvent(
                    event_id=str(uuid.uuid4()),
                    frame_id=frame.get("frame_id", ""),
                    frame_index=int(frame.get("frame_index", 0)),
                    timestamp_ms=int(round(float(frame.get("timestamp_seconds", 0.0)) * 1000.0)),
                    cursor_x=0,
                    cursor_y=0,
                    velocity=0.0,
                    is_click=True,
                    is_drag=False,
                    detection_method="ocr_new_block",
                    confidence=conf,
                    metadata={
                        "synthetic": True,
                        "synthesis_kind": "ocr_new_block",
                        "new_tokens_sample": sample,
                        "new_token_count": len(new_tokens),
                        "has_cta_hint": has_cta,
                        "target_label": " ".join(sample[:3]) if sample else "",
                    },
                ))
            elif len(removed_tokens) >= config.min_ocr_delta_tokens * 2:
                base = min(
                    config.ocr_removed_block_confidence,
                    0.28 + 0.04 * min(len(removed_tokens), 8),
                )
                sample = sorted(removed_tokens)[:6]
                events.append(CursorEvent(
                    event_id=str(uuid.uuid4()),
                    frame_id=frame.get("frame_id", ""),
                    frame_index=int(frame.get("frame_index", 0)),
                    timestamp_ms=int(round(float(frame.get("timestamp_seconds", 0.0)) * 1000.0)),
                    cursor_x=0,
                    cursor_y=0,
                    velocity=0.0,
                    is_click=True,
                    is_drag=False,
                    detection_method="ocr_removed_block",
                    confidence=base,
                    metadata={
                        "synthetic": True,
                        "synthesis_kind": "ocr_removed_block",
                        "removed_tokens_sample": sample,
                        "removed_token_count": len(removed_tokens),
                        "target_label": f"dismiss {sample[0]}" if sample else "dismiss overlay",
                    },
                ))

        prev_tokens = curr_tokens
        prev_frame = frame
    return events


def _synthesize_ui_element_change(
    frames: list[dict],
    config: ClickSynthesizerConfig,
) -> list[CursorEvent]:
    """Emit a click event when a button/link/dropdown disappears between
    adjacent frames.

    UI elements detected by the vision model in ``ui_elements_json`` carry
    a stable text+type signature.  If a button labelled "Sign in" was
    present at frame N and absent at N+1, that's strong evidence the
    user clicked it (form submit, navigation, modal close).

    We only flag the disappearance of elements with element_type in
    {button, link, tab, dropdown_option}.  Text-field disappearance is
    too noisy (fields move/scroll constantly).
    """
    events: list[CursorEvent] = []
    CLICKABLE_TYPES = {"button", "link", "tab", "dropdown_option", "step_indicator"}

    prev_sigs: dict[str, dict] = {}
    for frame in frames:
        elements = frame.get("ui_elements_json") or []
        if not isinstance(elements, list):
            elements = []
        curr_sigs: dict[str, dict] = {}
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            et = (elem.get("element_type") or "").lower()
            if et not in CLICKABLE_TYPES:
                continue
            sig = _ui_element_signature(elem)
            if sig and "::" in sig:
                curr_sigs[sig] = elem

        if prev_sigs:
            disappeared = set(prev_sigs.keys()) - set(curr_sigs.keys())
            for sig in disappeared:
                elem = prev_sigs[sig]
                elem_text = (elem.get("text") or "").strip()
                if not elem_text:
                    continue
                events.append(CursorEvent(
                    event_id=str(uuid.uuid4()),
                    frame_id=frame.get("frame_id", ""),
                    frame_index=int(frame.get("frame_index", 0)),
                    timestamp_ms=int(round(float(frame.get("timestamp_seconds", 0.0)) * 1000.0)),
                    cursor_x=0,
                    cursor_y=0,
                    velocity=0.0,
                    is_click=True,
                    is_drag=False,
                    detection_method="ui_element_change",
                    confidence=config.ui_element_change_confidence,
                    metadata={
                        "synthetic": True,
                        "synthesis_kind": "ui_element_disappearance",
                        "element_type": elem.get("element_type"),
                        "element_text": elem_text[:80],
                        "target_label": elem_text[:80],
                    },
                ))

        prev_sigs = curr_sigs
    return events


# ─── Strategy 6: form-value pattern detector ────────────────────────────────


# Regex patterns matching common form-field label + value pairs in OCR text.
# Each entry: (action_kind, friendly_label, regex).  The regex MUST capture
# exactly one group containing the user-entered value.  Patterns are tested
# in priority order; first hit per (frame, label) wins.
#
# These patterns are written defensively for OCR noise:
#   * tolerate variable whitespace (\s+)
#   * tolerate adjacency without space ("Annualincome")
#   * exclude common placeholder text via a negative-lookahead group
#
# Adding a new field is a one-line append — no other code changes.
# Words that look capitalized but are actually adjacent form-field LABELS,
# not user-entered values.  When a name-field pattern captures one of these
# we treat it as a placeholder-on-an-empty-form (no real user entry) and
# suppress the event.  Without this denylist, OCR like
#   "First name  Coverage amount Last name"
# incorrectly fires "First name = Coverage".
_NAME_VALUE_LABEL_DENYLIST = frozenset({
    "coverage", "amount", "annual", "income", "salary", "enter", "select",
    "type", "term", "age", "gender", "smoker", "occupation", "state",
    "zip", "postal", "phone", "mobile", "email", "address", "city",
    "country", "details", "information", "search", "submit", "next",
    "back", "continue", "apply", "calculate", "quote", "name",
    "first", "last", "middle", "full",
})


_FORM_FIELD_PATTERNS: list[tuple[str, str, "re.Pattern[str]"]] = [
    # Gender: capture Male/Female/Other/Non-binary/Prefer-not-to-say.
    # Reject placeholder "Selectyour gender" or "Select your gender".
    (
        "select_option",
        "Gender",
        re.compile(
            r"Gender\s+(?!Select)(Male|Female|Other|Non[\s-]?binary|Prefer\s+not[^,\.]{0,20})",
            re.IGNORECASE,
        ),
    ),
    # Smoker / cigarettes / tobacco use → Yes/No/Never/Sometimes.
    # The label varies ("Doyou use cigarettes or e-cigarettes?", "Smoker", etc.).
    (
        "select_option",
        "Smoker / cigarettes",
        re.compile(
            r"(?:cigarettes|e-?cigarettes|smoker|tobacco)[^?]{0,40}\?\s+(?!Select)(Yes|No|Never|Sometimes|Daily|Occasionally)",
            re.IGNORECASE,
        ),
    ),
    # Annual income: capture a dollar amount or large integer.
    # Reject placeholder "Enter your annual income".
    (
        "enter_text",
        "Annual income",
        re.compile(
            r"Annual\s*income\s*\$?\s*(?!Enter|SEnter|sEnter)(\d{1,3}(?:[,.]\d{3})*(?:\.\d+)?[KMB]?)",
            re.IGNORECASE,
        ),
    ),
    # Age: 1-3 digit value not followed by a year like "Enter your age".
    (
        "enter_text",
        "Age",
        re.compile(
            r"\bAge\s+(?!Enter)(\d{1,3})(?:\s|$|[^0-9])",
            re.IGNORECASE,
        ),
    ),
    # State: 2-letter abbreviation following "State".  Excludes
    # placeholder "Selectyour state".
    (
        "select_option",
        "State",
        re.compile(
            r"\bState\s+(?!Select)([A-Z]{2})\b",
        ),
    ),
    # ZIP / postal code.
    (
        "enter_text",
        "ZIP code",
        re.compile(
            r"(?:ZIP|Zip|Postal)\s*(?:code)?\s*(?!Enter)(\d{5}(?:-\d{4})?)",
        ),
    ),
    # First / Last / Full name capture (capitalized word(s) following the label).
    (
        "enter_text",
        "First name",
        re.compile(
            r"First\s*name\s+(?!Enter)([A-Z][a-zA-Z]{1,30}(?:\s+[A-Z][a-zA-Z]{1,30}){0,2})",
        ),
    ),
    (
        "enter_text",
        "Last name",
        re.compile(
            r"Last\s*name\s+(?!Enter)([A-Z][a-zA-Z]{1,30}(?:\s+[A-Z][a-zA-Z]{1,30}){0,2})",
        ),
    ),
    # Email — naive but useful when present.
    (
        "enter_text",
        "Email",
        re.compile(
            r"(?:Email|E-mail)\s+(?!Enter)([\w.+-]+@[\w-]+\.[\w.-]+)",
            re.IGNORECASE,
        ),
    ),
    # Phone — 10-digit US-style.
    (
        "enter_text",
        "Phone",
        re.compile(
            r"(?:Phone|Mobile|Cell)\s*\(?(?:number)?\)?\s*(?!Enter)\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})",
            re.IGNORECASE,
        ),
    ),
    # Coverage amount: e.g. "$500,000" near "Coverage" label.
    (
        "enter_text",
        "Coverage amount",
        re.compile(
            r"Coverage(?:\s+amount)?\s+\$?\s*(?!Enter|Select)(\d{1,3}(?:[,.]\d{3})*[KMB]?)",
            re.IGNORECASE,
        ),
    ),
    # Term length: number followed by "years"/"yr".
    (
        "select_option",
        "Term length",
        re.compile(
            r"Term(?:\s+length)?\s+(?!Select)(\d{1,2})\s*(?:years?|yrs?)",
            re.IGNORECASE,
        ),
    ),
    # Occupation: free text following "Occupation".  Excludes placeholder.
    (
        "enter_text",
        "Occupation",
        re.compile(
            r"Occupation\s+(?!Select|Enter)([A-Z][a-zA-Z\s]{2,30}?)(?:\s+(?:State|Annual|Age|Select|Enter)|$)",
        ),
    ),
]


def _extract_form_values(text: str) -> dict[str, tuple[str, str]]:
    """Run every form-field pattern against ``text`` and return a mapping
    of friendly_label → (action_kind, captured_value).

    Multiple patterns matching the same label keep the first hit.  The
    "Phone" pattern returns 3 capture groups; we join them with hyphens.

    Name fields (First/Last) are filtered against ``_NAME_VALUE_LABEL_DENYLIST``
    to reject captures of adjacent field labels like "Coverage" or
    "Annual" — those mean the form is still empty.
    """
    out: dict[str, tuple[str, str]] = {}
    if not text:
        return out
    name_labels = {"First name", "Last name", "Occupation"}
    for action_kind, label, pattern in _FORM_FIELD_PATTERNS:
        if label in out:
            continue
        m = pattern.search(text)
        if not m:
            continue
        if label == "Phone" and m.lastindex and m.lastindex >= 3:
            value = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        else:
            value = m.group(1).strip()
        if not value:
            continue
        # Reject "Coverage" / "Annual" etc. as a name — these are
        # adjacent form-labels, not user-entered values.
        if label in name_labels:
            first_word = value.split()[0].lower() if value.split() else ""
            if first_word in _NAME_VALUE_LABEL_DENYLIST:
                continue
            # Also reject if any word is in the denylist (catches
            # "John Coverage" too)
            if any(w.lower() in _NAME_VALUE_LABEL_DENYLIST for w in value.split()):
                continue
        out[label] = (action_kind, value)
    return out


def _synthesize_form_value_pattern(
    frames: list[dict],
    config: ClickSynthesizerConfig,
) -> list[CursorEvent]:
    """Detect form field transitions from placeholder → real value across
    adjacent frame pairs and emit one event per new (label, value) pair.

    For each frame:
      1. Pattern-match all known form fields against the OCR text
      2. Compare against the prior frame's extracted pairs
      3. When a label first appears WITH a value (it was either absent
         or placeholder-only on the prior frame) → emit a synthesized
         event with detection_method="form_value_inferred"

    Confidence is high (0.78) because the patterns explicitly exclude
    placeholder text and require well-formed values.

    The event's metadata carries:
      * target_label    — friendly field name (e.g. "Gender")
      * observed_value  — what the user entered (e.g. "Male")
      * action_kind     — enter_text / select_option (drives test-case
                          template selection downstream)
    """
    events: list[CursorEvent] = []
    prev_pairs: dict[str, str] = {}  # label → value
    for frame in frames:
        text = frame.get("extracted_text") or ""
        curr_pairs = _extract_form_values(text)

        # An event fires when a label has a value now that it didn't
        # have before OR when the value changed (user re-entered).
        for label, (action_kind, value) in curr_pairs.items():
            prev_value = prev_pairs.get(label, "")
            if prev_value == value:
                continue
            events.append(CursorEvent(
                event_id=str(uuid.uuid4()),
                frame_id=frame.get("frame_id", ""),
                frame_index=int(frame.get("frame_index", 0)),
                timestamp_ms=int(round(float(frame.get("timestamp_seconds", 0.0)) * 1000.0)),
                cursor_x=0,
                cursor_y=0,
                velocity=0.0,
                is_click=True,
                is_drag=False,
                detection_method="form_value_inferred",
                confidence=0.78,
                metadata={
                    "synthetic": True,
                    "synthesis_kind": "form_value_inferred",
                    "target_label": label,
                    "observed_value": value[:80],
                    "action_kind": action_kind,
                    "prior_value": prev_value[:80] if prev_value else "",
                },
            ))

        # Carry forward — prior_pairs is the full known state across all
        # frames so far, not just prior frame's set.  This lets the user
        # tab back to a field and have a clean before/after detected.
        for label, (_, value) in curr_pairs.items():
            prev_pairs[label] = value
    return events


# ─── Strategy 10: SCROLL / VIEWPORT-SHIFT DETECTION ─────────────────────────
#
# Without a visible cursor we can't directly see a scroll wheel event.
# But scrolls leave a distinctive OCR fingerprint:
#
#   Frame N      :  OCR text starts with "Header Hero About"
#                   OCR text ends with   "Section Two ..."
#   Frame N+1    :  OCR text starts with "About Pricing Plans"   ← shift up
#                   OCR text ends with   "Section Four ..."
#
# The set of tokens has high overlap but the ORDER and the
# leading/trailing windows have changed — a scroll signature.
#
# We compare adjacent frames' first-N and last-N tokens.  When the
# Jaccard overlap of the full token set is high (≥0.6) but the leading
# token windows have low overlap (≤0.3), that's a scroll.
#
# Same logic catches viewport-resize and tab-pane switches; we can
# disambiguate later.  For now we surface them all as a single
# "viewport_shift" event so the test-case generator knows the user
# scrolled to find something.

def _synthesize_viewport_shift(
    frames: list[dict],
    config: ClickSynthesizerConfig,
) -> list[CursorEvent]:
    """Emit a viewport_shift event when adjacent frames have high
    overall token overlap but low leading-window overlap — a scroll.

    Confidence is modest (0.55) because false positives are possible
    when a real page loads new content with the same vocabulary
    (e.g. paginated table).  The test-case generator should treat
    these as "user navigated within the page" rather than as concrete
    field interactions.
    """
    events: list[CursorEvent] = []
    sorted_frames = sorted(
        frames, key=lambda f: int(f.get("frame_index", 0)),
    )
    if len(sorted_frames) < 2:
        return events

    def _windowed_tokens(text: str, n: int, from_start: bool) -> list[str]:
        toks = [t for _, t in _tokenize_with_index(text)]
        if from_start:
            return [t.lower() for t in toks[:n]]
        return [t.lower() for t in toks[-n:]]

    WINDOW = 8  # tokens per leading/trailing window
    prev_frame: dict | None = None
    for frame in sorted_frames:
        if prev_frame is None:
            prev_frame = frame
            continue
        prev_text = prev_frame.get("extracted_text") or ""
        curr_text = frame.get("extracted_text") or ""
        if not prev_text or not curr_text:
            prev_frame = frame
            continue

        prev_all = {t.lower() for _, t in _tokenize_with_index(prev_text)}
        curr_all = {t.lower() for _, t in _tokenize_with_index(curr_text)}
        if not prev_all or not curr_all:
            prev_frame = frame
            continue

        all_jaccard = (
            len(prev_all & curr_all)
            / max(1, len(prev_all | curr_all))
        )

        prev_lead = set(_windowed_tokens(prev_text, WINDOW, from_start=True))
        curr_lead = set(_windowed_tokens(curr_text, WINDOW, from_start=True))
        lead_jaccard = (
            len(prev_lead & curr_lead)
            / max(1, len(prev_lead | curr_lead))
        ) if (prev_lead or curr_lead) else 1.0

        # Scroll signature: high overall overlap, low leading-window
        # overlap.  The URL must also be unchanged (a URL change would
        # be a navigation, not a scroll).
        prev_url = (prev_frame.get("url_or_path") or "").strip()
        curr_url = (frame.get("url_or_path") or "").strip()
        same_url = (prev_url == curr_url) or (not prev_url and not curr_url)

        if all_jaccard >= 0.60 and lead_jaccard <= 0.30 and same_url:
            events.append(CursorEvent(
                event_id=str(uuid.uuid4()),
                frame_id=frame.get("frame_id", ""),
                frame_index=int(frame.get("frame_index", 0)),
                timestamp_ms=int(round(float(frame.get("timestamp_seconds", 0.0)) * 1000.0)),
                cursor_x=0,
                cursor_y=0,
                velocity=0.0,
                is_click=False,  # scroll is not a click
                is_drag=False,
                detection_method="viewport_shift",
                confidence=0.55,
                metadata={
                    "synthetic": True,
                    "synthesis_kind": "viewport_shift",
                    "target_label": "scroll / viewport change",
                    "action_kind": "scroll",
                    "overall_overlap": round(all_jaccard, 2),
                    "leading_overlap": round(lead_jaccard, 2),
                },
            ))
        prev_frame = frame
    return events


# ─── Strategy 9: AUDIO INTENT ANCHORING ─────────────────────────────────────
#
# Covers the "fast typing" / "blurred OCR" case: when the SME narrated
# an action ("now I'm entering my age", "selecting Florida") but OCR
# never captured the resulting on-screen value, the transcript is the
# only witness.  This strategy reads transcript_segments and surfaces
# narrated actions as synthetic events anchored at the segment's
# timestamp.
#
# Detection pattern: an "action verb + noun" pair near a frame timestamp.
# Example phrases:
#     "Now I'll enter my date of birth"
#     "Selecting Florida from the state dropdown"
#     "Typing in the email address"
#     "I'm clicking submit"
#     "Choosing the smoker option"
#
# Confidence stays modest (0.55) because narration can drift from
# what's actually on screen — it's a complementary signal, not a
# replacement.

_AUDIO_ACTION_VERBS = frozenset({
    "enter", "entering", "entered",
    "type", "typing", "typed",
    "fill", "filling", "filled",
    "input", "inputting",
    "select", "selecting", "selected",
    "choose", "choosing", "chose", "chosen",
    "pick", "picking", "picked",
    "click", "clicking", "clicked",
    "tap", "tapping", "tapped",
    "press", "pressing", "pressed",
    "submit", "submitting", "submitted",
    "check", "checking", "checked",
    "toggle", "toggling", "toggled",
    "set", "setting",
})


def _extract_audio_intents(
    transcript_segments: list[dict],
) -> list[dict]:
    """Parse transcript segments for action-verb mentions.

    Returns a list of {timestamp_s, action_verb, target_phrase, raw}
    dicts representing SME-narrated user actions.  The target_phrase
    is the noun-phrase the verb was applied to (best-effort regex
    extraction), or empty when only the verb was spoken.
    """
    out: list[dict] = []
    if not transcript_segments:
        return out
    # Regex: action-verb followed within 8 words by a noun phrase.
    # The noun phrase is taken as 1-5 tokens, stopping at the next
    # action verb or sentence boundary.
    verb_pattern = "|".join(_AUDIO_ACTION_VERBS)
    intent_re = re.compile(
        r"\b(" + verb_pattern + r")\b"        # verb (group 1)
        r"(?:\s+(?:the|a|an|my|your|our)\s+)?"  # optional article
        r"(?:in\s+)?"                          # optional "in"
        r"([A-Za-z][A-Za-z0-9\s\-]{2,40}?)"    # target (group 2)
        r"(?=\s*(?:[,.;!?]|$|\band\b|\bthen\b|\bnow\b))",
        re.IGNORECASE,
    )
    for seg in transcript_segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("start_time") or 0.0)
        for m in intent_re.finditer(text):
            verb = m.group(1).lower()
            target = m.group(2).strip()
            # Filter trivial targets ("this", "that")
            target_words = target.lower().split()
            if not target_words:
                continue
            if target_words[0] in {"this", "that", "it", "them"}:
                continue
            out.append({
                "timestamp_s": start,
                "action_verb": verb,
                "target_phrase": target[:60],
                "raw": text[:120],
            })
    return out


def _synthesize_from_audio_intent(
    frames: list[dict],
    transcript_segments: list[dict],
    label_vocab: set[str],
    config: ClickSynthesizerConfig,
) -> list[CursorEvent]:
    """Emit a synthetic event for each narrated SME action that does
    NOT already have a strong OCR-derived counterpart.

    The strategy anchors each audio intent to the visual frame whose
    timestamp is closest to (and not after) the spoken anchor.  Lower
    confidence (0.55) reflects that narration is a complementary signal.

    We deliberately do NOT cross-check against already-emitted OCR
    events here — the dedupe pass at the end handles per-frame
    consolidation across all strategies and will fold a same-frame
    audio event into a higher-confidence form_value_inferred event.
    """
    if not transcript_segments or not frames:
        return []
    intents = _extract_audio_intents(transcript_segments)
    if not intents:
        return []

    # Sort frames by timestamp for binary-style lookup
    sorted_frames = sorted(
        frames, key=lambda f: float(f.get("timestamp_seconds", 0.0)),
    )
    events: list[CursorEvent] = []
    for intent in intents:
        ts = float(intent["timestamp_s"])
        # Find the frame whose timestamp is closest to (not after) ts.
        anchor: dict | None = None
        for f in sorted_frames:
            f_ts = float(f.get("timestamp_seconds", 0.0))
            if f_ts <= ts:
                anchor = f
            else:
                break
        if anchor is None:
            anchor = sorted_frames[0]

        # Action_kind derivation from spoken verb.
        verb_lower = intent["action_verb"]
        if verb_lower.startswith(("enter", "type", "fill", "input")):
            action_kind = "enter_text"
        elif verb_lower.startswith(("select", "choose", "pick")):
            action_kind = "select_option"
        elif verb_lower.startswith(("click", "tap", "press")):
            action_kind = "click_cta"
        elif verb_lower.startswith("submit"):
            action_kind = "submit_form"
        elif verb_lower.startswith(("check", "toggle")):
            action_kind = "check"
        else:
            action_kind = "interact"

        target = intent["target_phrase"]
        # If the narrated target matches a learned vocab label, prefer
        # the canonical label spelling for cross-referencing.
        for vocab_label in label_vocab:
            if (
                vocab_label in target.lower()
                or target.lower() in vocab_label
            ):
                target = vocab_label
                break

        events.append(CursorEvent(
            event_id=str(uuid.uuid4()),
            frame_id=anchor.get("frame_id", ""),
            frame_index=int(anchor.get("frame_index", 0)),
            timestamp_ms=int(round(ts * 1000.0)),
            cursor_x=0,
            cursor_y=0,
            velocity=0.0,
            is_click=True,
            is_drag=False,
            detection_method="audio_intent",
            confidence=0.55,
            metadata={
                "synthetic": True,
                "synthesis_kind": "audio_intent",
                "target_label": target,
                "action_kind": action_kind,
                "narration_excerpt": intent["raw"],
                "spoken_verb": verb_lower,
            },
        ))
    return events


# ─── Strategy 7: GENERIC placeholder→value transition detector ─────────────
#
# Domain-free.  Works on ANY UI: insurance, banking, healthcare, CRM,
# e-commerce, internal tools.  Detects the universal label-placeholder
# pattern:
#
#     Empty form :  "<Label>  <imperative-verb phrase>"
#                   e.g. "Gender Select your gender"
#                        "Account Type Choose account type"
#                        "Shipping Address Enter shipping address"
#
#     Filled form: "<Label>  <short value>"
#                   e.g. "Gender Male"
#                        "Account Type Checking"
#                        "Shipping Address 123 Main St"
#
# The detector does NOT need a vocabulary of field names.  It identifies
# labels by capitalization + length, and identifies placeholders by the
# presence of an imperative verb or by the label-echo pattern (the
# placeholder usually contains the label word, e.g. "Gender → Select
# your gender" echoes "gender").

# Imperative verbs that begin a placeholder, in every UI we'll encounter.
# Add new languages here by extending the set — the rest of the code is
# language-agnostic in structure (it works on tokens).
_IMPERATIVE_VERBS = frozenset({
    # English imperative verbs that begin a placeholder.  ONLY put real
    # verbs here — do not put question stems ("doyou", "whatis"), they
    # are not imperatives and would cause the learner to terminate
    # labels at the wrong position.
    "select", "enter", "choose", "pick", "type", "specify", "provide",
    "tell", "give", "add", "input", "fill", "set", "click", "tap",
    "press", "search", "find", "browse", "upload", "drag", "drop",
})

# Tokens that look capitalized but are pure UI chrome — they should
# never be a real form-field LABEL-START.  Kept intentionally small:
# generic nav-bar words ("Login", "Search", "Menu", "Home") and
# obvious imperative verbs.  Domain words like "Insurance", "Company",
# "Account" are NOT here because they can legitimately appear as the
# first word of a real label ("Insurance Provider", "Company Name").
# False positives from random caps in nav bars are filtered by the
# learner's requirement that a placeholder-verb follow the label —
# nav-bar words rarely satisfy that pattern.
_LABEL_BLOCKLIST = frozenset({
    "login", "logout", "logoff", "signin", "signup", "signout",
    "menu", "home", "back", "next", "continue", "previous",
    "submit", "cancel", "close", "exit", "yes", "no", "ok", "okay",
    "ask", "gemini",  # Chrome's AI-bar prompt text
    "click", "tap", "press",
    "select", "choose", "enter", "type", "pick", "specify",
})

# Pure article / joining words that are valid INSIDE a multi-word label
# ("Do you use cigarettes or e-cigarettes") but should not START a
# label on their own.  We don't put these in the blocklist because we
# want them allowed as continuation; the first-letter-uppercase
# requirement on `_is_label_token` filters them as label-starts.
_LABEL_JOINING_WORDS = frozenset({
    "and", "or", "of", "the", "a", "an", "to", "in", "on", "at",
    "for", "with", "from", "by", "your", "our", "their", "is", "are",
    "do", "does", "did", "use", "using",  # "Do you use X" pattern
})

# Words too short or generic to be a useful field VALUE — when these
# are the only candidates, skip emitting an event.
_VALUE_NOISE = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be",
    "do", "does", "did", "have", "has", "had",
    "i", "you", "we", "they", "it",
    "and", "or", "of", "to", "in", "on", "at",
})

# OCR commonly prepends a stray "S" or "Se" / "SEn" to a verb (e.g.
# "SEnter your annual income_").  When we strip such prefixes the rest
# should match an imperative verb.
_OCR_VERB_PREFIXES = ("s", "se", "sen", "sent", "ent", "enl", "selet")


def _strip_ocr_verb_prefix(tok_lower: str) -> str:
    """Best-effort recovery of an imperative verb that OCR mashed onto
    or fused with the prior label letter.

    Handles two OCR-fusion patterns observed in production:
      * Prefix-attached:  ``"SEnter"`` → ``"enter"``  (stray "S" prefix)
      * Suffix-attached:  ``"Selectyour"`` → ``"select"``  (verb fused
                          with a SHORT fusion-suffix word)

    Pattern B is INTENTIONALLY conservative: it requires the suffix
    after the verb to be a known short fusion-word ("your", "the",
    "here", "in", "to", ...).  Without this constraint, common English
    nouns that happen to start with a verb root (e.g. "Provider" →
    "provide", "Setting" → "set", "Tapper" → "tap") would be wrongly
    classified as verbs.
    """
    if tok_lower in _IMPERATIVE_VERBS:
        return tok_lower
    # Pattern A: stray prefix that hides a verb suffix.
    for prefix in _OCR_VERB_PREFIXES:
        if tok_lower.startswith(prefix) and len(tok_lower) > len(prefix):
            rest = tok_lower[len(prefix):]
            for verb in _IMPERATIVE_VERBS:
                if rest.startswith(verb):
                    return verb
    # Pattern B: a verb fused with a short suffix word.  The suffix
    # MUST be a recognised fusion target — typically a pronoun or
    # preposition that OCR jammed against the verb.
    _FUSION_SUFFIXES = (
        "your", "the", "a", "an", "here", "this", "that", "these",
        "those", "in", "on", "at", "to", "for", "by", "with", "and",
        "or", "name", "value",
    )
    for verb in _IMPERATIVE_VERBS:
        if len(verb) < 4:
            continue
        if not tok_lower.startswith(verb):
            continue
        rest = tok_lower[len(verb):]
        if rest in _FUSION_SUFFIXES:
            return verb
        # Also handle "Selectyourgender" — fusion + further continuation.
        for suffix in _FUSION_SUFFIXES:
            if rest.startswith(suffix) and len(rest) > len(suffix):
                # Make sure the next chunk starts a new word boundary
                # (the original would have been "Select your X").
                return verb
    return tok_lower


def _is_verb_start(token: str) -> bool:
    """True when this token represents the START of an imperative-verb
    placeholder, after stripping OCR-fusion."""
    return _strip_ocr_verb_prefix(token.lower()) in _IMPERATIVE_VERBS


def _is_label_token(token: str) -> bool:
    """A label-start is a capitalized word of length 3-30, not in the
    blocklist of generic / noise words.
    """
    if len(token) < 3 or len(token) > 30:
        return False
    if not token[0].isupper():
        return False
    if not token[1:].replace("-", "").isalpha():
        return False
    if token.lower() in _LABEL_BLOCKLIST:
        return False
    return True


def _is_placeholder_body(body_tokens: list[str], label_words: set[str]) -> bool:
    """A body is a placeholder when ANY of:
      * its first non-noise token is an imperative verb (after stripping
        OCR-mashed prefixes), OR
      * any of its tokens (case-insensitive) appears in the label words —
        the "Gender → Select your gender" echo pattern.

    label_words is the lowercased set of significant words in the label.
    """
    if not body_tokens:
        return False
    lowered = [_strip_ocr_verb_prefix(t.lower()) for t in body_tokens]
    # Filter out value-noise leading tokens
    non_noise = [t for t in lowered if t not in _VALUE_NOISE]
    if non_noise and non_noise[0] in _IMPERATIVE_VERBS:
        return True
    # Echo pattern: any token matches a label word (case-insensitive).
    for t in lowered:
        if t in label_words and len(t) >= 3:
            return True
    return False


def _is_likely_ocr_garbage(text: str) -> bool:
    """Heuristic OCR-garbage rejector for synthesized field values.

    Real user-entered values look like words or numbers; OCR noise on
    UI chrome / page content often looks like:
      * "GilHub Inbox 417 Latest"  — mid-word capitals (camelCase
        inside a token, e.g. "GilHub"), which real form values rarely
        have
      * "Iwitn oftns Uat"          — abnormally low vowel ratio
        (consonant clusters that aren't real words)

    Returns True when text matches one of these noise signatures.
    All-numeric tokens (zip codes, ages, dollar amounts) pass freely.
    """
    if not text:
        return True
    tokens = text.split()
    if not tokens:
        return True
    # If ANY token has an inner capital letter (not at the start), it's
    # likely OCR-conjoined text like "GilHub" or "PrEmium".  Real values
    # are either lowercase, Title-case, or all-caps.
    for t in tokens:
        if len(t) >= 4 and t[0].isupper():
            inner_caps = sum(1 for c in t[1:] if c.isupper())
            if inner_caps > 0 and not t.isupper():
                return True
    # If the value has only short non-numeric tokens and a low vowel
    # ratio, it's likely scrambled OCR.  Numbers are exempt.
    text_lower = text.lower()
    letter_count = sum(1 for c in text_lower if c.isalpha())
    if letter_count >= 4:
        vowels = sum(1 for c in text_lower if c in "aeiou")
        if vowels / letter_count < 0.20:
            return True
    return False


def _looks_like_value(tokens: list[str], label_words: set[str]) -> str:
    """Return the captured value when ``tokens`` look like a real
    user-entered value (not a placeholder, not just noise).  Returns
    empty string when no plausible value is present.

    Heuristics:
      * Drop a leading noise token ("a", "the", "is", ...)
      * Reject if the next significant token is an imperative verb
        (placeholder leaked through)
      * Reject if the value echoes the label
      * Keep at most 4 significant tokens
    """
    if not tokens:
        return ""
    significant: list[str] = []
    for t in tokens:
        tl = t.lower()
        if tl in _VALUE_NOISE:
            continue
        if _strip_ocr_verb_prefix(tl) in _IMPERATIVE_VERBS:
            return ""  # placeholder leaked through
        if tl in label_words and len(tl) >= 3:
            return ""  # label echo — empty form, not a value
        significant.append(t)
        if len(significant) >= 4:
            break
    if not significant:
        return ""
    # Reject single-character or all-numeric junk
    joined = " ".join(significant)
    if len(joined) <= 1:
        return ""
    return joined


def _tokenize_with_index(text: str) -> list[tuple[int, str]]:
    """Tokenize text into (start_offset, token) pairs.

    Tokens are alphanumeric runs (letters, digits, hyphens).  Pure-digit
    runs are kept too because they are common user-entered values
    (zip codes, ages, dollar amounts).
    """
    out: list[tuple[int, str]] = []
    for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9-]*", text):
        out.append((m.start(), m.group(0)))
    return out


def _is_label_continuation(token: str) -> bool:
    """A label can be a CAP word followed by lowercase continuation
    words like "income" in "Annual income", "name" in "First name",
    or short joining words in "Do you use cigarettes or e-cigarettes".

    Continuation tokens are alphabetic and short enough to plausibly
    belong to a label phrase.  Joining words ("or", "of", "the") are
    explicitly allowed.  Pure UI chrome (Login, Menu) is blocked.
    """
    tl = token.lower()
    if tl in _LABEL_BLOCKLIST:
        return False
    if tl in _LABEL_JOINING_WORDS:
        return True
    if len(token) < 2 or len(token) > 30:
        return False
    if not token.replace("-", "").isalpha():
        return False
    return True


def _learn_labels_from_punctuation(text: str) -> set[str]:
    """Additional label-learning path that uses punctuation cues
    instead of placeholder verbs.

    Many production UIs render labels with explicit punctuation:
        "Email: user@example.com"
        "Name *: John"
        "Status > Open"
        "Quantity → 3"

    These never have a placeholder verb, so :func:`_learn_label_vocabulary`
    can't see them.  This pass finds them by regex-matching common
    label-terminator characters in the raw OCR text (before tokenization
    drops the punctuation).

    Returned set is lowercased label strings.  Works on any UI domain —
    the patterns are universal punctuation conventions, not vocabulary.
    """
    out: set[str] = set()
    if not text:
        return out
    # Pattern: 1-4 capitalised words followed by COLON, then content.
    # We restrict to colon specifically because other punctuation
    # (`-`, `>`, `→`, `•`, `·`) appears too frequently in nav text and
    # generates false positives.  Colon is the unambiguous label-value
    # separator across most UIs.  Optional asterisk for required-field
    # markers is allowed before the colon.
    label_punct_re = re.compile(
        r"\b("                               # group 1: label
        r"[A-Z][A-Za-z]{1,29}"               #   capitalised first word
        r"(?:\s+(?:[a-z][a-zA-Z]{1,29}|[A-Z][A-Za-z]{1,29})){0,3}"  # 0-3 continuation
        r")\s*"
        r"\*?\s*"                             # optional asterisk
        r":"                                  # colon terminator
        r"\s*"
        r"(?=[A-Za-z0-9])",                    # followed by content
    )
    for m in label_punct_re.finditer(text):
        label_raw = m.group(1).strip()
        words = label_raw.split()
        if not (1 <= len(words) <= 4):
            continue
        # Reject labels that are ONLY blocklist words (Login: …).
        if all(w.lower() in _LABEL_BLOCKLIST for w in words):
            continue
        # Reject labels ending in joining word.
        if words[-1].lower() in _LABEL_JOINING_WORDS:
            continue
        out.add(label_raw.lower())
    return out


def _learn_label_vocabulary(frames: list[dict]) -> set[str]:
    """Pass 1: scan every frame's OCR for placeholder patterns and
    record the labels we observe.

    A "label" is identified by the universal placeholder convention:
    a capitalized word followed within a few tokens by an imperative
    verb, with optional lowercase continuation words in between (for
    multi-word labels like "Annual income" or "Do you use cigarettes").

    Concrete patterns learned from a Guardian-Life empty form:
        "Gender Selectyour gender"           → label "gender"
        "Annual income Enter your..."        → label "annual income"
        "Doyou use cigarettes...? Select"    → label "doyou use cigarettes ..."
        "First name Enter your first name"   → label "first name"

    Patterns learned from a banking wire form:
        "Account Number Enter account number" → label "account number"
        "Routing Number Enter routing"        → label "routing number"

    Returned set is lowercased.  Multi-frame learning means a label
    that appears as a placeholder in frame N is recognised on frame
    N+5 even when it's filled in (no placeholder verb visible).

    This is the linchpin of domain-freeness: we learn the UI's own
    field labels, not a hardcoded vocabulary.
    """
    # Scan window: how many tokens we'll look at after a cap-word to
    # find a placeholder verb.  6 covers most real-world labels
    # ("Do you use cigarettes or e-cigarettes? Select").
    MAX_LABEL_SPAN = 6
    labels: set[str] = set()
    for frame in frames:
        text = frame.get("extracted_text") or ""
        if not text:
            continue
        # NOTE: punctuation-based learning lives in
        # `_learn_labels_from_punctuation` and is called separately by
        # the strategy function so it can distinguish punct-only labels
        # (which trigger Case C with lower confidence) from
        # placeholder-anchored labels (which trigger Case A/B).
        toks = [t for _, t in _tokenize_with_index(text)]
        n = len(toks)
        i = 0
        while i < n:
            if not _is_label_token(toks[i]):
                i += 1
                continue
            # Look ahead up to MAX_LABEL_SPAN tokens for a verb.  Every
            # token in between must be a plausible label-continuation.
            # Special rule: once we've passed a LOWERCASE content token,
            # the next CAPITALISED label-token signals the start of a
            # DIFFERENT field — break there.
            #
            # Why: "Insurance Provider Choose" → both caps adjacent, no
            # lowercase between, so the label is "Insurance Provider".
            # But "Username admin example com Password Enter" → after
            # the lowercase value tokens (admin / example / com), the
            # cap "Password" is the NEXT field's label, not a continuation.
            verb_at = -1
            saw_lowercase_continuation = False
            for j in range(i + 1, min(i + 1 + MAX_LABEL_SPAN, n)):
                tj = toks[j]
                if _is_verb_start(tj):
                    verb_at = j
                    break
                if tj.isdigit():
                    break
                is_lt = _is_label_token(tj)
                is_c = _is_label_continuation(tj)
                # Cap-then-lowercase-then-cap = field boundary, stop.
                if saw_lowercase_continuation and is_lt:
                    break
                # A lowercase continuation (joining word, lowercase
                # content word) is acceptable but marks the chain as
                # "no more cap extensions allowed".
                if not is_lt and is_c:
                    saw_lowercase_continuation = True
                    continue
                if not is_lt and not is_c:
                    break
            if verb_at <= 0:
                i += 1
                continue
            # Label = the ENTIRE span between the cap-start and the
            # placeholder verb.  This is the cleanest signal: anything
            # between a capitalised label-start and a placeholder verb
            # IS the label, by definition of the placeholder convention.
            #
            # Don't trim trailing lowercase tokens — for "Annual income
            # Enter" the label is "Annual income" (both tokens); for
            # "Do you use cigarettes or e-cigarettes? Select" the label
            # is the entire question.  Trimming on capitalisation is
            # wrong because lowercase continuation words are legitimate
            # parts of multi-word labels.
            label_tokens = toks[i:verb_at]
            if 1 <= len(label_tokens) <= 6:
                # Defensive: don't emit a single-token "label" that's
                # an imperative verb in disguise.
                bad_single = (
                    len(label_tokens) == 1
                    and _is_verb_start(label_tokens[0])
                )
                # Real form labels never END in a joining/article word
                # like "your", "the", "a", "to".  If the learner trimmed
                # to such an ending, that's a sign of OCR corruption
                # (e.g. "Jive your X" misread of "Give your X").
                bad_ending = (
                    len(label_tokens) >= 2
                    and label_tokens[-1].lower() in _LABEL_JOINING_WORDS
                )
                if not bad_single and not bad_ending:
                    labels.add(" ".join(label_tokens).lower())
            # Advance past the verb so we don't re-learn nested labels.
            i = verb_at + 1
    return labels


def _segment_form_fields(
    text: str, label_vocab: set[str],
) -> list[dict]:
    """Pass 2: walk OCR text using the learned label vocabulary and
    identify (label, body, is_placeholder) triples.

    For each known label that appears in the OCR (matched longest-first
    to handle "Annual Income" before "Annual"):
      * body = tokens between this label and the next label occurrence
      * is_placeholder = body starts with imperative verb OR echoes label
    """
    if not label_vocab:
        return []
    tokens = [t for _, t in _tokenize_with_index(text)]
    n = len(tokens)
    if n == 0:
        return []

    def _match_label_at(idx: int) -> tuple[str, int]:
        """Try to match the longest known label starting at tokens[idx].
        Returns (label_text_lower, length_in_tokens) or ("", 0)."""
        if idx >= n or not _is_label_token(tokens[idx]):
            return "", 0
        # Try longest-first up to the longest learned label.
        max_len = min(6, n - idx)
        for label_len in range(max_len, 0, -1):
            candidate = " ".join(tokens[idx:idx + label_len]).lower()
            if candidate in label_vocab:
                return candidate, label_len
        return "", 0

    segments: list[dict] = []
    i = 0
    while i < n:
        matched_label, matched_len = _match_label_at(i)
        if not matched_label:
            i += 1
            continue

        original_label = " ".join(tokens[i:i + matched_len])
        label_words = {t.lower() for t in tokens[i:i + matched_len]}

        # Body extends until the NEXT known label occurrence or 6 tokens.
        body_start = i + matched_len
        body_end = body_start
        max_body = min(body_start + 6, n)
        while body_end < max_body:
            # Stop the body at the next known label start.
            nl, _ = _match_label_at(body_end)
            if nl:
                break
            body_end += 1
        body_tokens = tokens[body_start:body_end]

        if body_tokens:
            segments.append({
                "label": original_label,
                "body": " ".join(body_tokens),
                "body_tokens": body_tokens,
                "label_words": label_words,
                "is_placeholder": _is_placeholder_body(body_tokens, label_words),
            })
        i = body_end
    return segments


def _synthesize_generic_placeholder_transition(
    frames: list[dict],
    config: ClickSynthesizerConfig,
) -> list[CursorEvent]:
    """Generic placeholder→value transition detector.

    Algorithm per pair of adjacent frames:
      1. Segment each frame's OCR into (label, body, is_placeholder).
      2. Build a per-label lookup of the *most recent* state seen.
      3. For each label on the current frame:
           - if current is NOT a placeholder
           - AND the prior recorded state for this label WAS a placeholder
             (or absent)
           - AND the current body parses as a plausible value
         then emit an event with target_label = label, observed_value = value.

    Why this works across domains:
      * Labels are identified by capitalisation only — works in any UI
        that follows the standard "Capitalised Label : value" convention,
        which is universal across Western-script UIs.
      * Placeholders are identified by imperative verbs OR by the label
        echoing in the body — a structural cue, not a vocabulary lookup.
      * Values are identified by structural negation: short, doesn't
        start with an imperative verb, doesn't echo the label.

    Confidence is calibrated lower than insurance-pattern Strategy 6
    (0.72 vs 0.78) because the generic detector has more recall but
    slightly more false-positive risk on UIs where the placeholder
    convention isn't followed.
    """
    # Pass 1: learn the label vocabulary from any placeholder occurrence
    # across ALL frames.  This means we recognise "Gender" as a label
    # even on frames where it's already filled in (no placeholder verb
    # visible), so long as it appeared as a placeholder on any prior or
    # later frame in the recording.
    label_vocab = _learn_label_vocabulary(frames)

    # Pass 1.5: learn labels that NEVER show a placeholder.  These are
    # labels surfaced only via punctuation ("Email: ...", "Status > ...")
    # — common on dashboards, list views, detail pages.  For these we
    # use a lower-confidence transition rule (Case C) since we don't
    # have placeholder evidence to anchor the field.
    punct_only_labels: set[str] = set()
    for frame in frames:
        text = frame.get("extracted_text") or ""
        punct_learned = _learn_labels_from_punctuation(text)
        for lab in punct_learned:
            if lab not in label_vocab:
                punct_only_labels.add(lab)
                label_vocab = label_vocab | {lab}

    events: list[CursorEvent] = []
    # label_lower → (prev_body, prev_is_placeholder)
    prev_state: dict[str, tuple[str, bool]] = {}
    for frame in frames:
        text = frame.get("extracted_text") or ""
        segments = _segment_form_fields(text, label_vocab)
        # We may see the same label twice on one frame (multi-step
        # forms).  Use the LAST occurrence per label per frame — that's
        # the "freshest" state and the one a user-edit would land on.
        per_frame: dict[str, dict] = {}
        for seg in segments:
            per_frame[seg["label"].lower()] = seg

        for label_lower, seg in per_frame.items():
            curr_body = seg["body"]
            curr_is_placeholder = seg["is_placeholder"]
            # Use None as the "never seen" sentinel so we can distinguish
            # "first-ever sighting of this label" from "we saw it before
            # and it was a placeholder".  This prevents emitting an
            # event on a label's FIRST appearance with a value when no
            # placeholder was ever observed — the most common source
            # of false positives on nav bars and search overlays.
            prev_body, prev_was_placeholder = prev_state.get(label_lower, ("", None))

            # Re-edit support — two transition cases:
            #   Case A: first placeholder→value transition for this label
            #   Case B: re-edit (value→different value) AFTER a placeholder
            #           was previously observed for this label
            # Once a label has shown placeholder→value once, every
            # subsequent value change is also a user action — "typed
            # John, then changed to Mary" should emit two events, not one.
            ever_had_placeholder = prev_state.get(
                f"__{label_lower}__ever_ph__", ("", False),
            )[1]
            case_a_first_fill = (
                prev_was_placeholder is True
                and not curr_is_placeholder
                and curr_body != prev_body
            )
            case_b_re_edit = (
                prev_was_placeholder is False
                and not curr_is_placeholder
                and ever_had_placeholder
                and curr_body != prev_body
                and prev_body != ""
            )
            # Case C: punctuation-learned labels (no placeholder ever
            # observed).  Fires on any non-empty value change.  Lower
            # confidence because we have no placeholder anchor.
            # Tight value-shape filter: real form values are short
            # (≤3 tokens) OR all-numeric OR contain '@'.  This rejects
            # the most common Case C false-positive: nav-bar OCR like
            # "Resources About us Lama Log Hospital indemnity insurance"
            # being interpreted as a label-value pair.
            curr_body_tokens = seg["body_tokens"]
            value_shape_ok = (
                len(curr_body_tokens) <= 3
                or "@" in curr_body
                or any(t.isdigit() for t in curr_body_tokens)
                or all(t.islower() for t in curr_body_tokens)
            )
            case_c_punct_only = (
                label_lower in punct_only_labels
                and not curr_is_placeholder
                and curr_body != prev_body
                and curr_body.strip() != ""
                and not ever_had_placeholder
                and value_shape_ok
            )
            transitioned = (
                case_a_first_fill or case_b_re_edit or case_c_punct_only
            )
            if transitioned:
                value = _looks_like_value(seg["body_tokens"], seg["label_words"])
                value_is_garbage = bool(value) and _is_likely_ocr_garbage(value)

                # Strategy 11: when the value is unreadable (OCR garbage,
                # masked-password dots, captcha image OCR'd to nonsense),
                # we STILL emit an event — just with observed_value
                # marked as "[unreadable]" so the test-case generator
                # knows a user-action happened here even if the value
                # itself isn't usable.  Critical for captcha / masked
                # inputs / sub-pixel text where the pipeline cannot
                # recover the actual entry.
                if value_is_garbage and case_a_first_fill:
                    events.append(CursorEvent(
                        event_id=str(uuid.uuid4()),
                        frame_id=frame.get("frame_id", ""),
                        frame_index=int(frame.get("frame_index", 0)),
                        timestamp_ms=int(round(float(frame.get("timestamp_seconds", 0.0)) * 1000.0)),
                        cursor_x=0,
                        cursor_y=0,
                        velocity=0.0,
                        is_click=True,
                        is_drag=False,
                        detection_method="field_interaction_unreadable",
                        confidence=0.55,
                        metadata={
                            "synthetic": True,
                            "synthesis_kind": "field_interaction_unreadable",
                            "target_label": seg["label"],
                            "observed_value": "[unreadable]",
                            "raw_observed_value": value[:80],
                            "action_kind": "interact",
                            "prior_body": prev_body[:80] if prev_body else "",
                            "note": (
                                "field changed from placeholder to a value "
                                "OCR could not read — likely masked input, "
                                "captcha, or sub-pixel text"
                            ),
                        },
                    ))
                    value = ""  # suppress the normal path

                if value:
                    label = seg["label"]
                    # Heuristic action_kind: short/single-token values are
                    # likely dropdowns or radios (select_option); multi-
                    # word or numeric/symbol values are typed (enter_text).
                    is_short_choice = (
                        len(value.split()) == 1
                        and value.replace("-", "").isalpha()
                        and len(value) <= 12
                    )
                    action_kind = "select_option" if is_short_choice else "enter_text"
                    # detection_method varies by case so the UI can
                    # render different chips:
                    #   placeholder_transition — first fill (Case A)
                    #   field_re_edit          — re-edit (Case B)
                    #   labelled_value_change  — punctuation-only label (Case C)
                    if case_c_punct_only:
                        det_method = "labelled_value_change"
                        conf = 0.60  # weaker — no placeholder anchor
                    elif case_b_re_edit:
                        det_method = "field_re_edit"
                        conf = 0.72
                    else:
                        det_method = "placeholder_transition"
                        conf = 0.72
                    events.append(CursorEvent(
                        event_id=str(uuid.uuid4()),
                        frame_id=frame.get("frame_id", ""),
                        frame_index=int(frame.get("frame_index", 0)),
                        timestamp_ms=int(round(float(frame.get("timestamp_seconds", 0.0)) * 1000.0)),
                        cursor_x=0,
                        cursor_y=0,
                        velocity=0.0,
                        is_click=True,
                        is_drag=False,
                        detection_method=det_method,
                        confidence=conf,
                        metadata={
                            "synthetic": True,
                            "synthesis_kind": det_method,
                            "target_label": label,
                            "observed_value": value[:80],
                            "action_kind": action_kind,
                            "prior_body": prev_body[:80] if prev_body else "",
                            "current_body": curr_body[:80],
                            "is_re_edit": case_b_re_edit,
                            "is_punct_only": case_c_punct_only,
                        },
                    ))

            prev_state[label_lower] = (curr_body, curr_is_placeholder)
            # Track "did this label EVER show a placeholder?" — required
            # so a re-edit (Case B) only fires AFTER an initial fill,
            # never on a first-ever sighting of nav-bar text.
            if curr_is_placeholder:
                prev_state[f"__{label_lower}__ever_ph__"] = ("", True)
    return events


def _synthesize_from_frame_actions(
    scenes: list[dict],
    frame_by_index: dict[int, dict],
    config: ClickSynthesizerConfig,
) -> list[CursorEvent]:
    """Promote click-like frame_actions in scene_state_summary into
    CursorEvent click rows.

    The canonical pipeline's existing heuristic action classifier writes
    a frame-level action list into ``scene_state_summary.frame_actions``
    with action_kind labels like ``open_overlay``, ``state_change``,
    ``click``, ``click_cta``, ``close_overlay``.  These have already
    been gated by the pipeline's own confidence threshold, but they
    were never propagated to cursor_events.  We promote the explicitly
    click-like records here so the triangulator and detail panel see
    them as cursor signals.

    Only ``open_overlay``, ``close_overlay``, ``click``, ``click_cta``
    are promoted — ``state_change`` is excluded because that category
    is often just OCR text drift, not a user click.
    """
    CLICK_KINDS = {"open_overlay", "close_overlay", "click", "click_cta"}
    events: list[CursorEvent] = []
    for scene in scenes:
        summary = scene.get("scene_state_summary") or {}
        if not isinstance(summary, dict):
            continue
        actions = summary.get("frame_actions") or []
        if not isinstance(actions, list):
            continue
        for a in actions:
            if not isinstance(a, dict):
                continue
            kind = (a.get("action_kind") or "").lower()
            if kind not in CLICK_KINDS:
                continue
            conf = float(a.get("confidence") or 0.0)
            if conf < 0.5:
                continue
            after_idx = int(a.get("to_frame_index") or 0)
            frame = frame_by_index.get(after_idx)
            if frame is None:
                continue
            target = (a.get("target_label") or "").strip()
            delta_text = (a.get("delta_text") or "").strip()
            events.append(CursorEvent(
                event_id=str(uuid.uuid4()),
                frame_id=frame.get("frame_id", ""),
                frame_index=after_idx,
                timestamp_ms=int(round(float(a.get("to_timestamp_s") or 0.0) * 1000.0)),
                cursor_x=0,
                cursor_y=0,
                velocity=0.0,
                is_click=True,
                is_drag=False,
                detection_method="frame_action_promotion",
                confidence=min(0.85, conf + 0.05),
                metadata={
                    "synthetic": True,
                    "synthesis_kind": "frame_action_promotion",
                    "promoted_kind": kind,
                    "delta_text": delta_text[:160],
                    "target_label": target[:80] or delta_text.split()[0][:80] if delta_text else "",
                    "evidence_signals": a.get("evidence_signals") or [],
                },
            ))
    return events


def _synthesize_control_value(
    controls: list[dict],
    frame_by_id: dict[str, dict],
    config: ClickSynthesizerConfig,
) -> list[CursorEvent]:
    """Emit a click event for every evidence_control with a non-empty
    ``observed_value`` (or ``value_text``).

    A control that has a captured value is direct evidence the user
    interacted with it.  For text fields / textareas we still mark
    ``is_click=True`` so the triangulator pairs the value with a step;
    the action_kind on the resulting evidence_step comes from the
    triangulator's existing rules (which look at element_type + control
    action_kind), not from this synthesizer.
    """
    events: list[CursorEvent] = []
    for c in controls:
        value = (c.get("observed_value") or c.get("value_text") or "").strip()
        if not value:
            continue

        frame_id = c.get("frame_id") or ""
        frame = frame_by_id.get(frame_id)
        if frame is None:
            continue

        label = (c.get("display_label") or c.get("label_text") or "").strip()
        et = (c.get("element_type") or "").lower()
        action_kind = (c.get("action_kind") or "").lower()

        events.append(CursorEvent(
            event_id=str(uuid.uuid4()),
            frame_id=frame_id,
            frame_index=int(frame.get("frame_index", 0)),
            timestamp_ms=int(round(float(frame.get("timestamp_seconds", 0.0)) * 1000.0)),
            cursor_x=0,
            cursor_y=0,
            velocity=0.0,
            is_click=True,
            is_drag=False,
            detection_method="control_value",
            confidence=config.control_value_confidence,
            metadata={
                "synthetic": True,
                "synthesis_kind": "control_value",
                "control_id": c.get("control_id"),
                "control_label": label[:80],
                "control_value": value[:80],
                "element_type": et,
                "control_action_kind": action_kind,
                "target_label": label[:80] or et or "control",
            },
        ))
    return events


def _dedupe_events(events: list[CursorEvent]) -> list[CursorEvent]:
    """Drop duplicate events that describe the same user-action.

    Dedup key is (frame_index, target_label_normalised) — NOT frame_index
    alone — because a single frame can legitimately carry multiple
    distinct form-value events (Gender=Male AND Smoker=Yes appearing
    simultaneously after the user fills both fields).  Two strategies
    that fire on the same frame WITH the same target label are
    consolidated into one event with confidence bonus + corroboration
    metadata.

    A click-only event (no target_label) on the same frame as a form-value
    event collapses INTO the form-value event because the form-value
    strategy is more specific.
    """
    def _norm_label(e: CursorEvent) -> str:
        meta = e.metadata or {}
        tl = (meta.get("target_label") or "").strip().lower()
        # Strip OCR noise / URL prefix so navigation events still dedupe
        # against each other.
        return tl[:60]

    by_key: dict[tuple[int, str], list[CursorEvent]] = {}
    for e in events:
        key = (e.frame_index, _norm_label(e))
        by_key.setdefault(key, []).append(e)

    # Second-pass: events with NO target_label on the same frame as one
    # WITH a target_label fold into the latter (it's more specific).
    # We do this by collecting all labels-present-per-frame first.
    labels_per_frame: dict[int, set[str]] = {}
    for (fi, label), bucket in by_key.items():
        if label:
            labels_per_frame.setdefault(fi, set()).add(label)

    out: list[CursorEvent] = []
    consumed: set[tuple[int, str]] = set()

    # Pass 1: emit one event per (frame, label) bucket
    for key in sorted(by_key.keys(), key=lambda k: (k[0], k[1])):
        if key in consumed:
            continue
        fi, label = key
        bucket = list(by_key[key])

        # Fold no-label events on the same frame INTO a labelled bucket
        # when at least one labelled event exists.  This keeps the
        # generic "url_delta navigated" event from competing with the
        # specific "Gender = Male" event for the same frame.
        if label and (fi, "") in by_key and (fi, "") not in consumed:
            bucket.extend(by_key[(fi, "")])
            consumed.add((fi, ""))

        # Pick the highest-confidence event; record corroborators.
        bucket.sort(key=lambda e: e.confidence, reverse=True)
        winner = bucket[0]
        corroborators = [e.detection_method for e in bucket[1:]]
        winner.metadata = dict(winner.metadata or {})
        if corroborators:
            winner.metadata["corroborating_methods"] = corroborators
            agreement_bonus = min(0.12, 0.04 * len(corroborators))
            winner.confidence = min(0.92, winner.confidence + agreement_bonus)
        out.append(winner)
        consumed.add(key)

    # Pass 2: emit no-label events on frames that have no labelled
    # event at all (so generic navigation/click signals aren't lost
    # on frames where no form value was found).
    for key in sorted(by_key.keys()):
        if key in consumed:
            continue
        fi, label = key
        if label:
            continue
        if labels_per_frame.get(fi):
            # Already folded into a labelled event above
            continue
        bucket = by_key[key]
        bucket.sort(key=lambda e: e.confidence, reverse=True)
        winner = bucket[0]
        if len(bucket) > 1:
            corroborators = [e.detection_method for e in bucket[1:]]
            winner.metadata = dict(winner.metadata or {})
            winner.metadata["corroborating_methods"] = corroborators
            agreement_bonus = min(0.12, 0.04 * len(corroborators))
            winner.confidence = min(0.92, winner.confidence + agreement_bonus)
        out.append(winner)

    out.sort(key=lambda e: (e.frame_index, -e.confidence))
    return out


# ─── Public entry point ──────────────────────────────────────────────────────


def synthesize_clicks(
    *,
    frames: Iterable[dict],
    controls: Iterable[dict] = (),
    scenes: Iterable[dict] = (),
    transcript_segments: Iterable[dict] = (),
    config: Optional[ClickSynthesizerConfig] = None,
) -> list[CursorEvent]:
    """Produce synthesized cursor-click events for a single artifact.

    The caller passes the same per-frame data the rest of the pipeline
    already has — no new persistence, no new external calls.

    Parameters
    ----------
    frames
        Ordered iterable of frame dicts.  Each dict should expose:
        ``frame_id``, ``frame_index``, ``timestamp_seconds``,
        ``extracted_text``, ``url_or_path``, ``ui_elements_json``.
        Missing fields are tolerated — the corresponding strategy
        contributes zero events for that frame.
    controls
        Iterable of evidence_control dicts: ``control_id``, ``frame_id``,
        ``label_text``, ``observed_value`` / ``value_text``,
        ``element_type``, ``action_kind``, ``display_label``.
    config
        Optional override for synthesis thresholds.

    Returns
    -------
    list[CursorEvent]
        Events ordered by ``frame_index``, deduped (one per frame),
        with ``detection_method`` set to one of:
        ``url_delta``, ``ocr_new_block``, ``ocr_removed_block``,
        ``ui_element_change``, ``control_value``.
    """
    cfg = config or ClickSynthesizerConfig()
    frames_list = sorted(
        (dict(f) for f in frames),
        key=lambda f: int(f.get("frame_index", 0)),
    )
    if not frames_list:
        return []

    frame_by_id: dict[str, dict] = {
        f.get("frame_id", ""): f for f in frames_list if f.get("frame_id")
    }
    frame_by_index: dict[int, dict] = {
        int(f.get("frame_index", 0)): f for f in frames_list
    }
    controls_list = list(controls)
    scenes_list = list(scenes)
    transcript_list = list(transcript_segments)

    all_events: list[CursorEvent] = []
    all_events.extend(_synthesize_url_delta(frames_list, cfg))
    all_events.extend(_synthesize_ocr_block_delta(frames_list, cfg))
    all_events.extend(_synthesize_ui_element_change(frames_list, cfg))
    all_events.extend(_synthesize_from_frame_actions(scenes_list, frame_by_index, cfg))
    all_events.extend(_synthesize_control_value(controls_list, frame_by_id, cfg))
    # Strategy 7: GENERIC placeholder→value transition detector.
    # Works on any UI domain.  Now also covers re-edits (Case B),
    # punctuation-only labels (Case C), and unreadable values (captcha
    # / masked / blurred via field_interaction_unreadable events).
    all_events.extend(
        _synthesize_generic_placeholder_transition(frames_list, cfg)
    )
    # Strategy 6: INSURANCE-domain regex booster (optional confidence
    # boost on the demo artifact; zero impact on other domains).
    all_events.extend(_synthesize_form_value_pattern(frames_list, cfg))
    # Strategy 10: scroll / viewport-shift detection (no cursor needed).
    all_events.extend(_synthesize_viewport_shift(frames_list, cfg))
    # Strategy 9: audio-intent anchoring (recovers actions OCR missed).
    # Requires a label_vocab so narrated targets can be normalised to
    # canonical labels — we re-learn it here for the audio strategy.
    if transcript_list:
        audio_vocab = _learn_label_vocabulary(frames_list)
        for f in frames_list:
            t = f.get("extracted_text") or ""
            audio_vocab |= _learn_labels_from_punctuation(t)
        all_events.extend(
            _synthesize_from_audio_intent(
                frames_list, transcript_list, audio_vocab, cfg,
            )
        )

    return _dedupe_events(all_events)


__all__ = [
    "ClickSynthesizerConfig",
    "synthesize_clicks",
]
