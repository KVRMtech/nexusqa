#!/bin/bash
# TRACK 0.2 ROLLBACK DRILL — prove the fleet does not STAY on a build the gate
# refused.
#
# Self-restoring by construction: the trap puts the baseline back, returns the
# checkout to develop and rebuilds, whatever happens. A drill that can strand
# the VM in detached HEAD is a worse outcome than an unproven drill.
#
# Uses --exploration replay for the red verdict, so no crawl and no lock: the
# rollback path is the thing under test, not the crawler.
set -u
APP=86203785-1fed-4930-8edf-d83988adafab
SRC=/home/srika/nexus-src
COMPOSE=docker-compose.qec.yml
SVC=qe-explorer
cd "$SRC/Nexus_power" || exit 2
BASE=scripts/golden_crawl_baseline.json

GREEN=$(cat "$SRC/.last_green_deploy" 2>/dev/null | tr -d '\r ')
START=$(git -C "$SRC" rev-parse HEAD)
echo "start HEAD    : $START"
echo "last green    : ${GREEN:-<none>}"
if [ -z "$GREEN" ]; then echo "DRILL ABORT: no last-green anchor recorded"; exit 2; fi

cp "$BASE" /tmp/drill_baseline.bak
restore() {
  echo "--- restoring ---"
  git -C "$SRC" checkout -q develop 2>/dev/null || git -C "$SRC" checkout -q "$START"
  cp /tmp/drill_baseline.bak "$SRC/Nexus_power/$BASE" 2>/dev/null
  cd "$SRC/Nexus_power" && docker compose -f "$COMPOSE" up -d "$SVC" >/dev/null 2>&1
  echo "restored HEAD : $(git -C "$SRC" rev-parse --short HEAD)  ($(git -C "$SRC" rev-parse --abbrev-ref HEAD))"
}
trap restore EXIT

EXPL=$(docker exec nexus-postgres psql -U nexus -d qecentral -A -t -c \
  "SELECT exploration_id FROM qe_explorations WHERE app_id='$APP' AND status='completed' ORDER BY created_at DESC LIMIT 1" | tr -d '\r ')

# 1. Force a RED verdict from real evidence.
python3 - "$BASE" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
b["deepest_flow"] = int(b.get("deepest_flow", 0)) + 1
json.dump(b, open(sys.argv[1], "w"), indent=2)
PY
bash scripts/golden_crawl_gate.sh "$APP" --exploration "$EXPL" >/tmp/drill_gate.log 2>&1
RED=$?
echo "gate verdict  : exit $RED (want 3)"
grep -E '^FAIL' /tmp/drill_gate.log | head -2

if [ "$RED" != "3" ]; then echo "DRILL FAILED: gate did not return a red VERDICT"; exit 1; fi

# 2. The rollback command deploy.ps1 issues on a red verdict, verbatim in shape.
echo "--- rolling back to $GREEN ---"
( set -e
  cd "$SRC"; git checkout -q "$GREEN"; cd Nexus_power; ok=0
  for f in "$COMPOSE"; do
    for s in $SVC; do
      if docker compose -f "$f" config --services 2>/dev/null | grep -qx "$s"; then
        docker compose -f "$f" build "$s" >/dev/null 2>&1 && \
        docker compose -f "$f" up -d --force-recreate "$s" >/dev/null 2>&1 && ok=1
      fi
    done
  done
  test "$ok" = 1 )
RB=$?
NOW=$(git -C "$SRC" rev-parse HEAD)
echo "rollback rc   : $RB"
echo "HEAD after    : $NOW"

RUNNING=$(docker inspect -f '{{.State.Running}}' nexus-qe-explorer 2>/dev/null)
echo "explorer up   : $RUNNING"

echo "---"
if [ "$RB" = "0" ] && [ "$NOW" = "$GREEN" ] && [ "$RUNNING" = "true" ]; then
  echo "DRILL PASSED — a red gate returned the fleet to the last GREEN commit, serving."
  exit 0
fi
echo "DRILL FAILED — rc=$RB head=$NOW want=$GREEN running=$RUNNING"
exit 1
