"""AN E-SIGNATURE THAT COMPLETED IS A TERMINAL BUSINESS EVENT.

`_SUCCESS_RE` is deliberately short — every term in it is one an application
only renders when something WORKED, and "processing"/"pending"/"please wait"
are excluded on purpose. That conservatism is right and is kept here.

WHAT IT DID NOT COVER. A completed signature. Measured on LifeOps (client,
2026-08-27): the approved `Sign` crossing, once its PIN challenge was answered,
produced a genuinely NEW status region —

    before: []
    after : ["Electronic Delivery Consent signed and retained in the vault."]

and the document's own row flipped to Signed. The commit completed. But the
transition matched no success term, so the milestone recorded
`rung=(none) verified=False` for an event the application had just declared.

The pack itself calls signing irreversible (`rp.verb.sign`), so a signature
that completed is exactly the kind of terminal event this ladder exists to
recognise.

TWO NARROW ADDITIONS, both requiring COMPLETION, never intent:

  * an auxiliary construction with the domain's terminal verbs
    ("was signed", "has been issued", "were bound"); and
  * a past participle joined to a RETENTION verb ("signed and retained",
    "issued and filed") — the banner form that states an act and its durable
    consequence in one line.

WHAT MUST STILL NOT CONFIRM. The negations and the in-flight states, asserted
at greater length than the allowances, because a success vocabulary that
matches "not signed" is worse than none.
"""
from __future__ import annotations

import pytest

from app.boundary import _SUCCESS_RE, confirmation_transition


def _hit(text: str) -> bool:
    return bool(_SUCCESS_RE.search(text))


@pytest.mark.parametrize("text", [
    # THE MEASURED BANNER, verbatim.
    "Electronic Delivery Consent signed and retained in the vault.",
    "Illustration Receipt signed and retained in the vault.",
    "Contract issued and filed to the document vault.",
    "The application was signed.",
    "Your policy has been issued.",
    "The premium was paid.",
])
def test_a_completed_terminal_event_is_success_shaped(text):
    assert _hit(text), f"a completed business event was not recognised: {text!r}"


@pytest.mark.parametrize("text,why", [
    ("Document not signed.", "a negation"),
    ("This document is not signed yet.", "a negation"),
    ("No signed documents yet.", "a negation, and the app's real empty state"),
    ("Awaiting signature.", "in flight, not complete"),
    ("Signature pending.", "in flight, not complete"),
    ("Ready to sign.", "an invitation, not an event"),
    ("Sign document", "a BUTTON label — an offer, never a statement"),
    ("Processing your signature.", "explicitly in flight"),
    ("You will receive a signed copy by email.", "a future promise"),
])
def test_the_vocabulary_still_refuses_everything_short_of_completion(text, why):
    assert not _hit(text), f"{text!r} was treated as success — {why}"


def test_the_original_vocabulary_is_unchanged():
    """The terms that were already trusted must keep working."""
    for text in ["Application submitted successfully",
                 "Thank you for your application",
                 "Your request has been received",
                 "Confirmation number AL-4471"]:
        assert _hit(text), text
    for text in ["Processing…", "Please wait", "Your request is pending"]:
        assert not _hit(text), text


def test_a_requirement_is_not_a_confirmation_even_carrying_the_word():
    """THE GUARD LIVES IN THE CONSUMER, not the vocabulary.

    "PIN confirmation is required to create an auditable electronic signature
    event" contains `confirmation`, so the bare success regex matches it — and
    always has. It is the text of LifeOps' CHALLENGE modal, and it declares
    that the act has NOT happened. `confirmation_transition` therefore refuses
    any candidate that is rejection-shaped, the same polarity test the submit
    classifier applies.
    """
    challenge = ("PIN confirmation is required to create an auditable "
                 "electronic signature event.")
    assert _SUCCESS_RE.search(challenge), "premise: the bare regex does match it"
    detail, rung = confirmation_transition(
        [], [], aria_before=[], aria_after=[challenge], control_names=())
    assert (detail, rung) == ("", ""), "a requirement was credited as a confirmation"


def test_the_measured_signature_banner_now_confirms():
    """END TO END, with the strings measured on the live client application."""
    detail, rung = confirmation_transition(
        [], [], aria_before=[],
        aria_after=["Electronic Delivery Consent signed and retained in the vault."],
        control_names=("Preview", "Sign", "Continue"))
    assert rung == "aria_status", (detail, rung)
    assert "signed and retained" in detail
