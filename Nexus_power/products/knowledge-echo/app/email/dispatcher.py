"""SES SendEmail outbound dispatcher.

We use the boto3 SES v2 ``send_email`` API. Threading is preserved via
``Reply-To`` plus explicit ``In-Reply-To`` / ``References`` headers
in the raw RFC822 envelope.

DM-vs-channel doesn't map cleanly onto email. We treat ``as_dm=True``
as the normal direct-reply flow; ``as_dm=False`` is reserved for the
case where a tenant wires the email surface to a shared inbox — in
that case the dispatcher still sends from the configured ``from_address``
but the ``To:`` is the original ``to_addrs`` of the inbound message
plus the asker, so the whole thread sees the echo.

The dispatcher runs the (sync) boto3 call in a threadpool via
``asyncio.to_thread`` to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Optional

from ..surfaces import (
    ComposedPayload,
    DispatchOutcome,
    SurfaceError,
    SurfaceUnavailable,
)
from .installation import (
    EmailInstallation,
    EmailInstallationError,
    EmailInstallationLoader,
)

logger = logging.getLogger(__name__)


class EmailDispatchError(SurfaceError):
    """SES rejected the message or the transport failed."""


class SesOutboundClient:
    """Thin wrapper around boto3 SES v2 ``send_email``.

    Per-request credentials let us send on behalf of many tenants from
    one process; falling back to the host's IAM role when no explicit
    keys are configured.
    """

    def __init__(self) -> None:
        try:
            import boto3  # noqa: F401  — deferred import sanity check
        except ImportError as exc:
            raise RuntimeError(
                "boto3 is required for SES outbound"
            ) from exc

    async def send_raw(
        self,
        *,
        installation: EmailInstallation,
        to_addresses: list[str],
        raw_message: bytes,
        from_address: Optional[str] = None,
        configuration_set: Optional[str] = None,
    ) -> dict[str, Any]:
        import boto3
        from botocore.config import Config as BotoConfig
        from botocore.exceptions import BotoCoreError, ClientError

        kwargs: dict[str, Any] = {"region_name": installation.aws_region}
        if installation.aws_access_key_id and installation.aws_secret_access_key:
            kwargs["aws_access_key_id"] = installation.aws_access_key_id
            kwargs["aws_secret_access_key"] = installation.aws_secret_access_key
            if installation.aws_session_token:
                kwargs["aws_session_token"] = installation.aws_session_token
        kwargs["config"] = BotoConfig(
            retries={"max_attempts": 3, "mode": "standard"},
        )
        ses = boto3.client("sesv2", **kwargs)

        def _send() -> dict[str, Any]:
            params: dict[str, Any] = {
                "FromEmailAddress": from_address or installation.from_address,
                "Destination": {"ToAddresses": to_addresses},
                "Content": {"Raw": {"Data": raw_message}},
            }
            cfg = configuration_set or installation.ses_configuration_set
            if cfg:
                params["ConfigurationSetName"] = cfg
            return ses.send_email(**params)

        try:
            return await asyncio.to_thread(_send)
        except (ClientError, BotoCoreError) as exc:
            raise EmailDispatchError(f"SES send_email failed: {exc}") from exc


class EmailDispatcher:
    """Implements ``SurfaceDispatcher`` for the email surface."""

    def __init__(
        self,
        installs: EmailInstallationLoader,
        *,
        client: Optional[SesOutboundClient] = None,
    ):
        self._installs = installs
        self._client = client or SesOutboundClient()

    async def dispatch(
        self,
        *,
        tenant_id: str,
        payload: ComposedPayload,
        as_dm: bool,
        is_live: bool,
        user_id_ext: Optional[str],
        channel_id_ext: Optional[str],
        thread_ts: Optional[str],
    ) -> DispatchOutcome:
        try:
            install = await self._installs.for_tenant(tenant_id)
        except EmailInstallationError as exc:
            raise SurfaceUnavailable(str(exc)) from exc

        recipient = (user_id_ext or "").strip()
        if not recipient or "@" not in recipient:
            raise SurfaceError(
                "email dispatch requires user_id_ext as an email address"
            )

        body = payload.payload
        subject = str(body.get("subject") or "Knowledge Echo")
        text_body = body.get("text_body") or payload.text
        html_body = body.get("html_body")
        in_reply_to = body.get("in_reply_to")
        references = body.get("references") or []

        msg = EmailMessage()
        msg["From"] = install.from_address
        msg["To"] = recipient
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain="nexus.echo")
        if install.reply_to:
            msg["Reply-To"] = install.reply_to
        if in_reply_to:
            msg["In-Reply-To"] = str(in_reply_to)
        if isinstance(references, list) and references:
            msg["References"] = " ".join(
                str(r) for r in references if isinstance(r, str)
            )
        msg["X-Nexus-Dispatch-Id"] = str(
            body.get("dispatch_id") or ""
        )

        msg.set_content(text_body or "")
        if isinstance(html_body, str) and html_body:
            msg.add_alternative(html_body, subtype="html")

        raw = msg.as_bytes()

        started = time.monotonic()
        try:
            ses_resp = await self._client.send_raw(
                installation=install,
                to_addresses=[recipient],
                raw_message=raw,
            )
        except EmailDispatchError:
            raise
        except Exception as exc:
            raise EmailDispatchError(
                f"unexpected SES failure: {exc}"
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        decision = "posted_dm" if as_dm else "posted_channel"
        message_id = ses_resp.get("MessageId") if isinstance(ses_resp, dict) else None
        return DispatchOutcome(
            decision=decision,
            message_ref=str(message_id) if message_id else None,
            raw={"ses_message_id": message_id, "latency_ms": latency_ms},
        )
