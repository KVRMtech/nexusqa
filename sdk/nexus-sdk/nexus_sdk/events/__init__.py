"""
Nexus Event Bus — Pub/Sub via Redis Streams.

Every engine publishes events when it finishes work.
Other engines subscribe to events they care about.

This is how engines communicate WITHOUT tight coupling:
- Ears publishes "transcription.completed" 
- Shield subscribes and auto-processes the transcript
- Heart subscribes to "shield.redaction.completed"
- Backbone subscribes to "heart.rules.extracted"
- etc.

All events flow through Redis Streams for durability and replay.
"""

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from dataclasses import dataclass, field

import redis.asyncio as redis
import structlog

__all__ = [
    "NexusEvent",
    "EventBus",
    "fire_stub_alert",
]

logger = structlog.get_logger()


@dataclass
class NexusEvent:
    """A single event published by an engine."""
    
    event_type: str            # e.g., "ears.transcription.completed"
    tenant_id: str             # Which client's data
    trace_id: str              # For distributed tracing
    engine: str                # Which engine published this
    data: dict[str, Any]       # Event payload
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    session_id: Optional[str] = None  # KT session if applicable

    def to_dict(self) -> dict[str, str]:
        """Serialize for Redis Stream (all values must be strings)."""
        return {
            "event_type": self.event_type,
            "tenant_id": self.tenant_id,
            "trace_id": self.trace_id,
            "engine": self.engine,
            "session_id": self.session_id or "",
            "timestamp": self.timestamp,
            "data": json.dumps(self.data),
        }

    @classmethod
    def from_dict(cls, raw: dict[bytes, bytes]) -> "NexusEvent":
        """Deserialize from Redis Stream."""
        decoded = {k.decode(): v.decode() for k, v in raw.items()}
        return cls(
            event_type=decoded["event_type"],
            tenant_id=decoded["tenant_id"],
            trace_id=decoded["trace_id"],
            engine=decoded["engine"],
            session_id=decoded.get("session_id") or None,
            timestamp=decoded["timestamp"],
            data=json.loads(decoded.get("data", "{}")),
        )


class EventBus:
    """
    Redis Streams-based event bus for inter-engine communication.
    
    Usage:
        bus = EventBus(redis_url="redis://localhost:6379")
        await bus.connect()
        
        # Publish
        await bus.publish(NexusEvent(
            event_type="ears.transcription.completed",
            tenant_id="tenant_1",
            trace_id="abc-123",
            engine="ears",
            data={"transcript_id": "xyz"}
        ))
        
        # Subscribe
        async def handler(event: NexusEvent):
            print(f"Got event: {event.event_type}")
        
        await bus.subscribe("ears.transcription.completed", handler)
    """

    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_password: str = "",
        consumer_group: str = "nexus",
        consumer_name: Optional[str] = None,
    ):
        self._redis_host = redis_host
        self._redis_port = redis_port
        self._redis_password = redis_password
        self._consumer_group = consumer_group
        self._consumer_name = consumer_name or f"consumer-{id(self)}"
        self._client: Optional[redis.Redis] = None
        self._handlers: dict[str, list[Callable]] = {}
        self._running = False

    async def connect(self) -> None:
        """Connect to Redis."""
        self._client = redis.Redis(
            host=self._redis_host,
            port=self._redis_port,
            password=self._redis_password or None,
            decode_responses=False,
        )
        await self._client.ping()
        logger.info("event_bus.connected", host=self._redis_host, port=self._redis_port)

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        self._running = False
        if self._client:
            await self._client.aclose()
            logger.info("event_bus.disconnected")

    async def publish(self, event: NexusEvent) -> str:
        """
        Publish an event to the stream.
        
        Stream name = event_type (e.g., "ears.transcription.completed")
        Returns the message ID.
        """
        if not self._client:
            raise RuntimeError("EventBus not connected. Call connect() first.")

        stream_name = f"nexus:{event.event_type}"
        msg_id = await self._client.xadd(stream_name, event.to_dict())
        
        logger.info(
            "event_bus.published",
            event_type=event.event_type,
            tenant_id=event.tenant_id,
            trace_id=event.trace_id,
            stream=stream_name,
        )
        return msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)

    async def subscribe(
        self,
        event_type: str,
        handler: Callable[[NexusEvent], Any],
    ) -> None:
        """Register a handler for an event type."""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        
        # Ensure consumer group exists
        stream_name = f"nexus:{event_type}"
        try:
            if self._client:
                await self._client.xgroup_create(
                    stream_name, self._consumer_group, id="0", mkstream=True
                )
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                logger.warning("event_bus.subscribe_failed", event_type=event_type, error=str(e))
                return
        except Exception as e:
            logger.warning("event_bus.subscribe_failed", event_type=event_type, error=str(e))
            return
        
        logger.info("event_bus.subscribed", event_type=event_type, handler=handler.__name__)

    async def start_consuming(self) -> None:
        """Start consuming events from all subscribed streams."""
        if not self._handlers:
            logger.warning("event_bus.no_subscriptions")
            return

        self._running = True
        streams = {f"nexus:{et}": ">" for et in self._handlers}

        logger.info("event_bus.consuming_started", streams=list(self._handlers.keys()))

        while self._running:
            try:
                if not self._client:
                    break
                    
                results = await self._client.xreadgroup(
                    groupname=self._consumer_group,
                    consumername=self._consumer_name,
                    streams=streams,
                    count=10,
                    block=1000,  # Block for 1 second
                )

                for stream_name_bytes, messages in results:
                    stream_name = stream_name_bytes.decode()
                    event_type = stream_name.replace("nexus:", "")

                    for msg_id, msg_data in messages:
                        try:
                            event = NexusEvent.from_dict(msg_data)
                            
                            for handler in self._handlers.get(event_type, []):
                                await handler(event)

                            # Acknowledge the message
                            await self._client.xack(
                                stream_name, self._consumer_group, msg_id
                            )
                        except Exception as e:
                            logger.error(
                                "event_bus.handler_error",
                                event_type=event_type,
                                error=str(e),
                            )
                            # Move failed message to dead-letter queue
                            await self._move_to_dlq(
                                stream_name, self._consumer_group,
                                msg_id, msg_data, event_type, e,
                            )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("event_bus.consume_error", error=str(e))
                await asyncio.sleep(1)

        logger.info("event_bus.consuming_stopped")

    async def stop_consuming(self) -> None:
        """Stop the event consumer loop."""
        self._running = False

    # ─── Dead-Letter Queue ─────────────────────────────────────

    @staticmethod
    def _dlq_stream(event_type: str) -> str:
        """Return the DLQ stream name for a given event type."""
        return f"nexus:dlq:{event_type}"

    async def _move_to_dlq(
        self,
        stream_name: str,
        consumer_group: str,
        msg_id: bytes,
        msg_data: dict[bytes, bytes],
        event_type: str,
        error: Exception,
    ) -> None:
        """Move a failed message to the dead-letter queue stream and ACK original."""
        if not self._client:
            return
        try:
            dlq_payload: dict[str, str] = {
                k.decode() if isinstance(k, bytes) else k:
                v.decode() if isinstance(v, bytes) else v
                for k, v in msg_data.items()
            }
            dlq_payload["_dlq_original_stream"] = stream_name
            dlq_payload["_dlq_original_msg_id"] = (
                msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            )
            dlq_payload["_dlq_error"] = str(error)
            dlq_payload["_dlq_timestamp"] = datetime.now(timezone.utc).isoformat()

            await self._client.xadd(self._dlq_stream(event_type), dlq_payload)
            # Acknowledge original so it leaves the pending list
            await self._client.xack(stream_name, consumer_group, msg_id)
            logger.warning(
                "event_bus.moved_to_dlq",
                event_type=event_type,
                original_msg_id=dlq_payload["_dlq_original_msg_id"],
            )
        except Exception as dlq_err:
            # Never let DLQ bookkeeping break the main loop
            logger.error("event_bus.dlq_write_failed", error=str(dlq_err))

    async def get_dlq_messages(
        self, event_type: str, count: int = 100
    ) -> list[dict[str, str]]:
        """
        Retrieve dead-letter messages for a given event type.

        Returns a list of dicts (each dict is a DLQ message with metadata).
        """
        if not self._client:
            return []
        try:
            raw = await self._client.xrange(
                self._dlq_stream(event_type), count=count,
            )
            results: list[dict[str, str]] = []
            for msg_id, msg_data in raw:
                entry = {
                    k.decode() if isinstance(k, bytes) else k:
                    v.decode() if isinstance(v, bytes) else v
                    for k, v in msg_data.items()
                }
                entry["_dlq_msg_id"] = (
                    msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
                )
                results.append(entry)
            return results
        except Exception as exc:
            logger.error("event_bus.dlq_read_failed", error=str(exc))
            return []

    async def retry_dlq_message(
        self, event_type: str, dlq_msg_id: str
    ) -> bool:
        """
        Re-publish a DLQ message back to its original stream and delete
        it from the DLQ.

        Returns True if the message was successfully republished.
        """
        if not self._client:
            return False
        try:
            raw = await self._client.xrange(
                self._dlq_stream(event_type),
                min=dlq_msg_id, max=dlq_msg_id,
            )
            if not raw:
                logger.warning("event_bus.dlq_msg_not_found", dlq_msg_id=dlq_msg_id)
                return False

            _, msg_data = raw[0]
            entry = {
                k.decode() if isinstance(k, bytes) else k:
                v.decode() if isinstance(v, bytes) else v
                for k, v in msg_data.items()
            }
            original_stream = entry.pop("_dlq_original_stream", f"nexus:{event_type}")
            entry.pop("_dlq_original_msg_id", None)
            entry.pop("_dlq_error", None)
            entry.pop("_dlq_timestamp", None)
            entry.pop("_dlq_msg_id", None)

            # Re-publish to original stream
            await self._client.xadd(original_stream, entry)
            # Remove from DLQ
            await self._client.xdel(self._dlq_stream(event_type), dlq_msg_id)
            logger.info(
                "event_bus.dlq_message_retried",
                event_type=event_type,
                dlq_msg_id=dlq_msg_id,
            )
            return True
        except Exception as exc:
            logger.error("event_bus.dlq_retry_failed", error=str(exc))
            return False


# ─── Stub Fallback Alerting ───────────────────────────────────

def fire_stub_alert(
    event_bus: Optional[EventBus],
    engine_name: str,
    component: str,
    fallback_count: int = 1,
    reason: str = "model not available",
) -> None:
    """
    Fire-and-forget alert when an engine falls back to stub mode.

    Safe to call from both sync and async contexts.
    Publishes a ``{engine}.stub.fallback`` event to the event bus
    so monitoring dashboards / alert rules can react.
    """
    if event_bus is None or event_bus._client is None:
        return

    async def _publish() -> None:
        try:
            await event_bus.publish(NexusEvent(
                event_type=f"{engine_name}.stub.fallback",
                tenant_id="system",
                trace_id=f"stub-{engine_name}-{component}",
                engine=engine_name,
                data={
                    "component": component,
                    "fallback_count": fallback_count,
                    "reason": reason,
                    "severity": "warning",
                },
            ))
        except Exception:
            # Never let alerting break the hot path
            pass

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_publish())
    except RuntimeError:
        # No running loop — skip silently
        pass
