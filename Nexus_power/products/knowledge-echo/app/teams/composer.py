"""Adaptive Card composer for Microsoft Teams.

Produces an Adaptive Card v1.4 body wrapped in a Bot Framework activity
``attachments`` envelope. The dispatcher posts this as a reply via
``conversations/{conversationId}/activities`` against the activity's
``serviceUrl``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from ..matcher import MatchResult
from ..surfaces import ComposedPayload


ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"
ADAPTIVE_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"


class TeamsComposer:
    """Implements ``SurfaceComposer`` for the Teams surface."""

    def __init__(
        self,
        *,
        feedback_action_prefix: str = "echo_feedback",
        ask_sme_action_id: str = "echo_ask_sme",
        adaptive_card_version: str = "1.4",
    ) -> None:
        self._fb_prefix = feedback_action_prefix
        self._ask = ask_sme_action_id
        self._version = adaptive_card_version

    def compose(
        self,
        *,
        dispatch_id: str,
        question_text: str,
        match: MatchResult,
    ) -> Optional[ComposedPayload]:
        if match.is_empty:
            return None
        top = match.candidates[0]
        sim_pct = int(round(max(0.0, min(1.0, top.similarity)) * 100))

        sme_bits = []
        if top.speaker_id:
            sme_bits.append(top.speaker_id)
        if top.speaker_role:
            sme_bits.append(f"({top.speaker_role})")
        sme_label = " ".join(sme_bits) or "internal source"

        body = [
            {
                "type": "TextBlock",
                "size": "Small",
                "color": "Accent",
                "weight": "Bolder",
                "text": f"KNOWLEDGE ECHO · {sim_pct}% MATCH",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "size": "Default",
                "color": "Default",
                "text": _truncate(top.text, 500),
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "size": "Small",
                "isSubtle": True,
                "spacing": "Small",
                "wrap": True,
                "text": (
                    f"— {sme_label}"
                    + (f" · session `{top.session_id}`" if top.session_id else "")
                ),
            },
        ]
        if question_text:
            body.append(
                {
                    "type": "TextBlock",
                    "size": "Small",
                    "isSubtle": True,
                    "wrap": True,
                    "spacing": "Medium",
                    "text": f"_asked: {_truncate(question_text, 240)}_",
                }
            )

        actions = [
            {
                "type": "Action.Submit",
                "title": "👍 Helpful",
                "style": "positive",
                "data": {
                    "msteams": {"type": "messageBack"},
                    "action": f"{self._fb_prefix}:thumbs_up",
                    "dispatch_id": dispatch_id,
                },
            },
            {
                "type": "Action.Submit",
                "title": "👎 Not quite",
                "data": {
                    "msteams": {"type": "messageBack"},
                    "action": f"{self._fb_prefix}:thumbs_down",
                    "dispatch_id": dispatch_id,
                },
            },
            {
                "type": "Action.Submit",
                "title": "🙋 Ask SME",
                "data": {
                    "msteams": {"type": "messageBack"},
                    "action": self._ask,
                    "dispatch_id": dispatch_id,
                },
            },
        ]

        adaptive_card: dict[str, Any] = {
            "type": "AdaptiveCard",
            "version": self._version,
            "$schema": ADAPTIVE_CARD_SCHEMA,
            "body": body,
            "actions": actions,
        }

        activity_text = (
            f"Knowledge Echo · {sim_pct}% match — "
            + _truncate(top.text, 140)
        )

        payload: dict[str, Any] = {
            "type": "message",
            "textFormat": "plain",
            "text": activity_text,
            "attachments": [
                {
                    "contentType": ADAPTIVE_CARD_CONTENT_TYPE,
                    "content": adaptive_card,
                }
            ],
        }
        return ComposedPayload(
            surface="teams",
            text=activity_text,
            payload=payload,
            payload_hash=_hash_payload(payload),
            similarity_pct=sim_pct,
            primary_candidate=top.to_audit_dict(),
        )


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"


def _hash_payload(payload: dict[str, Any]) -> str:
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()
