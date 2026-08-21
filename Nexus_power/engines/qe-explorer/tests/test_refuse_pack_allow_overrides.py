"""The reviewed carve-out in `allow_overrides`, pinned from both sides.

`allow_overrides` is the refuse pack's ONLY escape hatch: a match here flips an
irreversible verdict back to allowed, ahead of every `irreversible_verbs` rule.
The pack shipped it EMPTY by design, and its own header says adding a row is "an
auditable, human-reviewed decision".

This module is that audit. It asserts what the row PERMITS and — at greater
length, because this is the half that matters — what it must still REFUSE. An
override tested only on the labels it was written for proves nothing about the
labels it was not.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.guard import classify_action_verb, load_refuse_pack

_PACK = load_refuse_pack(Settings().refuse_pack_path)

#: The row under audit.
_OVERRIDE_ID = "rp.allow.destination_advance_step"


def _verdict(name: str):
    return classify_action_verb(name, "https://app.example/step", _PACK)


# ── what it permits ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("label", [
    "Continue to Underwriting Decision",   # vkpower-life, lifestyle step
    "Continue to Payment",                 # vkpower-life, decision step
    "Continue to Signature",               # vkpower-life, beneficiary step
    "Proceed to Underwriting",
    "Next to Payment",
    "continue to the underwriting review",  # case + article + qualifier
])
def test_a_destination_shaped_advance_to_a_named_step_is_navigation(label):
    """Each of these changes route and does nothing the step is named after.

    Measured from vkpower-life's own source: the lifestyle step's handler is
    `router.push('/life-insurance/apply/decision/')`. Flagged danger, it is
    removed from every advance tier and the funnel's last four steps become
    unreachable -- so the crawl cannot complete a journey on the application at
    all.
    """
    verdict = _verdict(label)
    assert verdict.irreversible is False, (
        "%r was refused by %r. It is a destination advance: it navigates to a "
        "step, and the funnel cannot be walked past it."
        % (label, verdict.rule_id))
    assert verdict.rule_id == _OVERRIDE_ID


# ── what it must STILL refuse ───────────────────────────────────────────────

@pytest.mark.parametrize("label,why", [
    ("Submit to Underwriting",
     "the COMMIT form -- rp.verb.underwrite matches it explicitly"),
    ("Sign & Submit Application",
     "the real commit control on the application this row was written for"),
    ("Underwrite Now",
     "an imperative act, not a destination"),
    ("Continue to Underwriting and Bind",
     "carries a second irreversible verb; full-string anchoring must reject it"),
    ("Continue to Pay Now",
     "'pay' is not an enumerated destination noun -- only 'payment' is"),
    ("Continue to Sign the Policy",
     "'sign' is not enumerated -- only 'signature' is"),
    ("Continue to Payment and Confirm",
     "trailing commit word; not a full-string match"),
    ("Pay Now",
     "no advance word, no destination -- an ordinary commit"),
    ("Delete Application",
     "an unrelated irreversible verb must be untouched by this row"),
])
def test_the_override_does_not_reach_a_control_that_commits(label, why):
    """The half of the review that an override is usually not given.

    Every label here is one word away from something the row permits, which is
    exactly where a carve-out fails: not on the case it was written for, but on
    its neighbours.
    """
    verdict = _verdict(label)
    assert verdict.irreversible is True, (
        "%r was ALLOWED by %r, and it must not be: %s"
        % (label, verdict.rule_id, why))
    assert verdict.rule_id != _OVERRIDE_ID, (
        "%r was allowed by the destination-advance override: %s" % (label, why))


@pytest.mark.parametrize("label", [
    "Continue to Checkout",       # 'checkout' was never observed as a step name
    "Continue to Order",
    "Continue to Confirmation",
    "Continue to Delivery",
])
def test_the_override_never_claims_a_noun_nobody_measured(label):
    """A row that CLAIMS a label takes responsibility for it.

    These are not refused by the pack today -- no `irreversible_verbs` rule
    matches them -- so asserting a refusal here would be testing the pack's
    baseline rather than this row. What must hold is narrower and is the actual
    property: the override does not REACH them. The noun list was enumerated
    from three measured step names, and a fourth arriving by accident is how an
    enumerated list quietly becomes an open one.
    """
    assert _verdict(label).rule_id != _OVERRIDE_ID, (
        "%r was claimed by the destination-advance override. Its destination "
        "noun was never observed on any application; widening the list is a "
        "separate auditable decision." % label)


# ── the property the row rests on ───────────────────────────────────────────

def test_the_override_agrees_with_the_tier_2_shape_rule():
    """The row is only defensible because Tier 2 already draws this line.

    `is_destination_advance` permits a commit word in an advance label ONLY as a
    destination -- advance word, preposition, then the commit word strictly
    after it. On vkpower-life it returns True for every forward control in the
    funnel and False for the control that actually commits. That separation is
    what this row lets the refuse pack agree with; if it ever stopped holding,
    the row would be unjustified and this test says so.
    """
    from app.vocab import is_destination_advance

    for navigation in ("Continue to Underwriting Decision", "Continue to Payment",
                       "Continue to Signature"):
        assert is_destination_advance(navigation) is True, (
            "%r is no longer a Tier-2 destination advance, so the allow-override "
            "that permits it has lost its justification." % navigation)

    assert is_destination_advance("Sign & Submit Application") is False, (
        "the commit control now reads as a destination advance -- the "
        "discriminator this override rests on has broken.")


def test_the_pack_ships_exactly_one_reviewed_override():
    """A carve-out list that grows without anyone noticing is how the escape
    hatch becomes the rule. Adding a row is meant to be an auditable decision,
    so the count is pinned and a second row has to come with its own audit."""
    ids = [rule.id for rule in _PACK.allow_overrides]
    assert ids == [_OVERRIDE_ID], (
        "allow_overrides is %s. Every row here defeats the refuse pack for the "
        "labels it matches; each one needs its own review and its own tests."
        % ids)
