"""Tests for the audio intent extractor.

The extractor turns SME narration like "now I'll click submit" into
timestamped :class:`IntentEvent` records that downstream stages can use
to anchor scene boundaries and seed the triangulated action classifier.

Tests cover:
  * Each canonical intent_kind has working pattern coverage
  * Target phrase extraction from quoted and unquoted forms
  * Confidence is the product of pattern strength × segment confidence
  * Filler / pronoun targets are dropped (never returned as target_phrase)
  * Non-English ``language`` argument returns no events (until per-locale
    patterns are added)
  * Both dict-shaped and dataclass-shaped segments work
  * ``max_events`` caps the result
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from nexus_sdk.audio import (
    INTENT_KINDS,
    IntentEvent,
    extract_intent_events,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────

@dataclass
class _FakeSegment:
    text: str
    start_time: float
    end_time: float
    confidence: float = 1.0
    speaker: str = "SME"
    segment_index: int = 0


def _seg(text: str, *, start: float = 1.0, end: float = 3.5,
         conf: float = 1.0, idx: int = 0) -> _FakeSegment:
    return _FakeSegment(
        text=text, start_time=start, end_time=end,
        confidence=conf, speaker="SME", segment_index=idx,
    )


# ─── Coverage tests for each intent_kind ─────────────────────────────────────

def test_click_cta_quoted_target():
    events = extract_intent_events([_seg("Now I'll click \"Submit\".")])
    assert len(events) == 1
    e = events[0]
    assert e.intent_kind == "click_cta"
    assert e.target_phrase == "Submit"
    assert e.confidence > 0.9


def test_click_cta_unquoted_target():
    events = extract_intent_events([_seg("Let me click Continue here.")])
    assert len(events) == 1
    e = events[0]
    assert e.intent_kind == "click_cta"
    assert "Continue" in e.target_phrase


def test_click_cta_press_synonym():
    events = extract_intent_events([_seg("I'm going to press the Save button.")])
    assert len(events) == 1
    assert events[0].intent_kind == "click_cta"
    assert events[0].target_phrase.lower().startswith("save")


def test_enter_text():
    events = extract_intent_events([_seg("Now I'll type my date of birth.")])
    assert len(events) == 1
    assert events[0].intent_kind == "enter_text"


def test_enter_text_field_quoted():
    events = extract_intent_events([_seg('Let me enter "1990" in the year field.')])
    assert events[0].intent_kind == "enter_text"
    assert events[0].target_phrase == "1990"


def test_select_option():
    events = extract_intent_events([_seg("I'll select No for nicotine usage.")])
    assert len(events) == 1
    assert events[0].intent_kind == "select_option"
    # target_phrase should mention No (the option) not "for" (the preposition)
    assert events[0].target_phrase.lower().startswith("no")


def test_submit_form_phrase():
    events = extract_intent_events([_seg("Then I'll submit the form.")])
    assert events[0].intent_kind == "submit_form"


def test_submit_send_synonym():
    events = extract_intent_events([_seg("Now I'll send it for processing.")])
    assert events[0].intent_kind == "submit_form"


def test_navigate_url():
    events = extract_intent_events([_seg("Let me navigate to the Insurance Quote page.")])
    assert events[0].intent_kind == "navigate"
    assert "insurance" in events[0].target_phrase.lower()


def test_review_intent():
    events = extract_intent_events([_seg("Let me verify the estimate totals shown.")])
    assert events[0].intent_kind == "review"


def test_scroll_intent():
    events = extract_intent_events([_seg("I'll scroll down to see more.")])
    assert events[0].intent_kind == "scroll"


# ─── Negative cases — must NOT extract intent ────────────────────────────────

def test_descriptive_sentence_no_intent():
    """Pure description of UI without an action verb produces no event."""
    events = extract_intent_events([_seg("This page is the policy summary screen.")])
    assert events == []


def test_pronoun_target_dropped():
    """When the regex matches but only captures a pronoun, target is empty."""
    events = extract_intent_events([_seg("Now I'll click it.")])
    if events:
        assert events[0].target_phrase == ""


def test_too_short_segment_skipped():
    events = extract_intent_events([_seg("ok")])
    assert events == []


def test_blank_text_skipped():
    events = extract_intent_events([_seg("")])
    assert events == []


# ─── Timing + confidence tests ───────────────────────────────────────────────

def test_timestamp_in_milliseconds():
    events = extract_intent_events([_seg("I'll click Submit.", start=2.5, end=4.0)])
    assert events[0].timestamp_ms == 2500
    assert events[0].duration_ms == 1500
    assert events[0].end_ms() == 4000


def test_confidence_combines_pattern_and_segment():
    """Pattern base 0.95 × segment 0.5 = 0.475."""
    events = extract_intent_events([_seg("I'll click \"Submit\".", conf=0.5)])
    assert events[0].confidence == pytest.approx(0.475, abs=0.001)


def test_low_segment_confidence_lowers_event_confidence():
    events_high = extract_intent_events([_seg("I'll click Submit.", conf=1.0)])
    events_low = extract_intent_events([_seg("I'll click Submit.", conf=0.3)])
    assert events_low[0].confidence < events_high[0].confidence


# ─── Multiple segments & ordering ────────────────────────────────────────────

def test_multiple_segments_preserve_order_and_indexes():
    segs = [
        _seg("First I'll click Continue.", start=1.0, end=2.0, idx=0),
        _seg("Now I'll enter the policy number.", start=2.5, end=4.0, idx=1),
        _seg("Then I'll submit the form.", start=5.0, end=6.0, idx=2),
    ]
    events = extract_intent_events(segs)
    assert [e.intent_kind for e in events] == ["click_cta", "enter_text", "submit_form"]
    assert [e.segment_index for e in events] == [0, 1, 2]
    assert [e.timestamp_ms for e in events] == [1000, 2500, 5000]


def test_max_events_caps_result():
    segs = [_seg(f"Now I'll click button {n}.", idx=n) for n in range(10)]
    events = extract_intent_events(segs, max_events=3)
    assert len(events) == 3


def test_dict_segments_supported():
    """Callers passing plain dicts (e.g. SQL row dicts) work without conversion."""
    events = extract_intent_events([
        {"text": "Now I'll click Submit.", "start_time": 1.0, "end_time": 2.0, "confidence": 0.9},
    ])
    assert len(events) == 1
    assert events[0].intent_kind == "click_cta"


# ─── Language stub ───────────────────────────────────────────────────────────

def test_non_english_language_returns_empty():
    events = extract_intent_events(
        [_seg("Now I'll click Submit.")],
        language="es",
    )
    assert events == []


# ─── Sanity: every INTENT_KINDS entry can be triggered ───────────────────────

@pytest.mark.parametrize("phrase,expected", [
    ("Let me click Submit.", "click_cta"),
    ("I'll type my name in the field.", "enter_text"),
    ("Now I'll select Yes for the nicotine option.", "select_option"),
    ("Then I'll submit the form.", "submit_form"),
    ("Let me navigate to the Estimate page.", "navigate"),
    ("Let me verify the totals shown.", "review"),
    ("I'll scroll down here.", "scroll"),
    ("Now I'll open the Profile menu.", "open_overlay"),
    ("Let me close that dialog.", "close_overlay"),
])
def test_intent_kind_canonical_coverage(phrase, expected):
    events = extract_intent_events([_seg(phrase)])
    assert events
    assert events[0].intent_kind == expected
    assert expected in INTENT_KINDS


# ─── Phase F.3 — Word-level audio alignment ──────────────────────────────────

def _seg_with_words(text: str, *, start: float, end: float, words: list) -> dict:
    """Whisper-style segment with word_timestamps populated."""
    return {
        "text": text,
        "start_time": start,
        "end_time": end,
        "confidence": 1.0,
        "speaker": "SME",
        "segment_index": 0,
        "words_json": words,
    }


def test_word_level_alignment_anchors_to_verb_word():
    """When word timestamps are present, anchor at the verb's exact spoken-time
    rather than the segment start."""
    seg = _seg_with_words(
        "Now I'll click Submit",
        start=10.0,
        end=12.0,
        words=[
            {"word": "Now", "start": 10.05, "end": 10.20},
            {"word": "I'll", "start": 10.25, "end": 10.40},
            {"word": "click", "start": 10.55, "end": 10.80},
            {"word": "Submit", "start": 10.85, "end": 11.30},
        ],
    )
    events = extract_intent_events([seg])
    assert len(events) == 1
    e = events[0]
    # Anchor moved from segment-start (10000ms) to verb's spoken-time (10550ms).
    assert e.timestamp_ms == 10550
    assert e.verb_word_ts_ms == 10550
    assert e.target_word_ts_ms == 10850


def test_word_alignment_falls_back_to_segment_when_no_words():
    seg = _seg_with_words(
        "Now I'll click Submit",
        start=5.0,
        end=7.0,
        words=[],  # empty word list → fall back
    )
    events = extract_intent_events([seg])
    assert len(events) == 1
    assert events[0].timestamp_ms == 5000
    assert events[0].verb_word_ts_ms == 5000
    assert events[0].target_word_ts_ms == 5000


def test_word_alignment_handles_words_in_attribute_form():
    """Whisper segments can also expose ``words`` (not ``words_json``)."""
    class _Seg:
        text = "Let me click Save"
        start_time = 2.0
        end_time = 4.0
        confidence = 1.0
        speaker = "SME"
        segment_index = 0
        words = [
            {"word": "Let",   "start": 2.10, "end": 2.20},
            {"word": "me",    "start": 2.25, "end": 2.32},
            {"word": "click", "start": 2.40, "end": 2.65},
            {"word": "Save",  "start": 2.70, "end": 3.00},
        ]
    events = extract_intent_events([_Seg()])
    assert len(events) == 1
    assert events[0].verb_word_ts_ms == 2400
    assert events[0].target_word_ts_ms == 2700


def test_word_alignment_skips_filler_picks_first_verb():
    """When multiple action verbs appear, the first one in the matched
    span wins."""
    seg = _seg_with_words(
        "Let me click the Submit button",
        start=0.0,
        end=2.0,
        words=[
            {"word": "Let",    "start": 0.00, "end": 0.10},
            {"word": "me",     "start": 0.15, "end": 0.20},
            {"word": "click",  "start": 0.25, "end": 0.45},
            {"word": "the",    "start": 0.50, "end": 0.60},
            {"word": "Submit", "start": 0.65, "end": 0.95},
            {"word": "button", "start": 1.00, "end": 1.30},
        ],
    )
    events = extract_intent_events([seg])
    assert events
    assert events[0].verb_word_ts_ms == 250  # "click"
    assert events[0].target_word_ts_ms == 650  # "Submit"


def test_word_alignment_metadata_records_alignment_status():
    seg = _seg_with_words(
        "I'll click Save",
        start=1.0,
        end=2.0,
        words=[
            {"word": "I'll",  "start": 1.00, "end": 1.10},
            {"word": "click", "start": 1.20, "end": 1.40},
            {"word": "Save",  "start": 1.50, "end": 1.80},
        ],
    )
    events = extract_intent_events([seg])
    assert events[0].metadata.get("word_aligned") is True
    # No-words segment carries word_aligned=False.
    seg2 = _seg("I'll click Save", start=5.0, end=7.0)
    events2 = extract_intent_events([seg2])
    assert events2[0].metadata.get("word_aligned") is False
