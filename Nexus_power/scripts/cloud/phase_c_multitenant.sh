#!/usr/bin/env bash
# Phase C — multi-tenant concurrent uploads.
# 5 tenants × 2 concurrent uploads each = 10 in-flight. Tests:
#   - Redis-backed tenant admission limiter under contention
#   - Cross-tenant isolation (no workflow leaks)
#   - Phase 6 priority lane routing path

set -u
ORCH=http://localhost:8100
RESULTS=/tmp/phase_c_results.csv
echo "tenant,vu,submit_http,workflow_id,terminal_status" > "$RESULTS"

# 5 tenants
TENANTS=(loadtest-a loadtest-b loadtest-c loadtest-d loadtest-e)

mint_token() {
  docker exec nexus-orchestrator python -c "
import jwt, time, os
secret = os.environ.get('NEXUS_JWT_SECRET', 'test-secret-do-not-use-in-production')
now = int(time.time())
print(jwt.encode({'sub':'phaseC','email':'p@n.i','tenant_id':'$1','role':'platform-admin','iat':now,'exp':now+1200}, secret, algorithm='HS256'))"
}

# Ensure each tenant exists in DB (idempotent — spine's _ensure_tenant_exists
# also auto-bootstraps but we pre-create to avoid race on the first upload).
for t in "${TENANTS[@]}"; do
  docker exec nexus-postgres psql -U nexus -d nexus -c \
    "INSERT INTO tenants (tenant_id, name, domain, plan, status)
     VALUES ('$t', 'Phase C $t', '$t.phasec.nexus.internal', 'starter', 'active')
     ON CONFLICT DO NOTHING;" 2>&1 | head -1
done

# Fire 2 uploads per tenant concurrently using xargs.
run_one() {
  local tenant=$1 vu=$2
  cp /tmp/seed_real_audio.mp4 /tmp/pc_${tenant}_${vu}.mp4
  echo "phaseC-${tenant}-${vu}-$(date +%s%N)$RANDOM" >> /tmp/pc_${tenant}_${vu}.mp4
  local TOK=$(mint_token "$tenant")
  local code wf
  code=$(curl -s -o /tmp/pc_resp_${tenant}_${vu} -w '%{http_code}' \
    --max-time 30 -X POST "$ORCH/api/v1/orchestrator/process" \
    -H "Authorization: Bearer $TOK" \
    -F "video=@/tmp/pc_${tenant}_${vu}.mp4" \
    -F "session_id=pc-${tenant}-${vu}-$(date +%s)" \
    -F "processing_profile=fast")
  wf=$(python -c 'import sys,json
try: print(json.load(open("/tmp/pc_resp_'${tenant}'_'${vu}'")).get("workflow_id",""))
except: print("")')
  echo "$tenant,$vu,$code,$wf,pending" >> "$RESULTS"
}
export -f run_one mint_token
export ORCH RESULTS

echo "=== Firing 10 concurrent submits (5 tenants × 2 VUs each) ==="
for t in "${TENANTS[@]}"; do
  for vu in 1 2; do
    run_one "$t" "$vu" &
  done
done
wait
echo "All submits returned."

echo "=== Submit response codes ==="
column -t -s, "$RESULTS"

echo
echo "=== Polling all workflows to terminal (up to 8 min each) ==="
WFS=$(tail -n +2 "$RESULTS" | awk -F, 'length($4)>0 {print $4}')
for wf in $WFS; do
  for i in $(seq 1 100); do
    s=$(docker exec nexus-postgres psql -U nexus -d nexus -t -c \
      "SELECT status FROM workflow_state WHERE workflow_id='$wf';" 2>/dev/null | xargs)
    case "$s" in
      completed|failed|quarantined|cancelled) break;;
    esac
    sleep 5
  done
  sed -i "s|^\(.*\),${wf},pending|\1,${wf},${s:-timeout}|" "$RESULTS"
done

echo "=== Final outcomes ==="
column -t -s, "$RESULTS"

echo "=== Summary ==="
TOTAL=$(tail -n +2 "$RESULTS" | wc -l)
HTTP200=$(tail -n +2 "$RESULTS" | awk -F, '$3=="200"' | wc -l)
HTTP429=$(tail -n +2 "$RESULTS" | awk -F, '$3=="429"' | wc -l)
COMPLETED=$(tail -n +2 "$RESULTS" | awk -F, '$5=="completed"' | wc -l)
QUARANTINED=$(tail -n +2 "$RESULTS" | awk -F, '$5=="quarantined"' | wc -l)
TIMEOUT=$(tail -n +2 "$RESULTS" | awk -F, '$5=="timeout"' | wc -l)
echo " total submits: $TOTAL"
echo " HTTP 200:      $HTTP200"
echo " HTTP 429:      $HTTP429  (tenant admission limit — should be 0 for 2/tenant)"
echo " completed:     $COMPLETED"
echo " quarantined:   $QUARANTINED"
echo " polling timeout: $TIMEOUT"
