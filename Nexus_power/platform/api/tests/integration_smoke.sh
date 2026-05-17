#!/usr/bin/env bash
# Integration smoke test (P3-P7) — exercises every new endpoint against a
# running stack.
#
# Prerequisites:
#   * Platform API running on localhost:8000
#   * Heart engine running on localhost:8005 (or set HEART_ENGINE_URL)
#   * PostgreSQL up + migrations applied (`alembic upgrade head`)
#   * An existing artifact with visual evidence (set ARTIFACT_ID below)
#   * A valid JWT (set NEXUS_TOKEN; can be obtained via /v1/auth/login)
#
# What this verifies (HTTP layer):
#   P3 — lifecycle state transitions + comments + audit
#   P4 — Co-Architect chat (skipped if no LLM tier configured)
#   P5 — multi-format exports (Playwright / Cypress / Gherkin / JSON)
#   P6 — test-run ingest + per-scenario summary + last-run detail
#   P7 — artifact diff + heal-suggestions + apply-healing
#
# Each step prints PASS / FAIL with the HTTP status. Stops on first failure
# unless CONTINUE_ON_FAIL=1 is set.

set -u

: "${PLATFORM_API_URL:=http://localhost:8000}"
: "${ARTIFACT_ID:?Set ARTIFACT_ID to an artifact_id in your DB}"
: "${BASELINE_ARTIFACT_ID:=}"  # Optional — required only for P7 diff test
: "${SCENARIO_ID:=vs_001}"
: "${NEXUS_TOKEN:?Set NEXUS_TOKEN to a valid JWT}"
: "${CONTINUE_ON_FAIL:=0}"

PASS=0
FAIL=0

curl_status() {
  # Args: METHOD PATH [JSON_BODY]
  local method="$1" path="$2" body="${3:-}"
  local url="${PLATFORM_API_URL}${path}"
  local args=(-s -o /tmp/integration_resp.json -w '%{http_code}' -X "$method"
              -H "Authorization: Bearer $NEXUS_TOKEN")
  if [[ -n "$body" ]]; then
    args+=(-H "Content-Type: application/json" --data "$body")
  fi
  curl "${args[@]}" "$url"
}

check() {
  # Args: LABEL EXPECTED_STATUS METHOD PATH [JSON_BODY]
  local label="$1" expected="$2"
  shift 2
  local status
  status=$(curl_status "$@") || status=000
  if [[ "$status" == "$expected" ]]; then
    printf '  [PASS] %-60s HTTP %s\n' "$label" "$status"
    PASS=$((PASS + 1))
  else
    printf '  [FAIL] %-60s HTTP %s (expected %s)\n' "$label" "$status" "$expected"
    if [[ -s /tmp/integration_resp.json ]]; then
      printf '         body: '
      head -c 240 /tmp/integration_resp.json
      printf '\n'
    fi
    FAIL=$((FAIL + 1))
    if [[ "$CONTINUE_ON_FAIL" != "1" ]]; then
      echo "Stopping (set CONTINUE_ON_FAIL=1 to keep going)."
      summary_and_exit
    fi
  fi
}

summary_and_exit() {
  echo
  echo "=== Integration smoke summary: $PASS pass, $FAIL fail ==="
  if [[ "$FAIL" -gt 0 ]]; then
    exit 1
  fi
  exit 0
}

echo "=== P0 — Visual-Evidence Hardening (audit-gap closure) ==="
# Pagination on the six visual-evidence endpoints — must accept page/limit
# without breaking, and the response must include the new ``page`` and
# ``limit`` keys.  We use limit=2 so even a tiny artifact exercises the
# pagination math (offset = (page-1)*limit).
check "GET  /visual-scenes?page=1&limit=2"  200  GET \
  "/api/v1/artifacts/${ARTIFACT_ID}/visual-scenes?page=1&limit=2"
check "GET  /app-instances?page=1&limit=2"  200  GET \
  "/api/v1/artifacts/${ARTIFACT_ID}/app-instances?page=1&limit=2"
check "GET  /evidence-controls?page=1&limit=2"  200  GET \
  "/api/v1/artifacts/${ARTIFACT_ID}/evidence-controls?page=1&limit=2"
check "GET  /evidence-steps?page=1&limit=2"  200  GET \
  "/api/v1/artifacts/${ARTIFACT_ID}/evidence-steps?page=1&limit=2"
check "GET  /cursor-events?page=1&limit=2"  200  GET \
  "/api/v1/artifacts/${ARTIFACT_ID}/cursor-events?page=1&limit=2"
check "GET  /visual-evidence-graph?scene_offset=0&scene_limit=2"  200  GET \
  "/api/v1/artifacts/${ARTIFACT_ID}/visual-evidence-graph?scene_offset=0&scene_limit=2"
# Hard ceiling enforced — limit > 2000 must 422 (FastAPI validation)
check "GET  /visual-scenes?limit=99999 (rejects > max)"  422  GET \
  "/api/v1/artifacts/${ARTIFACT_ID}/visual-scenes?limit=99999"

echo
echo "=== P3 — Lifecycle ==="
check "GET  /scenarios/state"  200  GET  "/api/v1/e2e-architect/${ARTIFACT_ID}/scenarios/state"
check "POST transition (idempotent draft→draft)" 200 POST \
  "/api/v1/e2e-architect/${ARTIFACT_ID}/scenarios/${SCENARIO_ID}/transition" \
  '{"new_state":"draft","note":"integration smoke"}'
check "POST comment"  200  POST \
  "/api/v1/e2e-architect/${ARTIFACT_ID}/scenarios/${SCENARIO_ID}/comment" \
  '{"body":"integration smoke comment"}'

echo
echo "=== P4 — Co-Architect ==="
echo "  (skipping — needs LLM tier configured; covered by smoke test instead)"

echo
echo "=== P5 — Multi-format export ==="
check "GET  /tests/export-formats"  200  GET  "/api/v1/tests/export-formats"
# Each export will 422 if nothing is approved — that's a valid result we accept.
for fmt in playwright cypress gherkin json; do
  status=$(curl_status POST "/api/v1/tests/export?artifact_id=${ARTIFACT_ID}&format=${fmt}")
  if [[ "$status" == "200" ]] || [[ "$status" == "422" ]]; then
    printf '  [PASS] %-60s HTTP %s\n' "POST /tests/export?format=$fmt" "$status"
    PASS=$((PASS + 1))
  else
    printf '  [FAIL] %-60s HTTP %s (expected 200 or 422)\n' "POST /tests/export?format=$fmt" "$status"
    FAIL=$((FAIL + 1))
    if [[ "$CONTINUE_ON_FAIL" != "1" ]]; then summary_and_exit; fi
  fi
done

echo
echo "=== P6 — Execution Feedback ==="
NOW=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
INGEST_BODY=$(cat <<EOF
{
  "artifact_id": "${ARTIFACT_ID}",
  "ci_run_id": "integration-smoke-$(date +%s)",
  "ci_commit_sha": "smoke",
  "environment": "ci",
  "status": "passed",
  "started_at": "${NOW}",
  "completed_at": "${NOW}",
  "steps": [
    {"test_name": "${SCENARIO_ID} smoke step", "scenario_id": "${SCENARIO_ID}",
     "step_number": 1, "status": "passed", "duration_ms": 100}
  ]
}
EOF
)
check "POST /test-runs/ingest"  200  POST  "/api/v1/test-runs/ingest"  "$INGEST_BODY"
check "GET  /test-runs (artifact)"  200  GET  "/api/v1/e2e-architect/${ARTIFACT_ID}/test-runs?limit=5"
check "GET  /scenarios/runs/summary"  200  GET \
  "/api/v1/e2e-architect/${ARTIFACT_ID}/scenarios/runs/summary"
check "GET  /scenarios/{id}/last-run"  200  GET \
  "/api/v1/e2e-architect/${ARTIFACT_ID}/scenarios/${SCENARIO_ID}/last-run"

echo
echo "=== P7 — Diff + Heal ==="
if [[ -n "$BASELINE_ARTIFACT_ID" ]]; then
  check "POST /artifacts/diff"  200  POST  "/api/v1/artifacts/diff" \
    "{\"baseline_artifact_id\":\"${BASELINE_ARTIFACT_ID}\",\"current_artifact_id\":\"${ARTIFACT_ID}\"}"
else
  echo "  (skipping /artifacts/diff — set BASELINE_ARTIFACT_ID to enable)"
fi
check "POST /scenarios/heal-suggestions"  200  POST \
  "/api/v1/e2e-architect/${ARTIFACT_ID}/scenarios/heal-suggestions"  '{}'
# /apply-healing requires at least one suggestion. Send a dummy; we accept
# 422 (no suggestions to apply / invalid) as a valid contract response.
status=$(curl_status POST \
  "/api/v1/e2e-architect/${ARTIFACT_ID}/scenarios/apply-healing" \
  '{"suggestions":[{"scenario_id":"'"${SCENARIO_ID}"'","step_number":1,"old_control_id":"fake","new_control_id":"fake"}]}')
if [[ "$status" == "200" ]] || [[ "$status" == "422" ]]; then
  printf '  [PASS] %-60s HTTP %s\n' "POST /scenarios/apply-healing (dummy)" "$status"
  PASS=$((PASS + 1))
else
  printf '  [FAIL] %-60s HTTP %s\n' "POST /scenarios/apply-healing (dummy)" "$status"
  FAIL=$((FAIL + 1))
fi

summary_and_exit
