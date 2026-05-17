"""Adapter that exposes the Phase 2 Slack components as a SurfaceHandler.

The existing ``EchoCardComposer`` and ``SlackClient`` predate the surface
abstraction; this module wraps them without modification so Phase 4's
multi-surface orchestrator can call them through the same protocol it
uses for Teams/Email/Webhook.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from ..matcher import MatchResult
from ..slack import (
    EchoCardComposer,
    SlackClient,
    SlackClientError,
    SlackInstallationError,
    SlackInstallationLoader,
)
from .base import (
    ComposedPayload,
    DispatchOutcome,
    SurfaceError,
    SurfaceHandler,
    SurfaceUnavailable,
)

logger = logging.getLogger(__name__)


class SlackComposerAdapter:
    """Wraps ``EchoCardComposer`` into the ``SurfaceComposer`` protocol."""

    def __init__(self, composer: Optional[EchoCardComposer] = None):
        self._inner = composer or EchoCardComposer()

    def compose(
        self,
        *,
        dispatch_id: str,
        question_text: str,
        match: MatchResult,
    ) -> Optional[ComposedPayload]:
        card = self._inner.compose(
            dispatch_id=dispatch_id,
            question_text=question_text,
            match=match,
        )
        if card is None:
            return None
        return ComposedPayload(
            surface="slack",
            text=card.text,
            payload={"text": card.text, "blocks": card.blocks},
            payload_hash=card.payload_hash,
            similarity_pct=card.similarity_pct,
            primary_candidate=card.candidate.to_audit_dict(),
        )


class SlackDispatcherAdapter:
    """Wraps the Slack outbound client + installation loader."""

    def __init__(
        self,
        slack: SlackClient,
        installs: SlackInstallationLoader,
    ):
        self._slack = slack
        self._installs = installs

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
        except SlackInstallationError as exc:
            raise SurfaceUnavailable(str(exc)) from exc

        text = payload.text
        blocks = payload.payload.get("blocks") or []
        try:
            if as_dm:
                if not user_id_ext:
                    raise SurfaceError(
                        "slack DM requires user_id_ext"
                    )
                res = await self._slack.post_dm(
                    token=install.bot_token,
                    user_id=user_id_ext,
                    text=text,
                    blocks=blocks,
                )
                decision = "posted_dm"
            else:
                if not channel_id_ext:
                    raise SurfaceError(
                        "slack channel post requires channel_id_ext"
                    )
                res = await self._slack.post_message(
                    token=install.bot_token,
                    channel=channel_id_ext,
                    text=text,
                    blocks=blocks,
                    thread_ts=thread_ts,
                )
                decision = "posted_channel"
        except SlackClientError as exc:
            raise SurfaceError(str(exc)) from exc

        return DispatchOutcome(
            decision=decision,
            message_ref=res.message_ref,
            raw=res.raw,
        )


def build_slack_handler(
    *,
    slack: SlackClient,
    installs: SlackInstallationLoader,
    composer: Optional[EchoCardComposer] = None,
) -> SurfaceHandler:
    return SurfaceHandler(
        surface="slack",
        composer=SlackComposerAdapter(composer),
        dispatcher=SlackDispatcherAdapter(slack, installs),
    )


def payload_hash(text: str, payload: dict[str, Any]) -> str:
    """Stable SHA-256 of a (text, payload) tuple — shared by adapters."""
    body = json.dumps(
        {"text": text, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()
