"""B2 — CONSTRAINT-AWARE REPAIR: READ THE MASK THE APPLICATION DISPLAYS.

The Phase-5 backlog's second item, and it depends on B1 exactly as that
document says: a repair driven by anything other than the application's own
words is a guess wearing a retry's clothes.

MEASURED, against summit-life-carrier's real schema messages, before writing a
line: ``interpret()`` returned ``actionable=False`` for "Enter a valid SSN
(XXX-XX-XXXX)". The shape is right there in the sentence — the application is
not hinting about the value, it is SHOWING it — and nothing read it, so the
repair loop correctly refused to retry blind and the funnel stopped.

    Enter a valid SSN (XXX-XX-XXXX)   actionable=False   <- the gap
    Enter a valid ZIP code            actionable=True
    Enter a valid email address       actionable=True
    Minimum face amount is $10,000    actionable=True

STRUCTURAL, NEVER VOCABULARY. A mask is recognised by its SHAPE — placeholder
runs joined by separators — so it reads identically in every language a crawled
application speaks. The generator already knows how to satisfy a regex
(:mod:`app.fill_engine.patterns`), so B2 is the parse plus one adoption rule,
not a second generator.
"""
from __future__ import annotations

import pytest

from app.fill_engine import constraints as C
from app.fill_engine import patterns
from app.fill_engine.repair import tighten
from app.fill_engine.validation import interpret, mask_pattern


# ── the live case ──────────────────────────────────────────────────────────

def test_summits_own_ssn_message_becomes_a_value_the_schema_accepts():
    """THE ONE THAT MATTERS, end to end: the message the application actually
    renders, through the parse, through the generator, to a value that
    satisfies the regex summit's zod schema declares."""
    hint = interpret("Enter a valid SSN (XXX-XX-XXXX)")
    assert hint.actionable, "this returned False before B2 and stopped the funnel"

    cons = tighten(C.Constraints(), hint)
    value = patterns.satisfy(cons.pattern)
    assert value is not None

    import re
    assert re.match(r"^\d{3}-?\d{2}-?\d{4}$", value), (
        "the value must satisfy summit's own applicationSchema regex")


@pytest.mark.parametrize("message,expected", [
    ("Enter a valid SSN (XXX-XX-XXXX)", r"^\d{3}\-\d{2}\-\d{4}$"),
    ("Enter a valid ZIP code (99999-9999)", r"^\d{5}\-\d{4}$"),
    ("Enter a valid date (MM/DD/YYYY)", r"^\d{2}/\d{2}/\d{4}$"),
    ("Reference must be AAA-9999", r"^[A-Za-z]{3}\-\d{4}$"),
])
def test_a_mask_becomes_the_regex_it_draws(message, expected):
    assert mask_pattern(message) == expected


def test_the_mask_is_read_whatever_numeric_rule_the_words_also_carry():
    """A mask is ORTHOGONAL to a numeric code. "must be" classifies as a MIN
    with no number; returning early on that path lost the one thing in the
    sentence the generator could act on."""
    hint = interpret("Value must be XXX-XXX")
    assert hint.pattern == r"^\d{3}\-\d{3}$"


# ── what is NOT a mask ─────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Rated XXX for risk",             # a redaction, not a format
    "Enter a valid ZIP code",         # no mask shown at all
    "Select at least one option",
    "Minimum face amount is $10,000",  # a bound, and it is read as one
    "Enter your name",
    "",
])
def test_text_that_merely_looks_shouty_is_not_a_format(message):
    """A separator is REQUIRED: without one, "XXX" is as likely to be a rating
    or a redaction as a format, and inventing a pattern nobody can satisfy
    turns an honest ASK into a retry that can never converge."""
    assert mask_pattern(message) == ""


def test_the_control_for_that_refusal_the_same_token_with_separators_is_read():
    """FALSIFICATION CONTROL. Without it, a parser that read NOTHING would
    satisfy every refusal above and look like a careful rule."""
    assert mask_pattern("Rated XXX-XX for risk") == r"^\d{3}\-\d{2}$"


def test_a_bound_is_still_read_as_a_bound():
    """B2 must not swallow the rules that already worked."""
    hint = interpret("Minimum face amount is $10,000")
    assert hint.minimum == 10000.0
    assert hint.actionable


# ── the adoption rule ──────────────────────────────────────────────────────

def test_a_declared_pattern_is_never_replaced_by_a_message_s_mask():
    """Two regexes cannot be intersected, so a mask can only be ADOPTED where
    the control declared none — never substituted for the DOM's own, which is
    authoritative. ``tighten`` only ever narrows."""
    declared = C.Constraints(pattern=r"^\d{9}$")
    got = tighten(declared, interpret("Enter a valid SSN (XXX-XX-XXXX)"))
    assert got.pattern == r"^\d{9}$"


@pytest.mark.parametrize("input_type", ["date", "email", "number", "url",
                                        "time", "month", "tel"])
def test_a_mask_is_refused_for_an_input_whose_format_is_already_owned(input_type):
    """THE REGRESSION THIS PREVENTS. "Enter a valid date (MM/DD/YYYY)" yields
    a shape a REAL date satisfies only by accident, and a shape-only generator
    would answer it with 55/55/5555 — right shape, no meaning. The semantic
    path for these types is strictly better than the mask."""
    hint = interpret("Enter a valid date (MM/DD/YYYY)")
    assert tighten(C.Constraints(input_type=input_type), hint).pattern == ""


def test_the_control_a_bare_text_input_does_adopt_it():
    """FALSIFICATION CONTROL for the refusal above: on a plain text input —
    which is exactly what summit renders for SSN — the mask IS adopted."""
    hint = interpret("Enter a valid SSN (XXX-XX-XXXX)")
    assert tighten(C.Constraints(input_type="text"), hint).pattern != ""


def test_adopting_a_mask_marks_the_constraints_as_declared():
    """A value produced under a mask was constrained by the application, and
    the ledger must be able to say so rather than calling it unconstrained."""
    got = tighten(C.Constraints(), interpret("Enter a valid SSN (XXX-XX-XXXX)"))
    assert got.declared is True


# ── the known limit, stated rather than hidden ─────────────────────────────

def test_a_partial_mask_is_refused_because_it_is_worse_than_none():
    """THE BUG THIS CLOSES, found by driving the parser rather than reasoning
    about it: "(999) 999-9999" has parentheses that are part of the FORMAT, not
    a wrapper. The parenthesised branch rejects "999" as too short, and the
    bare branch then matched the TAIL — yielding ^\d{3}-\d{4}$, a pattern a
    complete phone number FAILS. A fragment is refused, so the field falls to
    an honest ASK.

    NOT CLAIMED: this format is still not READ. Refusing it is correct; parsing
    it needs telling format-parens from wrapper-parens, which is not done."""
    assert mask_pattern("Phone must be (999) 999-9999") == ""
    assert interpret("Phone must be (999) 999-9999").actionable is False
