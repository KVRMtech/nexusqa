"""Extract action-intent events from SME narration transcripts.

The SME's narration is the most reliable signal we have for what the
user *intended* to do at each point in time.  Unlike OCR (which lies
about typed values) and LLaVA (which describes scenes generically), an
utterance like "now I'm going to click Submit" is unambiguous about both
the action_kind (click) and the target (Submit).

This module parses :class:`TranscriptSegment`-like records and returns a
list of :class:`IntentEvent` records.  Each event carries:

    timestamp_ms   — segment start time, used as the anchor
    duration_ms    — segment length so downstream consumers can decide a
                     plausible window (the action usually happens during
                     or just after the utterance)
    intent_kind    — canonical action verb matching control_extractor's
                     vocabulary (click_cta, enter_text, select_option,
                     submit_form, navigate, review)
    target_phrase  — the noun phrase the SME named ("Submit", "Birthdate")
    raw_text       — the original segment text (for audit + UI display)
    confidence     — pattern match strength × segment.confidence; used
                     by the triangulator to weight this signal

Pure-Python, regex-based, no LLM call: the patterns are tuned for English
KT-narration grammar.  Multi-language support requires per-locale pattern
sets — out of scope for this module today, but the interface is ready
(see ``language`` parameter).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# ─── Canonical intent vocabulary ─────────────────────────────────────────────

# These values must match (a subset of) the action_kind taxonomy used by
# control_extractor.py and step_extractor.py so triangulation does not
# need a translation table.
INTENT_KINDS = (
    "click_cta",      # "click submit", "press continue", "tap the button"
    "enter_text",     # "type my name", "enter the policy number"
    "select_option",  # "select yes", "choose male", "pick the first option"
    "submit_form",    # "submit the form", "send it", "finalize"
    "navigate",       # "go to the dashboard", "open billing"
    "review",         # "let me check", "verify the totals", "review the result"
    "scroll",         # "scroll down", "go further down"
    "open_overlay",   # "open the menu", "expand this section"
    "close_overlay",  # "close that", "dismiss the popup"
)


@dataclass(frozen=True)
class IntentEvent:
    """One action the SME announced verbally.

    Equality is structural so tests can compare directly; the dataclass is
    frozen so callers do not mutate it after extraction.

    When the source transcript segment carries Whisper word-level
    timestamps, the extractor populates ``verb_word_ts_ms`` with the
    spoken-time of the action verb itself (e.g. the literal "click"
    word), giving sub-second precision on intent timing.  Without
    word-level data the field equals ``timestamp_ms`` and consumers
    fall back to segment-level alignment.
    """

    timestamp_ms: int
    duration_ms: int
    intent_kind: str
    target_phrase: str
    raw_text: str
    confidence: float
    speaker: str = ""
    segment_index: int = 0
    # Word-level timing (Phase F.3) — the verb's spoken-time within the
    # segment.  Defaults to ``timestamp_ms`` so callers reading without
    # awareness still get a valid anchor.
    verb_word_ts_ms: int = 0
    target_word_ts_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def end_ms(self) -> int:
        return self.timestamp_ms + max(0, self.duration_ms)


# ─── Pattern engine ──────────────────────────────────────────────────────────

# Each pattern entry is (intent_kind, regex, base_confidence).  Regex
# captures group ``target`` whenever a target phrase is identifiable; if
# no group matches, the target_phrase is left empty.  Patterns are
# ordered roughly from most-specific to most-generic so the first match
# wins.

_FILLER_PREFIX = (
    r"(?:i\W?(?:m|am)\s+going\s+to\s+|i\W?ll\s+|i\W?am\s+going\s+to\s+|i\W?m\s+about\s+to\s+|"
    r"let\s+me\s+|now\s+(?:i\s+|i\W?ll\s+|let\s+me\s+|i\W?m\s+going\s+to\s+)?|"
    r"first\s+(?:i\W?ll\s+|let\s+me\s+)?|"
    r"next\s+(?:i\W?ll\s+|i\W?m\s+going\s+to\s+|let\s+me\s+)?|"
    r"so\s+(?:i\W?ll\s+|let\s+me\s+|now\s+i\W?ll\s+)?|"
    r"then\s+(?:i\W?ll\s+|let\s+me\s+)?|"
    r"we\W?ll\s+|we\s+(?:are\s+going\s+to\s+|will\s+))?"
)

# Quoted target — "click 'Submit'", click "Submit"
_QUOTED_TARGET = r"(?:[\"“‘]([^\"”’]{1,80})[\"”’]|‘([^’]{1,80})’)"

# Label-like target — capitalised words, optional "the/a", up to 6 words
_LABEL_TARGET = (
    r"(?:the\s+|a\s+|an\s+)?"
    r"([A-Za-z][A-Za-z0-9_\-]*(?:\s+[A-Za-z][A-Za-z0-9_\-]*){0,5})"
    r"(?:\s+(?:button|link|field|menu|tab|option|dropdown|checkbox))?"
)


_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    # click_cta — strong patterns
    (
        "click_cta",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:click|press|tap|hit)\s+(?:on\s+)?{_QUOTED_TARGET}",
            re.IGNORECASE,
        ),
        0.95,
    ),
    (
        "click_cta",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:click|press|tap|hit)\s+(?:on\s+)?{_LABEL_TARGET}",
            re.IGNORECASE,
        ),
        # Confidence intentionally above submit_form (0.85) so that
        # "click Submit" is classified as a click on the Submit button
        # rather than a generic form-submit intent.  Same with "click Send".
        0.88,
    ),
    # enter_text
    (
        "enter_text",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:type|enter|fill\s+in|input|key\s+in|put)\s+"
            rf"(?:in\s+|into\s+)?{_QUOTED_TARGET}",
            re.IGNORECASE,
        ),
        0.90,
    ),
    (
        "enter_text",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:type|enter|fill\s+in|input)\s+"
            rf"(?:my\s+|the\s+|a\s+)?{_LABEL_TARGET}",
            re.IGNORECASE,
        ),
        0.75,
    ),
    # select_option
    (
        "select_option",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:select|choose|pick|set)\s+"
            rf"(?:the\s+|an?\s+)?{_QUOTED_TARGET}",
            re.IGNORECASE,
        ),
        0.90,
    ),
    (
        "select_option",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:select|choose|pick)\s+"
            rf"(?:the\s+|an?\s+)?{_LABEL_TARGET}",
            re.IGNORECASE,
        ),
        0.75,
    ),
    # submit_form — terminal action verbs
    (
        "submit_form",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:submit|send|finalize|finalise|"
            rf"complete\s+the\s+form|continue\s+to\s+the\s+next\s+step)\b",
            re.IGNORECASE,
        ),
        0.85,
    ),
    # navigate
    (
        "navigate",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:go\s+to|navigate\s+to|open\s+up|open|"
            rf"head\s+(?:to|over\s+to)|head\s+back\s+to)\s+"
            rf"(?:the\s+|a\s+|an\s+)?{_QUOTED_TARGET}",
            re.IGNORECASE,
        ),
        0.90,
    ),
    (
        "navigate",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:go\s+to|navigate\s+to|open|head\s+to)\s+"
            rf"(?:the\s+|a\s+|an\s+)?{_LABEL_TARGET}",
            re.IGNORECASE,
        ),
        0.78,
    ),
    # review — passive verification, useful as scene anchor for review states
    (
        "review",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:review|check|verify|confirm|make\s+sure|"
            rf"look\s+at|inspect|validate)\s+(?:the\s+|a\s+|an\s+|that\s+)?{_LABEL_TARGET}",
            re.IGNORECASE,
        ),
        0.65,
    ),
    # scroll
    (
        "scroll",
        re.compile(
            rf"\b{_FILLER_PREFIX}scroll\s+(?:down|up|further|back\s+up|to\s+the\s+top|"
            rf"to\s+the\s+bottom)?\b",
            re.IGNORECASE,
        ),
        0.65,
    ),
    # open_overlay / close_overlay
    # Strong overlay phrasing — explicit overlay-type noun anchors the
    # intent to overlay rather than navigation.  Boosted above the
    # navigate confidence so "open the Profile menu" classifies correctly.
    (
        "open_overlay",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:open|expand|reveal|pull\s+up)\s+"
            rf"(?:the\s+|a\s+|this\s+)?"
            r"([A-Za-z][\w\-]*(?:\s+[A-Za-z][\w\-]*){0,4})\s+"
            r"(?:menu|dropdown|dialog|popup|panel|sidebar|drawer|modal|overlay|"
            r"tooltip|context\s+menu)\b",
            re.IGNORECASE,
        ),
        0.85,
    ),
    (
        "close_overlay",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:close|dismiss|cancel|exit|hide)\s+"
            rf"(?:the\s+|a\s+|this\s+|that\s+)?"
            r"([A-Za-z][\w\-]*(?:\s+[A-Za-z][\w\-]*){0,4})\s+"
            r"(?:menu|dropdown|dialog|popup|panel|sidebar|drawer|modal|overlay)\b",
            re.IGNORECASE,
        ),
        0.85,
    ),
    # Generic open/close fallback — lower confidence so navigation patterns
    # win when the target type is ambiguous.
    (
        "open_overlay",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:open\s+up|expand|reveal|pull\s+up)\s+"
            rf"(?:the\s+|a\s+|this\s+)?{_LABEL_TARGET}",
            re.IGNORECASE,
        ),
        0.55,
    ),
    (
        "close_overlay",
        re.compile(
            rf"\b{_FILLER_PREFIX}(?:close|dismiss|cancel|exit|hide)\s+"
            rf"(?:the\s+|a\s+|this\s+|that\s+)?{_LABEL_TARGET}",
            re.IGNORECASE,
        ),
        0.55,
    ),
]


# Generic noise tokens that should never be returned as a target_phrase.
# The capturing groups occasionally pick up filler ("it", "this", "that")
# when no real target was named.
_TARGET_NOISE = frozenset({
    "", "it", "this", "that", "those", "these", "him", "her", "them",
    "now", "next", "first", "again", "here", "there", "okay", "ok",
})


def _clean_target(raw: str) -> str:
    """Strip common filler/noise from a captured target phrase."""
    if not raw:
        return ""
    text = re.sub(r"\s+", " ", raw).strip().strip(",.;:!?\"'")
    if text.lower() in _TARGET_NOISE:
        return ""
    # Drop trailing function-words that grammatically extend a noun phrase.
    text = re.sub(
        r"\s+(?:button|link|field|menu|tab|option|dropdown|checkbox)$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _extract_target(match: re.Match[str]) -> str:
    """Pull the target phrase from a regex match's named/positional groups."""
    for group_index in (1, 2, 3, 4):
        try:
            value = match.group(group_index)
        except (IndexError, error_type):
            value = None
        if value:
            cleaned = _clean_target(value)
            if cleaned:
                return cleaned
    return ""


# Python's re.error subclass — captured outside the function for typing.
error_type = re.error


# ─── Public API ──────────────────────────────────────────────────────────────

def extract_intent_events(
    segments: Iterable[Any],
    *,
    language: str = "en",
    max_events: Optional[int] = None,
) -> list[IntentEvent]:
    """Walk transcript segments and produce :class:`IntentEvent` records.

    ``segments`` is any iterable whose items expose the standard
    transcript-segment attributes the Ears engine emits:
    ``text``, ``start_time``, ``end_time``, ``confidence`` (optional),
    ``speaker`` (optional), ``segment_index`` (optional).  Both attribute
    access and dict-key access are supported so callers can pass either
    SQLAlchemy rows or pydantic models.

    ``language`` is reserved for future per-locale pattern sets.  Any
    value other than "en" returns an empty list today (caller can detect
    via ``len(events) == 0`` and fall back to text-only signals).

    ``max_events`` caps the result for very long sessions where every
    line of narration triggers a match.  Default ``None`` returns all.
    """
    if (language or "en").lower().strip() != "en":
        return []

    events: list[IntentEvent] = []

    for idx, seg in enumerate(segments):
        text = _attr(seg, "text") or ""
        if not text or len(text.strip()) < 4:
            continue
        start_s = float(_attr(seg, "start_time") or 0.0)
        end_s = float(_attr(seg, "end_time") or start_s)
        seg_conf = float(_attr(seg, "confidence") or 1.0)
        speaker = str(_attr(seg, "speaker") or "")
        seg_index = int(_attr(seg, "segment_index") or idx)

        matches_in_segment = _all_matches(text)
        if not matches_in_segment:
            continue

        segment_start_ms = max(0, int(round(start_s * 1000.0)))
        segment_duration_ms = max(0, int(round((end_s - start_s) * 1000.0)))
        seg_conf_clamped = max(0.0, min(1.0, seg_conf))
        words = _attr(seg, "words_json") or _attr(seg, "words") or []

        # Emit one IntentEvent per non-overlapping pattern hit. KT narration
        # is dense — a single 20-second segment commonly chains several
        # actions ("then clicking X, then selecting Y, then submitting Z").
        # Returning only the best match per segment collapses those into one
        # event anchored at the segment start, so downstream alignment to
        # frame transitions finds at most a single audio anchor regardless
        # of how many distinct narrated actions occurred.
        for intent_kind, target, base_conf, span in matches_in_segment:
            confidence = max(0.0, min(1.0, base_conf * seg_conf_clamped))

            # Phase F.3 — word-level alignment.  When the transcript
            # carries Whisper word timestamps, locate the verb and target
            # words inside the matched span and anchor the event at the
            # verb's spoken time. When no word timing is available, anchor
            # at the segment start (legacy behaviour).
            verb_word_ts_ms, target_word_ts_ms, anchor_ts_ms = _align_words_to_match(
                words=words,
                text=text,
                match_span=span,
                target_phrase=target,
                segment_start_ms=segment_start_ms,
            )

            events.append(IntentEvent(
                timestamp_ms=anchor_ts_ms,
                duration_ms=segment_duration_ms,
                intent_kind=intent_kind,
                target_phrase=target,
                raw_text=text.strip()[:500],
                confidence=round(confidence, 3),
                speaker=speaker,
                segment_index=seg_index,
                verb_word_ts_ms=verb_word_ts_ms,
                target_word_ts_ms=target_word_ts_ms,
                metadata={
                    "match_span": list(span),
                    "segment_start_ms": segment_start_ms,
                    "word_aligned": verb_word_ts_ms != segment_start_ms,
                },
            ))

            if max_events is not None and len(events) >= max_events:
                return events

    return events


def _align_words_to_match(
    *,
    words: Any,
    text: str,
    match_span: tuple[int, int],
    target_phrase: str,
    segment_start_ms: int,
) -> tuple[int, int, int]:
    """Locate the verb word and target word inside a Whisper word list.

    Returns ``(verb_word_ts_ms, target_word_ts_ms, anchor_ts_ms)``.

    ``words`` is the per-segment Whisper output, expected to be a list of
    objects shaped like ``{"word": "click", "start": 12.3, "end": 12.5}``
    (or analogous attribute access).  When the list is empty / malformed
    we fall back to the segment-level timestamp so callers always get a
    valid anchor.

    ``match_span`` is the (start_char, end_char) of the regex hit in
    ``text`` — its leading words are typically filler ("now I'll") and
    the verb sits near the start.  We pick:

      * verb_word: the FIRST verb-like word inside the span (rough
        heuristic: any word whose stem appears in the regex action verbs)
      * target_word: the FIRST word inside the span whose lower-cased
        form starts with the lower-cased ``target_phrase`` first token

    ``anchor_ts_ms`` is the verb_word_ts_ms when found, otherwise the
    segment_start_ms — keeps backwards compatibility for legacy callers.
    """
    if not words or not isinstance(words, (list, tuple)):
        return segment_start_ms, segment_start_ms, segment_start_ms

    span_start, span_end = match_span
    matched_text = text[span_start:span_end]
    if not matched_text.strip():
        return segment_start_ms, segment_start_ms, segment_start_ms

    target_first = (target_phrase.split()[0].lower() if target_phrase else "")

    # Reconstruct word offsets inside the original segment text by
    # walking the tokens in order — we accept any iterable shape.
    pos = 0
    verb_word_ts_ms: Optional[int] = None
    target_word_ts_ms: Optional[int] = None
    for word_entry in words:
        if isinstance(word_entry, dict):
            w = str(word_entry.get("word") or word_entry.get("text") or "")
            ws = word_entry.get("start") or word_entry.get("start_time") or 0
        else:
            w = str(getattr(word_entry, "word", None) or getattr(word_entry, "text", "") or "")
            ws = getattr(word_entry, "start", None) or getattr(word_entry, "start_time", None) or 0
        try:
            ws_ms = int(round(float(ws) * 1000.0))
        except (TypeError, ValueError):
            continue
        if not w:
            continue

        # Skip whitespace between tokens.  Whisper words usually carry
        # leading spaces ("' click'"); strip them for the search.
        token = w.strip()
        if not token:
            continue

        # Find this token's offset in the segment text starting from pos.
        idx = text.lower().find(token.lower(), pos)
        if idx < 0:
            continue
        pos = idx + len(token)

        # Inside the match span?
        if idx < span_start or idx >= span_end:
            continue

        # Verb candidate: token contains an action verb stem.
        if verb_word_ts_ms is None and _is_verb_word(token):
            verb_word_ts_ms = ws_ms
        # Target candidate: token matches the first word of target_phrase.
        if (
            target_word_ts_ms is None
            and target_first
            and token.lower().startswith(target_first)
        ):
            target_word_ts_ms = ws_ms

    verb_ts = verb_word_ts_ms if verb_word_ts_ms is not None else segment_start_ms
    target_ts = target_word_ts_ms if target_word_ts_ms is not None else verb_ts
    anchor_ts = verb_ts
    return verb_ts, target_ts, anchor_ts


_VERB_STEMS = (
    "click", "press", "tap", "hit", "type", "enter", "fill", "input",
    "select", "choose", "pick", "submit", "send", "navigate", "open",
    "close", "scroll", "review", "verify", "check", "confirm",
)


def _is_verb_word(token: str) -> bool:
    t = token.lower().strip(",.;:!?\"'`")
    if not t:
        return False
    return any(t == stem or t.startswith(stem) for stem in _VERB_STEMS)


def _attr(obj: Any, name: str) -> Any:
    """Read ``name`` from ``obj`` whether it is a dict, dataclass, ORM row, or pydantic model."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _best_match(text: str) -> Optional[tuple[str, str, float, tuple[int, int]]]:
    """Return ``(intent_kind, target, base_confidence, span)`` for the best
    pattern hit on a single segment, or ``None`` when nothing matched.

    "Best" is the highest-confidence pattern; on ties we keep the first
    pattern (which by construction is the most-specific one because of
    the ordering in ``_PATTERNS``).
    """
    best: Optional[tuple[str, str, float, tuple[int, int]]] = None
    for intent_kind, pattern, base_conf in _PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        target = _extract_target(match)
        candidate = (intent_kind, target, base_conf, match.span())
        if best is None or base_conf > best[2]:
            best = candidate
    return best


def _all_matches(
    text: str,
) -> list[tuple[str, str, float, tuple[int, int]]]:
    """Return every non-overlapping pattern hit in ``text``.

    Walks every pattern with ``finditer`` to collect all candidates, then
    resolves overlaps by keeping the highest-confidence hit when two
    patterns cover the same span. Returned list is sorted by span start so
    callers can emit events in narration order, which preserves the
    temporal relationship to frame transitions.

    Required for dense KT narration where a single Whisper segment can run
    20+ seconds and chain several actions ("then click X, then select Y").
    """
    candidates: list[tuple[str, str, float, tuple[int, int]]] = []
    for intent_kind, pattern, base_conf in _PATTERNS:
        for match in pattern.finditer(text):
            target = _extract_target(match)
            candidates.append((intent_kind, target, base_conf, match.span()))
    if not candidates:
        return []

    # Sort by start position; on tie, higher confidence first so the
    # overlap resolver below keeps it.
    candidates.sort(key=lambda c: (c[3][0], -c[2]))

    accepted: list[tuple[str, str, float, tuple[int, int]]] = []
    for cand in candidates:
        c_start, c_end = cand[3]
        # Drop if it overlaps with an already-accepted higher- or
        # equal-confidence hit. We compare against the most recent accepted
        # entries (typical narration has 1-5 actions per segment so a linear
        # scan is fine — the constant factor matters less than correctness).
        overlaps = False
        for kept in accepted:
            k_start, k_end = kept[3]
            if c_start < k_end and k_start < c_end:
                if kept[2] >= cand[2]:
                    overlaps = True
                    break
        if not overlaps:
            # Remove any previously-accepted hits this one supersedes.
            accepted = [
                k for k in accepted
                if not (c_start < k[3][1] and k[3][0] < c_end and cand[2] > k[2])
            ]
            accepted.append(cand)

    accepted.sort(key=lambda c: c[3][0])
    return accepted
