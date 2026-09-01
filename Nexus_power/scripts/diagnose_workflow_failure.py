"""Diagnose a failed (or stuck) canonical-processing workflow end-to-end.

Pulls together the three pieces of evidence needed to root-cause a
``visual_extraction`` (or any other stage) failure or hang:

1. Workflow-level state from the orchestrator
   - ``WorkflowInstance.error``
   - ``stages.<stage>.error``
   - ``stages.<stage>.progress_detail`` (engine job id, last poll, stall_secs)
   - Timeline events around the failure
2. Eyes-engine job state for the dispatched engine job
   - ``status``, ``error``, ``current_stage``, ``progress_percent``
   - ``processing_time_seconds`` if the job ran to completion
3. A pattern-matched best-guess root cause (failure mode) or
   stuck-stage classifier (running mode).

Accepts either a workflow id or a canonical artifact id — the artifact
endpoint returns the parent ``workflow_id`` so a single tool covers both
common paths.

Usage (PowerShell, host-side, with services port-forwarded):

    # Diagnose by workflow id
    python Nexus_power/scripts/diagnose_workflow_failure.py 9d106811-...

    # Diagnose by canonical artifact id (auto-resolves workflow id)
    python Nexus_power/scripts/diagnose_workflow_failure.py --artifact 8332446d-795...

    # Watch a still-running workflow, refreshing every 15s, until terminal
    python Nexus_power/scripts/diagnose_workflow_failure.py --artifact 8332446d... --watch

Or via docker:
    docker exec nexus-orchestrator python /app/scripts/diagnose_workflow_failure.py 9d... --gateway http://nexus-gateway:8080

Environment variables (all optional):
    GATEWAY_URL    default http://localhost:8080
    NEXUS_EMAIL    default admin@nexus.local
    NEXUS_PASSWORD default admin123
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx


DEFAULT_WORKFLOW_ID = "9d106811-351e-4d38-8eca-255728e9dcd0"
DEFAULT_GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8080")
DEFAULT_EMAIL = os.environ.get("NEXUS_EMAIL", "admin@nexus.local")
DEFAULT_PASSWORD = os.environ.get("NEXUS_PASSWORD", "admin123")


# ── Pattern-based hint matcher ───────────────────────────────────

_HINT_PATTERNS: list[tuple[str, str]] = [
    (
        "Engine restarted during processing",
        "Eyes container restarted mid-job. Likely OOM or healthcheck-induced "
        "restart. Check `docker logs nexus-eyes --tail 200` and "
        "`docker stats nexus-eyes` around the failure timestamp.",
    ),
    (
        "stalled for ",
        "Stall detection aborted the stage — engine_sub_stage didn't change "
        "for >= degrade threshold. Inspect Eyes logs for the last "
        "`current_stage` value; usually means a single Ollama call hung. "
        "Bump EYES_OLLAMA_INFERENCE_TIMEOUT or kill stuck Ollama process.",
    ),
    (
        "timed out after",
        "Polling exceeded max_poll_seconds (7200s). The Eyes job is still "
        "running or wedged. Inspect the eyes job directly to see its current "
        "stage and progress.",
    ),
    (
        "exhausted ",
        "Retry budget exhausted. Each attempt failed; the underlying error "
        "is in the eyes job result, not in the orchestrator wrapper.",
    ),
    (
        "returned HTTP 5",
        "Engine returned a 5xx. Eyes container may be unhealthy or its "
        "dependencies (Ollama, Redis, Spine) are unreachable.",
    ),
    (
        "returned HTTP 4",
        "Engine returned a 4xx. Auth, payload or idempotency-key mismatch. "
        "Check JWT validity and X-Idempotency-Key collision.",
    ),
    (
        "validation error",
        "Pydantic rejected the engine response. Check FrameAnalysis / "
        "VisualAnalysisResult fields — usually a non-list ui_elements/tables "
        "value from Ollama.",
    ),
    (
        "Out of memory",
        "OOM during processing. Reduce EYES_MAX_FPS_EXTRACT, raise "
        "frame_diff_threshold, or shrink multimodal_max_frames/scenes.",
    ),
    (
        "ffmpeg",
        "ffmpeg-related failure. Verify ffmpeg/ffprobe are installed in the "
        "eyes container (the engine logs `eyes.ffmpeg_missing` on startup if "
        "they aren't).",
    ),
    (
        "No frames extracted",
        "Frame extractor produced 0 frames — the source video is unreadable "
        "or zero-duration. Confirm the uploaded file is a valid video.",
    ),
    (
        "No job ID found at path",
        "Eyes /analyze-video response was malformed (missing job_id). "
        "Network proxy or auth middleware likely intercepted the response.",
    ),
]


def hint_for(error_text: str) -> str:
    """Return a likely root-cause hint based on the error string."""
    if not error_text:
        return (
            "Empty error string. The exception was raised with no message. "
            "Most likely path: orphaned-job recovery wrote an empty `error` "
            "field, OR an HTTP error with no body bubbled up from httpx. "
            "Re-run with verbose Eyes logging enabled "
            "(`LOG_LEVEL=DEBUG`)."
        )
    lowered = error_text.lower()
    for needle, hint in _HINT_PATTERNS:
        if needle.lower() in lowered:
            return hint
    return (
        "No matching pattern. Inspect the eyes job error field for the "
        "underlying exception."
    )


# ── HTTP helpers ─────────────────────────────────────────────────


def login(client: httpx.Client, email: str, password: str) -> str:
    r = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError(f"Login succeeded but no token returned: {r.text[:200]}")
    return token


def get_workflow(client: httpx.Client, headers: dict, workflow_id: str) -> dict:
    """Fetch via gateway-mapped path first, fall back to direct orchestrator."""
    paths = [
        f"/api/v1/workflows/{workflow_id}",
        f"/api/v1/orchestrator/workflows/{workflow_id}",
    ]
    last_err = None
    for path in paths:
        try:
            r = client.get(path, headers=headers, timeout=30.0)
            if r.status_code == 200:
                return r.json()
            last_err = f"{path} → HTTP {r.status_code}: {r.text[:200]}"
        except httpx.HTTPError as exc:
            last_err = f"{path} → {type(exc).__name__}: {exc}"
    raise RuntimeError(f"Workflow not reachable. Last error: {last_err}")


def get_eyes_job(client: httpx.Client, headers: dict, job_id: str) -> dict | None:
    try:
        r = client.get(f"/api/v1/eyes/jobs/{job_id}", headers=headers, timeout=30.0)
        if r.status_code == 200:
            return r.json()
        return {"_http_status": r.status_code, "_body": r.text[:500]}
    except httpx.HTTPError as exc:
        return {"_http_error": f"{type(exc).__name__}: {exc}"}


def resolve_artifact_to_workflow(
    client: httpx.Client, headers: dict, artifact_id: str,
) -> str:
    """Use the platform-api artifact-status endpoint to discover workflow_id."""
    r = client.get(
        f"/api/v1/artifacts/{artifact_id}/status",
        headers=headers,
        timeout=30.0,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"Artifact lookup failed: HTTP {r.status_code}: {r.text[:200]}"
        )
    body = r.json()
    wf_id = body.get("workflow_id")
    if not wf_id:
        raise RuntimeError(
            f"Artifact {artifact_id} has no workflow_id (status={body.get('status')}). "
            "The canonical-processing chain may not have started yet."
        )
    return wf_id


# ── Pretty printing ──────────────────────────────────────────────


_BAR = "─" * 72


def section(title: str) -> None:
    print(f"\n{_BAR}\n  {title}\n{_BAR}")


def kv(label: str, value: Any, *, indent: int = 0) -> None:
    pad = "  " * indent
    if value is None or value == "":
        value_repr = "<none>"
    elif isinstance(value, (dict, list)):
        value_repr = json.dumps(value, default=str, indent=2)
        # Indent multi-line JSON
        value_repr = ("\n" + pad + "    ").join(value_repr.splitlines())
    else:
        value_repr = str(value)
    print(f"{pad}{label}: {value_repr}")


def fmt_time(iso: str | None) -> str:
    if not iso:
        return "<none>"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M:%S UTC",
        )
    except Exception:
        return iso


# ── Stuck-stage classifier (for still-running workflows) ─────────


def classify_running_state(
    wf: dict,
    eyes_job: dict | None,
    stage_started_at: str | None,
    eyes_started_at: str | None,
) -> tuple[str, str]:
    """Return (verdict, hint) for a still-running visual_extraction stage.

    Verdict is one of: HEALTHY | SLOW | STUCK | FATAL_LIKELY.
    """
    # Stage budget: visual_extraction has timeout_seconds=7200 (2h), with a
    # stall-degrade threshold of 5400s (90 min). A multimodal upload should
    # finish within ~15 min for a 5-min video — more than 30 min is a strong
    # smell. Past 90 min the orchestrator will abort the stage anyway.
    stage_age = _age_seconds(stage_started_at)
    eyes_age = _age_seconds(eyes_started_at)

    if eyes_job is None:
        if stage_age and stage_age > 60:
            return (
                "FATAL_LIKELY",
                "Stage has been running for "
                f"{int(stage_age)}s but no eyes job has been registered. "
                "The POST to /api/v1/eyes/analyze-video is likely failing — "
                "check gateway/orchestrator logs.",
            )
        return ("HEALTHY", "Stage just started — no diagnosis yet.")

    if "_http_status" in eyes_job or "_http_error" in eyes_job:
        return (
            "FATAL_LIKELY",
            "Eyes engine is unreachable while polling. Container may have "
            "crashed or is restarting. Check `docker logs nexus-eyes`.",
        )

    eyes_status = (eyes_job.get("status") or "").lower()
    current_stage = (eyes_job.get("current_stage") or "").lower()
    progress = float(eyes_job.get("progress_percent") or 0.0)

    if eyes_status == "completed":
        return (
            "HEALTHY",
            "Eyes job COMPLETED — the orchestrator will pick it up on the "
            "next poll. No action needed.",
        )
    if eyes_status == "failed":
        return (
            "FATAL_LIKELY",
            f"Eyes job is FAILED with current_stage='{current_stage}'. "
            "Orchestrator will mark visual_extraction failed on the next "
            "poll. See the eyes_job.error field above.",
        )
    if eyes_status == "queued":
        if eyes_age and eyes_age > 120:
            return (
                "STUCK",
                f"Job has been QUEUED for {int(eyes_age)}s. The eyes worker "
                "loop isn't draining the Redis queue — the worker may have "
                "died, or no eyes container is in worker mode.",
            )
        return ("HEALTHY", "Eyes job is queued — worker hasn't picked it up yet.")

    if eyes_status == "processing":
        # Fine-grained classifier per known sub-stage
        if eyes_age is None:
            return ("HEALTHY", f"Eyes processing at sub_stage='{current_stage}'.")
        mins = eyes_age / 60.0
        # Expected duration per sub-stage (seconds)
        expected_max = {
            "queued": 60,
            "probing": 30,
            "splitting": 120,
            "extracting": 300,             # ffmpeg frame extraction
            "ocr": 600,                    # OCR pass on frames
            "scene_grouping": 30,
            "analyzing_scenes": 1800,      # 30 min budget for LLaVA on multimodal
            "scene_": 60,                  # individual scene_N/M (per-scene)
            "chunk_": 1800,                # individual chunk processing
        }
        # Match prefix-style stages like "scene_3/20" or "chunk_1/5"
        budget = None
        for prefix, secs in expected_max.items():
            if current_stage.startswith(prefix):
                budget = secs
                break
        if budget is None:
            budget = 600

        if eyes_age > budget * 2:
            return (
                "STUCK",
                f"Stuck at sub_stage='{current_stage}' for {mins:.1f} min — "
                f"that's >2× the expected budget ({budget}s). Most likely:\n"
                + _stage_specific_hint(current_stage, progress),
            )
        if eyes_age > budget:
            return (
                "SLOW",
                f"Slow at sub_stage='{current_stage}' for {mins:.1f} min "
                f"(budget {budget}s). Progress={progress:.1f}%. Watch for "
                "another 1–2 polls before declaring stuck.",
            )
        return (
            "HEALTHY",
            f"Eyes processing normally at sub_stage='{current_stage}' "
            f"(progress={progress:.1f}%, {mins:.1f} min in).",
        )

    return ("HEALTHY", f"Eyes status='{eyes_status}' — no rule matched.")


def _stage_specific_hint(current_stage: str, progress: float) -> str:
    cs = current_stage.lower()
    if cs.startswith("analyzing_scenes") or cs.startswith("scene_"):
        return (
            "      • A single Ollama LLaVA call is hung. Check "
            "`docker logs nexus-ollama --tail 100` for a stuck request.\n"
            "      • EYES_OLLAMA_INFERENCE_TIMEOUT (default 300s) should "
            "have aborted it — verify the env var is actually set in the "
            "eyes container.\n"
            "      • Try `docker exec nexus-ollama ollama ps` to see active "
            "model loads."
        )
    if cs == "ocr":
        return (
            "      • OCR pass on a very large frame set. The new "
            "max_fps_extract=2.0 + frame_diff_threshold=0.03 can yield "
            "1000+ frames on long videos.\n"
            "      • Check eyes logs for `eyes.ocr_frame_complete` cadence — "
            "if frames keep completing, just slow; if cadence stalled, "
            "EasyOCR is hung."
        )
    if cs == "extracting":
        return (
            "      • ffmpeg frame extraction is slow or hung. Confirm "
            "ffmpeg is healthy: `docker exec nexus-eyes ffmpeg -version`.\n"
            "      • For multi-GB videos, raise EYES_MAX_FPS_EXTRACT down to 1.0."
        )
    if cs.startswith("chunk_"):
        return (
            "      • Long-video chunked processing. Each 5-min chunk runs "
            "the full single-segment pipeline. Multi-hour videos will take "
            "an hour+.\n"
            "      • If a single chunk hangs, the parent job hangs."
        )
    return f"      • current_stage='{current_stage}', progress={progress:.1f}%."


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds()


# ── Diagnosis ────────────────────────────────────────────────────


def diagnose(
    workflow_id: str | None,
    artifact_id: str | None,
    gateway: str,
    email: str,
    password: str,
    watch: bool = False,
    interval: int = 15,
) -> int:
    print(f"Connecting to gateway: {gateway}")

    with httpx.Client(base_url=gateway, timeout=30.0) as client:
        # ── 1. Auth ────────────────────────────────────────────
        try:
            token = login(client, email, password)
        except (httpx.HTTPError, RuntimeError) as exc:
            print(f"\n[FATAL] Login failed: {exc}", file=sys.stderr)
            print(
                "Hint: check GATEWAY_URL and that the gateway container is "
                "running and reachable on the host.",
                file=sys.stderr,
            )
            return 2
        headers = {"Authorization": f"Bearer {token}"}
        print("Authenticated OK")

        # ── 1b. Artifact → workflow resolution ─────────────────
        if artifact_id and not workflow_id:
            try:
                workflow_id = resolve_artifact_to_workflow(client, headers, artifact_id)
                print(
                    f"Resolved artifact {artifact_id} → workflow {workflow_id}",
                )
            except RuntimeError as exc:
                print(f"\n[FATAL] {exc}", file=sys.stderr)
                return 3
        if not workflow_id:
            print("\n[FATAL] No workflow_id and no artifact_id provided.", file=sys.stderr)
            return 4

        print(f"Workflow under inspection: {workflow_id}")

        # ── Loop (watch mode) or single-shot ───────────────────
        while True:
            rc = _run_one_pass(client, headers, workflow_id)
            if not watch:
                return rc
            # In watch mode, exit only when terminal
            wf_status = _last_status_seen
            if wf_status in ("completed", "failed", "cancelled", "needs_review",
                             "degraded", "policy_blocked"):
                print(f"\n[WATCH] Reached terminal status: {wf_status}")
                return rc
            print(f"\n[WATCH] Sleeping {interval}s — Ctrl+C to stop…")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                return 130


# Module-level state used by watch mode to detect terminal status
_last_status_seen: str = ""


def _run_one_pass(client: httpx.Client, headers: dict, workflow_id: str) -> int:
    global _last_status_seen
    try:
        wf = get_workflow(client, headers, workflow_id)
    except RuntimeError as exc:
        print(f"\n[FATAL] {exc}", file=sys.stderr)
        return 3
    _last_status_seen = (wf.get("status") or "").lower()

    section("WORKFLOW SUMMARY")
    kv("workflow_id", wf.get("workflow_id"))
    kv("chain_id", wf.get("chain_id"))
    kv("status", wf.get("status"))
    kv("tenant_id", wf.get("tenant_id"))
    kv("session_id", wf.get("session_id"))
    kv("started_at", fmt_time(wf.get("started_at")))
    kv("completed_at", fmt_time(wf.get("completed_at")))
    wf_age = _age_seconds(wf.get("started_at"))
    if wf_age is not None and not wf.get("completed_at"):
        kv("running_for", f"{wf_age / 60:.1f} min")
    kv("workflow.error", wf.get("error"))

    input_data = wf.get("input_data") or {}
    kv("processing_profile", input_data.get("processing_profile"))
    kv("source_type", input_data.get("source_type"))
    kv("source_filename", input_data.get("source_filename"))
    kv("audio_file_id", input_data.get("audio_file_id"))
    kv("video_file_id", input_data.get("video_file_id"))
    kv("artifact_id", input_data.get("artifact_id"))
    fp = input_data.get("media_fingerprint") or ""
    kv("media_fingerprint", (fp[:16] + "…") if fp else None)

    # ── 3. Per-stage status ────────────────────────────────
    section("STAGES")
    stages = wf.get("stages") or {}
    for sid, se in stages.items():
        status = se.get("status", "?")
        duration_ms = se.get("duration_ms", 0)
        err = se.get("error")
        if status == "failed":
            marker = "X"
        elif status in ("skipped", "pending"):
            marker = "."
        elif status == "running":
            marker = ">"
        else:
            marker = "+"
        print(
            f"  {marker} {sid:30s} {status:10s} "
            f"{duration_ms:>10.0f}ms  retries={se.get('retries', 0)}"
        )
        if err:
            print(f"      |- error: {err}")

    # ── 4. Visual extraction deep-dive ─────────────────────
    ve = stages.get("visual_extraction") or {}
    section("VISUAL_EXTRACTION DETAIL")
    kv("status", ve.get("status"))
    kv("started_at", fmt_time(ve.get("started_at")))
    kv("completed_at", fmt_time(ve.get("completed_at")))
    stage_age = _age_seconds(ve.get("started_at"))
    if stage_age is not None and not ve.get("completed_at"):
        kv("running_for", f"{stage_age / 60:.1f} min")
    kv("duration_ms", ve.get("duration_ms"))
    kv("retries", ve.get("retries"))
    kv("stage.error", ve.get("error"))
    kv("progress_detail", ve.get("progress_detail"))

    # ── 5. Eyes job ────────────────────────────────────────
    progress_detail = ve.get("progress_detail") or {}
    engine_job_id = progress_detail.get("engine_job_id")
    eyes_job: dict | None = None
    eyes_started_at: str | None = None

    section("EYES JOB DETAIL")
    if not engine_job_id:
        print(
            "  No engine_job_id in progress_detail.\n"
            "  - If the stage is RUNNING, the orchestrator hasn't received "
            "a job_id back yet (the POST to /api/v1/eyes/analyze-video may "
            "be in flight or failing). Check orchestrator logs.\n"
            "  - If the stage is FAILED, the POST itself errored — look at "
            f"the gateway log around {fmt_time(ve.get('started_at'))}."
        )
    else:
        print(f"  Fetching eyes job {engine_job_id}...")
        eyes_job = get_eyes_job(client, headers, engine_job_id)
        if not eyes_job:
            print("  No data returned from eyes job lookup.")
        elif "_http_status" in eyes_job:
            print(
                f"  Eyes responded HTTP {eyes_job['_http_status']}: "
                f"{eyes_job.get('_body')}"
            )
        elif "_http_error" in eyes_job:
            print(f"  Eyes unreachable: {eyes_job['_http_error']}")
        else:
            kv("job_id", eyes_job.get("job_id"))
            kv("status", eyes_job.get("status"))
            kv("processing_profile", eyes_job.get("processing_profile"))
            kv("current_stage", eyes_job.get("current_stage"))
            kv("progress_percent", eyes_job.get("progress_percent"))
            kv("created_at", fmt_time(eyes_job.get("created_at")))
            eyes_started_at = eyes_job.get("created_at")
            ej_age = _age_seconds(eyes_started_at)
            if ej_age is not None and (
                eyes_job.get("status") or ""
            ).lower() in ("queued", "processing"):
                kv("running_for", f"{ej_age / 60:.1f} min")
            kv("processing_time_seconds", eyes_job.get("processing_time_seconds"))
            kv("original_filename", eyes_job.get("original_filename"))
            kv("eyes_job.error", eyes_job.get("error"))
            result = eyes_job.get("result") or {}
            if result:
                kv("result.frame_count", len(result.get("frames", [])))
                kv(
                    "result.total_frames_extracted",
                    result.get("total_frames_extracted"),
                )
                kv("result.pipeline_stages", result.get("pipeline_stages"))

    # ── 6. Timeline (last 15 events) ──────────────────────
    section("TIMELINE (last 15)")
    for ev in (wf.get("timeline") or [])[-15:]:
        ts = fmt_time(ev.get("timestamp"))
        print(f"  {ts}  {ev.get('event'):35s}  {ev.get('detail', '')}")

    # ── 7. Verdict ─────────────────────────────────────────
    wf_status = (wf.get("status") or "").lower()
    ve_status = (ve.get("status") or "").lower()

    if wf_status in ("failed", "cancelled"):
        section("LIKELY ROOT CAUSE (workflow already terminal)")
        wf_err = (wf.get("error") or "").strip()
        ve_err = (ve.get("error") or "").strip()
        eyes_err = ""
        if eyes_job and isinstance(eyes_job, dict):
            eyes_err = (eyes_job.get("error") or "").strip()
        kv("workflow.error", wf_err or "<empty>")
        kv("stage.error", ve_err or "<empty>")
        kv("eyes_job.error", eyes_err or "<empty>")
        primary = eyes_err or ve_err or wf_err
        print()
        print(f"  Hint based on '{primary[:120]}':")
        for line in hint_for(primary).splitlines():
            print(f"    {line}")
        return 0

    if ve_status == "running":
        section("LIVE STATUS (visual_extraction is running)")
        verdict, hint = classify_running_state(
            wf, eyes_job, ve.get("started_at"), eyes_started_at,
        )
        kv("verdict", verdict)
        for line in hint.splitlines():
            print(f"  {line}")
        return 0

    section("STATUS")
    print(
        f"  Workflow status='{wf_status}' "
        f"visual_extraction status='{ve_status}'. "
        "No specific diagnosis applies."
    )
    return 0


# ── Entrypoint ───────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "workflow_id",
        nargs="?",
        default=None,
        help=(
            "Workflow ID to diagnose. If omitted, falls back to "
            "--artifact, then to the bundled default."
        ),
    )
    parser.add_argument(
        "--artifact",
        default=None,
        help=(
            "Canonical artifact ID. The script resolves it to the parent "
            "workflow_id via /api/v1/artifacts/{id}/status."
        ),
    )
    parser.add_argument(
        "--gateway",
        default=DEFAULT_GATEWAY,
        help=f"Gateway base URL (default: {DEFAULT_GATEWAY})",
    )
    parser.add_argument(
        "--email", default=DEFAULT_EMAIL, help="Login email",
    )
    parser.add_argument(
        "--password", default=DEFAULT_PASSWORD, help="Login password",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll repeatedly until the workflow reaches a terminal state.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=15,
        help="Polling interval in seconds when --watch is set (default 15).",
    )
    args = parser.parse_args()

    workflow_id = args.workflow_id
    artifact_id = args.artifact
    if not workflow_id and not artifact_id:
        workflow_id = DEFAULT_WORKFLOW_ID

    try:
        return diagnose(
            workflow_id=workflow_id,
            artifact_id=artifact_id,
            gateway=args.gateway,
            email=args.email,
            password=args.password,
            watch=args.watch,
            interval=args.interval,
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
