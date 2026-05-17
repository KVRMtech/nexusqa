"""Block Kit composer — turns match results into a Slack message payload.

The composer is intentionally pure (no IO): given a question, a match
result, and tenant policy, it returns a tuple of ``(text, blocks)``.
This makes the unit tests trivial and keeps audit-payload-hashing
deterministic.

Layout (Slack Block Kit):

    Header  : "Knowledge Echo · NN% match"
    Quote   : block quote of the matched text (truncated, sanitised)
    Context : SME name + role + session reference
    Actions : Helpful / Not quite / Ask SME

Slack truncates long blocks server-side; we cap text at 3000 chars per
Slack limits, plus a defensive trim to keep messages readable.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..matcher import MatchCandidate, MatchResult

logger = logging.getLogger(__name__)


SLACK_TEXT_LIMIT = 3000
QUOTE_TRUNCATE = 800
HEADER_PREFIX = "Knowledge Echo"


@dataclass(frozen=True)
class EchoCard:
    text: str
    blocks: list[dict[str, Any]]
    payload_hash: str
    candidate: MatchCandidate
    similarity_pct: int

    def to_payload(self) -> dict[str, Any]:
        return {"text": self.text, "blocks": self.blocks}


class EchoCardComposer:
    def __init__(
        self,
        *,
        feedback_action_prefix: str = "echo_feedback",
        ask_sme_action_id: str = "echo_ask_sme",
    ) -> None:
        self._feedback_prefix = feedback_action_prefix
        self._ask_action = ask_sme_action_id

    def compose(
        self,
        *,
        dispatch_id: str,
        question_text: str,
        match: MatchResult,
    ) -> Optional[EchoCard]:
        """Return a card for the top candidate, or None if no match."""
        if match.is_empty:
            return None
        top = match.candidates[0]
        similarity_pct = int(round(max(0.0, min(1.0, top.similarity)) * 100))
        quote = _sanitise_for_slack(_truncate(top.text, QUOTE_TRUNCATE))
        sme_line = _sme_line(top)

        text = (
            f"{HEADER_PREFIX} · {similarity_pct}% match — "
            f"\"{_truncate(top.text, 140)}\""
        )
        text = _truncate(text, SLACK_TEXT_LIMIT)

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{HEADER_PREFIX} · {similarity_pct}% match",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"> {quote}",
                },
            },
        ]
        if sme_line:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": sme_line},
                    ],
                }
            )

        # Question echoed back as audit context (not shown prominently).
        if question_text:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                "_asked: "
                                + _sanitise_for_slack(
                                    _truncate(question_text, 200)
                                )
                                + "_"
                            ),
                        }
                    ],
                }
            )

        blocks.append(
            {
                "type": "actions",
                "block_id": f"{self._feedback_prefix}:{dispatch_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "👍 Helpful",
                            "emoji": True,
                        },
                        "style": "primary",
                        "action_id": f"{self._feedback_prefix}:thumbs_up",
                        "value": dispatch_id,
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "👎 Not quite",
                            "emoji": True,
                        },
                        "action_id": f"{self._feedback_prefix}:thumbs_down",
                        "value": dispatch_id,
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "🙋 Ask SME",
                            "emoji": True,
                        },
                        "action_id": self._ask_action,
                        "value": dispatch_id,
                    },
                ],
            }
        )

        payload_hash = _hash_payload(text, blocks)
        return EchoCard(
            text=text,
            blocks=blocks,
            payload_hash=payload_hash,
            candidate=top,
            similarity_pct=similarity_pct,
        )


# ── Helpers ─────────────────────────────────────────────────────


_MARKDOWN_ESCAPE_RE = re.compile(r"([<>&])")


def _sanitise_for_slack(s: str) -> str:
    """Escape Slack-significant chars and collapse whitespace.

    Slack interprets ``<...>`` as a link / user reference. We escape
    those plus ``&`` per Slack's documented rules.
    """
    if not s:
        return ""
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _sme_line(candidate: MatchCandidate) -> str:
    pieces: list[str] = []
    if candidate.speaker_id:
        pieces.append(f"*{_sanitise_for_slack(candidate.speaker_id)}*")
    if candidate.speaker_role:
        pieces.append(_sanitise_for_slack(candidate.speaker_role))
    if candidate.session_id:
        pieces.append(f"session `{_sanitise_for_slack(candidate.session_id)}`")
    if candidate.start_ms is not None and candidate.end_ms:
        pieces.append(_format_window(candidate.start_ms, candidate.end_ms))
    return " · ".join(pieces)


def _format_window(start_ms: int, end_ms: int) -> str:
    return f"{_ms_to_clock(start_ms)}–{_ms_to_clock(end_ms)}"


def _ms_to_clock(ms: int) -> str:
    if ms < 0:
        ms = 0
    total = ms // 1000
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _hash_payload(text: str, blocks: list[dict[str, Any]]) -> str:
    import json

    serialised = json.dumps(
        {"text": text, "blocks": blocks},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialised).hexdigest()
