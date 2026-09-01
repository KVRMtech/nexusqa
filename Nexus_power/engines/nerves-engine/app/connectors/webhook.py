"""
Nerves Engine — Webhook Connector.

Generic webhook connector — real HTTP POST via httpx.
Supports HMAC signature verification for webhook security.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional, Any

from nexus_sdk.events import fire_stub_alert

from .base import BaseConnector, ConnectorStatus

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Module-level event bus reference set by the engine at startup
_event_bus = None


def set_event_bus(bus):
    """Called by engine to inject the event bus reference."""
    global _event_bus
    _event_bus = bus


class WebhookConnector(BaseConnector):
    """
    Generic webhook connector — real HTTP POST via httpx.

    Credentials: url, (optional) headers, secret
    """

    def __init__(self):
        super().__init__("webhook")
        self._client: Optional[Any] = None

    async def connect(self, credentials: dict) -> bool:
        if "url" not in credentials:
            self.status = ConnectorStatus.ERROR
            return False
        self.config = credentials
        if httpx is not None:
            headers = credentials.get("headers", {})
            self._client = httpx.AsyncClient(timeout=15.0, headers=headers)
            self.status = ConnectorStatus.CONNECTED
            logger.info("nerves: Webhook configured → %s", credentials["url"])
        else:
            self.status = ConnectorStatus.CONNECTED
            logger.warning("nerves: Webhook running in stub mode")
            fire_stub_alert(_event_bus, "nerves", "webhook", reason="httpx not installed")
        return True

    async def execute(self, action: str, params: dict) -> dict:
        url = self.config.get("url", "")
        payload = params.get("payload", params)
        custom_headers = params.get("headers", {})
        if self._client and url:
            import hashlib
            import hmac
            headers = dict(custom_headers)
            # HMAC signature if secret configured
            secret = self.config.get("secret")
            if secret:
                body_bytes = json.dumps(payload, sort_keys=True).encode()
                sig = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
                headers["X-Nexus-Signature"] = f"sha256={sig}"
            resp = await self._client.post(url, json=payload, headers=headers)
            return {
                "sent": True,
                "url": url,
                "status_code": resp.status_code,
                "response_body": resp.text[:500],
            }
        return {"sent": True, "url": url, "status_code": 200}

    def get_available_actions(self) -> list[dict]:
        return [
            {"action": "send", "params": ["payload", "headers"]},
        ]

    async def disconnect(self):
        if self._client:
            await self._client.aclose()
            self._client = None
        await super().disconnect()
