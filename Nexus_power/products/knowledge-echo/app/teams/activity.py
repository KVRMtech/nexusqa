"""Parse Bot Framework activity payloads.

Bot Framework delivers a JSON ``Activity`` per inbound message::

    {
      "type": "message" | "conversationUpdate" | "invoke" | ... ,
      "id": "<activity-id>",
      "channelId": "msteams",
      "serviceUrl": "https://smba.trafficmanager.net/.../",
      "conversation": { "id": "<conv-id>", "tenantId": "<aad-tenant>", "conversationType": "personal|channel|groupChat" },
      "from": { "id": "<aad-user-id>", "name": "...", "aadObjectId": "..." },
      "recipient": { "id": "28:<bot-id>", "name": "Nexus" },
      "text": "<@Nexus> what is the LT5 lookback?",
      "channelData": { "teamsChannelId": "...", "team": {...}, "tenant": {...} },
      "replyToId": "<thread-anchor>" (optional)
    }

We map this to ``ParsedTeamsActivity``. Surface-equivalents:
``conversationType=personal`` is the DM analog; ``channel`` or
``groupChat`` is the channel analog.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TeamsActivityKind(str, Enum):
    MESSAGE_PERSONAL = "message.personal"
    MESSAGE_CHANNEL = "message.channel"
    MESSAGE_GROUP = "message.group"
    INVOKE = "invoke"
    CONVERSATION_UPDATE = "conversation_update"
    IGNORED = "ignored"


@dataclass(frozen=True)
class ParsedTeamsActivity:
    kind: TeamsActivityKind
    activity_id: Optional[str]
    service_url: Optional[str]
    aad_tenant_id: Optional[str]
    conversation_id: Optional[str]
    conversation_type: Optional[str]
    channel_id_ext: Optional[str]
    user_id_ext: Optional[str]
    user_name: Optional[str]
    text: str
    reply_to_id: Optional[str]
    bot_recipient_id: Optional[str]
    raw: dict[str, Any] = field(default_factory=dict)


_MENTION_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)


def _strip_mentions(text: str) -> str:
    """Remove Teams ``<at>...</at>`` mention tags from the message body."""
    if not text:
        return ""
    return _MENTION_RE.sub("", text).strip()


def parse_teams_activity(payload: dict[str, Any]) -> ParsedTeamsActivity:
    if not isinstance(payload, dict):
        return _ignored(payload if isinstance(payload, dict) else {})

    activity_type = (payload.get("type") or "").lower()
    conversation = payload.get("conversation") or {}
    channel_data = payload.get("channelData") or {}
    from_user = payload.get("from") or {}
    recipient = payload.get("recipient") or {}

    if not isinstance(conversation, dict):
        conversation = {}
    if not isinstance(channel_data, dict):
        channel_data = {}
    if not isinstance(from_user, dict):
        from_user = {}
    if not isinstance(recipient, dict):
        recipient = {}

    aad_tenant = None
    tenant_node = channel_data.get("tenant") or {}
    if isinstance(tenant_node, dict):
        aad_tenant = tenant_node.get("id")
    if not aad_tenant:
        aad_tenant = conversation.get("tenantId")

    conv_type = conversation.get("conversationType") or ""

    # Bot Framework sometimes delivers our own bot's messages (when a
    # human echo also @-mentions the bot via a thread reply). Refuse to
    # process anything where ``from.role == 'bot'`` or where the from-id
    # matches the bot's recipient id.
    from_role = (from_user.get("role") or "").lower()
    if from_role == "bot":
        return _ignored(payload, kind=TeamsActivityKind.IGNORED)
    bot_recipient_id = recipient.get("id")
    if (
        isinstance(bot_recipient_id, str)
        and isinstance(from_user.get("id"), str)
        and from_user["id"] == bot_recipient_id
    ):
        return _ignored(payload, kind=TeamsActivityKind.IGNORED)

    if activity_type == "message":
        if conv_type == "personal":
            kind = TeamsActivityKind.MESSAGE_PERSONAL
        elif conv_type == "channel":
            kind = TeamsActivityKind.MESSAGE_CHANNEL
        elif conv_type == "groupChat":
            kind = TeamsActivityKind.MESSAGE_GROUP
        else:
            kind = TeamsActivityKind.IGNORED
    elif activity_type == "invoke":
        kind = TeamsActivityKind.INVOKE
    elif activity_type == "conversationupdate":
        kind = TeamsActivityKind.CONVERSATION_UPDATE
    else:
        kind = TeamsActivityKind.IGNORED

    return ParsedTeamsActivity(
        kind=kind,
        activity_id=_optional_str(payload.get("id")),
        service_url=_optional_str(payload.get("serviceUrl")),
        aad_tenant_id=_optional_str(aad_tenant),
        conversation_id=_optional_str(conversation.get("id")),
        conversation_type=_optional_str(conv_type),
        channel_id_ext=_optional_str(
            channel_data.get("teamsChannelId")
            or conversation.get("id")
        ),
        user_id_ext=_optional_str(
            from_user.get("aadObjectId") or from_user.get("id")
        ),
        user_name=_optional_str(from_user.get("name")),
        text=_strip_mentions(str(payload.get("text") or "")),
        reply_to_id=_optional_str(payload.get("replyToId")),
        bot_recipient_id=_optional_str(bot_recipient_id),
        raw=payload,
    )


def _ignored(
    payload: dict[str, Any],
    *,
    kind: TeamsActivityKind = TeamsActivityKind.IGNORED,
) -> ParsedTeamsActivity:
    conversation = payload.get("conversation") if isinstance(payload, dict) else None
    if not isinstance(conversation, dict):
        conversation = {}
    return ParsedTeamsActivity(
        kind=kind,
        activity_id=_optional_str((payload or {}).get("id")) if isinstance(payload, dict) else None,
        service_url=_optional_str((payload or {}).get("serviceUrl")) if isinstance(payload, dict) else None,
        aad_tenant_id=None,
        conversation_id=_optional_str(conversation.get("id")),
        conversation_type=_optional_str(conversation.get("conversationType")),
        channel_id_ext=None,
        user_id_ext=None,
        user_name=None,
        text="",
        reply_to_id=None,
        bot_recipient_id=None,
        raw=payload if isinstance(payload, dict) else {},
    )


def _optional_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
