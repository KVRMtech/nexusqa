"""Signed outbound POST to a tenant-configured destination URL.

Production behaviour:

    * HMAC-SHA256 signature in ``X-Nexus-Signature`` (same format as
      inbound).
    * Exponential backoff on transient errors (5xx, timeouts).
    * Treats 4xx as terminal — the caller decides whether to retry.
    * Honors a tenant-provided header bag for upstream auth tokens etc.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import httpx

from ..surfaces import (
    ComposedPayload,
    DispatchOutcome,
    SurfaceError,
    SurfaceUnavailable,
)
from .installation import WebhookInstallation, WebhookInstallationLoader
from .signature import sign_webhook_body

logger = logging.getLogger(__name__)


class WebhookDispatchError(SurfaceError):
    """Transport / protocol failure when posting outbound."""


class WebhookDispatcher:
    """Implements ``SurfaceDispatcher`` for the webhook surface."""

    def __init__(
        self,
        installs: WebhookInstallationLoader,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._installs = installs
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds
        )
        self._max_retries = max(0, int(max_retries))

    async def aclose(self) -> None:
        await self._client.aclose()

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
        except Exception as exc:
            raise SurfaceUnavailable(str(exc)) from exc
        if install.outbound is None:
            raise SurfaceUnavailable(
                "Webhook installation has no outbound configuration"
            )

        body_dict = dict(payload.payload)
        # Annotate the outbound payload with surface routing context
        # so the receiver can implement its own DM-vs-channel logic.
        body_dict["surface"] = {
            "as_dm": bool(as_dm),
            "is_live": bool(is_live),
            "user_id": user_id_ext,
            "channel_id": channel_id_ext,
            "thread_id": thread_ts,
        }
        body = json.dumps(
            body_dict, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")

        sig_header, ts = sign_webhook_body(
            secret=install.outbound.outbound_secret, body=body
        )
        headers: dict[str, str] = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Nexus-Signature": sig_header,
            "X-Nexus-Timestamp": str(ts),
            "X-Nexus-Dispatch-Id": str(body_dict.get("dispatch_id") or ""),
            "User-Agent": "nexus-knowledge-echo/1.0",
            **install.outbound.extra_headers,
        }

        attempt = 0
        backoff = 0.5
        last_status: Optional[int] = None
        last_text: str = ""
        while attempt <= self._max_retries:
            attempt += 1
            try:
                resp = await self._client.post(
                    install.outbound.destination_url,
                    content=body,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                if attempt > self._max_retries:
                    raise WebhookDispatchError(
                        f"transport failure: {exc}"
                    ) from exc
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            last_status = resp.status_code
            last_text = (resp.text or "")[:512]
            if 200 <= resp.status_code < 300:
                decision = "posted_dm" if as_dm else "posted_channel"
                return DispatchOutcome(
                    decision=decision,
                    message_ref=resp.headers.get("X-Customer-Message-Id"),
                    raw={
                        "status_code": resp.status_code,
                        "body_excerpt": last_text,
                    },
                )
            if resp.status_code >= 500:
                if attempt > self._max_retries:
                    raise WebhookDispatchError(
                        f"upstream {resp.status_code}: {last_text}"
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            # 4xx — terminal
            raise WebhookDispatchError(
                f"upstream rejected {resp.status_code}: {last_text}"
            )

        raise WebhookDispatchError(
            f"exhausted retries; last_status={last_status} body={last_text}"
        )
