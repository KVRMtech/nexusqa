"""Parse Slack Events API payloads into a typed shape.

Slack delivers two flavours we care about for echo MVP:

* ``url_verification``  — initial handshake; respond with the challenge.
* ``event_callback`` with inner type ``app_mention`` or ``message``
  (specifically ``message.im`` or ``message.channels``).

Everything else (joins, edits, reactions outside our use) is mapped
to ``SlackEventKind.IGNORED`` so the route can ACK and move on.

The parser deliberately tolerates unknown fields — Slack adds keys
over time, and a strict mode would page us on every product update.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SlackEventKind(str, Enum):
    URL_VERIFICATION = "url_verification"
    APP_MENTION = "app_mention"
    MESSAGE_IM = "message.im"
    MESSAGE_CHANNEL = "message.channel"
    IGNORED = "ignored"


@dataclass(frozen=True)
class ParsedSlackEvent:
    kind: SlackEventKind
    team_id: Optional[str]
    event_id: Optional[str]
    user_id: Optional[str]
    channel_id: Optional[str]
    text: str
    thread_ts: Optional[str]
    event_ts: Optional[str]
    challenge: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


def parse_slack_event(payload: dict[str, Any]) -> ParsedSlackEvent:
    """Map a raw Slack JSON body to ParsedSlackEvent."""
    if not isinstance(payload, dict):
        return ParsedSlackEvent(
            kind=SlackEventKind.IGNORED,
            team_id=None,
            event_id=None,
            user_id=None,
            channel_id=None,
            text="",
            thread_ts=None,
            event_ts=None,
        )

    payload_type = payload.get("type")

    # URL verification is sent at app install time.
    if payload_type == "url_verification":
        return ParsedSlackEvent(
            kind=SlackEventKind.URL_VERIFICATION,
            team_id=None,
            event_id=None,
            user_id=None,
            channel_id=None,
            text="",
            thread_ts=None,
            event_ts=None,
            challenge=str(payload.get("challenge") or ""),
        )

    if payload_type != "event_callback":
        return ParsedSlackEvent(
            kind=SlackEventKind.IGNORED,
            team_id=str(payload.get("team_id") or "") or None,
            event_id=str(payload.get("event_id") or "") or None,
            user_id=None,
            channel_id=None,
            text="",
            thread_ts=None,
            event_ts=None,
        )

    event = payload.get("event") or {}
    if not isinstance(event, dict):
        return ParsedSlackEvent(
            kind=SlackEventKind.IGNORED,
            team_id=str(payload.get("team_id") or "") or None,
            event_id=str(payload.get("event_id") or "") or None,
            user_id=None,
            channel_id=None,
            text="",
            thread_ts=None,
            event_ts=None,
        )

    # Ignore bot messages — including our own — to avoid loops.
    if event.get("bot_id") or event.get("bot_profile") or event.get("subtype"):
        # Subtypes include 'bot_message', 'message_changed', 'message_deleted'.
        return ParsedSlackEvent(
            kind=SlackEventKind.IGNORED,
            team_id=str(payload.get("team_id") or "") or None,
            event_id=str(payload.get("event_id") or "") or None,
            user_id=str(event.get("user") or "") or None,
            channel_id=str(event.get("channel") or "") or None,
            text=str(event.get("text") or ""),
            thread_ts=str(event.get("thread_ts") or "") or None,
            event_ts=str(event.get("event_ts") or "") or None,
        )

    inner_type = event.get("type")
    channel_type = event.get("channel_type")

    kind: SlackEventKind
    if inner_type == "app_mention":
        kind = SlackEventKind.APP_MENTION
    elif inner_type == "message" and channel_type == "im":
        kind = SlackEventKind.MESSAGE_IM
    elif inner_type == "message" and channel_type in ("channel", "group"):
        kind = SlackEventKind.MESSAGE_CHANNEL
    else:
        kind = SlackEventKind.IGNORED

    return ParsedSlackEvent(
        kind=kind,
        team_id=str(payload.get("team_id") or "") or None,
        event_id=str(payload.get("event_id") or "") or None,
        user_id=str(event.get("user") or "") or None,
        channel_id=str(event.get("channel") or "") or None,
        text=str(event.get("text") or ""),
        thread_ts=str(event.get("thread_ts") or "") or None,
        event_ts=str(event.get("event_ts") or "") or None,
    )
