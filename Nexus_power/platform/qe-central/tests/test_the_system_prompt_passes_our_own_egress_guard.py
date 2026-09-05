"""Our own safety instruction must not trip our own safety scanner.

MEASURED 2026-09-04. Every LLM data-fill call in the fleet was being refused:

    qec.egress.pii_detected site=llm:field_value patterns=['credit_card']
    qec.platform_api.egress_blocked site=llm:field_value
    Middle Name -> status=unavailable   From -> unavailable   Keywords -> unavailable

Not one of those fields is a card. The match was in the SYSTEM prompt, which
told the model to use "card numbers that pass Luhn from published test prefixes
(e.g. 4111111111111111)". That number passes Luhn by design — it is the classic
Visa test PAN — so the egress guard scanned the instruction, found a valid card
number in the outbound payload, and did exactly what it exists to do.

THE GUARD WAS RIGHT. The prompt was wrong. So the literal is gone and the
instruction stays: a model does not need the digits spelled out to know what a
published test range is.

WHAT MADE IT EXPENSIVE. Nothing failed. `pick_value` returns ``unavailable`` on a
refusal — deliberately the same shape as a provider outage — and the crawler
falls back to its deterministic filler. So a crawl configured for LLM data
completed, filled every field with ``autotest``, and reported success. On the
application this was found on, 8 of 11 written values were that filler, two of
them in date inputs that cannot accept text.

This file exists so the next person who adds a helpful example to a prompt finds
out here, in a second, rather than from a crawl that silently stopped using its
model.
"""
from __future__ import annotations

import pytest

from app.services.value_agent import _SYSTEM, build_prompt


def _scan(text: str) -> list:
    """The REAL scanner the egress wire uses — never a local re-implementation.

    ``services/pii_egress_guard.scan`` lazily imports ``check_text`` from the SDK
    and FAILS CLOSED if it is missing, so this test calls the same function the
    wire calls. When the SDK is not installed into the interpreter it is loaded
    from its path in this repository rather than skipping: a skipped test proves
    nothing, and this one guards a prompt that reaches a third party.
    """
    try:
        from nexus_sdk.llm.pii_guard import check_text
    except ImportError:
        import os
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        sdk = os.path.abspath(os.path.join(here, "..", "..", "..", "sdk", "nexus-sdk"))
        if sdk not in sys.path:
            sys.path.insert(0, sdk)
        try:
            from nexus_sdk.llm.pii_guard import check_text
        except ImportError:  # pragma: no cover
            pytest.skip("nexus_sdk not importable from the interpreter or %s" % sdk)
    return list(check_text(text) or [])


#: The literal that caused the outage, kept verbatim so the control below is
#: driven by the real thing rather than a plausible-looking substitute.
VISA_TEST_PAN = "4111111111111111"


def test_the_system_prompt_is_clean():
    """The fix: the instruction the fleet actually sends must pass the wire."""
    hits = _scan(_SYSTEM)
    assert not hits, (
        "the system prompt trips our own egress guard, so every LLM data-fill "
        "call is refused before it leaves: %r"
        % [(getattr(h, 'pattern_name', h), getattr(h, 'masked', '')) for h in hits]
    )


def test_the_scanner_would_have_caught_it():
    """CONTROL — proves this test is not vacuous.

    If the guard cannot flag the exact string that caused the outage, then
    `test_the_system_prompt_is_clean` passes for the wrong reason and would keep
    passing if the literal came back.
    """
    hits = _scan("use a test card such as %s for this field" % VISA_TEST_PAN)
    assert any(getattr(h, 'pattern_name', h) == "credit_card" for h in hits), (
        "the guard no longer detects the Visa test PAN, so the clean-prompt "
        "assertion above proves nothing"
    )


def test_the_instruction_survived_the_edit():
    """The literal went; the RULE must not have gone with it.

    Deleting the guidance would silently let the model invent realistic-looking
    financial identifiers, which is a worse outcome than the bug being fixed.
    """
    lowered = _SYSTEM.lower()
    assert "test range" in lowered or "test ranges" in lowered
    assert "900-series" in _SYSTEM, "the SSN guidance was lost with the card example"
    assert VISA_TEST_PAN not in _SYSTEM


@pytest.mark.parametrize("field", [
    {"name": "Middle Name", "semantic_type": "full_name", "kind": "text"},
    {"name": "From", "semantic_type": "free_text", "kind": "text"},
    {"name": "Card Number", "semantic_type": "card_number", "kind": "text"},
    {"name": "Social Security Number", "semantic_type": "ssn", "kind": "text"},
])
def test_a_built_prompt_is_clean_for_ordinary_fields(field):
    """The user half of the payload, including the two field NAMES most likely
    to look alarming. Naming a field "Card Number" must not block the call — the
    guard is there to stop a card NUMBER leaving, not the words."""
    prompt = build_prompt(options=(), constraints="", section="",
                          page_title="", rejection="", **field)
    hits = _scan(prompt)
    assert not hits, (
        "a prompt for %r trips the egress guard: %r"
        % (field["name"], [(getattr(h, 'pattern_name', h), getattr(h, 'masked', '')) for h in hits])
    )
