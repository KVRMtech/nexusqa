"""
Nexus Queue Metrics Exporter — Prometheus metrics for GPU job queues.

Exposes queue depth metrics that K8s HPA uses for auto-scaling GPU worker pods.
Runs as a lightweight sidecar or standalone pod that polls Redis Streams.

Metrics exposed:
  nexus_queue_depth{engine="ears"}         — total jobs in stream
  nexus_queue_pending{engine="ears"}       — claimed but unacked
  nexus_queue_dlq_depth{engine="ears"}     — dead-letter queue size
  nexus_queue_consumer_count{engine="ears"} — active consumers
  nexus_queue_oldest_pending_ms{engine="ears"} — age of oldest unacked job

Usage:
  python -m nexus_sdk.queue_metrics

Or in a K8s deployment:
  CMD ["python", "-m", "nexus_sdk.queue_metrics"]
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from prometheus_client import Gauge, start_http_server

logger = logging.getLogger(__name__)

# ─── Prometheus Gauges ────────────────────────────────────────

QUEUE_DEPTH = Gauge(
    "nexus_queue_depth",
    "Total jobs waiting in the queue stream",
    ["engine"],
)

QUEUE_PENDING = Gauge(
    "nexus_queue_pending",
    "Jobs claimed by workers but not yet acknowledged",
    ["engine"],
)

QUEUE_DLQ_DEPTH = Gauge(
    "nexus_queue_dlq_depth",
    "Jobs in the dead-letter queue",
    ["engine"],
)

QUEUE_CONSUMER_COUNT = Gauge(
    "nexus_queue_consumers",
    "Number of active consumers in the worker group",
    ["engine"],
)

QUEUE_OLDEST_PENDING_MS = Gauge(
    "nexus_queue_oldest_pending_ms",
    "Age in milliseconds of the oldest unacknowledged job",
    ["engine"],
)


# ─── GPU engines to monitor ──────────────────────────────────

GPU_ENGINES = ["ears", "eyes", "heart"]


async def collect_metrics(
    redis_host: str,
    redis_port: int,
    redis_password: str,
    redis_db: int,
    engines: list[str],
) -> None:
    """Collect queue metrics from Redis Streams for all GPU engines."""
    import redis.asyncio as aioredis

    client = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        password=redis_password or None,
        db=redis_db,
        decode_responses=True,
        socket_connect_timeout=5,
    )

    try:
        await client.ping()
    except Exception as e:
        logger.error("Queue metrics: Redis connection failed: %s", e)
        return

    try:
        for engine in engines:
            stream_key = f"nexus:queue:{engine}"
            dlq_key = f"nexus:queue:{engine}:dlq"
            group_name = f"nexus:workers:{engine}"

            # Queue depth
            try:
                depth = await client.xlen(stream_key)
                QUEUE_DEPTH.labels(engine=engine).set(depth)
            except Exception:
                QUEUE_DEPTH.labels(engine=engine).set(0)

            # DLQ depth
            try:
                dlq_depth = await client.xlen(dlq_key)
                QUEUE_DLQ_DEPTH.labels(engine=engine).set(dlq_depth)
            except Exception:
                QUEUE_DLQ_DEPTH.labels(engine=engine).set(0)

            # Pending info from consumer group
            try:
                info = await client.xpending(stream_key, group_name)
                if isinstance(info, dict):
                    pending = info.get("pending", 0)
                    consumers = len(info.get("consumers", []))
                elif isinstance(info, (list, tuple)) and len(info) >= 4:
                    pending = info[0]
                    consumers = len(info[3]) if len(info) > 3 and info[3] else 0
                else:
                    pending = 0
                    consumers = 0

                QUEUE_PENDING.labels(engine=engine).set(pending)
                QUEUE_CONSUMER_COUNT.labels(engine=engine).set(consumers)
            except Exception:
                QUEUE_PENDING.labels(engine=engine).set(0)
                QUEUE_CONSUMER_COUNT.labels(engine=engine).set(0)

            # Oldest pending job age
            try:
                pending_detail = await client.xpending_range(
                    stream_key, group_name, min="-", max="+", count=1,
                )
                if pending_detail:
                    idle_ms = pending_detail[0].get("time_since_delivered", 0)
                    if isinstance(idle_ms, (int, float)):
                        QUEUE_OLDEST_PENDING_MS.labels(engine=engine).set(idle_ms)
                    else:
                        QUEUE_OLDEST_PENDING_MS.labels(engine=engine).set(0)
                else:
                    QUEUE_OLDEST_PENDING_MS.labels(engine=engine).set(0)
            except Exception:
                QUEUE_OLDEST_PENDING_MS.labels(engine=engine).set(0)

    finally:
        await client.aclose()


async def metrics_loop(
    redis_host: str,
    redis_port: int,
    redis_password: str,
    redis_db: int,
    engines: list[str],
    interval_seconds: int = 10,
) -> None:
    """Continuously collect metrics at the specified interval."""
    logger.info(
        "Queue metrics collector started: engines=%s interval=%ds",
        engines,
        interval_seconds,
    )
    while True:
        try:
            await collect_metrics(
                redis_host=redis_host,
                redis_port=redis_port,
                redis_password=redis_password,
                redis_db=redis_db,
                engines=engines,
            )
        except Exception as e:
            logger.error("Queue metrics collection error: %s", e)

        await asyncio.sleep(interval_seconds)


def main():
    """Entry point for the queue metrics exporter."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    redis_password = os.environ.get("REDIS_PASSWORD", "")
    redis_db = int(os.environ.get("REDIS_QUEUE_DB", "3"))
    metrics_port = int(os.environ.get("METRICS_PORT", "9191"))
    interval = int(os.environ.get("METRICS_INTERVAL_SECONDS", "10"))
    engines_str = os.environ.get("GPU_ENGINES", ",".join(GPU_ENGINES))
    engines = [e.strip() for e in engines_str.split(",") if e.strip()]

    # Start Prometheus HTTP server
    start_http_server(metrics_port)
    logger.info("Queue metrics exporter listening on :%d", metrics_port)

    # Run collection loop
    asyncio.run(metrics_loop(
        redis_host=redis_host,
        redis_port=redis_port,
        redis_password=redis_password,
        redis_db=redis_db,
        engines=engines,
        interval_seconds=interval,
    ))


if __name__ == "__main__":
    main()
