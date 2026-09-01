"""
End-to-end soak driver — the Phase 1 throughput gate.

Submits N concurrent workflows over a sliding window, measures
end-to-end latency per fixture, and reports a PASS/FAIL based on:

  - sustained_uploads_per_hour >= TARGET_RATE (default 100)
  - p95_latency_seconds <= TARGET_P95 (default 900)
  - completed_ratio >= 0.99

Runs decoupled from pytest so it can be invoked from CI cron, a
Kubernetes Job, or an operator shell:

  python -m tests.regression.runner \
      --orchestrator http://orchestrator:8100 \
      --token "$NEXUS_REGRESSION_TOKEN" \
      --duration 3600 \
      --rate 100 \
      --fixture-id zoom-call-with-audio-01
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from tests.regression.conftest import load_manifest, _fetch_to_cache


logger = logging.getLogger("regression.runner")


@dataclass
class Stats:
    submitted: int = 0
    completed: int = 0
    quarantined: int = 0
    timed_out: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def _submit_one(
    client: httpx.AsyncClient,
    fixture,
    media_path: Path,
    stats: Stats,
    deadline_seconds: int,
) -> None:
    submitted_at = time.time()
    stats.submitted += 1
    try:
        resp = await client.post(
            "/api/v1/canonical-workflows",
            json={
                "kind": fixture.kind,
                "tenant_id": "regression",
                "session_id": f"soak-{int(submitted_at*1000)}",
                "profile": fixture.profile,
                "initial_state": {"input_file": str(media_path)},
                "metadata": {"regression_id": fixture.id, "scenario": "soak"},
                "deadline_seconds": deadline_seconds,
            },
        )
        resp.raise_for_status()
        wf_id = resp.json()["workflow_id"]
    except Exception as e:
        stats.errors.append(f"submit:{e}")
        return

    end_by = submitted_at + deadline_seconds + 60  # +60s grace
    while time.time() < end_by:
        try:
            r = await client.get(f"/api/v1/canonical-workflows/{wf_id}")
            r.raise_for_status()
            body = r.json()
        except Exception:
            await asyncio.sleep(5)
            continue
        status = body["status"]
        if status == "completed":
            stats.latencies.append(time.time() - submitted_at)
            stats.completed += 1
            return
        if status in {"failed", "cancelled", "quarantined"}:
            stats.quarantined += 1
            stats.errors.append(f"{wf_id}:{status}:{body.get('error')}")
            return
        await asyncio.sleep(5)
    stats.timed_out += 1
    stats.errors.append(f"{wf_id}:timeout")


async def run(args) -> Stats:
    manifest = load_manifest()
    fixture = manifest[args.fixture_id]
    media_path = _fetch_to_cache(fixture.source, fixture.sha256)

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    timeout = httpx.Timeout(60.0, connect=10.0)

    stats = Stats()
    interval = 3600 / args.rate          # seconds between submissions
    end_at = time.time() + args.duration

    async with httpx.AsyncClient(
        base_url=args.orchestrator, headers=headers, timeout=timeout,
    ) as client:
        tasks: list[asyncio.Task] = []
        while time.time() < end_at:
            tasks.append(asyncio.create_task(
                _submit_one(client, fixture, media_path, stats, args.deadline)
            ))
            await asyncio.sleep(interval)
        await asyncio.gather(*tasks, return_exceptions=True)
    return stats


def evaluate(stats: Stats, args) -> int:
    duration_hours = max(args.duration / 3600.0, 1e-6)
    sustained = stats.completed / duration_hours
    p95 = (
        statistics.quantiles(stats.latencies, n=20)[-1]
        if len(stats.latencies) >= 20
        else (max(stats.latencies) if stats.latencies else 0.0)
    )
    completed_ratio = stats.completed / max(stats.submitted, 1)

    print(json.dumps({
        "submitted": stats.submitted,
        "completed": stats.completed,
        "quarantined": stats.quarantined,
        "timed_out": stats.timed_out,
        "sustained_per_hour": round(sustained, 2),
        "p95_latency_seconds": round(p95, 1),
        "completed_ratio": round(completed_ratio, 4),
        "first_errors": stats.errors[:10],
    }, indent=2))

    failed = False
    if sustained < args.target_rate * 0.97:    # 3% slack
        print(f"FAIL: sustained {sustained:.1f}/h < target {args.target_rate}/h")
        failed = True
    if p95 > args.target_p95:
        print(f"FAIL: p95 {p95:.1f}s > target {args.target_p95}s")
        failed = True
    if completed_ratio < 0.99:
        print(f"FAIL: completed ratio {completed_ratio:.3f} < 0.99")
        failed = True
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--orchestrator", required=True)
    p.add_argument("--token", default=os.environ.get("NEXUS_REGRESSION_TOKEN", ""))
    p.add_argument("--fixture-id", default="zoom-call-with-audio-01")
    p.add_argument("--duration", type=int, default=3600)
    p.add_argument("--rate", type=float, default=100.0)
    p.add_argument("--deadline", type=int, default=900)
    p.add_argument("--target-rate", type=float, default=100.0)
    p.add_argument("--target-p95", type=float, default=900.0)
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    stats = asyncio.run(run(args))
    return evaluate(stats, args)


if __name__ == "__main__":
    raise SystemExit(main())
