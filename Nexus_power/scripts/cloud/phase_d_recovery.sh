#!/usr/bin/env bash
# Phase D — recovery / replay / cancel exercises.

set -u
ORCH=http://localhost:8100
RESULTS=/tmp/phase_d_results.txt
> "$RESULTS"

log() { echo "$1" | tee -a "$RESULTS"; }

mint_token() {
  docker exec nexus-orchestrator python -c "
import jwt, time, os
secret = os.environ.get('NEXUS_JWT_SECRET', 'test-secret-do-not-use-in-production')
now = int(time.time())
print(jwt.encode({'sub':'phaseD','email':'p@n.i','tenant_id':'$1','role':'platform-admin','iat':now,'exp':now+1200}, secret, algorithm='HS256'))"
}

TENANT=68c79953
TOK=$(mint_token "$TENANT")

# ─── D1: replay a quarantined workflow ───────────────────────

log "═══ D1: Replay a quarantined workflow ═══"
WF_QUAR=$(docker exec nexus-postgres psql -U nexus -d nexus -t -c \
  "SELECT workflow_id FROM workflow_state WHERE status='quarantined' AND tenant_id='$TENANT' ORDER BY created_at DESC LIMIT 1;" 2>/dev/null | xargs)
if [ -z "$WF_QUAR" ]; then
  log "  no quarantined workflow on tenant $TENANT — skipping D1"
else
  log "  target: $WF_QUAR"
  resp=$(curl -s -X POST "$ORCH/api/v1/canonical-admin/dlq/workflows/$WF_QUAR/replay" \
    -H "Authorization: Bearer $TOK")
  log "  replay response: $resp"
  if echo "$resp" | grep -q '"new_deadline_at"'; then
    log "  ✅ deadline extended (replay endpoint fix working)"
  else
    log "  ⚠ unexpected replay response"
  fi
fi

# ─── D2: force-unstick a running workflow ────────────────────

log
log "═══ D2: Force-unstick endpoint smoke ═══"
WF_RUN=$(docker exec nexus-postgres psql -U nexus -d nexus -t -c \
  "SELECT workflow_id FROM workflow_state WHERE status='running' ORDER BY created_at DESC LIMIT 1;" 2>/dev/null | xargs)
if [ -z "$WF_RUN" ]; then
  log "  no running workflow — skipping D2 (will rerun if phase B leaves one)"
else
  log "  target: $WF_RUN"
  code=$(curl -s -o /tmp/d2_resp -w '%{http_code}' \
    -X POST "$ORCH/api/v1/canonical-admin/dlq/workflows/$WF_RUN/force-unstick" \
    -H "Authorization: Bearer $TOK")
  log "  force-unstick HTTP=$code body=$(head -c 200 /tmp/d2_resp)"
fi

# ─── D3: bulk replay-all-quarantined ─────────────────────────

log
log "═══ D3: Bulk replay-all-quarantined (filter by step) ═══"
code=$(curl -s -o /tmp/d3_resp -w '%{http_code}' \
  -X POST "$ORCH/api/v1/canonical-admin/dlq/workflows/replay-all-quarantined?limit=5" \
  -H "Authorization: Bearer $TOK")
log "  HTTP=$code"
log "  body (first 400 chars):"
log "  $(head -c 400 /tmp/d3_resp)"

# ─── D4: cancel an in-flight workflow ────────────────────────

log
log "═══ D4: Submit + cancel mid-flight ═══"
cp /tmp/seed_real_audio.mp4 /tmp/d4_cancel.mp4
echo "d4-cancel-$(date +%s%N)$RANDOM" >> /tmp/d4_cancel.mp4
resp=$(curl -sf -X POST "$ORCH/api/v1/orchestrator/process" \
  -H "Authorization: Bearer $TOK" \
  -F "video=@/tmp/d4_cancel.mp4" \
  -F "session_id=d4-cancel-$(date +%s)" \
  -F "processing_profile=fast")
WF_CANCEL=$(echo "$resp" | python -c 'import sys,json; print(json.load(sys.stdin).get("workflow_id",""))' 2>/dev/null)
log "  submitted: $WF_CANCEL"
sleep 5
log "  cancelling..."
cancel_code=$(curl -s -o /tmp/d4_cancel_resp -w '%{http_code}' \
  -X POST "$ORCH/api/v1/orchestrator/workflows/$WF_CANCEL/cancel" \
  -H "Authorization: Bearer $TOK" 2>&1)
# Phase 12 plane may use a different endpoint
if [ "$cancel_code" = "404" ]; then
  cancel_code=$(curl -s -o /tmp/d4_cancel_resp -w '%{http_code}' \
    -X POST "$ORCH/api/v1/canonical-workflows/$WF_CANCEL/cancel" \
    -H "Authorization: Bearer $TOK")
fi
log "  cancel HTTP=$cancel_code body=$(head -c 200 /tmp/d4_cancel_resp)"
sleep 15
final=$(docker exec nexus-postgres psql -U nexus -d nexus -t -c \
  "SELECT status FROM workflow_state WHERE workflow_id='$WF_CANCEL';" 2>/dev/null | xargs)
log "  workflow final state: $final"
if [ "$final" = "cancelled" ]; then
  log "  ✅ cancel propagated"
elif [ "$final" = "completed" ]; then
  log "  ⚠ workflow completed before cancel landed (fast profile is too fast for this test)"
else
  log "  unexpected final state: $final"
fi

# ─── D5: queue health admin endpoint ─────────────────────────

log
log "═══ D5: Queue health endpoint ═══"
curl -s -H "Authorization: Bearer $TOK" \
  "$ORCH/api/v1/canonical-admin/dlq/health" | head -c 800 >> "$RESULTS"
log

log
log "═══ Phase D complete ═══"
log "Full output: $RESULTS"
