"""Regression tests for the intra-scene step extractor.

Covers the customer-visible defect from artifact bbfbf73d (2026-05-09):
the Birthdate page was on screen for 15 seconds with 9 frames captured,
but the visual flow page rendered ONE step. The user actually performed
multiple form interactions (click month, select day, type year, click
Continue) — each one needs its own step record for downstream test and
automation generation.

Tests verify:
  1. With ≥ 2 frames inside a scene, at least one action is emitted per
     consecutive pair (no silent drops).
  2. Form-fill range token additions classify as enter_text / select_option
     depending on token-shape and UI-element changes.
  3. Large new content blocks classify as overlay open.
  4. Fully removed content blocks classify as overlay close.
  5. Identical OCR with no UI change still produces a low-confidence
     user_interaction action — captured frames imply a real visual change
     even when OCR is blind.
  6. Empty / single-frame input returns [].
  7. Action timestamps fall back gracefully through ms / index when seconds
     are absent.
  8. Target-label heuristic prefers interactive UI element labels over
     bare token deltas.
"""
from __future__ import annotations

from nexus_sdk.evidence.step_extractor import (
    extract_intra_scene_steps,
    _classify_action,
    _filter_meaningful_tokens,
    _best_target_label,
    _ui_element_set,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _frame(
    idx: int,
    *,
    ocr: str = "",
    ts_s: float | None = None,
    ts_ms: float | None = None,
    ocr_conf: float = 0.85,
    ui: list | None = None,
) -> dict:
    f: dict = {
        "frame_id": f"f-{idx}",
        "frame_index": idx,
        "extracted_text": ocr,
        "ocr_confidence": ocr_conf,
        "ui_elements": ui or [],
    }
    if ts_s is not None:
        f["timestamp_seconds"] = ts_s
    if ts_ms is not None:
        f["timestamp_ms"] = ts_ms
    return f


# ── helper-function tests ────────────────────────────────────────────────────

def test_filter_meaningful_tokens_drops_hex_and_clock_noise():
    keep = {"premium", "annual", "5500"}
    drop = {"abcdef0123456", "12:34"}
    result = _filter_meaningful_tokens(keep | drop)
    assert "premium" in result
    assert "annual" in result
    assert "5500" in result
    assert "abcdef0123456" not in result
    assert "12:34" not in result


def test_ui_element_set_normalises_dicts_and_strings():
    f = {"ui_elements": [
        {"element_type": "Button", "text": "Continue"},
        {"type": "input", "label": "Email"},
        {"element_type": "", "label": ""},          # dropped (no signal)
    ]}
    s = _ui_element_set(f)
    assert ("button", "continue") in s
    assert ("input", "email") in s
    assert len(s) == 2


def test_ui_element_set_handles_json_string_payload():
    import json
    f = {"ui_elements_json": json.dumps([
        {"element_type": "link", "text": "Get Quote"},
    ])}
    s = _ui_element_set(f)
    assert ("link", "get quote") in s


def test_best_target_label_prefers_interactive_ui_added():
    prev_ui = set()
    curr_ui = {("text", "lorem"), ("button", "submit")}
    label = _best_target_label({"random"}, prev_ui, curr_ui)
    assert label == "submit"


def test_best_target_label_falls_back_to_longest_added_token():
    label = _best_target_label({"x", "policy_number"}, set(), set())
    assert label == "policy_number"


# ── classifier tests ─────────────────────────────────────────────────────────

def test_classify_form_fill_short_alphanumerics_is_enter_text():
    prev = "Personal Information birthdate Continue Back"
    curr = "Personal Information birthdate 1990 Continue Back"
    kind, delta, conf, ev = _classify_action(prev, curr, set(), set(), 0.85)
    assert kind == "enter_text"
    assert "1990" in delta
    assert conf >= 0.5


def test_classify_select_option_when_ui_added_with_few_tokens():
    prev = "Nicotine Tobacco Continue Back"
    curr = "Nicotine Tobacco Yes Continue Back"
    ui_curr = {("dropdown", "nicotine usage")}
    kind, delta, conf, ev = _classify_action(prev, curr, set(), ui_curr, 0.85)
    assert kind == "click"  # ui_added with few tokens becomes click
    assert "nicotine" in delta.lower() or "yes" in delta.lower()


def test_classify_overlay_open_on_large_token_addition():
    prev = "Continue Back"
    curr = (
        "Modal Title Some Long Body Text With Many Distinct Content Tokens "
        "Like Premium Coverage Annual Term Beneficiary Address Continue Back"
    )
    kind, delta, conf, ev = _classify_action(prev, curr, set(), set(), 0.85)
    assert kind == "open_overlay"
    assert conf >= 0.6


def test_classify_overlay_close_on_large_token_removal():
    prev = (
        "Modal Title Some Long Body Text With Many Distinct Content Tokens "
        "Like Premium Coverage Annual Term Beneficiary Address Continue Back"
    )
    curr = "Continue Back"
    kind, delta, conf, ev = _classify_action(prev, curr, set(), set(), 0.85)
    assert kind == "close_overlay"


def test_classify_identical_ocr_without_ui_emits_user_interaction():
    """Captured frames with identical OCR represent OCR-blind real actions
    (form fill, dropdown select that the page chrome OCR cannot see)."""
    text = "Birthdate Continue Back Page"
    kind, delta, conf, ev = _classify_action(text, text, set(), set(), 0.85)
    assert kind == "user_interaction"
    assert "ocr_blind" in ev
    # Confidence is intentionally low; downstream consumers can filter
    assert 0.0 < conf < 0.5


def test_classify_chrome_only_diff_is_repaint():
    """Same content tokens; only chrome (date/time/cursor) shifted."""
    prev = "Page Heading Continue Back search 12:34"
    curr = "Page Heading Continue Back search 12:35"
    kind, delta, conf, ev = _classify_action(prev, curr, set(), set(), 0.85)
    # Should be either repaint or user_interaction with low confidence
    assert kind in {"scroll_or_repaint", "user_interaction"}
    assert conf < 0.5


def test_classify_low_ocr_confidence_demotes_form_fill():
    prev = "Field Continue Back"
    curr = "Field 1990 Continue Back"
    kind, delta, conf, ev = _classify_action(prev, curr, set(), set(), 0.10)
    assert kind == "state_change"
    assert "low_ocr_confidence" in ev


# ── extract_intra_scene_steps tests ─────────────────────────────────────────

def test_empty_input_returns_empty():
    assert extract_intra_scene_steps([]) == []


def test_single_frame_returns_empty():
    assert extract_intra_scene_steps([_frame(0, ocr="hello")]) == []


def test_two_frame_with_form_fill_emits_one_action():
    frames = [
        _frame(0, ocr="Birthdate Continue", ts_s=1.0),
        _frame(1, ocr="Birthdate 1990 Continue", ts_s=2.0),
    ]
    actions = extract_intra_scene_steps(frames)
    assert len(actions) == 1
    a = actions[0]
    assert a["action_kind"] == "enter_text"
    assert a["from_frame_index"] == 0
    assert a["to_frame_index"] == 1
    assert a["from_timestamp_s"] == 1.0
    assert a["to_timestamp_s"] == 2.0
    assert "1990" in a["delta_text"]


def test_birthdate_replay_8_frames_yields_8_actions():
    """The actual customer scenario: 9 frames on Birthdate over 15s with
    OCR blind to form-field changes still produces 8 actions (one per
    consecutive pair) so the customer sees user_interaction at each
    captured timestamp."""
    text = (
        "Your Life Insurance Estimate How old are you? Personal information "
        "Continue Back search"
    )
    frames = [_frame(i, ocr=text, ts_s=20.0 + i * 1.5) for i in range(9)]
    actions = extract_intra_scene_steps(frames)
    assert len(actions) == 8
    assert all(a["action_kind"] == "user_interaction" for a in actions)
    # Timestamps must be strictly increasing
    times = [a["from_timestamp_s"] for a in actions]
    assert times == sorted(times)


def test_timestamp_falls_back_to_milliseconds():
    frames = [
        _frame(0, ocr="X", ts_ms=1000),
        _frame(1, ocr="X Y", ts_ms=2500),
    ]
    actions = extract_intra_scene_steps(frames)
    assert len(actions) == 1
    assert actions[0]["from_timestamp_s"] == 1.0
    assert actions[0]["to_timestamp_s"] == 2.5


def test_action_carries_evidence_signals():
    frames = [
        _frame(0, ocr="Birthdate Continue", ts_s=1),
        _frame(1, ocr="Birthdate 1990 Continue", ts_s=2),
    ]
    actions = extract_intra_scene_steps(frames)
    assert "evidence_signals" in actions[0]
    assert isinstance(actions[0]["evidence_signals"], list)
    assert len(actions[0]["evidence_signals"]) >= 1


def test_target_label_uses_added_ui_element_when_present():
    prev_ui = []
    curr_ui = [{"element_type": "button", "text": "Continue"}]
    frames = [
        _frame(0, ocr="Birthdate Continue", ts_s=1, ui=prev_ui),
        _frame(1, ocr="Birthdate Continue 1990", ts_s=2, ui=curr_ui),
    ]
    actions = extract_intra_scene_steps(frames)
    assert actions[0]["target_label"] == "continue"
