"""ASKING A HUMAN IS A TYPED EXCEPTION, NOT THE DEFAULT.

Before this, every field the crawl could not fill produced the same output:
"provide a value and re-crawl". A genuine limit ("this needs a policy number that
exists in your system") and a field the agent simply failed to derive were
presented identically, so the client was asked to do the platform's work in both
cases and had no way to tell which was which.

Only a few reasons genuinely require a person: an identifier that must already
exist, a one-time code, a captcha, a hardware token, a legal signature.
EVERYTHING else is a gap on our side and is reported as one.

The bias in this module is deliberate and is pinned below: an ambiguous field
falls through to ``agent_gap``. A false "we need your help" is the worse error —
it tells a client the platform cannot do something it could in fact do, and it
lets our own gaps hide inside their to-do list forever.
"""
from __future__ import annotations

import pytest

from app.services.assist_classifier import (
    ASSIST_AGENT_GAP,
    ASSIST_CAPTCHA,
    ASSIST_ENTERPRISE_ACCOUNT,
    ASSIST_HARDWARE_TOKEN,
    ASSIST_LEGAL_APPROVAL,
    ASSIST_SECOND_FACTOR,
    HUMAN_REQUIRED,
    classify_assist_reason,
    fill_headline,
    summarize_fill,
)


def _f(name: str, **over) -> dict:
    e = {"name": name, "filled": False, "semantic_type": ""}
    e.update(over)
    return e


# ── the five genuine reasons ────────────────────────────────────────────────

@pytest.mark.parametrize("label", [
    "Policy Number", "Member Number", "Claim Number", "Account Number",
    "Contract number", "Certificate No", "Subscriber ID", "Member #",
])
def test_an_identifier_that_must_already_exist_needs_the_client(label):
    """No generator can invent a policy number that RESOLVES. Inventing one
    produces a test that passes against nothing, which is worse than asking."""
    assert classify_assist_reason(_f(label)) == ASSIST_ENTERPRISE_ACCOUNT


@pytest.mark.parametrize("label", [
    "One-time passcode", "Verification code", "SMS code", "OTP",
    "Authentication code", "Enter your 2FA code",
])
def test_a_one_time_code_needs_the_client(label):
    assert classify_assist_reason(_f(label)) == ASSIST_SECOND_FACTOR


def test_the_explorers_own_semantic_type_is_trusted_over_the_label():
    """The crawler already classifies fields. Where it has spoken, that beats any
    label matching here — a localised OTP field no English pattern can read is
    still correctly typed."""
    assert classify_assist_reason(
        _f("Código", semantic_type="one_time_code")) == ASSIST_SECOND_FACTOR


@pytest.mark.parametrize("label", ["CAPTCHA", "Enter the reCAPTCHA", "I'm not a robot"])
def test_a_captcha_needs_the_client(label):
    assert classify_assist_reason(_f(label)) == ASSIST_CAPTCHA


@pytest.mark.parametrize("label", [
    "Hardware token", "Security key", "YubiKey", "Approve on your device",
    "Approve in the app", "Push notification",
])
def test_a_device_approval_needs_the_client(label):
    assert classify_assist_reason(_f(label)) == ASSIST_HARDWARE_TOKEN


@pytest.mark.parametrize("label", [
    "Electronic signature", "e-Sign", "Sign here", "I certify that",
    "Signature",
])
def test_a_legal_signature_needs_the_client(label):
    assert classify_assist_reason(_f(label)) == ASSIST_LEGAL_APPROVAL


# ── the bias: ambiguity is OUR problem ──────────────────────────────────────

@pytest.mark.parametrize("label", [
    "First name", "Annual income", "Employer", "Street address", "Coverage amount",
    # Near-misses that must NOT read as enterprise identifiers:
    "Account type", "Policy term", "Member since", "Number of dependents",
    "Claim reason", "Reference", "", "   ",
])
def test_anything_else_is_recorded_as_a_gap_on_our_side(label):
    """THE BIAS THAT MATTERS. A false 'we need your help' tells a client the
    platform cannot do something it could — and lets our own gaps live in their
    to-do list forever. Ambiguity resolves toward our problem."""
    assert classify_assist_reason(_f(label)) == ASSIST_AGENT_GAP


def test_a_gap_is_never_counted_as_a_human_requirement():
    assert ASSIST_AGENT_GAP not in HUMAN_REQUIRED


# ── the summary ─────────────────────────────────────────────────────────────

def _coverage(entries: list[dict]) -> dict:
    return {"field_ledger": entries}


def test_the_summary_separates_what_we_did_from_what_we_need():
    cov = _coverage([
        _f("First name", filled=True, provenance="synthesized"),
        _f("Last name", filled=True, provenance="synthesized"),
        _f("Email", filled=True, provenance="journey"),
        _f("Date of birth", filled=True, provenance="recalled"),
        _f("Policy Number"),
        _f("Coverage preference"),
    ])
    s = summarize_fill(cov)
    assert s["discovered"] == 6
    assert s["auto_filled"] == 4
    assert [a["name"] for a in s["needs_assistance"]] == ["Policy Number"]
    assert [g["name"] for g in s["agent_gaps"]] == ["Coverage preference"]


def test_provenance_is_broken_out_so_a_green_result_can_be_read():
    """Autonomy decides how far the crawl got; provenance decides what the green
    MEANS. Four fields filled from the client's own data is a different claim
    from four invented ones, and the summary must not flatten them."""
    s = summarize_fill(_coverage([
        _f("A", filled=True, provenance="provided"),
        _f("B", filled=True, provenance="synthesized"),
        _f("C", filled=True, provenance="synthesized"),
    ]))
    assert s["provenance"] == {"provided": 1, "synthesized": 2}


def test_the_same_question_on_several_pages_is_asked_about_once():
    """A funnel repeats its questions. Listing "Policy Number" five times reads
    as five problems."""
    s = summarize_fill(_coverage([_f("Policy Number") for _ in range(5)]))
    assert len(s["needs_assistance"]) == 1


def test_an_empty_crawl_summarises_without_inventing_anything():
    for cov in (None, {}, {"field_ledger": None}, {"field_ledger": "nope"}):
        s = summarize_fill(cov)
        assert s == {"discovered": 0, "auto_filled": 0, "needs_assistance": [],
                     "agent_gaps": [], "provenance": {}}


# ── the headline ────────────────────────────────────────────────────────────

def test_the_headline_leads_with_the_work_already_done():
    """The old copy led with the ask ("provide 15 values"), which reads as though
    the crawl achieved nothing. Leading with the 13 is not spin — it is the
    larger and more decision-relevant number."""
    text = fill_headline(summarize_fill(_coverage(
        [_f(f"Field {i}", filled=True, provenance="synthesized") for i in range(13)]
        + [_f("Policy Number"), _f("Member Number")])))
    assert text.startswith("Automatically populated 13 of 15 fields.")
    assert "Policy Number" in text and "Member Number" in text


def test_the_headline_says_so_when_nothing_is_needed():
    text = fill_headline(summarize_fill(_coverage(
        [_f("A", filled=True, provenance="synthesized")])))
    assert "Nothing needs your input." in text


def test_the_headline_never_tells_the_client_to_re_crawl():
    """THE DEFECT IN THE OLD COPY. A data gap forced a full DISCOVERY restart —
    the pages were already known. Nothing in this copy may reintroduce that."""
    for entries in (
        [_f("Policy Number")],
        [_f("Coverage preference")],
        [_f("A", filled=True, provenance="synthesized"), _f("CAPTCHA")],
        [],
    ):
        text = fill_headline(summarize_fill(_coverage(entries))).lower()
        assert "re-crawl" not in text and "re-run" not in text
        assert "crawl again" not in text


def test_an_agent_gap_is_named_as_ours_in_the_copy():
    text = fill_headline(summarize_fill(_coverage([_f("Coverage preference")])))
    assert "our side" in text.lower()
