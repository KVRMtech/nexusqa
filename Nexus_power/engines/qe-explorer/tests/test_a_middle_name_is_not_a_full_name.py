"""A middle name is not a full name, and a date mask is not free text.

MEASURED on orangehrm 2026-09-04, scoring a Target crawl against the fields the
pages actually contain: 12 of 12 found (100%), 11 of 12 filled (92%), and 3 of
11 filled with the right KIND of data (27%). Two of the wrong nine:

    Middle Name   <- "Amelia Lockhart"   the applicant's WHOLE name
    yyyy-mm-dd    <- "autotest"          a string a date input cannot accept

"Middle Name" carries the token "name" and matched neither the first- nor
last-name rung, so it fell through to FULL_NAME. "yyyy-mm-dd" is what an
application with no accessible name is labelled by - its placeholder - and that
label named the FORMAT, which nothing read, so it classified UNKNOWN and was
answered by the free-text rung.

WHY THE MODEL CANNOT RESCUE EITHER. The LLM is rung 8 and
``forms._llm_should_answer`` consults it only when the ladder produced NOTHING or
its own "autotest" placeholder. "Amelia Lockhart" is a real value from a real
rung, so a confidently WRONG answer silently locks the model out of correcting
it. The date field it would have reached, but a value that cannot be entered is
still a failed fill. Both had to be fixed where the meaning is decided.

THE CONTROLS ARE THE POINT. A rule that reads "middle" is one careless token
away from claiming "Middle school district", and a rule that reads a date mask
is one away from claiming any hyphenated label. The over-block tests below are
what keep this narrow, and the persona test is what keeps it from moving every
value in every crawl.
"""
from __future__ import annotations

import pytest

from app import field_semantics as F


def _classify(name: str, **sig) -> str:
    base = {"tokens": name.lower().replace("-", " ").replace("_", " ").split(),
            "name": name}
    base.update(sig)
    return F.classify(base)["type"]


# ══════════════════════════════════════════════════════════════════════════
#  The two measured defects
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("label", ["Middle Name", "middle name", "MIDDLE NAME"])
def test_a_middle_name_is_classified_as_one(label):
    assert _classify(label) == F.MIDDLE_NAME, (
        "%r still resolves to %r — the funnel is answered with the applicant's "
        "whole name typed into the middle-name box, and it looks right in every "
        "report" % (label, _classify(label))
    )


@pytest.mark.parametrize("mask", ["yyyy-mm-dd", "dd/mm/yyyy", "mm-dd-yyyy",
                                  "YYYY-MM-DD", "dd.mm.yyyy", "yyyy/mm/dd"])
def test_a_field_named_by_a_date_mask_is_a_date(mask):
    """The application labelled the field with its FORMAT — that is a statement
    of type, made in the only place the markup left it."""
    assert _classify(mask) == F.DATE, (
        "%r resolved to %r, so a date input is answered with free text and the "
        "submit can never be valid" % (mask, _classify(mask))
    )


def test_the_new_type_is_inside_the_cage():
    """coerce() only passes members of VOCABULARY; an unregistered type silently
    becomes UNKNOWN, which is exactly how the first attempt at this failed."""
    assert F.MIDDLE_NAME in F.VOCABULARY
    assert F.coerce("middle_name") == F.MIDDLE_NAME
    assert F.MIDDLE_NAME in F.SENSITIVE, (
        "a middle name is personal data exactly as the other name parts are; "
        "outside SENSITIVE it becomes eligible for cross-client sharing"
    )


# ══════════════════════════════════════════════════════════════════════════
#  CONTROLS — the rules must stay narrow
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("label,expected", [
    ("First Name", F.GIVEN_NAME),
    ("Last Name", F.FAMILY_NAME),
    ("Full Name", F.FULL_NAME),
    ("Company Name", F.COMPANY),
    ("Date of Birth", F.DOB),
    ("Email", F.EMAIL),
])
def test_the_names_that_already_worked_do_not_move(label, expected):
    """CONTROL — every other name rung is load-bearing and unchanged."""
    assert _classify(label) == expected


@pytest.mark.parametrize("label", [
    "Middle school district",
    "Middle management headcount",
    "Middle tier",
])
def test_middle_without_a_name_word_is_not_a_middle_name(label):
    """CONTROL — the over-block this rule is one token away from.

    A live crawl once read "Tobacco use in the last 12 months" as a family name
    because the qualifier alone was enough, and answered the funnel with a
    surname. The qualifier must sit beside the word "name".
    """
    assert _classify(label) != F.MIDDLE_NAME


@pytest.mark.parametrize("label", [
    "Policy-number", "First-Last", "Add-on", "e-mail", "dd", "mm-", "Order-ID",
])
def test_an_ordinary_hyphenated_label_is_not_a_date_mask(label):
    """CONTROL — the mask rule must match a FORMAT, not any punctuated label.

    "Start Date" is deliberately NOT in this list: it contains the word "date"
    and is a date by the ordinary rung, so asserting it here would need an
    escape clause — and a control with an escape clause is not a control.
    """
    assert _classify(label) != F.DATE


def test_a_real_date_label_still_reaches_the_date_rung():
    """CONTROL — the mask rule must not have displaced the ordinary one."""
    assert _classify("Effective Date") == F.DATE


# ══════════════════════════════════════════════════════════════════════════
#  The value, and the persona it must not disturb
# ══════════════════════════════════════════════════════════════════════════

def _a_person(seed="tenant::app::seed-1"):
    from app.fill_engine import persona as P
    per = P.derive_persona(seed)
    for attr in dir(per):
        v = getattr(per, attr)
        if hasattr(v, "given_name") and hasattr(v, "family_name"):
            return v
    raise AssertionError("no person on the persona")


def test_the_middle_name_is_a_real_name_and_not_the_first_one():
    from app.fill_engine.generator import _middle_name_for
    who = _a_person()
    middle = _middle_name_for(who)
    assert middle and middle.isalpha(), "expected a name, got %r" % (middle,)
    assert middle.strip().lower() != who.given_name.strip().lower(), (
        "the middle name repeats the first name, which reads as a filled form "
        "that no real person would produce"
    )


def test_the_middle_name_is_stable():
    """A crawl that re-runs must present the same applicant."""
    from app.fill_engine.generator import _middle_name_for
    assert _middle_name_for(_a_person()) == _middle_name_for(_a_person())


def test_the_persona_stream_did_not_move():
    """CONTROL, and the reason the derivation reads the person rather than the seed.

    Drawing a fresh byte from the persona stream would shift EVERY later value —
    email, phone, national id, dates — for every field in every crawl, moving the
    goldens to fix one box. These are the values for this seed before the change.
    """
    who = _a_person()
    assert who.given_name == "Zachary"
    assert who.family_name == "Carrington"
    assert who.full_name == "Zachary Carrington"
