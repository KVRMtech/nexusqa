"""Event bus integration — subscribe to ``spine.canonical_artifact.ready``
and publish ``substrate.indexed`` / ``substrate.skipped`` / ``substrate.failed``.

Wraps ``nexus_sdk.events.EventBus`` so the worker code only sees
typed publish helpers and the subscription is in one place.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from nexus_sdk.events import EventBus, NexusEvent

logger = logging.getLogger(__name__)


class SubstrateEvents:
    SOURCE_TOPIC = "spine.canonical_artifact.ready"

    INDEXED = "substrate.indexed"
    SKIPPED = "substrate.skipped"
    FAILED = "substrate.failed"

    def __init__(self, bus: EventBus, *, enqueue):
        """
        Parameters
        ----------
        bus       : connected EventBus.
        enqueue   : async callable ``(tenant_id, session_id, artifact_id,
                    trace_id) -> None`` invoked on every received event.
        """
        self._bus = bus
        self._enqueue = enqueue
        self._subscribed = False

    async def subscribe(self) -> None:
        if self._subscribed:
            return
        await self._bus.subscribe(self.SOURCE_TOPIC, self._on_artifact_ready)
        self._subscribed = True
        logger.info(
            "substrate_events.subscribed topic=%s", self.SOURCE_TOPIC
        )

    async def _on_artifact_ready(self, event: NexusEvent) -> None:
        data = event.data or {}
        tenant_id = data.get("tenant_id") or event.tenant_id
        session_id = data.get("session_id") or event.session_id or ""
        artifact_id = data.get("artifact_id") or ""
        trace_id = event.trace_id or ""
        if not tenant_id or not artifact_id:
            logger.warning(
                "substrate_events.malformed_event missing tenant/artifact: %s",
                event,
            )
            return
        try:
            await self._enqueue(
                tenant_id=tenant_id,
                session_id=session_id,
                artifact_id=artifact_id,
                trace_id=trace_id,
            )
        except Exception as exc:
            logger.exception(
                "substrate_events.enqueue_failed artifact=%s err=%s",
                artifact_id,
                exc,
            )

    # ── Publishers ──────────────────────────────────────────────

    async def publish_substrate_indexed(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        session_id: str,
        artifact_id: str,
        segments_created: int,
        segments_skipped: int,
        segments_failed: int,
        outcome: str,
    ) -> None:
        await self._publish(
            self.INDEXED,
            tenant_id=tenant_id,
            trace_id=trace_id,
            session_id=session_id,
            data={
                "artifact_id": artifact_id,
                "session_id": session_id,
                "segments_created": segments_created,
                "segments_skipped": segments_skipped,
                "segments_failed": segments_failed,
                "outcome": outcome,
            },
        )

    async def publish_substrate_skipped(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        session_id: str,
        artifact_id: str,
        reason: str,
        detail: dict[str, Any],
    ) -> None:
        await self._publish(
            self.SKIPPED,
            tenant_id=tenant_id,
            trace_id=trace_id,
            session_id=session_id,
            data={
                "artifact_id": artifact_id,
                "session_id": session_id,
                "reason": reason,
                "detail": detail,
            },
        )

    async def publish_substrate_failed(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        session_id: str,
        artifact_id: str,
        error: str,
    ) -> None:
        await self._publish(
            self.FAILED,
            tenant_id=tenant_id,
            trace_id=trace_id,
            session_id=session_id,
            data={
                "artifact_id": artifact_id,
                "session_id": session_id,
                "error": error[:1024],
            },
        )

    async def _publish(
        self,
        event_type: str,
        *,
        tenant_id: str,
        trace_id: str,
        session_id: Optional[str],
        data: dict[str, Any],
    ) -> None:
        try:
            await self._bus.publish(
                NexusEvent(
                    event_type=event_type,
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    engine="knowledge-fusion",
                    session_id=session_id or None,
                    data=data,
                )
            )
        except Exception as exc:
            logger.warning(
                "substrate_events.publish_failed type=%s err=%s",
                event_type,
                exc,
            )
