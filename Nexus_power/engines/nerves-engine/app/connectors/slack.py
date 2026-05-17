"""
Nerves Engine — Slack Connector.

Slack integration via Web API.
Supports sending messages, DMs, confirmation requests, and file uploads.
"""

from __future__ import annotations

import logging
import uuid
import time
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


class SlackConnector(BaseConnector):
    """
    Slack integration via Web API.

    Credentials: bot_token, (optional) default_channel
    """

    def __init__(self):
        super().__init__("slack")
        self._client: Optional[Any] = None

    async def connect(self, credentials: dict) -> bool:
        if "bot_token" not in credentials:
            self.status = ConnectorStatus.ERROR
            return False
        self.config = credentials
        if httpx is not None:
            self._client = httpx.AsyncClient(
                base_url="https://slack.com/api",
                headers={
                    "Authorization": f"Bearer {credentials['bot_token']}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                timeout=15.0,
            )
            try:
                resp = await self._client.post("/auth.test")
                data = resp.json()
                if data.get("ok"):
                    self.status = ConnectorStatus.CONNECTED
                    logger.info("nerves: Slack connected as %s", data.get("user"))
                    return True
                logger.warning("nerves: Slack auth failed: %s", data.get("error"))
                self.status = ConnectorStatus.ERROR
                return False
            except Exception as exc:
                logger.warning("nerves: Slack connection failed: %s", exc)
                self.status = ConnectorStatus.ERROR
                return False
        self.status = ConnectorStatus.CONNECTED
        logger.warning("nerves: Slack running in stub mode")
        fire_stub_alert(_event_bus, "nerves", "slack", reason="httpx not installed")
        return True

    async def execute(self, action: str, params: dict) -> dict:
        handler = {
            "send_message": self._send_message,
            "send_dm": self._send_dm,
            "send_confirmation": self._send_confirmation,
            "upload_file": self._upload_file,
        }.get(action)
        if not handler:
            raise ValueError(f"Unknown Slack action: {action}")
        return await handler(params)

    def get_available_actions(self) -> list[dict]:
        return [
            {"action": "send_message", "params": ["channel", "text", "blocks"]},
            {"action": "send_dm", "params": ["user_id", "text"]},
            {"action": "send_confirmation", "params": ["channel", "question", "options"]},
            {"action": "upload_file", "params": ["channel", "file_content", "filename"]},
        ]

    async def _send_message(self, params: dict) -> dict:
        channel = params.get("channel", "#general")
        text = params.get("text", "")
        if self._client:
            payload: dict[str, Any] = {"channel": channel, "text": text}
            if params.get("blocks"):
                payload["blocks"] = params["blocks"]
            resp = await self._client.post("/chat.postMessage", json=payload)
            data = resp.json()
            if data.get("ok"):
                return {"sent": True, "channel": channel, "ts": data.get("ts", "")}
            return {"sent": False, "error": data.get("error", "unknown")}
        return {"sent": True, "channel": channel, "ts": str(time.time())}

    async def _send_dm(self, params: dict) -> dict:
        user_id = params.get("user_id", "")
        text = params.get("text", "")
        if self._client:
            # Open DM channel
            open_resp = await self._client.post("/conversations.open", json={"users": user_id})
            open_data = open_resp.json()
            if not open_data.get("ok"):
                return {"sent": False, "error": open_data.get("error")}
            channel_id = open_data["channel"]["id"]
            # Send message
            resp = await self._client.post("/chat.postMessage", json={
                "channel": channel_id,
                "text": text,
            })
            data = resp.json()
            return {"sent": data.get("ok", False), "user": user_id}
        return {"sent": True, "user": user_id}

    async def _send_confirmation(self, params: dict) -> dict:
        channel = params.get("channel", "#general")
        question = params.get("question", "Please confirm")
        options = params.get("options", ["Approve", "Reject"])
        confirm_id = str(uuid.uuid4())[:8]
        if self._client:
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{question}*"}},
                {
                    "type": "actions",
                    "block_id": f"confirm_{confirm_id}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": opt},
                            "action_id": f"confirm_{confirm_id}_{i}",
                            "value": opt,
                        }
                        for i, opt in enumerate(options)
                    ],
                },
            ]
            resp = await self._client.post("/chat.postMessage", json={
                "channel": channel,
                "text": question,
                "blocks": blocks,
            })
            data = resp.json()
            return {
                "sent": data.get("ok", False),
                "confirmation_id": confirm_id,
                "message": "Confirmation request sent",
            }
        return {"sent": True, "confirmation_id": confirm_id, "message": "Confirmation request sent"}

    async def _upload_file(self, params: dict) -> dict:
        if self._client:
            resp = await self._client.post("/files.upload", data={
                "channels": params.get("channel", "#general"),
                "content": params.get("file_content", ""),
                "filename": params.get("filename", "nexus_report.txt"),
                "title": params.get("filename", "nexus_report.txt"),
            })
            data = resp.json()
            file_id = data.get("file", {}).get("id", "")
            return {"uploaded": data.get("ok", False), "file_id": file_id}
        return {"uploaded": True, "file_id": str(uuid.uuid4())[:8]}

    async def disconnect(self):
        if self._client:
            await self._client.aclose()
            self._client = None
        await super().disconnect()
