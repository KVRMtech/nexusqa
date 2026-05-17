"""
Workflow diagnostic + remediation runbook.

A single CLI for the operator-facing failure modes the architect P1
review flagged:

  - Inspect a workflow: status, current step, DAG state, history, error.
  - Suggest the right remediation (replay, force-unstick, restart).
  - Optionally execute that remediation against the orchestrator's
    admin endpoints — without touching the DB directly.

Usage:
  python diagnose_workflow.py <workflow_id> \\
      --orchestrator-url http://localhost:8100 \\
      --admin-token "$(cat /etc/nexus/admin-token)"

  # Bulk: replay every workflow quarantined on a step
  python diagnose_workflow.py --replay-all \\
      --step eyes.ocr_frames \\
      --orchestrator-url http://localhost:8100 \\
      --admin-token "$ADMIN_TOKEN"

  # Health snapshot of every queue lane
  python diagnose_workflow.py --queue-health \\
      --orchestrator-url http://localhost:8100 \\
      --admin-token "$ADMIN_TOKEN"

Exit codes:
  0  diagnostics ran successfully (or remediation succeeded)
  1  workflow not found / API unreachable
  2  workflow is in an unexpected state and no automatic remediation
     is safe — human review required
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from typing import Any

import httpx


def _client(orchestrator_url: str, admin_token: str) -> httpx.Client:
    return httpx.Client(
        base_url=orchestrator_url.rstrip("/"),
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30.0,
    )


# ─── Diagnostic ──────────────────────────────────────────────


def diagnose(cli: httpx.Client, workflow_id: str) -> dict:
    """Pull workflow state + DAG slice from the orchestrator. Returns
    a structured diagnosis dict suitable for both `print` and the
    bulk `--replay-all` decision logic."""
    r = cli.get(f"/api/v1/workflows/{workflow_id}")
    if r.status_code == 404:
        return {"workflow_id": workflow_id, "found": False}
    r.raise_for_status()
    body = r.json()
    status = body.get("status", "unknown")
    current = body.get("current_stage") or body.get("current_step")
    dag_completed = body.get("dag_completed_steps") or []
    dag_in_flight = body.get("dag_in_flight_steps") or []
    plan_steps = body.get("plan_steps") or []
    err = body.get("error") or ""
    last_hb_age = body.get("last_heartbeat_age_seconds")
    suggestion = _suggest_action(
        status=status,
        current=current,
        dag_in_flight=dag_in_flight,
        last_hb_age=last_hb_age,
        error=err,
    )
    return {
        "found": True,
        "workflow_id": workflow_id,
        "status": status,
        "kind": body.get("kind"),
        "current_step": current,
        "dag_completed_steps": dag_completed,
        "dag_in_flight_steps": dag_in_flight,
        "plan_step_count": len(plan_steps),
        "last_heartbeat_age_seconds": last_hb_age,
        "error_summary": (err or "")[:240],
        "suggestion": suggestion,
        "metadata": body.get("metadata"),
    }


def _suggest_action(
    status: str,
    current: str | None,
    dag_in_flight: list[str],
    last_hb_age: float | None,
    error: str,
) -> dict[str, str]:
    """Map workflow state to a recommended remediation."""
    # Terminal states that need manual restart vs. quarantine replay.
    if status == "quarantined":
        return {
            "action": "replay-quarantined",
            "endpoint": "POST /api/v1/admin/dlq/workflows/{id}/replay",
            "reason": "Workflow is quarantined; safe to replay if you've fixed the root cause.",
        }
    if status in ("completed", "success", "succeeded"):
        return {
            "action": "none",
            "reason": "Workflow already terminal-success; no remediation needed.",
        }
    if status in ("failed", "cancelled"):
        return {
            "action": "manual-restart",
            "reason": (
                "Workflow failed terminally. Re-upload the source media if the "
                "user still wants the artifact; or replay if the root cause is "
                "now fixed."
            ),
        }
    # Running-but-stuck cases.
    if status in ("running", "pending"):
        if last_hb_age is not None and last_hb_age > 300:
            return {
                "action": "force-unstick",
                "endpoint": "POST /api/v1/admin/dlq/workflows/{id}/force-unstick",
                "reason": (
                    f"Heartbeat is {last_hb_age:.0f}s old (> 5min). The worker "
                    "likely died. Force-unstick clears in-flight + heartbeat and "
                    "lets the next dispatch retry."
                ),
            }
        if dag_in_flight:
            return {
                "action": "wait",
                "reason": (
                    f"Workflow is actively running step(s) {dag_in_flight} with a "
                    "recent heartbeat. Wait for completion or check the worker logs."
                ),
            }
        return {
            "action": "force-dispatch",
            "endpoint": "POST /api/v1/workflows/{id}/dispatch-next",
            "reason": (
                "Workflow is pending with no in-flight step. Manually trigger "
                "the next dispatch."
            ),
        }
    return {
        "action": "investigate",
        "reason": f"Unknown status {status!r} — investigate manually.",
    }


# ─── Remediation actions ─────────────────────────────────────


def replay_workflow(cli: httpx.Client, workflow_id: str) -> dict:
    r = cli.post(f"/api/v1/admin/dlq/workflows/{workflow_id}/replay")
    r.raise_for_status()
    return r.json()


def force_unstick(cli: httpx.Client, workflow_id: str) -> dict:
    r = cli.post(f"/api/v1/admin/dlq/workflows/{workflow_id}/force-unstick")
    r.raise_for_status()
    return r.json()


def replay_all(
    cli: httpx.Client,
    step: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> dict:
    params: dict[str, Any] = {"limit": limit}
    if step:
        params["step"] = step
    if kind:
        params["kind"] = kind
    r = cli.post(
        "/api/v1/admin/dlq/workflows/replay-all-quarantined",
        params=params,
    )
    r.raise_for_status()
    return r.json()


def queue_health(cli: httpx.Client) -> dict:
    r = cli.get("/api/v1/admin/dlq/health")
    r.raise_for_status()
    return r.json()


# ─── CLI ─────────────────────────────────────────────────────


def _print_diagnosis(d: dict) -> None:
    if not d.get("found"):
        print(f"workflow {d['workflow_id']} not found")
        return
    print(textwrap.dedent(
        f"""
        Workflow      : {d['workflow_id']}
        Kind          : {d['kind']}
        Status        : {d['status']}
        Current step  : {d['current_step']}
        DAG completed : {len(d['dag_completed_steps'])} / {d['plan_step_count']}
        DAG in-flight : {d['dag_in_flight_steps']}
        Last heartbeat: {d['last_heartbeat_age_seconds']}s ago
        Error         : {d['error_summary'] or '—'}
        ── Suggested action ───────────────────────────────────────
        Action        : {d['suggestion']['action']}
        Reason        : {d['suggestion']['reason']}
        """).strip())


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("workflow_id", nargs="?", default=None)
    ap.add_argument("--orchestrator-url", required=True)
    ap.add_argument("--admin-token", required=True)
    ap.add_argument(
        "--execute", action="store_true",
        help="Execute the suggested remediation (not just diagnose)",
    )
    ap.add_argument(
        "--replay-all", action="store_true",
        help="Bulk replay every quarantined workflow (use --step/--kind to filter)",
    )
    ap.add_argument("--step", default=None)
    ap.add_argument("--kind", default=None)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument(
        "--queue-health", action="store_true",
        help="Print per-lane queue health and exit",
    )
    args = ap.parse_args()

    cli = _client(args.orchestrator_url, args.admin_token)
    try:
        if args.queue_health:
            print(json.dumps(queue_health(cli), indent=2))
            return 0

        if args.replay_all:
            res = replay_all(
                cli, step=args.step, kind=args.kind, limit=args.limit,
            )
            print(json.dumps(res, indent=2, default=str))
            return 0

        if not args.workflow_id:
            print("workflow_id required (or use --replay-all / --queue-health)")
            return 2

        diag = diagnose(cli, args.workflow_id)
        _print_diagnosis(diag)
        if not diag.get("found"):
            return 1

        if args.execute:
            action = diag["suggestion"]["action"]
            print(f"\n── Executing: {action} ──")
            if action == "replay-quarantined":
                print(json.dumps(
                    replay_workflow(cli, args.workflow_id),
                    indent=2,
                ))
            elif action == "force-unstick":
                print(json.dumps(
                    force_unstick(cli, args.workflow_id),
                    indent=2,
                ))
            elif action in ("wait", "none"):
                print("(no remediation needed)")
            else:
                print(
                    f"action {action!r} requires manual review — "
                    "won't auto-execute"
                )
                return 2
        return 0
    except httpx.HTTPStatusError as e:
        print(f"API error: {e.response.status_code} {e.response.text[:200]}")
        return 1
    except httpx.ConnectError as e:
        print(f"Cannot reach orchestrator at {args.orchestrator_url}: {e}")
        return 1
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
