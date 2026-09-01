"""
Chaos test runner for the canonical pipeline.

The pipeline's reliability story rests on three claims:
  1. A worker can die mid-step and the next worker resumes that step
     (PEL replay + orphan recovery).
  2. A step can fail and be retried up to max_attempts before
     quarantine (per-step retry counter).
  3. Vision-model unavailability triggers graceful degradation, not a
     workflow crash (degraded_stages + OCR fallback).

This runner exercises those claims as proper integration scenarios
against a running stack (docker compose up). Each scenario:
  - Submits a real upload via the orchestrator.
  - Triggers the chaos action (restart eyes mid-OCR, kill spine after
    minimal-artifact write, etc).
  - Watches the workflow to terminal state.
  - Asserts the expected outcome (success, success-degraded, or
    quarantined).

NOT a unit test. NOT a stable CI gate (these tests are slow and
require Docker). Run them before a release, after major changes to
the queue / sweeper / worker logic, and after Phase boundary
implementations.

Usage:
  python scripts/chaos/chaos_runner.py \
    --orchestrator-url http://localhost:8014 \
    --canary-token "$(cat /etc/nexus/canary-token)" \
    --sample-video /opt/nexus/canary/sample-30s.mp4 \
    --scenario restart_eyes_during_ocr
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import httpx


# ─── Scenario harness ────────────────────────────────────────


@dataclass
class ScenarioContext:
    orchestrator_url: str
    canary_token: str
    sample_video: str
    correlation_id: str
    workflow_id: str | None = None
    canonical_id: str | None = None


def _post_upload(ctx: ScenarioContext) -> None:
    headers = {
        "Authorization": "Bearer " + ctx.canary_token,
        "X-Tenant-Id": "chaos",
        "X-Canary": "1",
        "X-Correlation-Id": ctx.correlation_id,
    }
    with open(ctx.sample_video, "rb") as fh:
        files = {"video_file": (os.path.basename(ctx.sample_video), fh, "video/mp4")}
        data = {
            "tenant_id": "chaos",
            "user_email": "chaos@nexus.internal",
            "processing_profile": "fast",
            "canary": "true",
        }
        with httpx.Client(timeout=60) as cli:
            r = cli.post(
                ctx.orchestrator_url.rstrip("/")
                + "/api/v1/orchestrator/process",
                headers=headers, data=data, files=files,
            )
            r.raise_for_status()
            body = r.json()
    ctx.workflow_id = body.get("workflow_id")
    ctx.canonical_id = body.get("canonical_workflow_id") or ctx.workflow_id


def _poll_status(
    ctx: ScenarioContext, timeout_seconds: int,
) -> tuple[str, dict]:
    deadline = time.time() + timeout_seconds
    headers = {"Authorization": "Bearer " + ctx.canary_token}
    last_body: dict = {}
    while time.time() < deadline:
        with httpx.Client(timeout=15) as cli:
            r = cli.get(
                ctx.orchestrator_url.rstrip("/")
                + f"/api/v1/workflows/{ctx.canonical_id}",
                headers=headers,
            )
            if r.status_code == 404:
                time.sleep(5)
                continue
            r.raise_for_status()
            last_body = r.json() or {}
        status = last_body.get("status", "unknown")
        if status in ("success", "succeeded", "completed",
                      "failed", "quarantined", "cancelled"):
            return status, last_body
        time.sleep(5)
    return "timeout", last_body


def _docker(*args: str) -> str:
    p = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False,
    )
    if p.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed (rc={p.returncode}): {p.stderr}"
        )
    return p.stdout.strip()


def _sleep_until_step(
    ctx: ScenarioContext, target_step_substring: str, timeout: int = 120,
) -> bool:
    """Poll workflow status until current_stage contains a substring,
    or timeout. Returns True if the step was observed."""
    deadline = time.time() + timeout
    headers = {"Authorization": "Bearer " + ctx.canary_token}
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=10) as cli:
                r = cli.get(
                    ctx.orchestrator_url.rstrip("/")
                    + f"/api/v1/workflows/{ctx.canonical_id}",
                    headers=headers,
                )
                body = r.json() if r.status_code == 200 else {}
        except Exception:
            body = {}
        cur = (body.get("current_stage") or "")
        if target_step_substring in cur:
            return True
        time.sleep(2)
    return False


# ─── Scenarios ───────────────────────────────────────────────


def scenario_restart_eyes_during_ocr(ctx: ScenarioContext) -> dict:
    """Restart the eyes container mid-OCR. Expected outcome:
      - workflow reaches a success or success-with-degraded terminal
      - nexus_workflow_orphans_recovered_total incremented OR
        nexus_queue_pel_replay_total{lane=~"eyes.*"} incremented
    """
    _post_upload(ctx)
    print(f"[chaos] submitted workflow={ctx.canonical_id}")
    observed = _sleep_until_step(ctx, "ocr", timeout=180)
    print(f"[chaos] observed ocr stage: {observed}")
    if observed:
        print("[chaos] restarting eyes container…")
        _docker("compose", "restart", "eyes")
    status, body = _poll_status(ctx, timeout_seconds=1800)
    return {
        "scenario": "restart_eyes_during_ocr",
        "workflow_id": ctx.canonical_id,
        "final_status": status,
        "degraded_stages": (body.get("metadata") or {}).get("degraded_stages"),
        "passed": status in ("success", "succeeded", "completed"),
    }


def scenario_kill_spine_after_minimal_artifact(ctx: ScenarioContext) -> dict:
    """Kill spine right after the minimal-artifact write. Expected:
      - artifact_id present in checkpoint (minimal row exists in DB)
      - Workflow eventually completes (enrichment succeeds on restart
        OR enrichment_update added to degraded_stages, workflow succeeds)
    """
    _post_upload(ctx)
    print(f"[chaos] submitted workflow={ctx.canonical_id}")
    observed = _sleep_until_step(
        ctx, "persist_minimal_artifact", timeout=180,
    )
    print(f"[chaos] observed persist_minimal_artifact: {observed}")
    if observed:
        # Wait a couple seconds to ensure the row landed before we kill.
        time.sleep(3)
        print("[chaos] killing spine container…")
        _docker("compose", "kill", "spine")
        time.sleep(2)
        _docker("compose", "start", "spine")
    status, body = _poll_status(ctx, timeout_seconds=1800)
    return {
        "scenario": "kill_spine_after_minimal_artifact",
        "workflow_id": ctx.canonical_id,
        "final_status": status,
        "degraded_stages": (body.get("metadata") or {}).get("degraded_stages"),
        "passed": status in ("success", "succeeded", "completed"),
    }


def scenario_ollama_outage_during_analyze_scenes(ctx: ScenarioContext) -> dict:
    """Take ollama down so analyze_scenes can't reach LLaVA. Expected:
      - Workflow succeeds with degraded_stages containing 'analyze_scenes'
      - nexus_eyes_vision_degraded_total incremented
    """
    _post_upload(ctx)
    print(f"[chaos] submitted workflow={ctx.canonical_id}")
    observed = _sleep_until_step(ctx, "analyze_scenes", timeout=180)
    if observed:
        print("[chaos] stopping ollama…")
        _docker("compose", "stop", "ollama")
    status, body = _poll_status(ctx, timeout_seconds=1800)
    # Restore ollama for next scenarios.
    print("[chaos] restoring ollama…")
    _docker("compose", "start", "ollama")
    degraded = (body.get("metadata") or {}).get("degraded_stages") or []
    return {
        "scenario": "ollama_outage_during_analyze_scenes",
        "workflow_id": ctx.canonical_id,
        "final_status": status,
        "degraded_stages": degraded,
        "passed": status in ("success", "succeeded", "completed")
                  and "analyze_scenes" in degraded,
    }


SCENARIOS: dict[str, Callable[[ScenarioContext], dict]] = {
    "restart_eyes_during_ocr": scenario_restart_eyes_during_ocr,
    "kill_spine_after_minimal_artifact":
        scenario_kill_spine_after_minimal_artifact,
    "ollama_outage_during_analyze_scenes":
        scenario_ollama_outage_during_analyze_scenes,
}


# ─── CLI ─────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--orchestrator-url", required=True)
    parser.add_argument("--canary-token", required=True)
    parser.add_argument("--sample-video", required=True)
    parser.add_argument(
        "--scenario", choices=list(SCENARIOS.keys()) + ["all"], default="all",
    )
    args = parser.parse_args()

    selected = (
        list(SCENARIOS.keys()) if args.scenario == "all" else [args.scenario]
    )
    overall_pass = True
    results = []
    for name in selected:
        ctx = ScenarioContext(
            orchestrator_url=args.orchestrator_url,
            canary_token=args.canary_token,
            sample_video=args.sample_video,
            correlation_id=f"chaos-{uuid.uuid4().hex[:8]}",
        )
        print(f"\n=== running scenario: {name} ===")
        try:
            r = SCENARIOS[name](ctx)
        except Exception as e:
            r = {"scenario": name, "error": str(e), "passed": False}
        results.append(r)
        overall_pass = overall_pass and bool(r.get("passed"))
        print(json.dumps(r, indent=2, default=str))

    print("\n=== summary ===")
    print(json.dumps(results, indent=2, default=str))
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
