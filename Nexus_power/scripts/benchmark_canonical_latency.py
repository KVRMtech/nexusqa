"""Benchmark canonical workflow latency for a local media file.

Usage:
    python scripts/benchmark_canonical_latency.py --video path/to/file.mp4
    python scripts/benchmark_canonical_latency.py --video path/to/file.mp4 --signoff
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


BENCHMARK_PROFILES = {
    "local-cpu-seeded": {
        "poll_interval": 5.0,
        "timeout_seconds": 5400.0,
        # P4: Per-stage SLA thresholds (seconds). Exceeding these
        # does NOT fail the benchmark — it emits a warning so the
        # team can triage latency regressions.
        "stage_sla_seconds": {
            "media_probe": 30,
            "audio_transcription": 2400,  # 40 min on CPU (diarization heavy)
            "visual_extraction": 1800,    # 30 min for Ollama on CPU
            "pii_redaction": 30,
            "visual_graph_assembly": 60,
            "artifact_persistence": 30,
            "canonical_quality_gate": 60,
        },
        "preflight": [
            {
                "name": "ears",
                "url_env": "NEXUS_EARS_HEALTH_URL",
                "default_url": "http://localhost:8002/health",
                "allowed_statuses": ["healthy", "degraded"],
                "mode_contains": {
                    "transcriber": ["whisper active="],
                },
            },
            {
                "name": "eyes",
                "url_env": "NEXUS_EYES_HEALTH_URL",
                "default_url": "http://localhost:8003/health",
                "allowed_statuses": ["healthy", "degraded"],
                "mode_contains": {
                    "visual_analyzer": ["ollama"],
                },
            },
        ],
    },
    "production-gpu-seeded": {
        "poll_interval": 10.0,
        "timeout_seconds": 3600.0,
        "stage_sla_seconds": {
            "media_probe": 10,
            "audio_transcription": 300,   # 5 min with GPU
            "visual_extraction": 600,     # 10 min with GPU
            "pii_redaction": 10,
            "visual_graph_assembly": 30,
            "artifact_persistence": 15,
            "canonical_quality_gate": 30,
        },
        "preflight": [
            {
                "name": "ears",
                "url_env": "NEXUS_EARS_HEALTH_URL",
                "default_url": "http://localhost:8002/health",
                "required_status": "healthy",
                "mode_contains": {
                    "transcriber": ["whisper active=", "device=cuda"],
                    "diarizer": ["pyannote", "device=cuda"],
                },
            },
            {
                "name": "eyes",
                "url_env": "NEXUS_EYES_HEALTH_URL",
                "default_url": "http://localhost:8003/health",
                "required_status": "healthy",
                "mode_contains": {
                    "ocr": ["easyocr", "device=cuda"],
                    "visual_analyzer": ["ollama"],
                },
            },
        ],
    },
}


def _json_request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    retries: int = 0,
    retry_delay_seconds: float = 2.0,
) -> dict:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except (urllib.error.URLError, ConnectionResetError, TimeoutError, socket.timeout):
            if attempt >= retries:
                raise
            time.sleep(retry_delay_seconds)


def _login(base_url: str, email: str, password: str) -> str:
    payload = json.dumps({"email": email, "password": password}).encode()
    data = _json_request(
        f"{base_url}/api/v1/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return data["access_token"]


def _resolve_profile(profile_name: str | None) -> dict | None:
    if not profile_name:
        return None
    return BENCHMARK_PROFILES[profile_name]


def _health_preflight(profile_name: str | None) -> dict:
    profile = _resolve_profile(profile_name)
    if not profile:
        return {"profile": None, "checks": []}

    checks: list[dict] = []
    for check in profile.get("preflight", []):
        url = os.environ.get(check["url_env"], check["default_url"])
        payload = _json_request(url, retries=2)
        observed_status = payload.get("status")
        modes = payload.get("modes", {})

        allowed_statuses = check.get("allowed_statuses")
        if allowed_statuses:
            if observed_status not in allowed_statuses:
                raise RuntimeError(
                    f"preflight {check['name']} expected status in {allowed_statuses} got {observed_status}"
                )
        elif observed_status != check["required_status"]:
            raise RuntimeError(
                f"preflight {check['name']} expected status={check['required_status']} got {observed_status}"
            )

        for mode_name, required_substrings in check.get("mode_contains", {}).items():
            observed_mode = str(modes.get(mode_name, ""))
            missing = [item for item in required_substrings if item not in observed_mode]
            if missing:
                raise RuntimeError(
                    f"preflight {check['name']} mode {mode_name} missing {missing}; observed={observed_mode}"
                )

        checks.append({
            "name": check["name"],
            "url": url,
            "status": observed_status,
            "modes": modes,
        })

    return {"profile": profile_name, "checks": checks}


def _signoff_preflight(base_url: str, token: str) -> dict:
    """Query admin engine health API and verify all engines are signoff-ready.

    Returns the admin response. Raises RuntimeError if any engine is not
    signoff-ready (stub mode, degraded, or unreachable).
    """
    admin_data = _json_request(
        f"{base_url}/api/v1/admin/engines",
        headers={"Authorization": f"Bearer {token}"},
        retries=2,
    )

    # New admin API returns {"engines": [...], "signoff_ready": bool, ...}
    engines = admin_data.get("engines", admin_data if isinstance(admin_data, list) else [])
    aggregate_ready = admin_data.get("signoff_ready")

    failures: list[str] = []
    for eng in engines:
        name = eng.get("name", "unknown")
        ready = eng.get("signoff_ready", False)
        mode = eng.get("mode", "unknown")
        status = eng.get("status", "unknown")
        if not ready:
            failures.append(f"{name}: status={status}, mode={mode}")

    if failures or aggregate_ready is False:
        msg = "Signoff preflight FAILED — engines not production-ready:\n"
        msg += "\n".join(f"  - {f}" for f in failures)
        raise RuntimeError(msg)

    return admin_data


def _build_multipart(video_path: Path, boundary: str) -> bytes:
    file_bytes = video_path.read_bytes()
    lines: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        lines.extend([
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            value.encode(),
        ])

    add_field("session_id", str(uuid.uuid4()))
    add_field("language", "en")

    lines.extend([
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="video"; filename="{video_path.name}"'.encode(),
        b"Content-Type: video/mp4",
        b"",
        file_bytes,
        f"--{boundary}--".encode(),
        b"",
    ])

    return b"\r\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(BENCHMARK_PROFILES.keys()))
    parser.add_argument("--base-url", default=os.environ.get("NEXUS_API_BASE", "http://localhost:8080"))
    parser.add_argument("--email", default=os.environ.get("NEXUS_BENCH_EMAIL", "admin@nexus.local"))
    parser.add_argument("--password", default=os.environ.get("NEXUS_BENCH_PASSWORD", "change-this-password"))
    parser.add_argument("--video", required=True)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=5400.0)
    parser.add_argument("--signoff", action="store_true",
                        help="Enforce signoff-ready check: fail if any engine is in stub mode or unreachable")
    args = parser.parse_args()

    profile = _resolve_profile(args.profile)
    if profile:
        if args.poll_interval == 5.0:
            args.poll_interval = profile["poll_interval"]
        if args.timeout_seconds == 5400.0:
            args.timeout_seconds = profile["timeout_seconds"]

    video_path = Path(args.video)
    if not video_path.exists():
        print(json.dumps({"error": f"Video not found: {video_path}"}))
        return 1

    preflight = _health_preflight(args.profile)

    token = _login(args.base_url, args.email, args.password)

    # ── Fix #6: Signoff guardrail ─────────────────────────────
    signoff_result = None
    if args.signoff:
        try:
            signoff_result = _signoff_preflight(args.base_url, token)
            print(json.dumps({"signoff_preflight": "PASS", "engines": signoff_result.get("engines", [])}), flush=True)
        except RuntimeError as exc:
            print(json.dumps({"signoff_preflight": "FAIL", "error": str(exc)}), flush=True)
            return 6  # signoff preflight failure

    boundary = f"----nexusbench{int(time.time() * 1000)}"
    body = _build_multipart(video_path, boundary)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }

    started = time.time()
    response = _json_request(
        f"{args.base_url}/api/v1/orchestrator/process",
        data=body,
        headers=headers,
        method="POST",
    )
    workflow_id = response["workflow_id"]
    print(json.dumps({"workflow_id": workflow_id, "status": "submitted"}), flush=True)

    # ── Fix #7: Enhanced polling with live stage status ────────
    last_printed_stage = None
    stage_start_times: dict[str, float] = {}
    stage_slas = (profile or {}).get("stage_sla_seconds", {})

    while True:
        workflow = _json_request(
            f"{args.base_url}/api/v1/orchestrator/workflows/{workflow_id}",
            headers={"Authorization": f"Bearer {token}"},
            retries=3,
        )
        status = workflow.get("status")

        # Print per-stage live status
        stages = workflow.get("stages", {})
        for stage_id, stage in stages.items():
            s_status = stage.get("status")
            if s_status == "running" and stage_id != last_printed_stage:
                stage_start_times.setdefault(stage_id, time.time())
                progress = stage.get("progress_detail", {})
                progress_pct = progress.get("progress_percent", "")
                current_sub = progress.get("current_stage", "")
                msg = f"  [{stage_id}] running"
                if progress_pct:
                    msg += f" ({progress_pct}%)"
                if current_sub:
                    msg += f" — {current_sub}"
                print(msg, flush=True)
                last_printed_stage = stage_id
            elif s_status == "running" and stage_id in stage_start_times:
                # Show Ears/Eyes progress updates on each poll
                progress = stage.get("progress_detail", {})
                progress_pct = progress.get("progress_percent")
                stall_sec = progress.get("stall_seconds")
                if progress_pct is not None:
                    elapsed = round(time.time() - stage_start_times[stage_id], 1)
                    msg = f"  [{stage_id}] {progress_pct}% — {elapsed}s elapsed"
                    if stall_sec and stall_sec > 60:
                        msg += f" (stall: {stall_sec}s)"
                    print(msg, flush=True)

                # Fail fast on SLA breach for local benchmark
                sla_limit = stage_slas.get(stage_id)
                if sla_limit and stage_id in stage_start_times:
                    stage_elapsed = time.time() - stage_start_times[stage_id]
                    if stage_elapsed > sla_limit * 1.5:
                        print(json.dumps({
                            "workflow_id": workflow_id,
                            "status": "sla_fail_fast",
                            "stage": stage_id,
                            "elapsed_seconds": round(stage_elapsed, 1),
                            "sla_seconds": sla_limit,
                        }, indent=2), flush=True)
                        # Don't hard-fail — just warn and continue
                        # The caller can interpret exit code 5 for SLA breach

            elif s_status in ("completed", "failed", "skipped") and stage_id in stage_start_times:
                duration = stage.get("duration_ms", 0)
                dur_s = round(duration / 1000, 1) if duration else round(time.time() - stage_start_times[stage_id], 1)
                print(f"  [{stage_id}] {s_status} in {dur_s}s", flush=True)
                del stage_start_times[stage_id]

        if status in {"completed", "degraded", "failed", "cancelled", "needs_review", "policy_blocked"}:
            break
        if time.time() - started > args.timeout_seconds:
            print(json.dumps({"workflow_id": workflow_id, "status": "timeout"}, indent=2))
            return 2
        time.sleep(args.poll_interval)

    artifact = None
    artifact_id = ((workflow.get("stages", {}).get("artifact_persistence") or {}).get("output") or {}).get("artifact_id")
    if artifact_id:
        artifact = _json_request(
            f"{args.base_url}/api/v1/artifacts/{artifact_id}/status",
            headers={"Authorization": f"Bearer {token}"},
            retries=3,
        )

    summary = {
        "profile": args.profile,
        "preflight": preflight,
        "signoff_preflight": signoff_result if args.signoff else None,
        "workflow_id": workflow_id,
        "status": workflow.get("status"),
        "created_at": workflow.get("created_at"),
        "started_at": workflow.get("started_at"),
        "completed_at": workflow.get("completed_at"),
        "total_wall_seconds": round(time.time() - started, 2),
        "stages": {},
        "stage_sla_breaches": [],
        "artifact": artifact,
        "failure_classification": None,  # Fix #7: classify failures
    }

    # P4: Expose stage truth with per-stage SLA checks
    stage_slas = (profile or {}).get("stage_sla_seconds", {})
    for stage_id, stage in workflow.get("stages", {}).items():
        duration_ms = stage.get("duration_ms", 0)
        duration_s = round(duration_ms / 1000.0, 2) if duration_ms else 0
        sla_limit = stage_slas.get(stage_id)
        sla_ok = True
        if sla_limit and duration_s > sla_limit:
            sla_ok = False
            summary["stage_sla_breaches"].append({
                "stage": stage_id,
                "duration_seconds": duration_s,
                "sla_seconds": sla_limit,
                "overshoot_seconds": round(duration_s - sla_limit, 2),
            })

        stage_info = {
            "status": stage.get("status"),
            "duration_ms": duration_ms,
            "duration_seconds": duration_s,
            "error": stage.get("error"),
        }
        if sla_limit:
            stage_info["sla_seconds"] = sla_limit
            stage_info["sla_ok"] = sla_ok
        # P1: Include progress detail if available
        progress = stage.get("progress_detail")
        if progress:
            stage_info["progress_detail"] = progress
        summary["stages"][stage_id] = stage_info

    # ── Fix #7: Failure classification ──────────────────────────
    # Exit code 0  = execution green AND business green
    # Exit code 3  = execution failure (workflow failed/cancelled)
    # Exit code 4  = business non-green (degraded/policy_blocked/needs_review/qg fail)
    # Exit code 5  = execution green but SLA breaches detected
    # Exit code 6  = signoff preflight failure (--signoff mode)
    workflow_status = workflow.get("status")
    artifact_outcome = (artifact or {}).get("quality_gate_outcome")

    # Execution green = workflow completed or degraded (all stages ran)
    execution_green = workflow_status in {"completed", "degraded", "needs_review", "policy_blocked"}
    # Business green = completed + artifact quality gate passed
    business_green = workflow_status == "completed" and (not artifact_outcome or artifact_outcome == "pass")
    has_sla_breaches = bool(summary["stage_sla_breaches"])

    # Classify failure type
    failed_stages = [
        sid for sid, s in workflow.get("stages", {}).items()
        if s.get("status") == "failed"
    ]
    stalled_stages = [
        sid for sid, s in workflow.get("stages", {}).items()
        if (s.get("progress_detail") or {}).get("stall_seconds", 0) > 300
    ]

    if not execution_green:
        if stalled_stages:
            classification = "execution_stall"
        elif failed_stages:
            classification = "execution_failure"
        else:
            classification = "execution_failure"
        summary["failure_classification"] = {
            "type": classification,
            "failed_stages": failed_stages,
            "stalled_stages": stalled_stages,
        }
    elif not business_green:
        summary["failure_classification"] = {
            "type": "business_failure",
            "workflow_status": workflow_status,
            "quality_gate_outcome": artifact_outcome,
        }
    elif has_sla_breaches:
        summary["failure_classification"] = {
            "type": "sla_breach",
            "breaches": summary["stage_sla_breaches"],
        }
    else:
        summary["failure_classification"] = {"type": "none"}

    # Check read-model consistency: verify workflow is readable from platform API
    try:
        platform_workflow = _json_request(
            f"{args.base_url}/api/v1/artifacts/workflows/{workflow_id}",
            headers={"Authorization": f"Bearer {token}"},
            retries=2,
        )
        summary["read_model_consistent"] = True
        summary["read_model_status"] = platform_workflow.get("status")
    except Exception:
        summary["read_model_consistent"] = False
        summary["read_model_status"] = None
        if summary["failure_classification"]["type"] == "none":
            summary["failure_classification"] = {
                "type": "read_model_inconsistency",
                "detail": "Workflow not found in platform read-model after completion",
            }

    print(json.dumps(summary, indent=2))

    if not execution_green:
        return 3  # hard execution failure

    if not business_green:
        return 4  # non-green business outcome

    if has_sla_breaches:
        print(f"\nWARNING: {len(summary['stage_sla_breaches'])} stage SLA breach(es) detected", flush=True)
        for breach in summary["stage_sla_breaches"]:
            print(f"  - {breach['stage']}: {breach['duration_seconds']}s > SLA {breach['sla_seconds']}s", flush=True)
        return 5  # execution green, business green, but SLA non-compliant

    return 0


if __name__ == "__main__":
    raise SystemExit(main())