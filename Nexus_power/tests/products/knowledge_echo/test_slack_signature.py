"""Slack signature verification — replay protection + HMAC."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from app.slack.signature import (
    SlackSignatureInvalid,
    SlackSignatureMissing,
    SlackSignatureReplay,
    verify_slack_signature,
)


SECRET = "test-signing-secret"


def _sign(secret: str, ts: str, body: bytes) -> str:
    base = f"v0:{ts}:".encode("utf-8") + body
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


def test_valid_signature_passes() -> None:
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    sig = _sign(SECRET, ts, body)
    verify_slack_signature(
        signing_secret=SECRET,
        timestamp=ts,
        received_signature=sig,
        body=body,
    )


def test_invalid_signature_rejected() -> None:
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    with pytest.raises(SlackSignatureInvalid):
        verify_slack_signature(
            signing_secret=SECRET,
            timestamp=ts,
            received_signature="v0=deadbeef",
            body=body,
        )


def test_tampered_body_rejected() -> None:
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    sig = _sign(SECRET, ts, body)
    with pytest.raises(SlackSignatureInvalid):
        verify_slack_signature(
            signing_secret=SECRET,
            timestamp=ts,
            received_signature=sig,
            body=body + b" x",  # tampered after signing
        )


def test_missing_signature_header_rejected() -> None:
    with pytest.raises(SlackSignatureMissing):
        verify_slack_signature(
            signing_secret=SECRET,
            timestamp=str(int(time.time())),
            received_signature=None,
            body=b"{}",
        )


def test_missing_timestamp_rejected() -> None:
    with pytest.raises(SlackSignatureMissing):
        verify_slack_signature(
            signing_secret=SECRET,
            timestamp=None,
            received_signature="v0=...",
            body=b"{}",
        )


def test_replay_outside_window_rejected() -> None:
    body = b"{}"
    old_ts = str(int(time.time()) - 600)
    sig = _sign(SECRET, old_ts, body)
    with pytest.raises(SlackSignatureReplay):
        verify_slack_signature(
            signing_secret=SECRET,
            timestamp=old_ts,
            received_signature=sig,
            body=body,
            max_age_seconds=300,
        )


def test_non_numeric_timestamp_rejected() -> None:
    with pytest.raises(SlackSignatureInvalid):
        verify_slack_signature(
            signing_secret=SECRET,
            timestamp="not-a-number",
            received_signature="v0=abc",
            body=b"{}",
        )


def test_signing_secret_required() -> None:
    with pytest.raises(SlackSignatureMissing):
        verify_slack_signature(
            signing_secret="",
            timestamp=str(int(time.time())),
            received_signature="v0=abc",
            body=b"{}",
        )
