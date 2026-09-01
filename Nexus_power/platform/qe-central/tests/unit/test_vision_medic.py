"""R5 Vision Medic — unit tests (pure, no network, no LLM).

Pins the classification, prompt building, proposal parsing, and validation
pipeline for the multimodal vision escalation of DOM-opaque controls.
"""
from __future__ import annotations

import json

import pytest

from app.services.vision_medic import (
    ACTION_CLICK_REGION,
    ACTION_DISPLAY_ONLY,
    STATUS_DISPLAY_ONLY,
    STATUS_PROPOSED,
    STATUS_UNAVAILABLE,
    VisionDecision,
    build_vision_prompt,
    consult_vision,
    is_vision_candidate,
    parse_vision_proposal,
    validate_proposal,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _control(*, tag="div", role="generic", name="", **kw) -> dict:
    return {"tag": tag, "role": role, "name": name, **kw}


def _bbox(x=100, y=200, width=300, height=150) -> dict:
    return {"x": x, "y": y, "width": width, "height": height}


def _page_ctx(title="Quote Page", url="https://app.example.com/quote") -> dict:
    return {"title": title, "url": url}


FAKE_SCREENSHOT = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"


# ── is_vision_candidate ──────────────────────────────────────────────────────

def test_canvas_is_candidate():
    result = is_vision_candidate(_control(tag="canvas"))
    assert result["candidate"] is True
    assert result["surface_type"] == "canvas"


def test_svg_is_candidate():
    result = is_vision_candidate(_control(tag="svg"))
    assert result["candidate"] is True
    assert result["surface_type"] == "svg"


def test_cross_origin_iframe_is_candidate():
    result = is_vision_candidate(_control(tag="iframe", cross_origin=True))
    assert result["candidate"] is True
    assert result["surface_type"] == "iframe"


def test_unnamed_iframe_is_candidate():
    result = is_vision_candidate(_control(tag="iframe", name=""))
    assert result["candidate"] is True
    assert result["surface_type"] == "iframe"


def test_named_iframe_is_not_candidate():
    result = is_vision_candidate(_control(tag="iframe", name="Login Frame"))
    assert result["candidate"] is False


def test_unlabeled_generic_role_is_candidate():
    result = is_vision_candidate(_control(tag="div", role="generic", name=""))
    assert result["candidate"] is True
    assert result["surface_type"] == "unlabeled"


def test_unlabeled_no_role_is_candidate():
    result = is_vision_candidate(_control(tag="div", role="", name=""))
    assert result["candidate"] is True
    assert result["surface_type"] == "unlabeled"


def test_named_div_is_not_candidate():
    result = is_vision_candidate(_control(tag="div", role="generic", name="Submit"))
    assert result["candidate"] is False
    assert result["surface_type"] == "dom"


def test_normal_input_is_not_candidate():
    result = is_vision_candidate(_control(tag="input", role="textbox", name="Age"))
    assert result["candidate"] is False


def test_button_with_name_is_not_candidate():
    result = is_vision_candidate(_control(tag="button", role="button", name="Next"))
    assert result["candidate"] is False


def test_empty_control_is_not_candidate():
    result = is_vision_candidate({})
    assert result["candidate"] is False


def test_presentation_role_no_name_is_candidate():
    result = is_vision_candidate(_control(tag="div", role="presentation", name=""))
    assert result["candidate"] is True
    assert result["surface_type"] == "unlabeled"


# ── build_vision_prompt ──────────────────────────────────────────────────────

def test_prompt_contains_control_shape():
    prompt = build_vision_prompt(
        control=_control(tag="canvas", kind="interactive", css_hint=".chart-area"),
        element_bbox=_bbox(),
        page_context=_page_ctx(),
    )
    assert "<canvas>" in prompt
    assert "width=300" in prompt
    assert "height=150" in prompt
    assert "Quote Page" in prompt
    assert ".chart-area" in prompt


def test_prompt_handles_empty_context():
    prompt = build_vision_prompt(
        control=_control(tag="svg"),
        element_bbox={},
        page_context={},
    )
    assert "<svg>" in prompt


def test_prompt_includes_attributes():
    prompt = build_vision_prompt(
        control=_control(tag="canvas", attributes={"data-type": "chart", "aria-label": ""}),
        element_bbox=_bbox(),
        page_context=_page_ctx(),
    )
    assert "data-type" in prompt
    assert "chart" in prompt


# ── parse_vision_proposal ────────────────────────────────────────────────────

def test_parse_click_region():
    raw = '{"action": "click_region", "x": 50, "y": 30, "reason": "button visible"}'
    result = parse_vision_proposal(raw)
    assert result.status == STATUS_PROPOSED
    assert result.action == ACTION_CLICK_REGION
    assert result.click_x == 50
    assert result.click_y == 30
    assert "button" in result.reason


def test_parse_display_only():
    raw = '{"action": "display_only", "reason": "decorative chart"}'
    result = parse_vision_proposal(raw)
    assert result.status == STATUS_DISPLAY_ONLY
    assert result.action == ACTION_DISPLAY_ONLY


def test_parse_unavailable():
    raw = '{"action": "unavailable", "reason": "too blurry"}'
    result = parse_vision_proposal(raw)
    assert result.status == STATUS_UNAVAILABLE


def test_parse_unrecognized_action():
    raw = '{"action": "drag_and_drop", "reason": "need drag"}'
    result = parse_vision_proposal(raw)
    assert result.status == STATUS_UNAVAILABLE


def test_parse_json_with_fences():
    raw = '```json\n{"action": "click_region", "x": 10, "y": 20, "reason": "ok"}\n```'
    result = parse_vision_proposal(raw)
    assert result.status == STATUS_PROPOSED
    assert result.click_x == 10


def test_parse_dict_input():
    result = parse_vision_proposal({"action": "click_region", "x": 5, "y": 5, "reason": "x"})
    assert result.status == STATUS_PROPOSED
    assert result.click_x == 5


def test_parse_malformed_returns_unavailable():
    result = parse_vision_proposal("I think you should click somewhere")
    assert result.status == STATUS_UNAVAILABLE


def test_parse_invalid_coordinates():
    raw = '{"action": "click_region", "x": "not_a_number", "y": 10}'
    result = parse_vision_proposal(raw)
    assert result.status == STATUS_UNAVAILABLE
    assert "invalid coordinates" in result.reason


def test_parse_empty_string():
    result = parse_vision_proposal("")
    assert result.status == STATUS_UNAVAILABLE


# ── validate_proposal ────────────────────────────────────────────────────────

def test_validate_in_bounds():
    proposal = VisionDecision(status=STATUS_PROPOSED, action=ACTION_CLICK_REGION,
                              click_x=50, click_y=30)
    result = validate_proposal(proposal, _bbox(width=300, height=150))
    assert result["valid"] is True


def test_validate_x_out_of_bounds():
    proposal = VisionDecision(status=STATUS_PROPOSED, action=ACTION_CLICK_REGION,
                              click_x=400, click_y=30)
    result = validate_proposal(proposal, _bbox(width=300, height=150))
    assert result["valid"] is False
    assert "width" in result["reason"]


def test_validate_y_out_of_bounds():
    proposal = VisionDecision(status=STATUS_PROPOSED, action=ACTION_CLICK_REGION,
                              click_x=50, click_y=200)
    result = validate_proposal(proposal, _bbox(width=300, height=150))
    assert result["valid"] is False
    assert "height" in result["reason"]


def test_validate_negative_coordinates():
    proposal = VisionDecision(status=STATUS_PROPOSED, action=ACTION_CLICK_REGION,
                              click_x=-10, click_y=30)
    result = validate_proposal(proposal, _bbox(width=300, height=150))
    assert result["valid"] is False
    assert "negative" in result["reason"]


def test_validate_zero_dimension_element():
    proposal = VisionDecision(status=STATUS_PROPOSED, action=ACTION_CLICK_REGION,
                              click_x=0, click_y=0)
    result = validate_proposal(proposal, _bbox(width=0, height=0))
    assert result["valid"] is False
    assert "zero" in result["reason"]


def test_validate_non_click_always_valid():
    proposal = VisionDecision(status=STATUS_DISPLAY_ONLY, action=ACTION_DISPLAY_ONLY)
    result = validate_proposal(proposal, _bbox())
    assert result["valid"] is True


def test_validate_edge_coordinates():
    proposal = VisionDecision(status=STATUS_PROPOSED, action=ACTION_CLICK_REGION,
                              click_x=300, click_y=150)
    result = validate_proposal(proposal, _bbox(width=300, height=150))
    assert result["valid"] is True


# ── consult_vision (async, with fake propose_fn) ─────────────────────────────

async def test_consult_vision_proposed():
    async def _fake(prompt, image):
        return {"action": "click_region", "x": 42, "y": 17, "reason": "radio button"}

    result = await consult_vision(
        tenant_id="t-1",
        control=_control(tag="canvas"),
        screenshot_b64=FAKE_SCREENSHOT,
        element_bbox=_bbox(),
        page_context=_page_ctx(),
        propose_fn=_fake,
    )
    assert result.status == STATUS_PROPOSED
    assert result.click_x == 42
    assert result.click_y == 17


async def test_consult_vision_not_a_candidate():
    result = await consult_vision(
        tenant_id="t-1",
        control=_control(tag="input", role="textbox", name="Age"),
        screenshot_b64=FAKE_SCREENSHOT,
        element_bbox=_bbox(),
        page_context=_page_ctx(),
        propose_fn=lambda p, i: {"action": "click_region", "x": 0, "y": 0},
    )
    assert result.status == STATUS_UNAVAILABLE
    assert "not a vision candidate" in result.reason


async def test_consult_vision_no_screenshot():
    result = await consult_vision(
        tenant_id="t-1",
        control=_control(tag="canvas"),
        screenshot_b64="",
        element_bbox=_bbox(),
        page_context=_page_ctx(),
        propose_fn=lambda p, i: {},
    )
    assert result.status == STATUS_UNAVAILABLE
    assert "no screenshot" in result.reason


async def test_consult_vision_llm_failure():
    async def _failing(prompt, image):
        raise RuntimeError("LLM timeout")

    result = await consult_vision(
        tenant_id="t-1",
        control=_control(tag="canvas"),
        screenshot_b64=FAKE_SCREENSHOT,
        element_bbox=_bbox(),
        page_context=_page_ctx(),
        propose_fn=_failing,
    )
    assert result.status == STATUS_UNAVAILABLE
    assert "failed" in result.reason


async def test_consult_vision_no_propose_fn():
    result = await consult_vision(
        tenant_id="t-1",
        control=_control(tag="canvas"),
        screenshot_b64=FAKE_SCREENSHOT,
        element_bbox=_bbox(),
        page_context=_page_ctx(),
        propose_fn=None,
    )
    assert result.status == STATUS_UNAVAILABLE
    assert "no vision LLM" in result.reason


async def test_consult_vision_rejects_out_of_bounds():
    async def _fake(prompt, image):
        return {"action": "click_region", "x": 999, "y": 999, "reason": "off screen"}

    result = await consult_vision(
        tenant_id="t-1",
        control=_control(tag="canvas"),
        screenshot_b64=FAKE_SCREENSHOT,
        element_bbox=_bbox(width=100, height=50),
        page_context=_page_ctx(),
        propose_fn=_fake,
    )
    assert result.status == STATUS_UNAVAILABLE
    assert "rejected" in result.reason


async def test_consult_vision_display_only():
    async def _fake(prompt, image):
        return {"action": "display_only", "reason": "static chart"}

    result = await consult_vision(
        tenant_id="t-1",
        control=_control(tag="canvas"),
        screenshot_b64=FAKE_SCREENSHOT,
        element_bbox=_bbox(),
        page_context=_page_ctx(),
        propose_fn=_fake,
    )
    assert result.status == STATUS_DISPLAY_ONLY


async def test_consult_vision_empty_control():
    result = await consult_vision(
        tenant_id="t-1", control={}, screenshot_b64=FAKE_SCREENSHOT,
        element_bbox=_bbox(), page_context=_page_ctx(),
    )
    assert result.status == STATUS_UNAVAILABLE
