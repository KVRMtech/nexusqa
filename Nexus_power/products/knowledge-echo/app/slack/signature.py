"""Slack request signature verification.

Slack signs every webhook with:

    v0:{timestamp}:{raw_body}

HMAC-SHA256'd with the workspace's signing secret. The resulting hex
digest is sent as ``X-Slack-Signature: v0=<hex>``. We:

1. Reject requests older than ``max_age_seconds`` (replay protection).
2. Reconstruct the basestring and HMAC.
3. Compare with ``hmac.compare_digest`` (constant time).

These are the production-mandatory checks. A request that fails any
of them never touches the orchestrator.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


_VERSION = "v0"


class SlackSignatureError(Exception):
    """Base class for verification failures."""


class SlackSignatureMissing(SlackSignatureError):
    """Required headers are absent."""


class SlackSignatureReplay(SlackSignatureError):
    """Timestamp is outside the configured replay window."""


class SlackSignatureInvalid(SlackSignatureError):
    """HMAC did not match."""


@dataclass(frozen=True)
class _Inputs:
    timestamp: str
    received_sig: str
    body: bytes


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp: Optional[str],
    received_signature: Optional[str],
    body: bytes,
    max_age_seconds: int = 300,
    now_epoch: Optional[float] = None,
) -> None:
    """Raise on failure. Returns None on success.

    Splitting verification from the request handler keeps the rule set
    pure: the same function is used by the FastAPI route, by tests, and
    by any tool that wants to replay a captured event.
    """
    if not signing_secret:
        raise SlackSignatureMissing("signing_secret is required")
    if not timestamp:
        raise SlackSignatureMissing(
            "missing X-Slack-Request-Timestamp header"
        )
    if not received_signature:
        raise SlackSignatureMissing(
            "missing X-Slack-Signature header"
        )

    try:
        ts_epoch = int(timestamp)
    except ValueError as exc:
        raise SlackSignatureInvalid(
            f"non-numeric timestamp: {timestamp!r}"
        ) from exc

    current = now_epoch if now_epoch is not None else time.time()
    age = abs(int(current) - ts_epoch)
    if age > max_age_seconds:
        raise SlackSignatureReplay(
            f"timestamp age {age}s exceeds max_age {max_age_seconds}s"
        )

    expected = _compute_signature(signing_secret, timestamp, body)
    if not hmac.compare_digest(expected, received_signature):
        raise SlackSignatureInvalid("HMAC mismatch")


def _compute_signature(signing_secret: str, timestamp: str, body: bytes) -> str:
    basestring = f"{_VERSION}:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(
        signing_secret.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()
    return f"{_VERSION}={digest}"
