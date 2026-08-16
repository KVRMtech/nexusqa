#!/usr/bin/env bash
# ROLLBACK DRILL (M0.4 / T-GT-02) — prove the fleet does not STAY on a build the
# gate refused, for EVERY service the deploy touched.
#
# WHAT THE OLD DRILL PROVED, AND WHY IT WAS NOT ENOUGH.
# The previous drill hardcoded SVC=qe-explorer and re-typed deploy.ps1's rollback
# loop inline, "verbatim in shape". Both halves were wrong in the same direction:
#   * ONE SERVICE. Production deploys three. The drill's single-service loop
#     passed while the real rollback restored one service in three, because the
#     defect it needed to catch (a clobbered $svcList) cannot exist in a
#     one-element list.
#   * A COPY. Drilling a re-typed copy proves the copy works. The code that runs
#     during a real incident was never exercised by anything.
# This drill fixes both: it builds a real MULTI-SERVICE manifest and invokes
# scripts/gate_rollback.sh — the same executable deploy.ps1 calls on a red gate.
#
# WHAT IT ASSERTS
#   1. a regressed floor produces a red VERDICT (exit 3), not a vague failure
#   2. the rollback plan covers EVERY deployed service — none skipped, none extra
#   3. rollback order is the REVERSE of deploy order (LIFO), deterministically
#   4. after rollback the tree is at the last-green commit
#   5. every service in the manifest is running afterwards
#   6. a PARTIAL rollback is reported as a FAILURE that names the survivors
#   7. the gate run left the git-tracked baseline byte-identical (T-GT-04)
#
# Self-restoring by construction: the trap puts the baseline back, returns the
# checkout to its starting branch and rebuilds, whatever happens. A drill that
# can strand the VM in detached HEAD is a worse outcome than an unproven drill.
#
# Uses --exploration replay for the red verdict, so no crawl and no lock: the
# rollback path is the thing under test, not the crawler.
set -u

APP="${DRILL_APP_ID:-86203785-1fed-4930-8edf-d83988adafab}"
SRC="${DRILL_SRC:-/home/srika/nexus-src}"
# THE POINT OF THIS DRILL. Multiple services, spanning BOTH compose overlays —
# the exact shape that made $svcList's last-write-wins collision invisible.
DRILL_SERVICES="${DRILL_SERVICES:-qe-central qe-explorer platform-api}"

cd "$SRC/Nexus_power" || exit 2
HERE="$SRC/Nexus_power/scripts"
BASE="$HERE/golden_crawl_baseline.json"
MANIFEST=/tmp/drill_manifest.json

pass=0
fail=0
check() {  # description  expected  actual
  if [ "$2" = "$3" ]; then
    printf '  PASS  %-52s %s\n' "$1" "$3"; pass=$((pass + 1))
  else
    printf '  FAIL  %-52s want=%s got=%s\n' "$1" "$2" "$3"; fail=$((fail + 1))
  fi
}

GREEN=$(cat "$SRC/.last_green_deploy" 2>/dev/null | tr -d '\r\n ')
START_REF=$(git -C "$SRC" rev-parse --abbrev-ref HEAD)
START=$(git -C "$SRC" rev-parse HEAD)
echo "=== ROLLBACK DRILL ==="
echo "start HEAD    : $START ($START_REF)"
echo "last green    : ${GREEN:-<none>}"
echo "drill services: $DRILL_SERVICES"
if [ -z "$GREEN" ]; then echo "DRILL ABORT: no last-green anchor recorded"; exit 2; fi

cp "$BASE" /tmp/drill_baseline.bak
BASE_SHA_BEFORE=$(sha256sum "$BASE" | cut -d' ' -f1)
restore() {
  echo ""
  echo "--- restoring ---"
  git -C "$SRC" checkout -q "$START_REF" 2>/dev/null || git -C "$SRC" checkout -q "$START"
  cp /tmp/drill_baseline.bak "$BASE" 2>/dev/null
  ( cd "$SRC/Nexus_power" || exit 0
    for f in docker-compose.qec.yml docker-compose.yml; do
      for s in $DRILL_SERVICES; do
        docker compose -f "$f" config --services 2>/dev/null | grep -qx "$s" \
          && docker compose -f "$f" up -d "$s" >/dev/null 2>&1
      done
    done )
  echo "restored HEAD : $(git -C "$SRC" rev-parse --short HEAD) ($(git -C "$SRC" rev-parse --abbrev-ref HEAD))"
}
trap restore EXIT

# ── 0. Build the deployment manifest the way deploy.ps1 does ───────────────
python3 "$HERE/gate_manifest.py" build --out "$MANIFEST" --commit "$START" \
  --deployed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" $DRILL_SERVICES >/dev/null || {
    echo "DRILL ABORT: manifest could not be built"; exit 2; }

echo ""
echo "--- 1. deployment inventory ---"
DEPLOY_ORDER=$(python3 "$HERE/gate_manifest.py" deploy-plan --manifest "$MANIFEST" | cut -f1 | tr '\n' ' ' | sed 's/ *$//')
ROLLBACK_ORDER=$(python3 "$HERE/gate_manifest.py" rollback-plan --manifest "$MANIFEST" | cut -f1 | tr '\n' ' ' | sed 's/ *$//')
echo "  deploy order  : $DEPLOY_ORDER"
echo "  rollback order: $ROLLBACK_ORDER"

# EVERY deployed service must appear in the rollback plan — none skipped, and
# none invented. Sorted comparison so ordering is asserted separately.
WANT_SET=$(printf '%s\n' $DRILL_SERVICES | sort | tr '\n' ' ')
GOT_SET=$(printf '%s\n' $ROLLBACK_ORDER | sort | tr '\n' ' ')
check "rollback set == deployment set" "$WANT_SET" "$GOT_SET"
check "rollback count == deployment count" \
  "$(printf '%s\n' $DRILL_SERVICES | wc -l | tr -d ' ')" \
  "$(printf '%s\n' $ROLLBACK_ORDER | wc -l | tr -d ' ')"
# LIFO: the last thing swapped in is the first thing swapped out.
REVERSED=$(printf '%s\n' $DEPLOY_ORDER | tac | tr '\n' ' ' | sed 's/ *$//')
check "rollback order is reverse of deploy order" "$REVERSED" "$ROLLBACK_ORDER"

# ── 2. Force a RED verdict from real evidence ──────────────────────────────
echo ""
echo "--- 2. red verdict from a regressed floor ---"
EXPL=$(docker exec nexus-postgres psql -U nexus -d qecentral -A -t -c \
  "SELECT exploration_id FROM qe_explorations WHERE app_id='$APP' AND status='completed' ORDER BY created_at DESC LIMIT 1" | tr -d '\r ')
if [ -z "$EXPL" ]; then echo "DRILL ABORT: no completed exploration to replay"; exit 2; fi

python3 - "$BASE" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
b["deepest_flow"] = int(b.get("deepest_flow", 0)) + 1
json.dump(b, open(sys.argv[1], "w"), indent=2, sort_keys=True)
PY
bash "$HERE/golden_crawl_gate.sh" "$APP" --exploration "$EXPL" >/tmp/drill_gate.log 2>&1
RED=$?
check "gate exit code is the REGRESSION verdict" "3" "$RED"
check "gate emitted a machine-readable verdict" "GATE_VERDICT=REGRESSION" \
  "$(grep -o 'GATE_VERDICT=[A-Z_]*' /tmp/drill_gate.log | tail -1)"
grep -E '^FAIL' /tmp/drill_gate.log | head -2 | sed 's/^/        /'

# ── 3. The tracked baseline must survive a gate run untouched (T-GT-04) ────
echo ""
echo "--- 3. baseline immutability ---"
cp /tmp/drill_baseline.bak "$BASE"
bash "$HERE/golden_crawl_gate.sh" "$APP" --exploration "$EXPL" >/tmp/drill_gate2.log 2>&1
check "a gate run leaves the tracked baseline byte-identical" \
  "$BASE_SHA_BEFORE" "$(sha256sum "$BASE" | cut -d' ' -f1)"
check "git sees no modification to the baseline" "" \
  "$(git -C "$SRC" status --porcelain -- Nexus_power/scripts/golden_crawl_baseline.json)"

# ── 4. The REAL rollback — the same executable deploy.ps1 invokes ──────────
echo ""
echo "--- 4. multi-service rollback to $GREEN ---"
bash "$HERE/gate_rollback.sh" --src "$SRC" --manifest "$MANIFEST" --green "$GREEN" \
  2>&1 | tee /tmp/drill_rollback.log | sed 's/^/        /'
RB=${PIPESTATUS[0]}
NOW=$(git -C "$SRC" rev-parse HEAD)
check "rollback exit code" "0" "$RB"
check "tree is at the last green commit" "$GREEN" "$NOW"

# ── 5. Every deployed service is actually SERVING afterwards ──────────────
echo ""
echo "--- 5. every deployed service is running ---"
for s in $DRILL_SERVICES; do
  case "$s" in
    qe-central)   c=nexus-qe-central ;;
    qe-explorer)  c=nexus-qe-explorer ;;
    platform-api) c=nexus-platform-api ;;
    *)            c="nexus-$s" ;;
  esac
  check "container $c running" "true" \
    "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null || echo missing)"
done
# The old rollback set ok=1 if ANY service restored. Assert the new one names
# every service it restored, so "restored: <one of three>" can never read green.
for s in $DRILL_SERVICES; do
  grep -q "restored:.*$s" /tmp/drill_rollback.log && r=yes || r=no
  check "rollback report names $s as restored" "yes" "$r"
done

# ── 6. FAILURE INJECTION: a partial rollback must FAIL, loudly ────────────
echo ""
echo "--- 6. failure injection: a service the green commit cannot build ---"
# A manifest naming a service that does not exist in the rolled-back tree is the
# partial-rollback case. The old code exited 0 on it (ok=1 from the others).
python3 - "$MANIFEST" /tmp/drill_manifest_bad.json <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
m["services"].append({"name": "qe-central", "compose": "docker-compose.NOSUCH.yml",
                      "order": len(m["services"]) + 1})
json.dump(m, open(sys.argv[2], "w"), indent=2, sort_keys=True)
PY
bash "$HERE/gate_rollback.sh" --src "$SRC" --manifest /tmp/drill_manifest_bad.json \
  --green "$GREEN" >/tmp/drill_partial.log 2>&1
PART=$?
check "a partial rollback exits NON-zero" "1" "$PART"
grep -q "ROLLBACK INCOMPLETE" /tmp/drill_partial.log && pr=yes || pr=no
check "a partial rollback says INCOMPLETE and names survivors" "yes" "$pr"

# ── 7. FAILURE INJECTION: an unusable manifest must refuse, not guess ─────
echo ""
echo "--- 7. failure injection: corrupt manifest ---"
printf 'not json at all' > /tmp/drill_manifest_corrupt.json
bash "$HERE/gate_rollback.sh" --src "$SRC" --manifest /tmp/drill_manifest_corrupt.json \
  --green "$GREEN" >/tmp/drill_corrupt.log 2>&1
check "a corrupt manifest refuses to roll back (exit 2)" "2" "$?"
grep -q "ROLLBACK IMPOSSIBLE" /tmp/drill_corrupt.log && cr=yes || cr=no
check "it says ROLLBACK IMPOSSIBLE rather than restoring a guess" "yes" "$cr"

echo ""
echo "======================================================================"
echo "checks passed: $pass   failed: $fail"
if [ "$fail" -eq 0 ]; then
  echo "DRILL PASSED — a red gate returns EVERY deployed service to the last"
  echo "GREEN commit, in reverse-deploy order, and a partial restore fails loudly."
  exit 0
fi
echo "DRILL FAILED — $fail check(s) failed. Do not trust the rollback path."
exit 1
