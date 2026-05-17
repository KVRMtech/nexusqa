"""
Canary uploader for the Nexus QA canonical pipeline.

Submits a synthetic upload to the orchestrator every N minutes,
watches the workflow to a terminal state, and either:

  - Pushes the result to a Prometheus pushgateway (preferred), OR
  - Writes a one-line JSON log to stdout that a log-based metric can
    scrape.

Purpose: surface "the pipeline accepts uploads but quietly drops them"
failures that don't show up in regular Prometheus metrics (because no
workflow_state row ever gets to a terminal state — there's nothing to
count). The canary is the only metric that says "an end-user upload
would succeed right now."

Usage (cron, every 15 min on the orchestrator host):

  */15 * * * * /usr/bin/python3 /opt/nexus/canary/canary_upload.py \
                 --orchestrator-url http://localhost:8014 \
                 --canary-token "$(cat /etc/nexus/canary-token)" \
                 --sample-video /opt/nexus/canary/sample-2s.mp4 \
                 --pushgateway http://prometheus-pushgateway:9091 \
                 --timeout-seconds 300

Usage (Kubernetes CronJob): see infrastructure/helm/nexus-qa/templates/canary-cronjob.yaml

The script exits 0 on canary success, non-zero on failure. The
pushgateway metrics let dashboards/alerts fire even when the script
itself is healthy (no successful upload in N min → alert).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx


CANARY_TENANT_ID = "nexus-canary"
CANARY_USER_EMAIL = "canary@nexus.internal"


def _now() -> float:
    return time.time()


def _log(payload: dict) -> None:
    """Single-line JSON log so a log-based metric can pick it up."""
    payload.setdefault("source", "nexus.canary")
    payload.setdefault("ts", _now())
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


def _push_metric(
    pushgateway: str, job: str, metrics: dict[str, float],
) -> None:
    """POST metrics to Prometheus pushgateway in the text exposition
    format. One job per canary run; pushgateway replaces by job."""
    if not pushgateway:
        return
    lines = []
    for name, value in metrics.items():
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    body = "\n".join(lines) + "\n"
    url = pushgateway.rstrip("/") + f"/metrics/job/{job}"
    try:
        with httpx.Client(timeout=10) as cli:
            r = cli.post(url, content=body)
            r.raise_for_status()
    except Exception as e:
        _log({"event": "pushgateway_failed", "err": str(e), "url": url})


def upload_and_watch(
    orchestrator_url: str,
    canary_token: str,
    sample_video: Path,
    timeout_seconds: int,
) -> dict:
    """Submit one upload and watch it to a terminal state. Returns a
    result dict suitable for both logging and pushgateway."""
    if not sample_video.exists():
        raise FileNotFoundError(f"sample video missing: {sample_video}")

    correlation_id = f"canary-{uuid.uuid4().hex[:8]}"
    started = _now()
    headers = {
        "Authorization": "Bearer " + canary_token,
        "X-Tenant-Id": CANARY_TENANT_ID,
        "X-Canary": "1",
        "X-Correlation-Id": correlation_id,
    }
    workflow_id: str | None = None
    canonical_id: str | None = None
    terminal_status: str = "timeout"
    error: str | None = None

    try:
        # ─── Submit upload ─────────────────────────────────
        with httpx.Client(timeout=60) as cli:
            with sample_video.open("rb") as fh:
                files = {
                    "video_file": (sample_video.name, fh, "video/mp4"),
                }
                data = {
                    "tenant_id": CANARY_TENANT_ID,
                    "user_email": CANARY_USER_EMAIL,
                    "processing_profile": "fast",
                    "canary": "true",
                }
                r = cli.post(
                    orchestrator_url.rstrip("/")
                    + "/api/v1/orchestrator/process",
                    headers=headers, data=data, files=files,
                )
                if r.status_code >= 400:
                    raise RuntimeError(
                        f"upload returned {r.status_code}: {r.text[:300]}"
                    )
                body = r.json()
                workflow_id = body.get("workflow_id")
                canonical_id = body.get("canonical_workflow_id") or body.get("workflow_id")
                _log({
                    "event": "uploaded",
                    "workflow_id": workflow_id,
                    "canonical_id": canonical_id,
                    "correlation_id": correlation_id,
                })

        # ─── Watch to terminal state ───────────────────────
        if not canonical_id:
            raise RuntimeError("orchestrator did not return canonical_workflow_id")
        deadline = started + timeout_seconds
        poll_interval = 5.0
        while _now() < deadline:
            with httpx.Client(timeout=15) as cli:
                r = cli.get(
                    orchestrator_url.rstrip("/")
                    + f"/api/v1/workflows/{canonical_id}",
                    headers=headers,
                )
                if r.status_code == 404:
                    # Workflow row not yet visible — give it a tick.
                    time.sleep(poll_interval)
                    continue
                r.raise_for_status()
                status = (r.json() or {}).get("status", "unknown")
            if status in ("succeeded", "completed", "success"):
                terminal_status = "success"
                break
            if status in ("failed", "quarantined", "cancelled"):
                terminal_status = status
                error = "workflow reached non-success terminal state"
                break
            time.sleep(poll_interval)
        else:
            error = f"workflow did not reach terminal state within {timeout_seconds}s"

    except Exception as e:
        terminal_status = "exception"
        error = str(e)

    elapsed = _now() - started
    return {
        "correlation_id": correlation_id,
        "workflow_id": workflow_id,
        "canonical_id": canonical_id,
        "status": terminal_status,
        "elapsed_seconds": elapsed,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--orchestrator-url", required=True)
    parser.add_argument("--canary-token", required=True)
    parser.add_argument("--sample-video", required=True, type=Path)
    parser.add_argument("--pushgateway", default="")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--job-name", default="nexus_canary_upload")
    args = parser.parse_args()

    started = _now()
    result = upload_and_watch(
        orchestrator_url=args.orchestrator_url,
        canary_token=args.canary_token,
        sample_video=args.sample_video,
        timeout_seconds=args.timeout_seconds,
    )
    result["completed_at"] = _now()
    _log({"event": "canary_complete", **result})

    success = 1.0 if result["status"] == "success" else 0.0
    _push_metric(
        args.pushgateway, args.job_name,
        {
            "nexus_canary_upload_success": success,
            "nexus_canary_upload_duration_seconds": result["elapsed_seconds"],
            "nexus_canary_upload_last_run_timestamp": _now(),
        },
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
