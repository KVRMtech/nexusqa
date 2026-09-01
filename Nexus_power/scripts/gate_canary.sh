#!/bin/bash
# TRACK 0.1 CANARY — prove a regression turns the gate RED.
#
# The assertions are pure functions of a recorded exploration, so this needs no
# deploy and no re-crawl: raise a floor above the value the last green crawl
# actually achieved, re-evaluate that same evidence, and require a red verdict.
# Then restore the baseline and require green again — a canary that leaves the
# floor raised would quietly break every later gate run.
set -u
APP=86203785-1fed-4930-8edf-d83988adafab
cd /home/srika/nexus-src/Nexus_power || exit 2
BASE=scripts/golden_crawl_baseline.json

EXPL=$(docker exec nexus-postgres psql -U nexus -d qecentral -A -t -c \
  "SELECT exploration_id FROM qe_explorations WHERE app_id='$APP' AND status='completed' ORDER BY created_at DESC LIMIT 1" | tr -d '\r ')
if [ -z "$EXPL" ]; then echo "CANARY ABORT: no completed exploration"; exit 2; fi
echo "canary against exploration $EXPL"

cp "$BASE" /tmp/baseline.bak
trap 'cp /tmp/baseline.bak "$BASE"; rm -f /tmp/canary_state.json' EXIT

# The canary runs the gate three times. Against the host's real gap state that
# would age every unmet floor three runs closer to overdue on every canary — so
# a routine canary would eventually turn the gate red by being run. Isolate it.
export GOLDEN_GATE_STATE=/tmp/canary_state.json
rm -f "$GOLDEN_GATE_STATE"

# 1. Baseline unchanged -> must be GREEN (proves the canary's own control case).
bash scripts/golden_crawl_gate.sh "$APP" --exploration "$EXPL" >/tmp/c1.log 2>&1
G1=$?
echo "control run (unmodified baseline): exit $G1"

# 2. Raise deepest_flow one above what the crawl achieved -> must be RED (exit 3).
python3 - "$BASE" <<'PY'
import json, sys
p = sys.argv[1]
b = json.load(open(p))
b["deepest_flow"] = int(b.get("deepest_flow", 0)) + 1
json.dump(b, open(p, "w"), indent=2)
print("floor raised to", b["deepest_flow"])
PY
bash scripts/golden_crawl_gate.sh "$APP" --exploration "$EXPL" >/tmp/c2.log 2>&1
G2=$?
echo "canary run (floor raised):        exit $G2"
grep -E '^(FAIL|GATE)' /tmp/c2.log | head -5

cp /tmp/baseline.bak "$BASE"

# 3. Restored -> must be GREEN again.
bash scripts/golden_crawl_gate.sh "$APP" --exploration "$EXPL" >/tmp/c3.log 2>&1
G3=$?
echo "restore run (baseline restored):  exit $G3"

echo "---"
if [ "$G1" = "0" ] && [ "$G2" = "3" ] && [ "$G3" = "0" ]; then
  echo "CANARY PASSED — a regressed floor turns the gate RED (exit 3) and only then."
  exit 0
fi
echo "CANARY FAILED — expected green(0)/red(3)/green(0), got $G1/$G2/$G3"
echo "--- control ---";  tail -20 /tmp/c1.log
echo "--- canary ---";   tail -20 /tmp/c2.log
exit 1
