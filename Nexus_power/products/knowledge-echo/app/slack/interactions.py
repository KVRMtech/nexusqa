"""Parse Slack block-action interaction payloads.

Slack sends button clicks to the configured interactivity URL with the
``payload`` form field carrying JSON. We pluck the fields we care
about for feedback collection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ParsedInteraction:
    kind: str  # 'block_actions' | 'ignored'
    team_id: Optional[str]
    user_id: Optional[str]
    channel_id: Optional[str]
    message_ts: Optional[str]
    response_url: Optional[str]
    action_id: Optional[str]
    action_value: Optional[str]
    callback_id: Optional[str]


def parse_block_actions(payload_form_field: str) -> ParsedInteraction:
    """Parse the ``payload`` form value sent by Slack interactivity."""
    if not payload_form_field:
        return _ignored()
    try:
        body = json.loads(payload_form_field)
    except (TypeError, ValueError):
        return _ignored()
    if not isinstance(body, dict):
        return _ignored()
    if body.get("type") != "block_actions":
        return _ignored()
    actions = body.get("actions") or []
    if not isinstance(actions, list) or not actions:
        return _ignored()
    action = actions[0]
    if not isinstance(action, dict):
        return _ignored()
    user = body.get("user") or {}
    channel = body.get("channel") or {}
    message = body.get("message") or {}
    return ParsedInteraction(
        kind="block_actions",
        team_id=_optional_str(body.get("team", {}).get("id")) if isinstance(body.get("team"), dict) else None,
        user_id=_optional_str(user.get("id")) if isinstance(user, dict) else None,
        channel_id=_optional_str(channel.get("id")) if isinstance(channel, dict) else None,
        message_ts=_optional_str(message.get("ts")) if isinstance(message, dict) else None,
        response_url=_optional_str(body.get("response_url")),
        action_id=_optional_str(action.get("action_id")),
        action_value=_optional_str(action.get("value")),
        callback_id=_optional_str(action.get("block_id")),
    )


def _ignored() -> ParsedInteraction:
    return ParsedInteraction(
        kind="ignored",
        team_id=None,
        user_id=None,
        channel_id=None,
        message_ts=None,
        response_url=None,
        action_id=None,
        action_value=None,
        callback_id=None,
    )


def _optional_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
