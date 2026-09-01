"""U2 — the page Perceiver: enumerate controls + outcomes from a screenshot.
Pure parse + injectable propose_fn (no LLM, no browser)."""
from __future__ import annotations

import asyncio

from app.services.vision_medic import parse_perceived, perceive_controls


def test_parse_perceived_extracts_controls_and_values():
    raw = ('{"controls":[{"label":"Continue","role":"button","bbox":[500,600,100,40],'
           '"click_x":550,"click_y":620}],'
           '"displayed_values":[{"label":"Premium","text":"$42.10"}]}')
    got = parse_perceived(raw)
    assert got["controls"][0]["label"] == "Continue"
    assert got["controls"][0]["click_x"] == 550 and got["controls"][0]["click_y"] == 620
    assert got["displayed_values"] == [{"label": "Premium", "text": "$42.10"}]


def test_parse_perceived_defaults_click_to_bbox_center():
    c = parse_perceived({"controls": [{"label": "Go", "bbox": [100, 200, 50, 20]}]})["controls"][0]
    assert c["click_x"] == 125 and c["click_y"] == 210
    assert c["role"] == "button"


def test_parse_perceived_tolerates_junk_and_code_fences():
    assert parse_perceived("not json") == {"controls": [], "displayed_values": []}
    assert parse_perceived("```json\n{\"controls\":[]}\n```") == {"controls": [], "displayed_values": []}
    # a bad control (no label, empty bbox) is dropped
    assert parse_perceived({"controls": ["bad", {"label": "", "bbox": [0, 0, 0, 0]}]})["controls"] == []


def test_perceive_controls_with_fake_propose():
    async def fake(prompt, img):
        # M3.1 / T-VIS-03 — the OUTPUT CONTRACT is no longer restated in the user
        # prompt. It lives in exactly one place (PERCEIVE_SYSTEM, reached through
        # system_prompt_for), because two copies in two channels is how the
        # endpoint came to send the perceive contract and the click-region
        # contract on the same call. The user prompt now carries page context.
        from app.services.vision_medic import PERCEIVE_SYSTEM

        assert "SCREENSHOT" not in prompt
        assert "SCREENSHOT" in PERCEIVE_SYSTEM
        return '{"controls":[{"label":"Pay","role":"button","bbox":[10,10,80,30]}],"displayed_values":[]}'

    got = asyncio.run(perceive_controls(tenant_id="t", screenshot_b64="abc", propose_fn=fake))
    assert got["controls"][0]["label"] == "Pay"


def test_perceive_controls_degrades_without_propose_or_screenshot():
    async def run():
        assert await perceive_controls(tenant_id="t", screenshot_b64="", propose_fn=lambda *a: None) == {
            "controls": [], "displayed_values": []}
        assert await perceive_controls(tenant_id="t", screenshot_b64="abc", propose_fn=None) == {
            "controls": [], "displayed_values": []}
    asyncio.run(run())


def test_perceive_controls_never_raises_on_propose_error():
    async def boom(prompt, img):
        raise RuntimeError("llm down")

    assert asyncio.run(perceive_controls(tenant_id="t", screenshot_b64="abc", propose_fn=boom)) == {
        "controls": [], "displayed_values": []}
