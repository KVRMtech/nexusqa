"""SNS notification parsing + signature verification.

AWS SNS HTTPS subscribers receive POSTs with a JSON body containing
``Type``, ``MessageId``, ``Signature``, ``SigningCertURL``, etc.
The signature is over a canonical string built from a specific set
of fields per ``Type``.

This module implements:

    * ``parse_sns_notification(body)``  — typed object + verdict
    * ``verify_sns_signature(payload, cert_loader)`` — raises on
      bad signature; returns None on success.

We fetch the SigningCertURL via a caller-provided async loader so
production deployments can plug in a real httpx client (with allow-
list of ``sns.amazonaws.com``) while tests can stub a fixture.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ── Errors ──────────────────────────────────────────────────────


class SnsSignatureError(Exception):
    """Verification failed: missing fields, untrusted cert URL, or bad sig."""


@dataclass(frozen=True)
class SnsSubscriptionConfirmation:
    """SNS sends a confirmation URL when a subscription is created."""

    topic_arn: str
    token: str
    subscribe_url: str


@dataclass(frozen=True)
class SnsNotification:
    type: str  # 'Notification' | 'SubscriptionConfirmation' | 'UnsubscribeConfirmation'
    message_id: str
    topic_arn: str
    signing_cert_url: str
    signature: str
    signature_version: str
    timestamp: str
    subject: Optional[str]
    message: str
    raw: dict[str, Any]


# ── Parsing ─────────────────────────────────────────────────────


def parse_sns_notification(body: bytes | str | dict[str, Any]) -> SnsNotification:
    if isinstance(body, (bytes, bytearray)):
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SnsSignatureError(f"SNS body not UTF-8 JSON: {exc}") from exc
    elif isinstance(body, str):
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise SnsSignatureError(f"SNS body not JSON: {exc}") from exc
    elif isinstance(body, dict):
        data = body
    else:
        raise SnsSignatureError(
            f"unsupported SNS body type: {type(body).__name__}"
        )
    if not isinstance(data, dict):
        raise SnsSignatureError("SNS body root must be an object")

    required = (
        "Type",
        "MessageId",
        "TopicArn",
        "Signature",
        "SigningCertURL",
        "Timestamp",
    )
    for k in required:
        if not isinstance(data.get(k), str) or not data[k]:
            raise SnsSignatureError(f"SNS body missing required field: {k}")

    return SnsNotification(
        type=data["Type"],
        message_id=data["MessageId"],
        topic_arn=data["TopicArn"],
        signing_cert_url=data["SigningCertURL"],
        signature=data["Signature"],
        signature_version=str(data.get("SignatureVersion") or "1"),
        timestamp=data["Timestamp"],
        subject=data.get("Subject"),
        message=data.get("Message") or "",
        raw=data,
    )


# ── Cert URL allowlist + canonical strings ─────────────────────


_CERT_HOST_RE = re.compile(
    r"^sns\.[a-z0-9-]+\.amazonaws\.com$",
    re.IGNORECASE,
)


def _validate_cert_url(url: str) -> None:
    """Refuse to fetch the signing certificate from anywhere but SNS."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SnsSignatureError(
            f"SigningCertURL must be https: {url}"
        )
    if not _CERT_HOST_RE.match(parsed.netloc):
        raise SnsSignatureError(
            f"SigningCertURL host not allowed: {parsed.netloc}"
        )
    if not parsed.path.endswith(".pem"):
        raise SnsSignatureError(
            f"SigningCertURL must point to a .pem: {url}"
        )


def _canonical_string(n: SnsNotification) -> bytes:
    """Build the string-to-sign per the SNS HTTP signing contract."""
    # Order is fixed and documented:
    # https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
    if n.type == "Notification":
        fields = [
            ("Message", n.message),
            ("MessageId", n.message_id),
        ]
        if n.subject is not None:
            fields.append(("Subject", n.subject))
        fields.extend(
            [
                ("Timestamp", n.timestamp),
                ("TopicArn", n.topic_arn),
                ("Type", n.type),
            ]
        )
    elif n.type in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
        # Both confirmation types include Token + SubscribeURL.
        token = n.raw.get("Token") or ""
        subscribe_url = n.raw.get("SubscribeURL") or ""
        if not token or not subscribe_url:
            raise SnsSignatureError(
                f"{n.type} missing Token / SubscribeURL"
            )
        fields = [
            ("Message", n.message),
            ("MessageId", n.message_id),
            ("SubscribeURL", subscribe_url),
            ("Timestamp", n.timestamp),
            ("Token", token),
            ("TopicArn", n.topic_arn),
            ("Type", n.type),
        ]
    else:
        raise SnsSignatureError(f"unsupported SNS Type: {n.type}")

    parts: list[str] = []
    for k, v in fields:
        parts.append(k)
        parts.append("\n")
        parts.append(v)
        parts.append("\n")
    return "".join(parts).encode("utf-8")


# ── Signature verification ─────────────────────────────────────


CertLoader = Callable[[str], Awaitable[bytes]]


async def verify_sns_signature(
    notification: SnsNotification,
    *,
    cert_loader: CertLoader,
    expected_topic_arn: Optional[str] = None,
) -> None:
    """Raise on any verification failure."""
    if notification.signature_version not in ("1", "2"):
        raise SnsSignatureError(
            f"unsupported SignatureVersion: {notification.signature_version}"
        )
    if expected_topic_arn and notification.topic_arn != expected_topic_arn:
        raise SnsSignatureError(
            f"topic_arn mismatch: got {notification.topic_arn!r}"
        )
    _validate_cert_url(notification.signing_cert_url)

    cert_pem = await cert_loader(notification.signing_cert_url)
    if not cert_pem:
        raise SnsSignatureError("cert_loader returned empty body")

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise SnsSignatureError(
            "cryptography is required for SNS verification"
        ) from exc

    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except Exception as exc:
        raise SnsSignatureError(f"invalid signing certificate: {exc}") from exc

    canonical = _canonical_string(notification)
    try:
        signature_bytes = base64.b64decode(notification.signature)
    except Exception as exc:
        raise SnsSignatureError(f"signature not base64: {exc}") from exc

    public_key = cert.public_key()
    hash_alg = (
        hashes.SHA1()
        if notification.signature_version == "1"
        else hashes.SHA256()
    )

    try:
        public_key.verify(  # type: ignore[union-attr]
            signature_bytes,
            canonical,
            padding.PKCS1v15(),
            hash_alg,
        )
    except Exception as exc:
        raise SnsSignatureError(f"signature verification failed: {exc}") from exc


def subscription_confirmation(
    notification: SnsNotification,
) -> Optional[SnsSubscriptionConfirmation]:
    if notification.type != "SubscriptionConfirmation":
        return None
    token = notification.raw.get("Token") or ""
    subscribe_url = notification.raw.get("SubscribeURL") or ""
    if not token or not subscribe_url:
        return None
    return SnsSubscriptionConfirmation(
        topic_arn=notification.topic_arn,
        token=token,
        subscribe_url=subscribe_url,
    )
