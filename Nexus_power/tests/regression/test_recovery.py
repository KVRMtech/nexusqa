"""
Recovery regression — proves that no stuck workflow survives a worker
restart, which is the Phase 1 hard acceptance criterion.

Setup:
  - Submit a slow workflow (multimodal, full sample).
  - While a long step is running, kill the worker pod (via the cluster
    admin API) or restart its process locally.
  - Assert that the orchestrator's sweeper redispatches the step and
    the workflow completes within its deadline.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest


# These tests need cluster admin access. In CI they run in a dedicated
# regression cluster; locally skip unless explicitly enabled.
pytestmark = pytest.mark.skipif(
    not os.environ.get("NEXUS_REGRESSION_ALLOW_KILL"),
    reason="set NEXUS_REGRESSION_ALLOW_KILL=1 to run worker-kill tests",
)


def _kill_worker(engine: str) -> None:
    """Kill one pod of the named engine. kubectl context must point at
    the cluster under test."""
    cmd = [
        "kubectl", "delete", "pod",
        "-n", "nexus-qa",
        "-l", f"app.kubernetes.io/component={engine}-engine",
        "--field-selector=status.phase=Running",
        "--wait=false",
    ]
    subprocess.run(cmd, check=False, capture_output=True)


def test_worker_kill_during_video_extract(
    manifest,
    fixture_loader,
    orchestrator_client,
):
    fixture_id = "zoom-call-with-audio-01"
    fixture = manifest[fixture_id]
    media_path = fixture_loader(fixture_id)

    resp = orchestrator_client.post(
        "/api/v1/canonical-workflows",
        json={
            "kind": fixture.kind,
            "tenant_id": "regression",
            "session_id": f"recovery-{fixture_id}",
            "profile": "fast",
            "initial_state": {"input_file": str(media_path)},
            "metadata": {"regression_id": fixture_id, "scenario": "worker_kill"},
        },
    )
    assert resp.status_code in (200, 201)
    wf_id = resp.json()["workflow_id"]

    # Wait until the workflow advances past the first step, then kill.
    started_eyes = False
    deadline = time.time() + 1500
    killed_once = False

    while time.time() < deadline:
        body = orchestrator_client.get(f"/api/v1/canonical-workflows/{wf_id}").json()
        status = body["status"]
        step = body.get("current_step")
        if step and step.startswith("eyes.") and not started_eyes:
            started_eyes = True
        if started_eyes and not killed_once and step == "eyes.analyze_scenes":
            _kill_worker("eyes")
            killed_once = True
        if status in {"completed", "failed", "cancelled", "quarantined"}:
            break
        time.sleep(5)

    assert killed_once, "test never reached the kill window"
    body = orchestrator_client.get(f"/api/v1/canonical-workflows/{wf_id}").json()
    assert body["status"] == "completed", (
        f"recovery failed: status={body['status']} step={body.get('current_step')} "
        f"err={body.get('error')}"
    )
