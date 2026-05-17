"""Email plugin: SNS parsing, SES parser, composer."""

from __future__ import annotations

import json

import pytest

from app.email.composer import EmailComposer, EmailComposerContext
from app.email.parser import (
    EmailInboundError,
    SesVerdictFailure,
    parse_ses_email_event,
)
from app.email.sns import (
    SnsSignatureError,
    parse_sns_notification,
    subscription_confirmation,
)
from app.matcher import MatchCandidate, MatchResult


def _candidate(similarity: float = 0.91) -> MatchCandidate:
    return MatchCandidate(
        node_id="n1",
        node_type="TranscriptSegment",
        similarity=similarity,
        text="California cigar lookback is 24 months.",
        speaker_id="priya",
        speaker_role="underwriting",
        session_id="sess-1",
        artifact_id="art-1",
        start_ms=1000,
        end_ms=5000,
        ordinal=3,
        product_ids=("lt5",),
        raw={},
    )


def _match(sim: float = 0.91) -> MatchResult:
    band = "high" if sim >= 0.85 else "medium"
    return MatchResult(
        candidates=[_candidate(sim)], top_similarity=sim, confidence_band=band  # type: ignore[arg-type]
    )


# ── SNS parser ─────────────────────────────────────────────────


def _sns_envelope(extra: dict | None = None) -> dict:
    base = {
        "Type": "Notification",
        "MessageId": "abc-123",
        "TopicArn": "arn:aws:sns:us-east-1:111:nexus-email",
        "Subject": "Amazon SES Email Receipt Notification",
        "Message": "{}",
        "Timestamp": "2026-05-12T10:00:00.000Z",
        "Signature": "deadbeef",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-foo.pem",
    }
    if extra:
        base.update(extra)
    return base


def test_sns_parser_recognises_notification() -> None:
    n = parse_sns_notification(_sns_envelope())
    assert n.type == "Notification"
    assert n.topic_arn.endswith("nexus-email")


def test_sns_parser_recognises_subscription_confirmation() -> None:
    env = _sns_envelope(
        {
            "Type": "SubscriptionConfirmation",
            "Token": "tok-xyz",
            "SubscribeURL": "https://sns.us-east-1.amazonaws.com/confirm",
        }
    )
    n = parse_sns_notification(env)
    sc = subscription_confirmation(n)
    assert sc is not None
    assert sc.token == "tok-xyz"


def test_sns_parser_rejects_missing_fields() -> None:
    body = _sns_envelope()
    body.pop("Signature")
    with pytest.raises(SnsSignatureError):
        parse_sns_notification(body)


def test_sns_parser_rejects_non_json() -> None:
    with pytest.raises(SnsSignatureError):
        parse_sns_notification(b"not-json")


# ── SES parser ─────────────────────────────────────────────────


def _ses_event(*, verdicts: dict | None = None, extra: dict | None = None) -> dict:
    v = {
        "spfVerdict": {"status": "PASS"},
        "dkimVerdict": {"status": "PASS"},
        "dmarcVerdict": {"status": "PASS"},
    }
    if verdicts:
        v.update(verdicts)
    base = {
        "notificationType": "Received",
        "mail": {
            "messageId": "<msg-1@nexus.example>",
            "source": "jordan@nexus.example",
            "commonHeaders": {
                "from": ["Jordan <jordan@nexus.example>"],
                "to": ["t47@inbox.nexus.ai"],
                "subject": "How does CA tobacco lookback work?",
            },
            "headers": [
                {"name": "Message-ID", "value": "<msg-1@nexus.example>"},
                {"name": "In-Reply-To", "value": "<prior-1@nexus.example>"},
                {
                    "name": "References",
                    "value": "<a@x> <b@y>",
                },
            ],
        },
        "receipt": v,
    }
    if extra:
        base.update(extra)
    return base


def test_ses_parser_extracts_essentials() -> None:
    parsed = parse_ses_email_event(_ses_event())
    assert parsed.from_addr == "jordan@nexus.example"
    assert parsed.subject.startswith("How does CA tobacco")
    assert parsed.in_reply_to == "<prior-1@nexus.example>"
    assert parsed.references == ("<a@x>", "<b@y>")
    assert parsed.message_id == "<msg-1@nexus.example>"


def test_ses_parser_rejects_dkim_fail() -> None:
    event = _ses_event(verdicts={"dkimVerdict": {"status": "FAIL"}})
    with pytest.raises(SesVerdictFailure):
        parse_ses_email_event(event)


def test_ses_parser_rejects_unknown_notification_type() -> None:
    event = _ses_event()
    event["notificationType"] = "Sent"
    with pytest.raises(EmailInboundError):
        parse_ses_email_event(event)


def test_ses_parser_accepts_json_string_message() -> None:
    event = _ses_event()
    parsed = parse_ses_email_event(json.dumps(event))
    assert parsed.subject.startswith("How does CA tobacco")


def test_ses_parser_requires_subject_or_body() -> None:
    event = _ses_event()
    event["mail"]["commonHeaders"]["subject"] = ""
    # Provide no body either.
    with pytest.raises(EmailInboundError):
        parse_ses_email_event(event)


# ── Composer ───────────────────────────────────────────────────


def test_email_composer_returns_none_when_no_match() -> None:
    composer = EmailComposer()
    out = composer.compose(
        dispatch_id="d1",
        question_text="q",
        match=MatchResult(candidates=[], top_similarity=0.0, confidence_band="none"),
    )
    assert out is None


def test_email_composer_returns_subject_and_html_and_text() -> None:
    composer = EmailComposer()
    out = composer.compose(
        dispatch_id="d1",
        question_text="What is the CA tobacco lookback?",
        match=_match(0.95),
    )
    assert out is not None
    p = out.payload
    assert p["subject"]
    assert "<!doctype html>" in p["html_body"].lower()
    assert "What is the CA tobacco" in p["text_body"]
    assert p["dispatch_id"] == "d1"
    # In-Reply-To is None when context isn't supplied.
    assert p["in_reply_to"] is None
    assert p["references"] == []


def test_email_composer_with_context_threads_reply() -> None:
    composer = EmailComposer()
    ctx = EmailComposerContext(
        in_reply_to="<original-1@x>",
        references=("<original-1@x>",),
        original_subject="CA tobacco question",
        asker_name="Jordan",
    )
    scoped = composer.with_context(ctx)
    out = scoped.compose(
        dispatch_id="d1",
        question_text="What is the CA tobacco lookback?",
        match=_match(0.95),
    )
    assert out is not None
    p = out.payload
    assert p["subject"].lower().startswith("re:")
    assert p["in_reply_to"] == "<original-1@x>"
    assert p["references"] == ["<original-1@x>"]
    assert "Jordan" in p["text_body"]


def test_email_composer_html_escapes_user_input() -> None:
    composer = EmailComposer()
    candidate = MatchCandidate(
        node_id="n",
        node_type="TranscriptSegment",
        similarity=0.95,
        text="<script>alert(1)</script>",
        speaker_id="<b>Boss</b>",
        speaker_role="director",
        session_id="s",
        artifact_id="a",
        start_ms=0,
        end_ms=1,
        ordinal=0,
        product_ids=(),
        raw={},
    )
    match = MatchResult(
        candidates=[candidate], top_similarity=0.95, confidence_band="high"
    )
    out = composer.compose(
        dispatch_id="d1", question_text="hi & bye", match=match
    )
    assert out is not None
    html_body = out.payload["html_body"]
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "hi &amp; bye" in html_body
