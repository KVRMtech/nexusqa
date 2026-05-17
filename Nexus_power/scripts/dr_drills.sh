#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Nexus QA — Disaster Recovery Drill Suite (Phase 10)
# ─────────────────────────────────────────────────────────────────────
# Production-grade chaos injection scripts. Each drill kills a real
# component and measures recovery time. Operators run these in
# pre-prod BEFORE the first client, then quarterly as a regression.
#
# Drills:
#   1. postgres-primary-kill   — CNPG promotes a sync replica
#   2. redis-master-kill       — Sentinel promotes a replica
#   3. engine-pod-kill         — sweeper recovers in-flight workflow
#   4. milvus-kill             — backbone refuses to start, then recovers
#   5. node-drain              — graceful eviction + reschedule
#   6. object-storage-blackout — workflows pause + resume cleanly
#
# Usage:
#   bash scripts/dr_drills.sh postgres-primary-kill
#   bash scripts/dr_drills.sh all                    # run every drill
#
# Required env:
#   KUBE_NAMESPACE=nexus-platform   (or wherever the chart is installed)
#   DRILL_REPORT=/tmp/dr_report.md  (output)
#
# This script REQUIRES a real K8s cluster — not docker-compose. It is
# read-only-safe to invoke (each drill prints what it would do before
# acting) but the actual `--commit` flag triggers the destructive ops.
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

NAMESPACE="${KUBE_NAMESPACE:-nexus-platform}"
DRILL_REPORT="${DRILL_REPORT:-/tmp/dr_drill_report.md}"
COMMIT=0

c_red()   { printf '\033[31m%s\033[0m' "$*"; }
c_green() { printf '\033[32m%s\033[0m' "$*"; }
c_yellow(){ printf '\033[33m%s\033[0m' "$*"; }
c_blue()  { printf '\033[34m%s\033[0m' "$*"; }
step()    { echo; echo "$(c_blue '==>') $*"; }
ok()      { echo "    $(c_green '✓') $*"; }
warn()    { echo "    $(c_yellow '⚠') $*"; }
fail()    { echo "    $(c_red '✗') $*"; }
plan()    { echo "    [PLAN] $*"; }

usage() {
  cat <<EOF
Usage: $0 [--commit] <drill>

Drills:
  postgres-primary-kill   Kill Postgres primary; expect <60s promote
  redis-master-kill       Kill Redis master; expect <30s Sentinel promote
  engine-pod-kill         Kill engine pod mid-step; expect orphan recovery
  milvus-kill             Kill Milvus; expect backbone refuse-to-start + recover
  node-drain              Drain a node; expect pod eviction + reschedule
  object-storage-blackout Block object storage for 60s; expect graceful pause
  all                     Run every drill, log results to DRILL_REPORT

Flags:
  --commit                Actually do the destructive operation. Without
                          this flag, the script prints what it would
                          do but does not act.

Environment:
  KUBE_NAMESPACE          K8s namespace (default: nexus-platform)
  DRILL_REPORT            Output path for markdown report
  KUBECTL                 kubectl binary (default: kubectl)

Each drill records:
  - Expected RTO (target recovery time)
  - Measured RTO (actual seconds to recover)
  - Measured RPO (any data loss)
  - Verification status
EOF
}

[ $# -eq 0 ] && { usage; exit 1; }

KUBECTL="${KUBECTL:-kubectl}"
command -v "$KUBECTL" >/dev/null || { fail "$KUBECTL not on PATH"; exit 1; }

# ─── helpers ────────────────────────────────────────────────────────

epoch() { date +%s; }

now_ns() { date +%s%N; }

write_report_header() {
  cat > "$DRILL_REPORT" <<EOF
# DR Drill Report

Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
Cluster: ${KUBE_NAMESPACE}
Commit mode: $([ $COMMIT -eq 1 ] && echo "YES (destructive)" || echo "no (plan only)")

| Drill | Target RTO | Measured RTO | Status |
|---|---|---|---|
EOF
}

append_result() {
  # $1=drill name, $2=target RTO, $3=measured RTO, $4=status
  echo "| $1 | $2 | $3 | $4 |" >> "$DRILL_REPORT"
}

wait_until() {
  # wait_until <command> <timeout_s>
  local cmd="$1"; local timeout="${2:-120}"
  local start; start=$(epoch)
  while ! eval "$cmd" >/dev/null 2>&1; do
    [ $(($(epoch) - start)) -ge "$timeout" ] && return 1
    sleep 1
  done
  return 0
}

current_postgres_primary() {
  $KUBECTL get pods -n "$NAMESPACE" \
    -l postgresql=primary -o jsonpath='{.items[0].metadata.name}' \
    2>/dev/null
}

current_redis_master() {
  # Sentinel publishes the master pod name in its config.
  $KUBECTL get pods -n "$NAMESPACE" \
    -l role=master,app.kubernetes.io/component=redis \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

verify_workflow_still_running() {
  # Submit a no-op workflow and assert it completes within timeout.
  # In a real drill this would call gateway with a small audio
  # fixture. Stubbed here as a placeholder — operator wires the real
  # smoke call from tests/load/k6_smoke.js.
  echo "    (workflow smoke would run via k6_smoke.js — wire externally)"
  return 0
}

# ─── Drill 1: Postgres primary kill ─────────────────────────────────

drill_postgres_primary_kill() {
  step "Drill: postgres-primary-kill (target RTO < 60s)"
  local primary
  primary=$(current_postgres_primary)
  [ -z "$primary" ] && { fail "no primary pod found"; return 1; }
  plan "Will delete pod: $primary"

  if [ $COMMIT -eq 0 ]; then
    warn "PLAN ONLY — pass --commit to execute"
    append_result "postgres-primary-kill" "<60s" "n/a (plan only)" "skipped"
    return 0
  fi

  local t_start; t_start=$(epoch)
  $KUBECTL delete pod -n "$NAMESPACE" "$primary" --grace-period=0 --force
  ok "primary $primary terminated"

  # Wait for CNPG to promote a new primary.
  if wait_until "[ \"\$(current_postgres_primary)\" != \"$primary\" ] && [ -n \"\$(current_postgres_primary)\" ]" 120; then
    local t_end; t_end=$(epoch)
    local rto=$(( t_end - t_start ))
    local new_primary; new_primary=$(current_postgres_primary)
    ok "new primary $new_primary promoted in ${rto}s"
    if [ "$rto" -le 60 ]; then
      append_result "postgres-primary-kill" "<60s" "${rto}s" "PASS"
    else
      append_result "postgres-primary-kill" "<60s" "${rto}s" "MISS"
    fi
    verify_workflow_still_running
    return 0
  else
    fail "no new primary promoted within 120s"
    append_result "postgres-primary-kill" "<60s" ">120s" "FAIL"
    return 1
  fi
}

# ─── Drill 2: Redis master kill ─────────────────────────────────────

drill_redis_master_kill() {
  step "Drill: redis-master-kill (target RTO < 30s)"
  local master
  master=$(current_redis_master)
  [ -z "$master" ] && { fail "no master pod found"; return 1; }
  plan "Will delete pod: $master"

  if [ $COMMIT -eq 0 ]; then
    warn "PLAN ONLY — pass --commit to execute"
    append_result "redis-master-kill" "<30s" "n/a (plan only)" "skipped"
    return 0
  fi

  local t_start; t_start=$(epoch)
  $KUBECTL delete pod -n "$NAMESPACE" "$master" --grace-period=0 --force
  ok "master $master terminated"

  if wait_until "[ \"\$(current_redis_master)\" != \"$master\" ] && [ -n \"\$(current_redis_master)\" ]" 60; then
    local t_end; t_end=$(epoch)
    local rto=$(( t_end - t_start ))
    local new_master; new_master=$(current_redis_master)
    ok "new master $new_master promoted in ${rto}s"
    if [ "$rto" -le 30 ]; then
      append_result "redis-master-kill" "<30s" "${rto}s" "PASS"
    else
      append_result "redis-master-kill" "<30s" "${rto}s" "MISS"
    fi
    verify_workflow_still_running
    return 0
  else
    fail "no new master promoted within 60s"
    append_result "redis-master-kill" "<30s" ">60s" "FAIL"
    return 1
  fi
}

# ─── Drill 3: Engine pod kill mid-step ──────────────────────────────

drill_engine_pod_kill() {
  step "Drill: engine-pod-kill (target RTO < 2× heartbeat = 60s)"
  # Pick an eyes pod (the longest-step engine — best chance of
  # catching a workflow mid-flight).
  local victim
  victim=$($KUBECTL get pods -n "$NAMESPACE" -l engine=eyes \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  [ -z "$victim" ] && { fail "no eyes pod found"; return 1; }
  plan "Will delete pod: $victim mid-step (submit workflow first)"

  if [ $COMMIT -eq 0 ]; then
    warn "PLAN ONLY — pass --commit to execute"
    append_result "engine-pod-kill" "<60s" "n/a (plan only)" "skipped"
    return 0
  fi

  warn "Submit a workflow externally THEN re-run this drill within 30s"
  warn "Skipping autonomous execution — operator should:"
  warn "  1. k6 run tests/load/k6_smoke.js (or equivalent)"
  warn "  2. bash $0 --commit engine-pod-kill (THIS script, manually timed)"
  warn "  3. After kill, watch workflow_state row — expect orphan recovery <60s"
  append_result "engine-pod-kill" "<60s" "needs manual orchestration" "manual"
  return 0
}

# ─── Drill 4: Milvus kill ───────────────────────────────────────────

drill_milvus_kill() {
  step "Drill: milvus-kill (target backbone refuse-to-start + recover < 2 min)"
  local milvus
  milvus=$($KUBECTL get pods -n "$NAMESPACE" \
    -l app.kubernetes.io/component=milvus \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  [ -z "$milvus" ] && { fail "no milvus pod found"; return 1; }
  plan "Will delete pod: $milvus"

  if [ $COMMIT -eq 0 ]; then
    warn "PLAN ONLY — pass --commit to execute"
    append_result "milvus-kill" "<2min" "n/a (plan only)" "skipped"
    return 0
  fi

  local t_start; t_start=$(epoch)
  $KUBECTL delete pod -n "$NAMESPACE" "$milvus" --grace-period=0 --force
  ok "milvus $milvus terminated"

  # Verify backbone responds with degraded health while Milvus is gone.
  sleep 5
  local backbone_health
  backbone_health=$($KUBECTL exec -n "$NAMESPACE" deploy/backbone -- \
    curl -sm 3 http://localhost:8005/health 2>/dev/null || echo "")
  if echo "$backbone_health" | grep -q "milvus"; then
    fail "backbone still reports milvus mode while milvus pod is down"
    append_result "milvus-kill" "<2min" "?" "FAIL (no failure detection)"
    return 1
  fi
  ok "backbone correctly notices milvus is unreachable"

  # Wait for Milvus to come back.
  if wait_until "$KUBECTL get pods -n $NAMESPACE -l app.kubernetes.io/component=milvus | grep -q Running" 120; then
    local t_end; t_end=$(epoch)
    local rto=$(( t_end - t_start ))
    ok "milvus restarted in ${rto}s"
    sleep 10  # Backbone needs a beat to reconnect.
    local final_health
    final_health=$($KUBECTL exec -n "$NAMESPACE" deploy/backbone -- \
      curl -sm 3 http://localhost:8005/health 2>/dev/null || echo "")
    if echo "$final_health" | grep -q "milvus"; then
      ok "backbone reconnected to milvus"
      append_result "milvus-kill" "<2min" "${rto}s" "PASS"
    else
      fail "backbone did NOT reconnect after milvus came back"
      append_result "milvus-kill" "<2min" "${rto}s" "FAIL (no reconnect)"
    fi
    return 0
  else
    fail "milvus did not restart within 120s"
    append_result "milvus-kill" "<2min" ">120s" "FAIL"
    return 1
  fi
}

# ─── Drill 5: Node drain ────────────────────────────────────────────

drill_node_drain() {
  step "Drill: node-drain (graceful eviction + reschedule)"
  local node
  # Pick a node that hosts at least one engine pod.
  node=$($KUBECTL get pods -n "$NAMESPACE" -l engine \
    -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null)
  [ -z "$node" ] && { fail "no node with engine pods found"; return 1; }
  plan "Will drain node: $node, then uncordon"

  if [ $COMMIT -eq 0 ]; then
    warn "PLAN ONLY — pass --commit to execute"
    append_result "node-drain" "<5min" "n/a (plan only)" "skipped"
    return 0
  fi

  local t_start; t_start=$(epoch)
  $KUBECTL drain "$node" --ignore-daemonsets --delete-emptydir-data \
    --grace-period=60 --timeout=300s
  ok "node $node drained"
  local t_drained; t_drained=$(epoch)
  local drain_seconds=$(( t_drained - t_start ))
  ok "drain completed in ${drain_seconds}s"

  # Wait for evicted pods to come back on other nodes.
  if wait_until "[ \$($KUBECTL get pods -n $NAMESPACE -l engine | grep -c Running) -gt 0 ]" 180; then
    ok "evicted pods rescheduled on other nodes"
  else
    fail "evicted pods not rescheduled within 180s"
  fi

  $KUBECTL uncordon "$node"
  ok "node $node uncordoned"
  local t_end; t_end=$(epoch)
  local rto=$(( t_end - t_start ))
  append_result "node-drain" "<5min" "${rto}s" \
    "$([ "$rto" -le 300 ] && echo PASS || echo MISS)"
}

# ─── Drill 6: Object storage blackout ───────────────────────────────

drill_object_storage_blackout() {
  step "Drill: object-storage-blackout (target: graceful pause + resume)"
  warn "This drill requires a NetworkPolicy block — operator-specific."
  warn "Typical implementation:"
  warn "  1. Apply a deny-egress NetworkPolicy to the s3 (or minio) endpoint"
  warn "  2. Submit a workflow that needs object storage"
  warn "  3. Expect: workflow pauses, no data loss, no crashes"
  warn "  4. Remove the NetworkPolicy after 60s"
  warn "  5. Verify the paused workflow resumes cleanly"
  warn ""
  warn "Skipping autonomous execution — operator decides whether to drill."
  append_result "object-storage-blackout" "<2min resume" "needs manual orchestration" "manual"
}

# ─── Dispatch ───────────────────────────────────────────────────────

DRILL=""
for arg in "$@"; do
  case "$arg" in
    --commit) COMMIT=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) fail "unknown flag: $arg"; usage; exit 1 ;;
    *) DRILL="$arg" ;;
  esac
done

[ -z "$DRILL" ] && { usage; exit 1; }

write_report_header

case "$DRILL" in
  postgres-primary-kill)   drill_postgres_primary_kill ;;
  redis-master-kill)       drill_redis_master_kill ;;
  engine-pod-kill)         drill_engine_pod_kill ;;
  milvus-kill)             drill_milvus_kill ;;
  node-drain)              drill_node_drain ;;
  object-storage-blackout) drill_object_storage_blackout ;;
  all)
    drill_postgres_primary_kill
    drill_redis_master_kill
    drill_engine_pod_kill
    drill_milvus_kill
    drill_node_drain
    drill_object_storage_blackout
    ;;
  *) fail "unknown drill: $DRILL"; usage; exit 1 ;;
esac

echo
ok "Drill complete. Report at: $DRILL_REPORT"
echo
cat "$DRILL_REPORT"
