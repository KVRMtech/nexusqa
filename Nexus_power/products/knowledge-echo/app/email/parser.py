"""Parse the SES inbound event embedded in an SNS Notification.

SES delivers inbound emails to an S3 bucket and publishes a JSON
notification whose ``Message`` field carries the SES event::

    { "notificationType": "Received",
      "mail": { "messageId": "...", "source": "...", "destination": [...],
                "commonHeaders": { "subject": "...", "from": [...] } },
      "receipt": { "spfVerdict": {"status": "PASS"},
                   "dkimVerdict": {"status": "PASS"},
                   "dmarcVerdict": {"status": "PASS"},
                   "action": {...} },
      "content": "<base64 raw email>"   // only if 'AddHeader' + 'Inline'
    }

We refuse processing when any verdict is ``FAIL``; ``GRAY`` /
``PROCESSING_FAILED`` map to ``SesVerdictFailure`` so the route can
log + ACK without delivering an echo.

The parser does *not* fetch the email body from S3 — that is the
caller's responsibility (S3 client) when a richer body is needed.
For the orchestrator we only need the question text, which lives
in ``commonHeaders.subject`` or the first text segment.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


class EmailInboundError(Exception):
    """The SES event was malformed or had a FAIL verdict."""


class SesVerdictFailure(EmailInboundError):
    """One or more anti-spoofing verdicts failed."""


@dataclass(frozen=True)
class ParsedEmail:
    message_id: str
    in_reply_to: Optional[str]
    references: tuple[str, ...]
    from_addr: str
    to_addrs: tuple[str, ...]
    subject: str
    snippet: str
    raw_event: dict[str, Any] = field(default_factory=dict)


_ADDR_RE = re.compile(r"<([^>]+)>")


def _extract_addr(raw: str) -> str:
    if not raw:
        return ""
    m = _ADDR_RE.search(raw)
    if m:
        return m.group(1).strip().lower()
    return raw.strip().lower()


def _header_lookup(headers: list[dict[str, Any]], name: str) -> Optional[str]:
    """SES delivers headers as a list of ``{name, value}`` dicts."""
    if not isinstance(headers, list):
        return None
    name_lower = name.lower()
    for h in headers:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == name_lower:
            v = h.get("value")
            if isinstance(v, str):
                return v
    return None


def _verdict(receipt: dict[str, Any], key: str) -> str:
    node = receipt.get(key) or {}
    if isinstance(node, dict):
        return str(node.get("status") or "").upper()
    return ""


def parse_ses_email_event(message: str | dict[str, Any]) -> ParsedEmail:
    """Parse the inner SES Message payload.

    ``message`` may be the JSON-encoded string from
    ``SnsNotification.message`` or an already-decoded dict.
    """
    if isinstance(message, str):
        try:
            data = json.loads(message)
        except ValueError as exc:
            raise EmailInboundError(
                f"SES message is not JSON: {exc}"
            ) from exc
    elif isinstance(message, dict):
        data = message
    else:
        raise EmailInboundError(
            f"unsupported SES message type: {type(message).__name__}"
        )

    if data.get("notificationType") != "Received":
        raise EmailInboundError(
            f"unsupported SES notificationType: {data.get('notificationType')!r}"
        )

    mail = data.get("mail") or {}
    receipt = data.get("receipt") or {}
    if not isinstance(mail, dict) or not isinstance(receipt, dict):
        raise EmailInboundError("SES event missing mail/receipt")

    # Anti-spoofing verdicts — refuse on FAIL; warn on missing.
    for key in ("spfVerdict", "dkimVerdict", "dmarcVerdict"):
        verdict = _verdict(receipt, key)
        if verdict == "FAIL":
            raise SesVerdictFailure(
                f"SES anti-spoofing verdict {key}=FAIL — refusing"
            )
        if not verdict:
            logger.info("email.verdict_missing key=%s", key)

    common = mail.get("commonHeaders") or {}
    if not isinstance(common, dict):
        common = {}

    headers = mail.get("headers")
    in_reply_to = _header_lookup(headers, "In-Reply-To") if headers else None
    references_raw = _header_lookup(headers, "References") if headers else None
    references: tuple[str, ...] = ()
    if references_raw:
        references = tuple(
            r.strip() for r in references_raw.split() if r.strip()
        )

    from_list = common.get("from") or []
    if not isinstance(from_list, list):
        from_list = []
    from_addr = _extract_addr(from_list[0]) if from_list else ""

    to_list = common.get("to") or []
    if not isinstance(to_list, list):
        to_list = []
    to_addrs = tuple(_extract_addr(a) for a in to_list if isinstance(a, str))

    subject = common.get("subject") or ""
    if not isinstance(subject, str):
        subject = str(subject)

    # SES may embed a snippet under ``mail.body`` when the rule action
    # is "SNS" with "Encoding=Base64+Inline". When absent we fall back
    # to the subject.
    body_node = mail.get("body") or data.get("content") or ""
    snippet = (
        body_node.strip()
        if isinstance(body_node, str) and body_node.strip()
        else subject.strip()
    )
    if not snippet:
        raise EmailInboundError("SES event has neither body nor subject")

    return ParsedEmail(
        message_id=str(mail.get("messageId") or "").strip(),
        in_reply_to=in_reply_to,
        references=references,
        from_addr=from_addr,
        to_addrs=to_addrs,
        subject=subject,
        snippet=snippet,
        raw_event=data,
    )
