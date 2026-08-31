"""B2, LIMIT CLOSED — "(999) 999-9999" IS READ AS ONE COMPLETE FORMAT.

The first cut of the mask reader shipped this as a named limit: the
parentheses in an area code are PART of the format, not a wrapper around it,
and the reader could only refuse the token (correctly — the alternative was a
partial pattern a complete phone number fails). It is now recognised as one
complete token — a short parenthesised run, then a run, then separated runs —
matched before the general branches so neither of them ever sees a fragment.

The value the generator produces from it satisfies the format the application
drew, which is the whole point of a mask.
"""
from __future__ import annotations

import re

import pytest

from app.fill_engine import constraints as C
from app.fill_engine import patterns
from app.fill_engine.repair import tighten
from app.fill_engine.validation import interpret, mask_pattern


def test_the_area_code_format_is_read_end_to_end():
    """THE LIMIT, CLOSED: message -> pattern -> a value that satisfies the
    application's own drawn format."""
    hint = interpret("Phone must be (999) 999-9999")
    assert hint.actionable, "this was the named limit and returned False"
    cons = tighten(C.Constraints(input_type="text"), hint)
    value = patterns.satisfy(cons.pattern)
    assert value is not None
    assert re.match(r"^\(\d{3}\) \d{3}-\d{4}$", value), value


@pytest.mark.parametrize("message,expected", [
    ("Phone must be (999) 999-9999", r"^\(\d{3}\) \d{3}\-\d{4}$"),
    ("Enter a phone like (XXX) XXX-XXXX", r"^\(\d{3}\) \d{3}\-\d{4}$"),
    ("Format: (999)999-9999", r"^\(\d{3}\)\d{3}\-\d{4}$"),
    ("Use ( 999 ) 999-9999 please", r"^\(\d{3}\) \d{3}\-\d{4}$"),
])
def test_the_shape_is_drawn_faithfully_including_its_parentheses(message, expected):
    assert mask_pattern(message) == expected


def test_a_fragment_is_still_refused_when_no_complete_format_precedes_it():
    """THE PROPERTY THE FIRST CUT PROTECTED IS KEPT: a bare tail after a
    closing paren that is NOT an area code is still a fragment, still refused."""
    assert mask_pattern("see note (b) 999-9999 is wrong") == ""


def test_the_general_parenthesised_mask_is_unchanged():
    """The SSN case that motivated B2 must resolve exactly as before."""
    assert mask_pattern("Enter a valid SSN (XXX-XX-XXXX)") == r"^\d{3}\-\d{2}\-\d{4}$"


def test_a_tel_input_still_refuses_the_mask():
    """Adoption stays narrow: a `tel` input owns its own format and the
    semantic path is strictly better than a shape."""
    hint = interpret("Phone must be (999) 999-9999")
    assert tighten(C.Constraints(input_type="tel"), hint).pattern == ""
