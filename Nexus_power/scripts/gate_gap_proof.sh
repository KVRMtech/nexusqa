#!/bin/bash
# STEP 3 PROOF — an unenforced floor is never green, and turns RED once overdue.
# Subject: forms_confirmed, a REAL gap (0 confirmed of 9 submits), not a fake.
set -u
APP=86203785-1fed-4930-8edf-d83988adafab
cd /home/srika/nexus-src/Nexus_power || exit 2
BASE=scripts/golden_crawl_baseline.json
cp "$BASE" /tmp/gap_base.bak
trap 'cp /tmp/gap_base.bak "$BASE"' EXIT

# Start from a clean gap history so the run counter is unambiguous.
python3 - "$BASE" <<'PY'
import json, sys
b = json.load(open(sys.argv[1])); b.pop("_gaps", None)
json.dump(b, open(sys.argv[1], "w"), indent=2)
PY

EXPL=$(docker exec nexus-postgres psql -U nexus -d qecentral -A -t -c \
  "SELECT exploration_id FROM qe_explorations WHERE app_id='$APP' AND status='completed' ORDER BY created_at DESC LIMIT 1" | tr -d '\r ')
echo "replaying $EXPL (GAP_MAX_RUNS=3)"

for i in 1 2 3 4; do
  bash scripts/golden_crawl_gate.sh "$APP" --exploration "$EXPL" >/tmp/g$i.log 2>&1
  rc=$?
  line=$(grep -E 'NOT YET ENFORCED|overdue|unmet for' /tmp/g$i.log | head -1 | sed 's/^ *//')
  echo "run $i: exit $rc  | $line"
done

echo "---"
r1=$(bash -c 'grep -c "GATE PASSED" /tmp/g1.log'); r4rc=$(grep -c "overdue" /tmp/g4.log)
G1=$(grep -q "GATE PASSED" /tmp/g1.log && echo pass || echo fail)
G4=$(grep -q "GATE FAILED" /tmp/g4.log && echo fail || echo pass)
echo "run1 verdict=$G1   run4 verdict=$G4"
if [ "$G1" = "pass" ] && [ "$G4" = "fail" ]; then
  echo "STEP 3 PROVEN — a never-met floor is tolerated while young, RED once overdue."
  exit 0
fi
echo "STEP 3 NOT PROVEN"; tail -25 /tmp/g4.log; exit 1
