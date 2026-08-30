"""RUNG 4: WHAT THE APPLICATION MINTED, SO ONE FLOW CAN FEED THE NEXT.

A service or claims flow opens by demanding something that did not exist until
an earlier flow created it — a policy number, a claim reference, a quote id. No
generator can invent one (the application checks its own database), no model can
know one, and the client cannot list them in advance because they will not exist
until the crawl runs. That is why a crawl covers the front of a product and
nothing behind it.

The crawl itself just created one. It walked the apply funnel, the confirmation
screen printed the number, and rung 4 is the decision to keep it.

THE RULE THAT DEFINES THIS RUNG IS CAUSATION, NOT SHAPE. A reference is minted
only when it was ABSENT before the crawl acted and PRESENT after. Every test
below that says "not minted" is guarding that line, because the alternative —
recording every id-shaped string on a confirmation page — sweeps up the
customer's own account number and yesterday's data, and hands a downstream flow
a reference this crawl never created.

Each refusal is asserted WITH ITS CONTROL, because "the registry answered
nothing" is satisfied just as well by a registry that never answers.
"""
from __future__ import annotations

import pytest

from app.minted import (MAX_REFERENCES, MintRegistry, is_reference_label,
                        looks_like_a_reference)


def _pair(label, value):
    return {"label": label, "value": value}


# ── the whole point: a submitted application hands its number downstream ───

def test_a_reference_the_submit_created_is_available_to_the_next_flow():
    """THE POINT OF THE RUNG, end to end and in four lines."""
    reg = MintRegistry()
    reg.observe([_pair("Product", "Term Life")])          # the apply page
    reg.mint([_pair("Application Number", "APP-2026-8871")])  # the confirmation
    assert reg.value_for("Application Number") == "APP-2026-8871"


def test_the_service_flow_finds_the_policy_the_apply_flow_issued():
    reg = MintRegistry()
    reg.mint([_pair("Policy Number", "POL-44120")])
    # A later flow's field is worded differently, as they always are.
    assert reg.value_for("Policy number") == "POL-44120"
    assert reg.value_for("Your Policy Number") == "POL-44120"


@pytest.mark.parametrize("label,value", [
    ("Policy Number", "POL-44120"),
    ("Claim Reference", "CLM-2026-0091"),
    ("Quote ID", "Q-889321"),
    ("Confirmation Number", "8871-2026"),
    ("Case #", "CASE-10023"),
    ("Order No", "ORD-55019"),
])
def test_the_references_financial_applications_actually_mint(label, value):
    reg = MintRegistry()
    assert reg.mint([_pair(label, value)]) == [label.lower().rstrip(":#").strip()]
    assert reg.value_for(label) == value


# ── act-then-diff: the rule that separates rung 4 from rung 3 ──────────────

def test_a_value_already_on_the_page_before_the_action_was_not_minted_by_it():
    """THE ONE THAT MATTERS. The customer's own account number is printed on
    the confirmation screen beside the new application number. Crediting it to
    the crawl would hand a downstream flow a reference this crawl never
    created — and the flow would dead-end on a validation the application was
    right to raise."""
    reg = MintRegistry()
    reg.observe([_pair("Account Number", "ACCT-0001")])   # already theirs
    reg.mint([_pair("Account Number", "ACCT-0001"),
              _pair("Application Number", "APP-2026-8871")])
    assert reg.value_for("Account Number") is None
    assert reg.value_for("Application Number") == "APP-2026-8871"


def test_the_control_for_the_diff_the_same_value_mints_when_it_is_new():
    """FALSIFICATION CONTROL. Without it, a registry that minted NOTHING — a
    broken label rule, an inverted diff — would satisfy the test above and look
    like a working causation rule."""
    reg = MintRegistry()
    reg.mint([_pair("Account Number", "ACCT-0001")])
    assert reg.value_for("Account Number") == "ACCT-0001"


def test_a_second_sighting_is_not_a_second_mint():
    reg = MintRegistry()
    reg.mint([_pair("Policy Number", "POL-1")])
    assert reg.mint([_pair("Policy Number", "POL-1")]) == []
    assert reg.count == 1


def test_observing_a_page_never_mints_anything_by_itself():
    """``observe`` is the baseline half. If it could mint, every id on every
    ordinary page would be credited to the crawl and the rung would collapse
    into a worse version of harvest."""
    reg = MintRegistry()
    reg.observe([_pair("Policy Number", "POL-1")])
    assert reg.count == 0
    assert reg.value_for("Policy Number") is None


# ── the label carries the evidence ─────────────────────────────────────────

@pytest.mark.parametrize("label", [
    "Annual Income", "Coverage Amount", "Date of Birth", "Height",
    "Premium", "Age", "Weight",
])
def test_a_field_that_merely_holds_digits_is_not_a_reference(label):
    """These sit on the same confirmation panel as the real reference and are
    the same shape. The LABEL is what separates them."""
    assert not is_reference_label(label)


@pytest.mark.parametrize("label", [
    "Policy Number", "Claim Reference", "Application ID", "Quote ID",
    "Confirmation Code", "Member Number", "Certificate No",
])
def test_the_control_for_labels_a_real_reference_label_is_recognised(label):
    """FALSIFICATION CONTROL for the seven refusals above."""
    assert is_reference_label(label)


@pytest.mark.parametrize("value", [
    "12/05/2026",        # a date
    "10:45",             # a time
    "$1,200.00",         # money
    "18%",               # a percentage
    "7",                 # a row count
    "Approved",          # prose, no digit
    "Thank you for submitting your application today number 5",  # a sentence
])
def test_a_value_that_is_not_an_identifier_is_refused(value):
    assert not looks_like_a_reference(value)


def test_the_control_for_values_a_real_reference_shape_passes():
    """FALSIFICATION CONTROL for the seven refusals above."""
    for value in ("APP-2026-8871", "POL-44120", "8871-2026", "Q-889321"):
        assert looks_like_a_reference(value)


def test_a_date_labelled_as_a_reference_is_still_refused():
    """BOTH gates must hold. A confirmation panel renders "Reference Date:
    12/05/2026" right beside the reference itself."""
    reg = MintRegistry()
    reg.mint([_pair("Reference Date", "12/05/2026")])
    assert reg.count == 0


# ── strict matching, for harvest's reason ──────────────────────────────────

def test_a_policy_number_is_never_typed_into_a_claim_reference_field():
    """A loose match would open the downstream flow with the wrong key, and the
    application would be right to reject it."""
    reg = MintRegistry()
    reg.mint([_pair("Policy Number", "POL-1")])
    assert reg.value_for("Claim Reference") is None


def test_a_value_the_application_already_rejected_is_not_offered_again():
    reg = MintRegistry()
    reg.mint([_pair("Policy Number", "POL-1")])
    assert reg.value_for("Policy Number", refused=["POL-1"]) is None


def test_an_unrelated_field_gets_nothing():
    reg = MintRegistry()
    reg.mint([_pair("Policy Number", "POL-1")])
    assert reg.value_for("First Name") is None
    assert reg.value_for("") is None


# ── bounds and evidence ────────────────────────────────────────────────────

def test_the_registry_does_not_grow_without_bound():
    reg = MintRegistry(max_references=3)
    reg.mint([_pair(f"Policy {i} Number", f"POL-{i}0000") for i in range(50)])
    assert reg.count == 3


def test_evidence_learns_the_labels_and_the_count_never_the_values():
    """A minted reference is real client-system data. The QUESTION travels to
    evidence; the ANSWER does not — the same line the rest of the pipeline
    holds."""
    reg = MintRegistry()
    reg.mint([_pair("Policy Number", "POL-SECRET-44120")])
    assert reg.count == 1
    assert reg.labels() == ["policy number"]
    assert "POL-SECRET-44120" not in repr(reg.labels())


def test_nothing_here_raises_on_junk():
    """A malformed page must not stop a crawl."""
    reg = MintRegistry()
    reg.observe(None)
    reg.mint(None)
    reg.mint([{}, {"label": None, "value": None}, {"value": "x"}])
    assert reg.count == 0
    assert MAX_REFERENCES > 0
