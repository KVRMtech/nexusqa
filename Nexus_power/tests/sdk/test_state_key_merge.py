"""Regression tests for the semantic state_key merge pass.

Covers the customer-visible defect from artifact bbfbf73d
(2026-05-09): nine consecutive scenes for one "Estimate review"
step rendered as nine flow nodes because the OCR-similarity merger
could not collapse them through cursor-induced LLaVA description
drift.  The fix uses scene_state_summary.state_key (already
persisted) as the dedup signal, with URL-prefix and sandwich
fallbacks for OCR-mangled frames.

Tests verify:
  1. Adjacent scenes with identical state_key + reliable quality merge.
  2. Mid-merge weak scene with no signal sandwiches into the run.
  3. OCR-truncated state_key (ends with ``_``) merges with the full key.
  4. Cleanly-terminated shorter state_key does NOT merge with a longer one
     (different sub-pages must stay distinct).
  5. Different roots never merge.
  6. Anti-merge guards (controls change, keyframe break, action evidence)
     block the merge even when state_keys agree.
  7. visit_count is stamped onto scene_state_summary when n > 1.
  8. Kill switch (``EYES_STATE_KEY_MERGE=false``) preserves input.
"""
from __future__ import annotations

import os
import pytest

from nexus_sdk.evidence.build_scenes import (
    _merge_by_state_key,
    _state_keys_compatible,
    _normalized_url_path,
)


ART = "art-state-key-test"


# ── helpers ──────────────────────────────────────────────────────────────────

def _scene(
    idx: int,
    state_key: str,
    quality: str = "strong",
    url: str = "",
    *,
    has_keyframe: bool = False,
    controls: list | None = None,
    entry_action: str = "",
    exit_action: str = "",
    ocr: str = "",
    step_label: str = "",
) -> dict:
    return {
        "scene_id": f"s-{idx}",
        "artifact_id": ART,
        "scene_index": idx,
        "detected_url": url,
        "ocr_text": ocr,
        "start_ms": idx * 1000,
        "end_ms": idx * 1000 + 800,
        "frame_ids": [f"f-{idx}"],
        "screen_name": step_label or f"Scene {idx}",
        "completeness_confidence": 0.85 if quality != "weak" else 0.5,
        "has_keyframe_boundary": has_keyframe,
        "visible_text_fingerprint": [],
        "detected_controls": controls or [],
        "entry_action": entry_action,
        "exit_action": exit_action,
        "scene_state_summary": {
            "state_key": state_key,
            "state_quality": quality,
            "step_label": step_label or f"Step {idx}",
        },
    }


# ── helper-function unit tests ───────────────────────────────────────────────

def test_state_keys_compatible_exact():
    assert _state_keys_compatible("a.b.c", "a.b.c") is True


def test_state_keys_compatible_truncation():
    """OCR-truncated key (ends with `_`) merges with full key."""
    assert _state_keys_compatible(
        "usaa.insurance.life_insurance_",
        "usaa.insurance.life_insurance_quote.estimate",
    ) is True


def test_state_keys_compatible_clean_shorter_does_not_merge():
    """A cleanly-terminated shorter key is a *different* sub-page."""
    # 'life' (landing) is NOT a noisy reading of '...life_insurance_quote...birthdate'
    assert _state_keys_compatible(
        "usaa.insurance.life",
        "usaa.insurance.life_insurance_quote.personal_information.birthdate",
    ) is False


def test_state_keys_compatible_clean_quote_does_not_merge():
    """OCR captured '/quote' cleanly — could be the parent page, not the child."""
    assert _state_keys_compatible(
        "usaa.insurance.life_insurance_quote",
        "usaa.insurance.life_insurance_quote.estimate",
    ) is False


def test_state_keys_compatible_different_roots():
    assert _state_keys_compatible("usaa.checking", "usaa.insurance") is False


def test_state_keys_compatible_empty_inputs():
    assert _state_keys_compatible("", "anything") is False
    assert _state_keys_compatible("anything", "") is False


def test_normalized_url_path_strips_trailing_junk():
    """OCR-induced trailing punctuation collapses to a comparable prefix."""
    assert _normalized_url_path(
        "https://usaa.com/insurance/life-insurance-quote-="
    ) == "usaa.com/insurance/life-insurance-quote"
    assert _normalized_url_path(
        "https://usaa.com/insurance/life-insurance-_"
    ) == "usaa.com/insurance/life-insurance"


def test_normalized_url_path_handles_empty_and_garbage():
    assert _normalized_url_path("") == ""
    # Anything without a scheme is treated as a host; trailing slash collapsed.
    assert _normalized_url_path("not-a-url") == "not-a-url"


# ── merge behaviour tests ────────────────────────────────────────────────────

def test_adjacent_same_state_key_merges():
    """Three consecutive scenes with identical state_key → 1 scene, visit_count=3."""
    scenes = [
        _scene(0, "usaa.estimate", "strong", "https://usaa.com/estimate", step_label="Estimate"),
        _scene(1, "usaa.estimate", "strong", "https://usaa.com/estimate", step_label="Estimate"),
        _scene(2, "usaa.estimate", "degraded", "https://usaa.com/estimate", step_label="Estimate"),
    ]
    merged = _merge_by_state_key(scenes, ART)
    assert len(merged) == 1
    assert merged[0]["scene_state_summary"]["visit_count"] == 3


def test_distinct_state_keys_stay_separate():
    """Different steps are never merged."""
    scenes = [
        _scene(0, "usaa.birthdate", "strong", "https://usaa.com/birthdate"),
        _scene(1, "usaa.estimate",  "strong", "https://usaa.com/estimate"),
    ]
    merged = _merge_by_state_key(scenes, ART)
    assert len(merged) == 2


def test_weak_scene_sandwiches_into_strong_run():
    """OCR drop-out frame between two reliable scenes is folded in."""
    scenes = [
        _scene(0, "usaa.estimate", "strong", "https://usaa.com/estimate"),
        _scene(1, "",              "weak",   ""),  # transient OCR failure
        _scene(2, "usaa.estimate", "strong", "https://usaa.com/estimate"),
    ]
    merged = _merge_by_state_key(scenes, ART)
    assert len(merged) == 1
    assert merged[0]["scene_state_summary"]["visit_count"] == 3


def test_truncated_state_key_merges_with_full():
    """OCR-truncated state_key still merges with a reliable full reading."""
    scenes = [
        _scene(0, "usaa.insurance.life_insurance_quote.estimate", "strong",
               "https://usaa.com/insurance/life-insurance-quote-public/estimate"),
        _scene(1, "usaa.insurance.life_insurance_",                "strong",
               "https://usaa.com/insurance/life-insurance-_"),
    ]
    merged = _merge_by_state_key(scenes, ART)
    assert len(merged) == 1


def test_url_only_match_when_state_keys_drift():
    """When state_keys diverge but normalized URL matches, still merge."""
    scenes = [
        _scene(0, "usaa.insurance.life_insurance_quote.estimate", "strong",
               "https://usaa.com/insurance/estimate"),
        _scene(1, "garbled.ocr.key.different",                    "degraded",
               "https://usaa.com/insurance/estimate"),
    ]
    merged = _merge_by_state_key(scenes, ART)
    assert len(merged) == 1


def test_keyframe_break_blocks_merge():
    """A hard keyframe boundary stops the merge regardless of state_key match."""
    scenes = [
        _scene(0, "usaa.estimate", "strong", "https://usaa.com/estimate"),
        _scene(1, "usaa.estimate", "strong", "https://usaa.com/estimate", has_keyframe=True),
    ]
    merged = _merge_by_state_key(scenes, ART)
    assert len(merged) == 2


def test_controls_change_blocks_merge():
    """Different control sets indicate a real in-page state transition."""
    scenes = [
        _scene(0, "usaa.estimate", "strong", "https://usaa.com/estimate", controls=["c1"]),
        _scene(1, "usaa.estimate", "strong", "https://usaa.com/estimate", controls=["c1", "c2"]),
    ]
    merged = _merge_by_state_key(scenes, ART)
    assert len(merged) == 2


def test_action_evidence_blocks_merge():
    """Explicit entry/exit action between two scenes preserves the boundary."""
    scenes = [
        _scene(0, "usaa.estimate", "strong", "https://usaa.com/estimate", exit_action="click_submit"),
        _scene(1, "usaa.estimate", "strong", "https://usaa.com/estimate"),
    ]
    merged = _merge_by_state_key(scenes, ART)
    assert len(merged) == 2


def test_kill_switch_preserves_input(monkeypatch: pytest.MonkeyPatch):
    """``EYES_STATE_KEY_MERGE=false`` returns input untouched."""
    monkeypatch.setenv("EYES_STATE_KEY_MERGE", "false")
    scenes = [
        _scene(0, "usaa.estimate", "strong", "https://usaa.com/estimate"),
        _scene(1, "usaa.estimate", "strong", "https://usaa.com/estimate"),
    ]
    merged = _merge_by_state_key(scenes, ART)
    assert len(merged) == 2


def test_two_weak_scenes_in_a_row_do_not_merge():
    """Without a reliable anchor neither side counts as a merge candidate."""
    scenes = [
        _scene(0, "", "weak", ""),
        _scene(1, "", "weak", ""),
    ]
    merged = _merge_by_state_key(scenes, ART)
    assert len(merged) == 2


def test_merge_preserves_chronological_index_assignment():
    """After merging, scene_index is contiguous from 0 and scene_id is regenerated."""
    scenes = [
        _scene(0, "usaa.estimate", "strong", "https://usaa.com/estimate"),
        _scene(1, "usaa.estimate", "strong", "https://usaa.com/estimate"),
        _scene(2, "usaa.birthdate", "strong", "https://usaa.com/birthdate"),
    ]
    merged = _merge_by_state_key(scenes, ART)
    assert len(merged) == 2
    assert [s["scene_index"] for s in merged] == [0, 1]
    assert all(s["scene_id"] for s in merged)


def test_real_world_artifact_pattern():
    """Replays the bbfbf73d failure: form-fills then 9 estimate reviews."""
    base = "usaa.insurance.life_insurance_quote"
    seq = [
        ("usaa.insurance.life",       "https://usaa.com/insurance/life"),                 # landing
        (f"{base}.personal_information.birthdate",        "https://usaa.com/insurance/life/birthdate"),
        (f"{base}.personal_information.nicotine_tobacco", "https://usaa.com/insurance/life/nicotine"),
        (f"{base}.personal_information.nicotine_tobacco", "https://usaa.com/insurance/life/nicotine"),
        (f"{base}.personal_information.nicotine_tobacco", "https://usaa.com/insurance/life/nicotine"),
        (f"{base}.personal_information.military_status",  "https://usaa.com/insurance/life/military"),
        # 9 estimate frames in a row with various OCR-induced state_key drift
        (f"{base}.estimate",            "https://usaa.com/insurance/life/estimate"),
        (f"{base}.estimate",            "https://usaa.com/insurance/life/estimate"),
        (f"{base}.estimate",            "https://usaa.com/insurance/life/estimate"),
        ("",                            ""),                                              # transient OCR failure
        (f"{base}.estimate",            "https://usaa.com/insurance/life/estimate"),
        (f"{base}.estimate",            "https://usaa.com/insurance/life/estimate"),
        (f"{base}.estimate",            "https://usaa.com/insurance/life/estimate"),
        (f"{base}.estimate",            "https://usaa.com/insurance/life/estimate"),
        (f"{base}.estimate",            "https://usaa.com/insurance/life/estimate"),
    ]
    scenes = []
    for i, (key, url) in enumerate(seq):
        quality = "weak" if not key else "strong"
        scenes.append(_scene(i, key, quality, url))

    merged = _merge_by_state_key(scenes, ART)
    # Expected: 1 landing + 1 birthdate + 1 nicotine (×3 visits) + 1 military + 1 estimate (×9 visits)
    assert len(merged) == 5

    visits = [s["scene_state_summary"].get("visit_count", 1) for s in merged]
    # Estimate should be the largest run
    assert max(visits) == 9
    # Nicotine merged 3 visits
    assert sorted(visits) == [1, 1, 1, 3, 9]
