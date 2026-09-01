"""HMAC-SHA256 signing for inbound + outbound webhook bodies.

Inbound:
    Header ``X-Nexus-Signature: t=<unix_ts>,v1=<hex_hmac>``
    Basestring: ``t={timestamp}\n{raw_body}``
    Replay protection: timestamp must be within ``max_age_seconds``.

Outbound:
    Same scheme; the outbound POSTer signs with the tenant's shared
    secret. Receivers verify with the same module to round-trip.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional


class WebhookSignatureError(Exception):
    """Verification failed: missing, malformed, replay, or tampered."""


_HEADER_NAME = "X-Nexus-Signature"
_TIMESTAMP_KEY = "t"
_SIGNATURE_KEY = "v1"


def sign_webhook_body(
    *,
    secret: str,
    body: bytes,
    timestamp: Optional[int] = None,
) -> tuple[str, int]:
    """Produce a (header_value, timestamp) tuple suitable for outbound."""
    if not secret:
        raise WebhookSignatureError("secret is required to sign")
    ts = int(timestamp if timestamp is not None else time.time())
    sig = _compute_hmac(secret, ts, body)
    return f"{_TIMESTAMP_KEY}={ts},{_SIGNATURE_KEY}={sig}", ts


def verify_webhook_signature(
    *,
    secret: str,
    header_value: Optional[str],
    body: bytes,
    max_age_seconds: int = 300,
    now_epoch: Optional[float] = None,
) -> None:
    """Raise WebhookSignatureError on any verification failure."""
    if not secret:
        raise WebhookSignatureError("secret is required to verify")
    if not header_value:
        raise WebhookSignatureError(
            f"missing {_HEADER_NAME} header"
        )
    parts = _parse_header(header_value)
    ts_raw = parts.get(_TIMESTAMP_KEY)
    sig = parts.get(_SIGNATURE_KEY)
    if ts_raw is None or sig is None:
        raise WebhookSignatureError(
            "header missing 't' or 'v1' components"
        )
    try:
        ts = int(ts_raw)
    except ValueError as exc:
        raise WebhookSignatureError(
            f"non-numeric timestamp in header: {ts_raw!r}"
        ) from exc

    now = int(now_epoch if now_epoch is not None else time.time())
    if abs(now - ts) > max_age_seconds:
        raise WebhookSignatureError(
            f"timestamp age {abs(now - ts)}s exceeds max_age "
            f"{max_age_seconds}s"
        )

    expected = _compute_hmac(secret, ts, body)
    if not hmac.compare_digest(expected, sig):
        raise WebhookSignatureError("HMAC mismatch")


def _compute_hmac(secret: str, ts: int, body: bytes) -> str:
    basestring = f"t={ts}\n".encode("utf-8") + body
    return hmac.new(
        secret.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()


def _parse_header(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in value.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out
