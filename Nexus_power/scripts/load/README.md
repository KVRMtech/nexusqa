# Load test — Nexus QA canonical pipeline

Architect P2 exit criteria: validate that 1000+/hour uploads sustain
without melting queue depth or losing artifacts.

## Quick start (local)

```bash
# 1. Mint a JWT for the canary tenant (must exist in tenants table).
TOKEN=$(docker exec nexus-orchestrator python -c "
import jwt, time, os
secret = os.environ.get('NEXUS_JWT_SECRET', 'test-secret-do-not-use-in-production')
now = int(time.time())
print(jwt.encode({
    'sub': 'k6-canary', 'email': 'k6@nexus.internal',
    'tenant_id': 'nexus-platform', 'role': 'admin',
    'iat': now, 'exp': now + 86400,
}, secret, algorithm='HS256'))")

# 2. Run k6 (10 VUs × 6s think-time → ~17 RPS → 1000+/hr).
k6 run \
  -e ORCH_URL=http://localhost:8100 \
  -e TOKEN="$TOKEN" \
  -e SAMPLE_VIDEO=$(pwd)/Nexus_power/data/evidence/test_synth.mp4 \
  -e VUS=10 -e DURATION=10m \
  scripts/load/k6_canonical_pipeline.js
```

## Pass criteria

The k6 thresholds (declared in the script) are the hard pass/fail:

  | metric                      | threshold      |
  |-----------------------------|----------------|
  | `submit_p95`                | < 800 ms       |
  | `completion_rate`           | ≥ 0.98         |
  | `artifact_minimal_p95`      | < 90 s         |
  | `workflow_terminal_p95`     | < 0.9 × TIMEOUT|

If any threshold fails, k6 exits non-zero — the CI gate fails.

## What to watch on the live stack during the run

  - `nexus_workflow_queue_depth{lane=...}` should plateau, not climb.
  - `nexus_workflow_queue_oldest_pending_age_seconds` < 300 (matches
    the alert threshold in `infrastructure/observability/alerts/`).
  - `nexus_artifact_minimal_persisted_total` ramps in lockstep with
    `nexus_workflow_created_total`.
  - `nexus_eyes_ocr_frame_timeout_total` stays flat (one-frame timeouts
    are recoverable; a slope means OCR is collapsing).

## When the test fails

  1. Check `/api/v1/canonical-admin/dlq/health` — if a lane is saturated,
     KEDA didn't scale fast enough; lower `pollingInterval` in
     `values-production.yaml` or raise `maxReplicas`.
  2. Check the Grafana dashboard's "Stuck workflows" panel.
  3. Run `scripts/runbook/diagnose_workflow.py <wf-id>` on a failed
     workflow to see the suggested remediation.
