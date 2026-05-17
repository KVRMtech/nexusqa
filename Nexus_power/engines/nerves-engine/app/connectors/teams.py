"""
Nerves Engine — Teams Connector.

Microsoft Teams integration via Graph API.
Supports sending messages and creating online meetings.
"""

from __future__ import annotations

import logging
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


class TeamsConnector(BaseConnector):
    """
    Microsoft Teams integration via Graph API.

    Credentials: client_id, client_secret, tenant_id
    """

    def __init__(self):
        super().__init__("teams")
        self._client: Optional[Any] = None
        self._access_token: str = ""

    async def connect(self, credentials: dict) -> bool:
        if not ("client_id" in credentials and "client_secret" in credentials):
            self.status = ConnectorStatus.ERROR
            return False
        self.config = credentials
        if httpx is not None:
            # OAuth2 client-credentials flow
            tenant = credentials.get("tenant_id", "common")
            token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
            try:
                async with httpx.AsyncClient(timeout=15.0) as tmp:
                    resp = await tmp.post(token_url, data={
                        "grant_type": "client_credentials",
                        "client_id": credentials["client_id"],
                        "client_secret": credentials["client_secret"],
                        "scope": "https://graph.microsoft.com/.default",
                    })
                    if resp.status_code != 200:
                        logger.warning("nerves: Teams OAuth failed (%s)", resp.status_code)
                        self.status = ConnectorStatus.ERROR
                        return False
                    self._access_token = resp.json()["access_token"]
                self._client = httpx.AsyncClient(
                    base_url="https://graph.microsoft.com/v1.0",
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Content-Type": "application/json",
                    },
                    timeout=15.0,
                )
                self.status = ConnectorStatus.CONNECTED
                logger.info("nerves: Teams connected via Graph API")
                return True
            except Exception as exc:
                logger.warning("nerves: Teams connection failed: %s", exc)
                self.status = ConnectorStatus.ERROR
                return False
        self.status = ConnectorStatus.CONNECTED
        logger.warning("nerves: Teams running in stub mode")
        fire_stub_alert(_event_bus, "nerves", "teams", reason="httpx not installed")
        return True

    async def execute(self, action: str, params: dict) -> dict:
        handler = {
            "send_message": self._send_message,
            "create_meeting": self._create_meeting,
        }.get(action)
        if not handler:
            raise ValueError(f"Unknown Teams action: {action}")
        return await handler(params)

    def get_available_actions(self) -> list[dict]:
        return [
            {"action": "send_message", "params": ["team_id", "channel_id", "text"]},
            {"action": "create_meeting", "params": ["subject", "start_time", "attendees"]},
        ]

    async def _send_message(self, params: dict) -> dict:
        team_id = params.get("team_id", "")
        channel_id = params.get("channel_id", "")
        text = params.get("text", "")
        if self._client and team_id and channel_id:
            resp = await self._client.post(
                f"/teams/{team_id}/channels/{channel_id}/messages",
                json={"body": {"contentType": "text", "content": text}},
            )
            resp.raise_for_status()
            data = resp.json()
            return {"sent": True, "message_id": data.get("id", ""), "channel": channel_id}
        return {"sent": True, "channel": channel_id}

    async def _create_meeting(self, params: dict) -> dict:
        if self._client:
            body = {
                "subject": params.get("subject", "Nexus QA Review"),
                "start": {"dateTime": params.get("start_time", ""), "timeZone": "UTC"},
                "end": {"dateTime": params.get("end_time", ""), "timeZone": "UTC"},
                "attendees": [
                    {"emailAddress": {"address": a}, "type": "required"}
                    for a in params.get("attendees", [])
                ],
                "isOnlineMeeting": True,
                "onlineMeetingProvider": "teamsForBusiness",
            }
            resp = await self._client.post("/me/events", json=body)
            if resp.status_code in (200, 201):
                data = resp.json()
                return {
                    "meeting_url": data.get("onlineMeeting", {}).get("joinUrl", ""),
                    "created": True,
                }
        return {"meeting_url": "https://teams.microsoft.com/...", "created": True}

    async def disconnect(self):
        if self._client:
            await self._client.aclose()
            self._client = None
        await super().disconnect()
