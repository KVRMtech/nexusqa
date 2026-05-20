#!/usr/bin/env bash
# Phase B — multi-shape upload battery.
# Submits 10 uploads with varied shapes, polls each to terminal,
# logs outcomes to /tmp/phase_b_results.csv.

set -u

ORCH=http://localhost:8100
TENANT=68c79953
TENANT2=nexus-platform
RESULTS=/tmp/phase_b_results.csv
echo "id,scenario,profile,workflow_id,artifact_id,terminal_status,artifact_status,qg_passed,qg_outcome,duration_s,error" > "$RESULTS"

mint_token() {
  docker exec nexus-orchestrator python -c "
import jwt, time, os
secret = os.environ.get('NEXUS_JWT_SECRET', 'test-secret-do-not-use-in-production')
now = int(time.time())
print(jwt.encode({'sub':'defect-hunt','email':'p@n.i','tenant_id':'$1','role':'platform-admin','iat':now,'exp':now+7200}, secret, algorithm='HS256'))"
}

submit() {
  local file=$1 profile=$2 tenant=$3 idem_suffix=$4
  local tag="$5"
  cp "$file" "/tmp/pb_${idem_suffix}.mp4" 2>/dev/null || cp "$file" "/tmp/pb_${idem_suffix}.m4a"
  if [ "$tag" != "duplicate" ]; then
    echo "pb-${idem_suffix}-$(date +%s%N)$RANDOM" >> "/tmp/pb_${idem_suffix}.mp4" 2>/dev/null || \
      echo "pb-${idem_suffix}-$(date +%s%N)$RANDOM" >> "/tmp/pb_${idem_suffix}.m4a"
  fi
  local form_field="video"
  [[ "$file" == *.m4a ]] && form_field="audio"
  local upload_path="/tmp/pb_${idem_suffix}.mp4"
  [[ "$file" == *.m4a ]] && upload_path="/tmp/pb_${idem_suffix}.m4a"

  local TOK=$(mint_token "$tenant")
  curl -sf -X POST "$ORCH/api/v1/orchestrator/process" \
    -H "Authorization: Bearer $TOK" \
    -F "${form_field}=@${upload_path}" \
    -F "session_id=pb-${idem_suffix}-$(date +%s)" \
    -F "processing_profile=${profile}" 2>&1
}

poll() {
  local wf=$1
  local timeout_iter=120  # 120 × 5s = 10 min
  for i in $(seq 1 $timeout_iter); do
    local s=$(docker exec nexus-postgres psql -U nexus -d nexus -t -c "SELECT status FROM workflow_state WHERE workflow_id='$wf';" 2>/dev/null | xargs)
    case "$s" in
      completed|failed|quarantined|cancelled) echo "$s"; return;;
    esac
    sleep 5
  done
  echo "timeout"
}

art_state() {
  local aid=$1
  docker exec nexus-postgres psql -U nexus -d nexus -t -c "SELECT status||'|'||quality_gate_passed||'|'||COALESCE(quality_gate_outcome,'NULL') FROM canonical_artifacts WHERE artifact_id='$aid';" 2>/dev/null | xargs
}

run_test() {
  local idx=$1 scenario="$2" file=$3 profile=$4 tenant=$5 tag=$6
  echo
  echo "═══ [$idx] $scenario ═══"
  local T0=$(date +%s)
  local resp=$(submit "$file" "$profile" "$tenant" "$idx" "$tag")
  local wf=$(echo "$resp" | python -c 'import sys,json; print(json.load(sys.stdin).get("workflow_id",""))' 2>/dev/null)
  local aid=$(echo "$resp" | python -c 'import sys,json; print(json.load(sys.stdin).get("artifact_id",""))' 2>/dev/null)
  if [ -z "$wf" ]; then
    local http_code=$(echo "$resp" | head -c 200)
    echo "$idx,$scenario,$profile,SUBMIT_FAIL,,,,, , ${http_code:0:100}" >> "$RESULTS"
    echo "  ← SUBMIT FAILED ($scenario): ${resp:0:200}"
    return
  fi
  echo "  workflow: $wf"
  echo "  artifact: $aid"
  local term=$(poll "$wf")
  local T1=$(date +%s)
  local dur=$((T1 - T0))
  local astate=$(art_state "$aid")
  echo "  terminal: $term (${dur}s)  art_state: $astate"
  IFS='|' read -r art_status art_qg art_outcome <<< "$astate"
  echo "$idx,$scenario,$profile,$wf,$aid,$term,$art_status,$art_qg,$art_outcome,$dur," >> "$RESULTS"
}

# ─── Battery ─────────────────────────────────────────────────

cd /c/Users/harik/nexusqa

run_test 1 "audio-only m4a"           /tmp/audio_only.m4a                                  fast       "$TENANT"  unique
run_test 2 "video-only no-audio"      Nexus_power/data/evidence/test_synth.mp4             fast       "$TENANT"  unique
run_test 3 "short audio+video 17s"    /tmp/seed_real_audio.mp4                             fast       "$TENANT"  unique
run_test 4 "medium audio+video 35s"   Nexus_power/data/evidence/test_video2.mp4            fast       "$TENANT"  unique
run_test 5 "larger audio+video 38s"   Nexus_power/data/evidence/test_video.mp4             fast       "$TENANT"  unique
run_test 6 "multimodal profile"       /tmp/seed_real_audio.mp4                             multimodal "$TENANT"  unique
run_test 7 "duplicate fingerprint"    /tmp/pb_3.mp4                                        fast       "$TENANT"  duplicate
run_test 8 "deep profile (long)"      /tmp/seed_real_audio.mp4                             deep       "$TENANT"  unique
run_test 9 "cross-tenant"             /tmp/seed_real_audio.mp4                             fast       "$TENANT2" unique
run_test 10 "second multimodal"       Nexus_power/data/evidence/test_video2.mp4            multimodal "$TENANT"  unique

echo
echo "═══ Results CSV ═══"
column -t -s, "$RESULTS"
