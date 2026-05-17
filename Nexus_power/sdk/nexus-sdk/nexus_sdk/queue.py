"""
Nexus SDK — Durable Job Queue backed by Redis Streams.

Replaces in-memory FastAPI BackgroundTasks with a persistent,
horizontally-scalable job queue. GPU engines (Ears, Eyes, Heart)
use this to decouple job submission from job processing:

  API pods:     Accept requests → enqueue jobs → return job_id
  Worker pods:  Claim jobs from stream → process on GPU → ack

Redis Streams provide:
  - Durability: jobs survive pod restarts
  - Consumer groups: multiple workers share the load
  - Acknowledgment: unacked jobs get reclaimed after timeout
  - Dead-letter: failed jobs moved to DLQ after max retries
  - Metrics: queue depth exposed for K8s HPA scaling

Design:
  - One stream per engine: ``nexus:queue:{engine_name}``
  - One consumer group per engine: ``nexus:workers:{engine_name}``
  - Each worker has a unique consumer name (hostname + pid)
  - Pending Entry List (PEL) tracks claimed-but-unacked jobs
  - Idle timeout triggers reclaim of abandoned jobs

Usage (API side):
    queue = JobQueue(engine_name="ears", redis_host="localhost")
    await queue.connect()
    await queue.enqueue(job_id="abc", payload={...})

Usage (Worker side):
    queue = JobQueue(engine_name="ears", redis_host="localhost")
    await queue.connect()
    async for job in queue.consume():
        try:
            await process(job)
            await queue.ack(job["_msg_id"])
        except Exception:
            await queue.nack(job["_msg_id"], job)
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


@dataclass
class QueueConfig:
    """Configuration for the job queue."""

    engine_name: str
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 3

    # Consumer group settings
    consumer_group: str = ""  # auto-derived from engine_name if empty
    consumer_name: str = ""  # auto-derived from hostname+pid if empty

    # Processing settings
    batch_size: int = 1  # jobs to claim per read (1 for GPU-bound work)
    block_ms: int = 2000  # block timeout when waiting for jobs
    # Reclaim threshold for abandoned messages. The previous 5-min
    # default was shorter than long-running steps (eyes.ocr_frames on
    # CPU runs 10-20 min for a text-heavy screen recording), which
    # triggered an infinite reclaim loop: the consumer-group's
    # XAUTOCLAIM would re-deliver the message every 5 min and the new
    # consumer would re-process the entire OCR batch from frame 0,
    # never finishing before the next 5-min reclaim. 30 min is past
    # the worst-case OCR batch and still surfaces a genuinely dead
    # worker via the orchestrator's workflow-heartbeat sweeper
    # (orphan_threshold_seconds=120) ahead of any redelivery.
    claim_idle_ms: int = 1_800_000  # 30 min — accommodates long OCR/LLaVA batches
    max_retries: int = 3  # max retries before DLQ
    retry_base_delay: float = 2.0  # base delay in seconds for exponential backoff
    retry_max_delay: float = 60.0  # cap on backoff delay
    job_ttl_seconds: int = 86400 * 7  # 7 days stream retention
    # Phase 2: stream trimming on ack to bound Redis memory. Without
    # this, completed entries accumulate forever even after ack.
    trim_every_n_acks: int = 200      # trim once per N acks
    trim_maxlen: int = 10_000          # keep last ~10K entries

    def __post_init__(self):
        if not self.consumer_group:
            self.consumer_group = f"nexus:workers:{self.engine_name}"
        if not self.consumer_name:
            hostname = socket.gethostname()
            pid = os.getpid()
            self.consumer_name = f"{self.engine_name}-{hostname}-{pid}"


class JobQueue:
    """
    Durable job queue using Redis Streams.

    Supports two modes:
      - Producer: enqueue() jobs from API endpoints
      - Consumer: consume() jobs in worker pods

    Both modes share the same Redis stream per engine.
    """

    def __init__(
        self,
        engine_name: str,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_password: str = "",
        redis_db: int = 3,
        **kwargs,
    ):
        self._config = QueueConfig(
            engine_name=engine_name,
            redis_host=redis_host,
            redis_port=redis_port,
            redis_password=redis_password,
            redis_db=redis_db,
            **kwargs,
        )
        self._redis = None
        self._connected = False
        self._consuming = False

    # ─── Stream Keys ──────────────────────────────────────────

    @property
    def stream_key(self) -> str:
        return f"nexus:queue:{self._config.engine_name}"

    @property
    def dlq_key(self) -> str:
        return f"nexus:queue:{self._config.engine_name}:dlq"

    @property
    def metrics_key(self) -> str:
        return f"nexus:queue:{self._config.engine_name}:metrics"

    # ─── Connection ───────────────────────────────────────────

    async def connect(self) -> bool:
        """Connect to Redis and ensure consumer group exists."""
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.Redis(
                host=self._config.redis_host,
                port=self._config.redis_port,
                password=self._config.redis_password or None,
                db=self._config.redis_db,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=10,
                retry_on_timeout=True,
            )
            await self._redis.ping()

            # Create consumer group (idempotent)
            try:
                await self._redis.xgroup_create(
                    self.stream_key,
                    self._config.consumer_group,
                    id="0",
                    mkstream=True,
                )
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    raise

            self._connected = True
            logger.info(
                "JobQueue[%s]: connected, stream=%s group=%s consumer=%s",
                self._config.engine_name,
                self.stream_key,
                self._config.consumer_group,
                self._config.consumer_name,
            )
            return True

        except ImportError:
            logger.error("JobQueue: redis package not installed")
            return False
        except Exception as exc:
            logger.error("JobQueue[%s]: connection failed: %s", self._config.engine_name, exc)
            self._redis = None
            self._connected = False
            return False

    async def close(self):
        """Close Redis connection."""
        self._consuming = False
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            self._connected = False

    # ─── Producer: Enqueue Jobs ───────────────────────────────

    async def enqueue(
        self,
        job_id: str,
        payload: dict[str, Any],
        priority: str = "normal",
    ) -> str:
        """
        Add a job to the queue.

        Returns the Redis Stream message ID.
        Raises RuntimeError if not connected.
        """
        if not self._connected or not self._redis:
            raise RuntimeError("JobQueue not connected")

        message = {
            "job_id": job_id,
            "payload": json.dumps(payload, default=str),
            "priority": priority,
            "enqueued_at": str(time.time()),
            "retry_count": "0",
        }

        msg_id = await self._redis.xadd(
            self.stream_key,
            message,
        )

        # Update queue depth metric
        await self._update_metrics()

        logger.info(
            "JobQueue[%s]: enqueued job=%s msg_id=%s",
            self._config.engine_name,
            job_id,
            msg_id,
        )
        return msg_id

    # ─── Consumer: Claim & Process Jobs ───────────────────────

    async def consume(self) -> AsyncIterator[dict[str, Any]]:
        """
        Continuously claim and yield jobs from the stream.

        Yields dicts with keys: job_id, payload (parsed), _msg_id, _retry_count.
        The caller must call ack() after successful processing or nack() on failure.

        This method:
          0. Replays this consumer's own PEL (covers restart with same
             consumer_name and a result-POST failure that left a message
             in PEL without ack)
          1. Reclaims abandoned jobs from OTHER consumers (idle > claim_idle_ms)
          2. Reads new jobs from the stream
          3. Blocks for block_ms when no jobs are available
        """
        if not self._connected or not self._redis:
            raise RuntimeError("JobQueue not connected")

        self._consuming = True
        logger.info(
            "JobQueue[%s]: consumer started, consumer=%s",
            self._config.engine_name,
            self._config.consumer_name,
        )

        # Phase 0 (one-shot): replay our own PEL. Without this, a message
        # claimed by a previous worker incarnation (same consumer_name —
        # happens when Docker reuses a container hostname after rebuild,
        # or when the worker crashes after claim but before ack) would be
        # invisible to subsequent `xreadgroup STREAMS ... >` calls and
        # stuck until XAUTOCLAIM eventually fires (30 min default). This
        # bug caused canonical workflows to sit in eyes.ocr_frames "running"
        # forever after engine restarts.
        try:
            pel_results = await self._redis.xreadgroup(
                groupname=self._config.consumer_group,
                consumername=self._config.consumer_name,
                streams={self.stream_key: "0"},
                count=self._config.batch_size * 8,  # drain a reasonable batch
            )
            for _stream_name, messages in (pel_results or []):
                if not messages:
                    continue
                logger.warning(
                    "JobQueue[%s]: replaying %d entries from own PEL on startup "
                    "(consumer=%s)",
                    self._config.engine_name,
                    len(messages),
                    self._config.consumer_name,
                )
                # Phase 4 — count own-PEL replays so the dashboard can
                # alert on restart loops. Lane label = engine name.
                try:
                    from nexus_sdk.workflows.metrics import get_workflow_metrics
                    get_workflow_metrics().record_pel_replay(
                        lane=self._config.engine_name,
                        count=len(messages),
                    )
                except Exception:
                    pass
                for msg_id, msg_data in messages:
                    if not msg_data:
                        # PEL entry whose stream entry was already deleted —
                        # ack to clear the dangling claim.
                        await self._redis.xack(
                            self.stream_key,
                            self._config.consumer_group,
                            msg_id,
                        )
                        continue
                    job = self._parse_message(msg_id, msg_data)
                    if job:
                        yield job
        except Exception as e:
            logger.warning(
                "JobQueue[%s]: PEL replay failed (continuing): %s",
                self._config.engine_name, e,
            )

        while self._consuming:
            # Phase 1: Reclaim abandoned jobs from other dead consumers
            reclaimed = await self._reclaim_abandoned()
            for job in reclaimed:
                yield job

            # Phase 2: Read new jobs from stream
            try:
                results = await self._redis.xreadgroup(
                    groupname=self._config.consumer_group,
                    consumername=self._config.consumer_name,
                    streams={self.stream_key: ">"},
                    count=self._config.batch_size,
                    block=self._config.block_ms,
                )

                for _stream_name, messages in results:
                    for msg_id, msg_data in messages:
                        job = self._parse_message(msg_id, msg_data)
                        if job:
                            # Check exponential backoff: skip if retry_after hasn't elapsed
                            retry_after = float(job.get("retry_after", 0))
                            if retry_after > time.time():
                                # Not ready — re-add to stream tail and ack the
                                # current PEL entry so it doesn't block the consumer.
                                await self._redis.xadd(
                                    self.stream_key,
                                    msg_data,
                                )
                                await self._redis.xack(
                                    self.stream_key,
                                    self._config.consumer_group,
                                    msg_id,
                                )
                                continue
                            yield job

            except Exception as e:
                if self._consuming:
                    logger.error(
                        "JobQueue[%s]: consume error: %s",
                        self._config.engine_name,
                        e,
                    )
                    import asyncio
                    await asyncio.sleep(1)

    def stop(self):
        """Signal the consumer loop to stop."""
        self._consuming = False

    # ─── Acknowledgment ───────────────────────────────────────

    async def ack(self, msg_id: str) -> None:
        """Acknowledge successful processing of a job.

        Phase 2: opportunistically trim the stream after ack to keep
        Redis memory bounded. Without XTRIM, completed entries
        accumulate forever and a 7-day TTL plus busy queue eats RAM.
        We trim by length (MAXLEN ~) using a probabilistic delete
        (`~` = "approximately"), which is O(1) amortized for Redis.
        """
        if not self._connected or not self._redis:
            return
        await self._redis.xack(
            self.stream_key,
            self._config.consumer_group,
            msg_id,
        )
        # Trim every Nth ack to avoid touching the stream on every
        # message. With trim_every=200 and a stream growing at 1
        # msg/sec, we touch it every ~3 min.
        self._acks_since_trim = getattr(self, "_acks_since_trim", 0) + 1
        if self._acks_since_trim >= self._config.trim_every_n_acks:
            self._acks_since_trim = 0
            try:
                # MAXLEN ~ {limit} = approximate trim, much faster than
                # exact. Default keeps the last 10K entries which is
                # plenty for replay/audit and bounded for memory.
                await self._redis.xtrim(
                    self.stream_key,
                    maxlen=self._config.trim_maxlen,
                    approximate=True,
                )
            except Exception as e:
                logger.debug(
                    "JobQueue[%s]: xtrim failed (non-fatal): %s",
                    self._config.engine_name, e,
                )
        await self._update_metrics()
        logger.debug("JobQueue[%s]: acked msg_id=%s", self._config.engine_name, msg_id)

    async def nack(self, msg_id: str, job: dict[str, Any]) -> None:
        """
        Handle a failed job.

        If retry_count < max_retries: re-enqueue with incremented count.
        Otherwise: move to dead-letter queue.
        """
        if not self._connected or not self._redis:
            return

        retry_count = job.get("_retry_count", 0) + 1

        if retry_count >= self._config.max_retries:
            # Move to DLQ
            await self._move_to_dlq(msg_id, job, f"max retries ({self._config.max_retries}) exceeded")
        else:
            # Exponential backoff: delay = base * 2^(retry-1), capped at max_delay
            delay = min(
                self._config.retry_base_delay * (2 ** (retry_count - 1)),
                self._config.retry_max_delay,
            )
            retry_after = time.time() + delay

            # Re-enqueue with incremented retry count and scheduled time
            payload = job.get("payload", {})
            if isinstance(payload, str):
                payload_str = payload
            else:
                payload_str = json.dumps(payload, default=str)

            await self._redis.xadd(
                self.stream_key,
                {
                    "job_id": job.get("job_id", "unknown"),
                    "payload": payload_str,
                    "priority": job.get("priority", "normal"),
                    "enqueued_at": job.get("enqueued_at", str(time.time())),
                    "retry_count": str(retry_count),
                    "retry_after": str(retry_after),
                },
            )
            logger.warning(
                "JobQueue[%s]: retry %d/%d for job=%s (backoff %.1fs)",
                self._config.engine_name,
                retry_count,
                self._config.max_retries,
                job.get("job_id"),
                delay,
            )

        # Ack original message to remove from PEL
        await self._redis.xack(
            self.stream_key,
            self._config.consumer_group,
            msg_id,
        )
        await self._update_metrics()

    # ─── Abandoned Job Reclaim ────────────────────────────────

    async def _reclaim_abandoned(self) -> list[dict[str, Any]]:
        """
        Reclaim jobs that have been claimed but not acked within
        claim_idle_ms. This handles dead/restarted worker pods.
        """
        if not self._redis:
            return []

        reclaimed = []
        try:
            # XAUTOCLAIM: atomically reclaim idle messages
            result = await self._redis.xautoclaim(
                name=self.stream_key,
                groupname=self._config.consumer_group,
                consumername=self._config.consumer_name,
                min_idle_time=self._config.claim_idle_ms,
                count=self._config.batch_size,
            )

            # xautoclaim returns (next_start_id, [(msg_id, data), ...], [deleted_ids])
            if result and len(result) >= 2:
                messages = result[1]
                for msg_id, msg_data in messages:
                    if not msg_data:
                        continue
                    job = self._parse_message(msg_id, msg_data)
                    if job:
                        logger.warning(
                            "JobQueue[%s]: reclaimed abandoned job=%s msg_id=%s",
                            self._config.engine_name,
                            job.get("job_id"),
                            msg_id,
                        )
                        reclaimed.append(job)

        except Exception as e:
            logger.debug("JobQueue[%s]: reclaim error: %s", self._config.engine_name, e)

        return reclaimed

    # ─── Dead-Letter Queue ────────────────────────────────────

    async def _move_to_dlq(self, msg_id: str, job: dict, reason: str) -> None:
        """Move a permanently failed job to the dead-letter queue stream."""
        if not self._redis:
            return

        payload = job.get("payload", {})
        if not isinstance(payload, str):
            payload = json.dumps(payload, default=str)

        await self._redis.xadd(
            self.dlq_key,
            {
                "job_id": job.get("job_id", "unknown"),
                "payload": payload,
                "original_msg_id": str(msg_id),
                "reason": reason,
                "retry_count": str(job.get("_retry_count", 0)),
                "failed_at": str(time.time()),
            },
        )
        logger.error(
            "JobQueue[%s]: job=%s moved to DLQ: %s",
            self._config.engine_name,
            job.get("job_id"),
            reason,
        )

    async def get_dlq_jobs(self, count: int = 100) -> list[dict]:
        """Retrieve jobs from the dead-letter queue."""
        if not self._connected or not self._redis:
            return []
        raw = await self._redis.xrange(self.dlq_key, count=count)
        results = []
        for msg_id, data in raw:
            entry = dict(data)
            entry["_dlq_msg_id"] = msg_id
            if "payload" in entry:
                try:
                    entry["payload"] = json.loads(entry["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(entry)
        return results

    async def retry_dlq_job(self, dlq_msg_id: str) -> bool:
        """Re-enqueue a DLQ job back to the main stream."""
        if not self._connected or not self._redis:
            return False
        try:
            raw = await self._redis.xrange(self.dlq_key, min=dlq_msg_id, max=dlq_msg_id)
            if not raw:
                return False
            _, data = raw[0]
            payload = data.get("payload", "{}")
            await self._redis.xadd(
                self.stream_key,
                {
                    "job_id": data.get("job_id", "unknown"),
                    "payload": payload,
                    "priority": "normal",
                    "enqueued_at": str(time.time()),
                    "retry_count": "0",
                },
            )
            await self._redis.xdel(self.dlq_key, dlq_msg_id)
            return True
        except Exception as exc:
            logger.error("JobQueue[%s]: DLQ retry failed: %s", self._config.engine_name, exc)
            return False

    # ─── Metrics (for K8s HPA) ────────────────────────────────

    async def _update_metrics(self) -> None:
        """Update queue depth metrics in Redis for HPA consumption."""
        if not self._redis:
            return
        try:
            # Stream length = total pending + unread
            stream_len = await self._redis.xlen(self.stream_key)

            # Pending = claimed but not yet acked
            info = await self._redis.xpending(
                self.stream_key,
                self._config.consumer_group,
            )
            pending_count = info.get("pending", 0) if isinstance(info, dict) else (info[0] if info else 0)

            await self._redis.hset(
                self.metrics_key,
                mapping={
                    "queue_depth": str(stream_len),
                    "pending_count": str(pending_count),
                    "updated_at": str(time.time()),
                },
            )
        except Exception:
            pass

    async def get_queue_depth(self) -> int:
        """Get the current queue depth (for metrics/health checks)."""
        if not self._connected or not self._redis:
            return 0
        try:
            return await self._redis.xlen(self.stream_key)
        except Exception:
            return 0

    async def get_pending_count(self) -> int:
        """Get count of claimed-but-unacked jobs."""
        if not self._connected or not self._redis:
            return 0
        try:
            info = await self._redis.xpending(
                self.stream_key,
                self._config.consumer_group,
            )
            return info.get("pending", 0) if isinstance(info, dict) else (info[0] if info else 0)
        except Exception:
            return 0

    async def get_metrics(self) -> dict[str, Any]:
        """Get all queue metrics."""
        depth = await self.get_queue_depth()
        pending = await self.get_pending_count()
        dlq_depth = 0
        if self._connected and self._redis:
            try:
                dlq_depth = await self._redis.xlen(self.dlq_key)
            except Exception:
                pass

        return {
            "queue_depth": depth,
            "pending_count": pending,
            "dlq_depth": dlq_depth,
            "consumer_group": self._config.consumer_group,
            "consumer_name": self._config.consumer_name,
            "connected": self._connected,
        }

    # ─── Internals ────────────────────────────────────────────

    def _parse_message(self, msg_id: str, msg_data: dict) -> Optional[dict[str, Any]]:
        """Parse a raw Redis Stream message into a job dict."""
        try:
            payload_raw = msg_data.get("payload", "{}")
            try:
                payload = json.loads(payload_raw)
            except (json.JSONDecodeError, TypeError):
                payload = payload_raw

            return {
                "job_id": msg_data.get("job_id", "unknown"),
                "payload": payload,
                "priority": msg_data.get("priority", "normal"),
                "enqueued_at": msg_data.get("enqueued_at", "0"),
                "retry_after": msg_data.get("retry_after", "0"),
                "_msg_id": msg_id,
                "_retry_count": int(msg_data.get("retry_count", "0")),
            }
        except Exception as e:
            logger.error("JobQueue[%s]: parse error: %s", self._config.engine_name, e)
            return None

    @property
    def is_connected(self) -> bool:
        return self._connected
