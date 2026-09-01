"""R3 Crawl Medic — unit tests for the caged agent service.

Tests cover the vocabulary parser, input validation, and the prompt builder.
LLM integration is NOT tested here (it goes through platform_api.complete_llm
which requires a live connection); the parser and guards are fully deterministic.
"""
from __future__ import annotations

import pytest

from app.services.crawl_medic import (
    MedicDecision,
    STATUS_DISPLAY_ONLY,
    STATUS_PROPOSED,
    STATUS_UNAVAILABLE,
    VOCABULARY,
    _build_prompt,
    _parse_reply,
)


# ── Vocabulary completeness ─────────────────────────────────────────────────

def test_vocabulary_contains_all_documented_actions():
    expected = {
        "click", "press:Space", "press:Enter", "press:ArrowDown",
        "open_then_pick", "display_only", "unavailable",
    }
    assert VOCABULARY == expected


# ── Reply parser ─────────────────────────────────────────────────────────────

def test_parse_exact_click():
    d = _parse_reply("click")
    assert d.status == STATUS_PROPOSED
    assert d.action == "click"


def test_parse_exact_press_space():
    d = _parse_reply("press:Space")
    assert d.status == STATUS_PROPOSED
    assert d.action == "press:Space"


def test_parse_exact_press_enter():
    d = _parse_reply("press:Enter")
    assert d.status == STATUS_PROPOSED
    assert d.action == "press:Enter"


def test_parse_exact_press_arrowdown():
    d = _parse_reply("press:ArrowDown")
    assert d.status == STATUS_PROPOSED
    assert d.action == "press:ArrowDown"


def test_parse_exact_open_then_pick():
    d = _parse_reply("open_then_pick")
    assert d.status == STATUS_PROPOSED
    assert d.action == "open_then_pick"


def test_parse_display_only():
    d = _parse_reply("display_only")
    assert d.status == STATUS_DISPLAY_ONLY
    assert d.action == "display_only"


def test_parse_unavailable():
    d = _parse_reply("unavailable")
    assert d.status == STATUS_UNAVAILABLE


def test_parse_case_insensitive():
    d = _parse_reply("CLICK")
    assert d.status == STATUS_PROPOSED
    assert d.action == "click"


def test_parse_with_whitespace():
    d = _parse_reply("  press:Space  \n")
    assert d.status == STATUS_PROPOSED
    assert d.action == "press:Space"


def test_parse_with_surrounding_text():
    d = _parse_reply("I think you should click the control")
    assert d.status == STATUS_PROPOSED
    assert d.action == "click"


def test_parse_unrecognized_reply():
    d = _parse_reply("do something magical")
    assert d.status == STATUS_UNAVAILABLE


def test_parse_empty_reply():
    d = _parse_reply("")
    assert d.status == STATUS_UNAVAILABLE


def test_parse_open_then_pick_in_sentence():
    d = _parse_reply("try open_then_pick to reveal options")
    assert d.status == STATUS_PROPOSED
    assert d.action == "open_then_pick"


# ── Prompt builder ───────────────────────────────────────────────────────────

def test_prompt_includes_control_name():
    p = _build_prompt(
        control={"name": "Product Type", "kind": "radio", "role": "radio",
                 "tag": "input"},
        intent="select",
        ladder_results=[
            {"rung": "native_set_checked", "observation": "intent_unmet"},
            {"rung": "click_element", "observation": "intent_unmet"},
        ],
        page_context={"title": "Quote", "url": "https://example.com/quote"},
    )
    assert "Product Type" in p
    assert "radio" in p
    assert "native_set_checked" in p
    assert "click_element" in p
    assert "Quote" in p


def test_prompt_limits_ladder_results():
    many = [{"rung": f"rung_{i}", "observation": "failed"} for i in range(20)]
    p = _build_prompt(
        control={"name": "X"}, intent="fill",
        ladder_results=many, page_context={},
    )
    assert "rung_7" in p
    assert "rung_8" not in p


def test_prompt_handles_empty_page_context():
    p = _build_prompt(
        control={"name": "Field"}, intent="fill",
        ladder_results=[], page_context={},
    )
    assert "Field" in p


def test_prompt_includes_css_hint():
    p = _build_prompt(
        control={"name": "X", "css_hint": ".custom-card"},
        intent="click", ladder_results=[], page_context={},
    )
    assert ".custom-card" in p


def test_prompt_includes_attributes():
    p = _build_prompt(
        control={"name": "X", "attributes": {"data-test": "abc", "aria-label": "Choose"}},
        intent="click", ladder_results=[], page_context={},
    )
    assert "data-test" in p
    assert "aria-label" in p


# ── Input validation (consult_medic) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_consult_rejects_empty_control():
    from app.services.crawl_medic import consult_medic
    d = await consult_medic(
        tenant_id="t", control={}, intent="click",
        ladder_results=[], page_context={},
    )
    assert d.status == STATUS_UNAVAILABLE


@pytest.mark.asyncio
async def test_consult_rejects_nameless_control():
    from app.services.crawl_medic import consult_medic
    d = await consult_medic(
        tenant_id="t", control={"kind": "radio"}, intent="click",
        ladder_results=[], page_context={},
    )
    assert d.status == STATUS_UNAVAILABLE


@pytest.mark.asyncio
async def test_consult_skips_disabled_control():
    from app.services.crawl_medic import consult_medic
    d = await consult_medic(
        tenant_id="t", control={"name": "X", "disabled": True}, intent="click",
        ladder_results=[], page_context={},
    )
    assert d.status == STATUS_DISPLAY_ONLY


@pytest.mark.asyncio
async def test_consult_rejects_danger_control():
    from app.services.crawl_medic import consult_medic
    d = await consult_medic(
        tenant_id="t", control={"name": "X", "danger": True}, intent="click",
        ladder_results=[], page_context={},
    )
    assert d.status == STATUS_UNAVAILABLE
