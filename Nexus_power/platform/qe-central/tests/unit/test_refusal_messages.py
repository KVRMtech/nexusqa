"""Fix B: enumerated refusals → client-friendly, actionable messages.

Guards that the humane translation stays COMPLETE (every enumerated reason mapped)
and stays HONEST (never leaks the raw code; always tells the operator what to do).
"""
from app.clients.refusal_messages import _DEFAULT, client_refusal_message
from app.substrate.schema import REFUSAL_REASONS


def test_every_reason_has_a_specific_message():
    """A new REFUSAL_REASON can't silently fall through to the generic fallback."""
    for reason in REFUSAL_REASONS:
        msg = client_refusal_message(reason)
        assert msg and msg != _DEFAULT, f"{reason!r} has no specific client message"


def test_messages_never_leak_the_raw_code_and_stay_actionable():
    for reason in REFUSAL_REASONS:
        msg = client_refusal_message(reason)
        assert reason not in msg, f"{reason!r} leaks its raw code into the message"
        assert any(
            w in msg.lower()
            for w in ("re-crawl", "provide", "check", "contact", "start a fresh")
        ), f"{reason!r} message is not actionable: {msg!r}"


def test_unknown_or_empty_code_falls_back_safely():
    assert client_refusal_message("totally_unknown") == _DEFAULT
    assert client_refusal_message("") == _DEFAULT
    assert client_refusal_message(None) == _DEFAULT  # type: ignore[arg-type]


def test_missing_fill_value_reads_like_guidance_not_a_stacktrace():
    msg = client_refusal_message("missing_fill_value")
    assert "missing_fill_value" not in msg
    assert ("dropdown" in msg.lower() or "field" in msg.lower())
    assert "re-crawl" in msg.lower()
