"""
Wave-A regression tests for Phase 2 (``EYES_PHASE2_GUARDS``).

Covers the three customer-visible defects Wave A is meant to fix permanently
plus a parity check confirming flag-OFF behaviour remains byte-identical to
the Phase-1 release.

A1  — every scene carries the new guard signals (has_keyframe_boundary,
      visible_text_fingerprint, plus empty placeholder slots).
A2  — flag OFF: form-fill scenes on same URL stay distinct (Phase-1 promise).
A2  — flag ON:  scrolling micro-shift on same URL merges (de-fragmentation).
A2  — flag ON:  form-fill scenes on same URL still stay distinct (struct fp).
A2  — flag ON:  hard keyframe between scenes blocks the merge.
A3  — flag OFF: unknown element_type emits "interact" (legacy parity).
A3  — flag ON:  unknown element_type is dropped silently.
A3  — flag ON:  canonical kinds map cleanly through the verb dictionary.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import pytest

from nexus_sdk.evidence import build_scenes as bs
from nexus_sdk.evidence.build_scenes import (
    build_scenes,
    _hard_keyframe_indices,
    _structural_fingerprint,
    _can_merge_phase2,
    _phase2_guards_enabled,
)
from nexus_sdk.evidence.control_extractor import (
    ControlExtractor,
    CANONICAL_ACTION_KINDS,
    _STRUCTURAL_KIND_MAP,
)


ART = "art-phase2-guards"
SESS = "sess-phase2-guards"
TEN = "tenant-phase2"


# ── flag helper ──────────────────────────────────────────────────────────────

@pytest.fixture
def phase2_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("EYES_PHASE2_GUARDS", "true")
    assert _phase2_guards_enabled() is True
    yield
    # monkeypatch undoes on teardown


@pytest.fixture
def phase2_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("EYES_PHASE2_GUARDS", "false")
    assert _phase2_guards_enabled() is False
    yield


# ── frame helpers ────────────────────────────────────────────────────────────

def _frame(idx: int, dhash_int: int, ocr: str = "", page_title: str = "") -> dict:
    """Build a frame dict that build_scenes() understands.  ``dhash_int`` is
    a 64-bit perceptual hash; we hex-encode it the same way the eyes engine
    does so the parser sees a real value."""
    return {
        "frame_id": str(uuid.uuid4()),
        "frame_index": idx,
        "timestamp_ms": idx * 1000,
        "dhash": f"{dhash_int:016x}",
        "extracted_text": ocr,
        "ocr_confidence": 0.9,
        "description": "",
        "page_title": page_title,
        "ui_elements": [],
    }


# ── A1 — scene guard signal population ───────────────────────────────────────


def test_a1_scenes_carry_phase2_guard_fields():
    """Every scene must have the three new guard fields, even when the flag
    is off.  The fields are additive metadata; consumers gate on the flag."""
    frames = [
        _frame(0, 0xAAAA_AAAA_AAAA_AAAA, "Home page Welcome"),
        _frame(1, 0xAAAA_AAAA_AAAA_AAAB, "Home page Welcome"),  # near-identical
    ]
    scenes = build_scenes(frames, ART, SESS, TEN)
    assert len(scenes) >= 1
    for s in scenes:
        assert "has_keyframe_boundary" in s
        assert "visible_text_fingerprint" in s
        assert "detected_controls" in s
        assert "entry_action" in s
        assert "exit_action" in s
        assert isinstance(s["visible_text_fingerprint"], list)
        assert s["detected_controls"] == []
        assert s["entry_action"] == ""
        assert s["exit_action"] == ""


def test_a1_hard_keyframe_indices_detects_large_jumps():
    """A hash jump > threshold * 1.5 marks the next frame as a hard keyframe."""
    # threshold=0.10 → hard threshold = 0.15
    # 0xAAAA…0000 vs 0x5555…FFFF flips ~all 64 bits → distance ~1.0
    frames = [
        _frame(0, 0xAAAA_AAAA_0000_0000),
        _frame(1, 0xAAAA_AAAA_0000_0001),  # tiny diff
        _frame(2, 0x5555_5555_FFFF_FFFE),  # huge diff
    ]
    hard = _hard_keyframe_indices(frames, threshold=0.10)
    assert 2 in hard
    assert 1 not in hard
    assert 0 not in hard


def test_a1_structural_fingerprint_strips_chrome():
    """Chrome words drop out; content tokens remain."""
    fp = _structural_fingerprint(
        "Home Search Settings Annual Premium 5000 Submit Cancel"
    )
    assert "premium" in fp
    assert "annual" in fp
    assert "5000" in fp
    assert "home" not in fp        # chrome
    assert "search" not in fp      # chrome
    assert "settings" not in fp    # chrome
    assert "submit" not in fp      # chrome
    assert "cancel" not in fp      # chrome


def test_a1_structural_fingerprint_distinguishes_form_fills():
    """Two form-fill states with identical chrome but different values have
    distinct structural fingerprints — this is the central Phase-2 promise."""
    fp_a = _structural_fingerprint(
        "Home Settings Search Annual Premium 5000 Coverage 100000 Submit"
    )
    fp_b = _structural_fingerprint(
        "Home Settings Search Annual Premium 5500 Coverage 150000 Submit"
    )
    # Content tokens differ on the values
    assert fp_a != fp_b
    # Jaccard between content tokens should be < 0.9 (the Phase-2 floor)
    inter = len(fp_a & fp_b)
    union = len(fp_a | fp_b)
    jaccard = inter / union if union else 0.0
    assert jaccard < 0.9, (
        f"Jaccard {jaccard:.2f} too high — form-fill states would still merge"
    )


# ── A2 — merge behaviour, flag OFF and ON ────────────────────────────────────


def _make_form_fill_scenes() -> list[dict]:
    """Two scenes on the same URL with chrome-heavy OCR but different filled
    values — the canonical Phase-1 bug case."""
    return [
        {
            "scene_id": "a", "artifact_id": ART, "session_id": SESS, "tenant_id": TEN,
            "scene_index": 0,
            "detected_url": "https://example.com/quote",
            "ocr_text": "Home Search Settings Annual Premium 5000 Coverage 100000 Submit",
            "start_ms": 0, "end_ms": 2000,
            "frame_ids": ["fa1"], "completeness_confidence": 0.8,
            "has_keyframe_boundary": False,
            "visible_text_fingerprint": list(_structural_fingerprint(
                "Home Search Settings Annual Premium 5000 Coverage 100000 Submit"
            )),
            "detected_controls": [],
            "entry_action": "", "exit_action": "",
        },
        {
            "scene_id": "b", "artifact_id": ART, "session_id": SESS, "tenant_id": TEN,
            "scene_index": 1,
            "detected_url": "https://example.com/quote",
            "ocr_text": "Home Search Settings Annual Premium 5500 Coverage 150000 Submit",
            "start_ms": 2000, "end_ms": 4000,
            "frame_ids": ["fa2"], "completeness_confidence": 0.8,
            "has_keyframe_boundary": False,
            "visible_text_fingerprint": list(_structural_fingerprint(
                "Home Search Settings Annual Premium 5500 Coverage 150000 Submit"
            )),
            "detected_controls": [],
            "entry_action": "", "exit_action": "",
        },
    ]


def test_a2_flag_off_form_fill_scenes_stay_distinct(phase2_off):
    """Flag OFF → Phase-1-compatible.  Form-fill states stay distinct."""
    scenes = bs._merge_same_url_scenes(_make_form_fill_scenes(), ART)
    assert len(scenes) == 2


def test_a2_flag_on_form_fill_scenes_stay_distinct(phase2_on):
    """Flag ON → structural fingerprint guard prevents form-fill merge.
    This is the customer-facing defect we're hardening against."""
    scenes = bs._merge_same_url_scenes(_make_form_fill_scenes(), ART)
    assert len(scenes) == 2, (
        "form-fill states must remain distinct under Phase 2"
    )


def test_a2_flag_on_micro_shift_does_merge(phase2_on):
    """Flag ON → near-identical OCR (scroll micro-shift) collapses to one
    scene.  This is the legitimate de-fragmentation Phase 2 enables.

    A real scroll micro-shift produces essentially identical OCR because the
    page text doesn't change with scroll position; only the rendered pixels
    shift.  The dHash distance triggers a new scene boundary upstream, but
    the fingerprints and OCR remain (effectively) identical.
    """
    # A real scroll micro-shift: OCR text is identical because the page
    # didn't change, only the dHash differs (pixels moved a few rows).
    text_a = "Home Search Annual Premium 5000 Coverage 100000 Submit"
    text_b = "Home Search Annual Premium 5000 Coverage 100000 Submit"
    s1 = {
        "scene_id": "a", "artifact_id": ART, "session_id": SESS, "tenant_id": TEN,
        "scene_index": 0,
        "detected_url": "https://example.com/q",
        "ocr_text": text_a,
        "start_ms": 0, "end_ms": 1000,
        "frame_ids": ["x1"], "completeness_confidence": 0.7,
        "has_keyframe_boundary": False,
        "visible_text_fingerprint": list(_structural_fingerprint(text_a)),
        "detected_controls": [], "entry_action": "", "exit_action": "",
    }
    s2 = {
        "scene_id": "b", "artifact_id": ART, "session_id": SESS, "tenant_id": TEN,
        "scene_index": 1,
        "detected_url": "https://example.com/q",
        "ocr_text": text_b,
        "start_ms": 1000, "end_ms": 2000,
        "frame_ids": ["x2"], "completeness_confidence": 0.85,
        "has_keyframe_boundary": False,
        "visible_text_fingerprint": list(_structural_fingerprint(text_b)),
        "detected_controls": [], "entry_action": "", "exit_action": "",
    }
    scenes = bs._merge_same_url_scenes([s1, s2], ART)
    assert len(scenes) == 1, "scroll micro-shift should merge under Phase 2"


def test_a2_flag_on_keyframe_boundary_blocks_merge(phase2_on):
    """Flag ON → has_keyframe_boundary on either scene blocks the merge,
    even when raw OCR + structural fingerprint are identical."""
    text = "Home Annual Premium 5000 Submit"
    fp = list(_structural_fingerprint(text))
    s1 = {
        "scene_id": "a", "artifact_id": ART, "session_id": SESS, "tenant_id": TEN,
        "scene_index": 0,
        "detected_url": "https://example.com/q",
        "ocr_text": text,
        "start_ms": 0, "end_ms": 1000,
        "frame_ids": ["k1"], "completeness_confidence": 0.7,
        "has_keyframe_boundary": False,
        "visible_text_fingerprint": fp,
        "detected_controls": [], "entry_action": "", "exit_action": "",
    }
    s2 = {
        "scene_id": "b", "artifact_id": ART, "session_id": SESS, "tenant_id": TEN,
        "scene_index": 1,
        "detected_url": "https://example.com/q",
        "ocr_text": text,
        "start_ms": 1000, "end_ms": 2000,
        "frame_ids": ["k2"], "completeness_confidence": 0.85,
        # The hard cut: this scene opened with a keyframe — modal pop, etc.
        "has_keyframe_boundary": True,
        "visible_text_fingerprint": fp,
        "detected_controls": [], "entry_action": "", "exit_action": "",
    }
    scenes = bs._merge_same_url_scenes([s1, s2], ART)
    assert len(scenes) == 2, "hard keyframe must block merge"


def test_a2_can_merge_phase2_helper_signals():
    """Direct unit test of the predicate."""
    base = {
        "detected_url": "https://x.com/y",
        "ocr_text": "Home Search Term Foo Bar Baz",
        "visible_text_fingerprint": ["foo", "bar", "baz"],
        "start_ms": 0, "end_ms": 1000,
        "has_keyframe_boundary": False,
        "detected_controls": [], "entry_action": "", "exit_action": "",
    }
    near_dup = dict(base)
    near_dup["start_ms"] = 1000
    near_dup["end_ms"] = 2000
    assert _can_merge_phase2(base, near_dup, raw_sim=0.80, struct_sim=0.90) is True

    # Different URL → cannot merge
    diff_url = dict(near_dup)
    diff_url["detected_url"] = "https://other.com/y"
    assert _can_merge_phase2(base, diff_url, raw_sim=0.80, struct_sim=0.90) is False

    # Time gap > 10s → cannot merge
    far_in_time = dict(near_dup)
    far_in_time["start_ms"] = 15_000
    far_in_time["end_ms"] = 16_000
    assert _can_merge_phase2(base, far_in_time, raw_sim=0.80, struct_sim=0.90) is False

    # Different controls → cannot merge
    diff_controls = dict(near_dup)
    diff_controls["detected_controls"] = ["Submit"]
    base_with = dict(base)
    base_with["detected_controls"] = ["Cancel"]
    assert _can_merge_phase2(base_with, diff_controls, raw_sim=0.80, struct_sim=0.90) is False


# ── A3 — canonical action_kind taxonomy ──────────────────────────────────────


def _ui_element(element_type: str, label: str = "Submit", **props) -> dict:
    return {
        "element_type": element_type,
        "text": label,
        "properties": props,
        "bbox": [10, 10, 100, 40],
    }


def _scene_with(elements: list[dict]) -> tuple[dict, dict]:
    sid = str(uuid.uuid4())
    fid = str(uuid.uuid4())
    scene = {
        "scene_id": sid,
        "ocr_text": "Submit Cancel Username Password",
    }
    frame = {
        "frame_id": fid,
        "extracted_text": "Submit Cancel Username Password",
        "ocr_confidence": 0.9,
        "description": "Submit Cancel form",
        "ui_elements": elements,
        "ui_elements_json": elements,
    }
    return scene, frame


def test_a3_canonical_kinds_set_is_locked():
    """Contract test: the canonical set is the one downstream test gen expects."""
    expected = {
        "click_cta", "enter_text", "select_option",
        "toggle", "navigate", "upload_file", "drag_handle",
    }
    assert CANONICAL_ACTION_KINDS == expected


def test_a3_structural_map_only_emits_canonical():
    """Every value in the structural map is a canonical kind."""
    for et, kind in _STRUCTURAL_KIND_MAP.items():
        assert kind in CANONICAL_ACTION_KINDS, f"{et}→{kind} is not canonical"


def test_a3_flag_off_unmapped_element_emits_interact(phase2_off):
    """Flag OFF: eligible-but-unmapped element_type → legacy 'interact' verb.

    ``table_cell`` is in ``ELIGIBLE_ELEMENT_TYPES`` (so it survives the
    eligibility gate) but has no entry in either the legacy or canonical
    fallback maps.  Under Phase 1 it gets the meaningless ``"interact"``
    verb — exactly the behaviour Phase 2 is meant to fix.
    """
    scene, frame = _scene_with([
        _ui_element("button", label="Submit"),
        _ui_element("table_cell", label="Row Data"),
    ])
    ctrls = ControlExtractor().extract(scene=scene, frame=frame, artifact_id=ART, tenant_id=TEN)
    kinds = {c["action_kind"] for c in ctrls}
    # Phase 1 still emits the ambiguous "interact" verb for unmapped elements.
    assert "interact" in kinds, f"expected interact in kinds, got {kinds}"
    # Critically: button uses legacy "click", NOT canonical "click_cta".
    btn_kinds = {c["action_kind"] for c in ctrls
                 if (c.get("label_text") or "").lower().startswith("submit")}
    assert btn_kinds == {"click"}


def test_a3_flag_on_unmapped_element_is_dropped(phase2_on):
    """Flag ON: eligible-but-unmapped element_type is dropped silently.

    The button stays (canonical mapping), the table_cell disappears (no
    canonical mapping → no first-class control row).  No ``"interact"``
    noise pollutes the output.
    """
    scene, frame = _scene_with([
        _ui_element("button", label="Submit"),
        _ui_element("table_cell", label="Row Data"),
    ])
    ctrls = ControlExtractor().extract(scene=scene, frame=frame, artifact_id=ART, tenant_id=TEN)
    kinds = {c["action_kind"] for c in ctrls}
    assert "interact" not in kinds
    # Every emitted kind is canonical
    for k in kinds:
        assert k in CANONICAL_ACTION_KINDS, (
            f"non-canonical action_kind {k!r} emitted under Phase 2"
        )
    # The button survived; the table_cell did not.
    btn = [c for c in ctrls if (c.get("label_text") or "").lower().startswith("submit")]
    assert len(btn) == 1
    assert btn[0]["action_kind"] == "click_cta"


def test_a3_flag_on_button_maps_to_click_cta(phase2_on):
    """Flag ON: button → click_cta (canonical), not 'click' (legacy)."""
    scene, frame = _scene_with([
        _ui_element("button", label="Submit"),
    ])
    ctrls = ControlExtractor().extract(scene=scene, frame=frame, artifact_id=ART, tenant_id=TEN)
    assert any(c["action_kind"] == "click_cta" for c in ctrls), (
        f"expected click_cta, got {[c['action_kind'] for c in ctrls]}"
    )


def test_a3_flag_on_explicit_kind_overrides_structural(phase2_on):
    """An explicit canonical kind in properties wins over the structural map."""
    scene, frame = _scene_with([
        # link element_type would fall back to click_cta, but explicit
        # action_kind=navigate should win.
        _ui_element("link", label="Open dashboard", action_kind="navigate"),
    ])
    ctrls = ControlExtractor().extract(scene=scene, frame=frame, artifact_id=ART, tenant_id=TEN)
    assert ctrls and ctrls[0]["action_kind"] == "navigate"


def test_a3_flag_on_explicit_non_canonical_falls_through(phase2_on):
    """An explicit non-canonical kind → structural fallback applies."""
    scene, frame = _scene_with([
        # element_type=button maps structurally to click_cta;
        # explicit "interact" is non-canonical so structural wins.
        _ui_element("button", label="Submit", action_kind="interact"),
    ])
    ctrls = ControlExtractor().extract(scene=scene, frame=frame, artifact_id=ART, tenant_id=TEN)
    assert ctrls
    assert ctrls[0]["action_kind"] == "click_cta"
