#!/usr/bin/env bash
# GOLDEN CRAWL GATE (A2) — a deploy that has not passed one real crawl is not a
# deploy.
#
# WHY THIS EXISTS. On 2026-08-14 seven deploys shipped and THREE broke something
# only a live crawl revealed: a strict-contract refusal that discarded 35 minutes
# of evidence, a danger gate that silently refused the route the operator had
# onboarded, and a read-back verification that rejected every selection it made.
# Each was found by a human running a crawl and reading the substrate, 35 minutes
# at a time. Twice the unit tests were green because the fake modelled what had
# been REASONED about rather than what the live app does.
#
# A green test suite says the code does what its author believed. Only a real
# crawl says the funnel still works.
#
# ── THE RATCHET ─────────────────────────────────────────────────────────────
# This gate does NOT hardcode target numbers. A gate that is red because of a
# known open defect gets ignored within a week, and an ignored gate is worse than
# none — it converts a real signal into background noise.
#
# Instead it remembers the BEST value ever observed for each funnel metric and
# fails only on a REGRESSION below it. So:
#   * it can never be permanently red — today's reality becomes today's floor;
#   * it tightens automatically as the funnel improves, with no one editing
#     thresholds;
#   * a metric that has never worked (wizard advances, today) is reported as an
#     open GAP rather than a failure, and becomes enforced the moment it first
#     works.
#
# The baseline lives in git (scripts/golden_crawl_baseline.json) so the funnel's
# best-known state is version-controlled evidence, reviewed like any other change.
#
# Usage (on the VM):
#   scripts/golden_crawl_gate.sh <app_id> [--update-baseline]
#
# Exit 0 = no regression. Exit 1 = a funnel metric went backwards; the deploy
# should be rolled back or the regression fixed before anything else ships.
set -u

APP_ID="${1:-}"
UPDATE_BASELINE=0
REBASELINE_REASON=""
case "${2:-}" in
  --update-baseline) UPDATE_BASELINE=1 ;;
  # THE THIRD STATE. A metric can legitimately FALL when the funnel gets better:
  # consolidating duplicated one-step fragments into one real journey lowers the
  # page, flow and submit counts while strictly improving the result. With only
  # "fail" and "silently raise the floor" available, the operator's options were
  # to ignore a red gate or to rubber-stamp it — and the second converts a
  # regression detector into a formality on its first disagreement.
  #
  # Lowering a floor therefore requires a WRITTEN REASON, recorded in the
  # baseline next to the value it lowered. A floor can still only move down when
  # a human says why, and the why is reviewable in git forever after.
  --rebaseline)
    REBASELINE_REASON="${3:-}"
    if [ -z "$REBASELINE_REASON" ]; then
      echo "--rebaseline requires a reason: $0 <app_id> --rebaseline \"why\"" >&2
      exit 2
    fi ;;
esac

if [ -z "$APP_ID" ]; then
  echo "usage: $0 <app_id> [--update-baseline | --rebaseline \"reason\"]" >&2
  exit 2
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
BASELINE="$HERE/golden_crawl_baseline.json"
QEC=nexus-qe-central
PG=nexus-postgres
POLL_SECONDS="${GOLDEN_POLL_SECONDS:-60}"
MAX_POLLS="${GOLDEN_MAX_POLLS:-55}"

say() { printf '%s\n' "$*"; }
psql_qec() { docker exec "$PG" psql -U nexus -d qecentral -A -t -c "$1" 2>/dev/null | tr -d '\r'; }

# ── 1. Dispatch a real crawl ────────────────────────────────────────────────
say "=== GOLDEN CRAWL GATE ==="
say "app: $APP_ID"

DISPATCH=$(docker exec "$QEC" python -c "
import os, json, time, urllib.request, jwt, sys
sec = os.environ['NEXUS_JWT_SECRET']
tok = jwt.encode({'sub':'golden-gate','tenant_id':'__platform__',
                  'email':'gate@nexus.internal','role':'admin',
                  'exp':int(time.time())+3600}, sec, algorithm='HS256')
if isinstance(tok, bytes): tok = tok.decode()
body = json.dumps({'app_id': '$APP_ID'}).encode()
req = urllib.request.Request('http://localhost:8093/api/v1/qec/explorations',
    data=body, method='POST',
    headers={'Authorization':'Bearer '+tok,'Content-Type':'application/json'})
try:
    r = urllib.request.urlopen(req, timeout=60)
    print(json.loads(r.read().decode())['exploration_id'])
except urllib.error.HTTPError as e:
    # A REFUSAL is a legitimate gate outcome and must be legible, not a crash:
    # B1 refuses an e2e request it cannot honour, and that is the gate failing
    # loudly for exactly the right reason.
    sys.stderr.write('DISPATCH REFUSED %s: %s\n' % (e.code, e.read().decode()[:400]))
    sys.exit(3)
" 2>&1)
RC=$?
if [ $RC -ne 0 ]; then
  say "FAIL — the crawl could not be dispatched:"
  say "$DISPATCH"
  exit 1
fi
EXPL="${DISPATCH: -36}"
say "exploration: $EXPL"

# ── 2. Wait for a terminal state ────────────────────────────────────────────
STATUS=""
for _ in $(seq 1 "$MAX_POLLS"); do
  STATUS=$(psql_qec "SELECT status FROM qe_explorations WHERE exploration_id='$EXPL';" | tr -d ' ')
  case "$STATUS" in
    completed) break ;;
    failed|refused|cancelled|error|stalled)
      say "FAIL — crawl ended '$STATUS'"
      psql_qec "SELECT left(COALESCE(error,''),400) FROM qe_explorations WHERE exploration_id='$EXPL';"
      exit 1 ;;
  esac
  sleep "$POLL_SECONDS"
done
if [ "$STATUS" != "completed" ]; then
  say "FAIL — crawl did not reach a terminal state within the budget"
  exit 1
fi

# ── 3. Read the funnel ──────────────────────────────────────────────────────
# Every metric below is already recorded by the crawl; nothing here recomputes
# or estimates. The gate reads the same evidence a human would.
read_metrics() {
  psql_qec "
    SELECT concat_ws(' ',
      COALESCE((stats->>'visits')::int, 0),
      COALESCE((stats->'generate'->>'generated')::int, 0),
      COALESCE((stats->'coverage'->>'forms_found')::int, 0),
      COALESCE((stats->'coverage'->>'forms_submitted')::int, 0),
      COALESCE(jsonb_array_length(stats->'coverage'->'fields_inferred'), 0),
      COALESCE((stats->'coverage'->'flow_summary'->>'flows_found')::int, 0),
      COALESCE((stats->'coverage'->'flow_summary'->>'deepest_flow_steps')::int, 0),
      (SELECT COALESCE(sum((v)::int), 0) FROM jsonb_each_text(
          COALESCE(stats->'coverage'->'flow_summary'->'advances_by_tier','{}'::jsonb)) AS t(k,v))
    )
    FROM qe_explorations WHERE exploration_id='$EXPL';"
}
set -- $(read_metrics)
VISITS="${1:-0}"; GENERATED="${2:-0}"; FORMS="${3:-0}"; SUBMITTED="${4:-0}"
AUTOFILL="${5:-0}"; FLOWS="${6:-0}"; DEEPEST="${7:-0}"; ADVANCES="${8:-0}"

# Posture + auth are pass/fail facts, not counts.
TRAVERSAL=$(psql_qec "SELECT COALESCE(stats->'coverage'->>'traversal','') FROM qe_explorations WHERE exploration_id='$EXPL';" | tr -d ' ')
AUTH_BLOCKED=$(psql_qec "SELECT COALESCE(stats->'coverage'->>'auth_blocked','false') FROM qe_explorations WHERE exploration_id='$EXPL';" | tr -d ' ')

say ""
say "--- funnel ---"
printf '  %-22s %s\n' traversal "$TRAVERSAL" pages "$VISITS" forms "$FORMS" \
  auto_filled "$AUTOFILL" submitted "$SUBMITTED" flows "$FLOWS" \
  deepest_flow "$DEEPEST" wizard_advances "$ADVANCES" tests "$GENERATED"

fail=0

# ── 4. Absolute invariants — these must ALWAYS hold ────────────────────────
# Not ratcheted: a crawl that cannot sign in, or runs at the wrong posture, has
# not tested anything, and no historical best makes that acceptable.
if [ "$TRAVERSAL" != "full" ]; then
  say "FAIL  posture: expected 'full', got '${TRAVERSAL:-<none>}' — the crawl ran a sampled probe"
  fail=1
else
  say "OK    posture: full"
fi
if [ "$AUTH_BLOCKED" = "true" ]; then
  say "FAIL  auth: the crawl was blocked at a login wall"
  fail=1
else
  say "OK    auth: not blocked"
fi

# A CHOICE WIDGET THAT WOULD NOT CONFIRM ITS OWN ANSWER. Fixed from 6 to 0 by
# the open-and-pick read-back work; it was only ever a log line, so the fix
# could regress with nothing to notice. Zero is the floor, not a best-ever.
OPEN_UNVERIFIED=$(psql_qec "SELECT COALESCE((stats->'coverage'->>'open_choice_unverified')::int, 0) FROM qe_explorations WHERE exploration_id='$EXPL';" | tr -d ' ')
if [ "${OPEN_UNVERIFIED:-0}" -gt 0 ]; then
  say "FAIL  open_choice_unverified: $OPEN_UNVERIFIED — a choice was picked and would not read back"
  fail=1
else
  say "OK    open_choice_unverified: 0"
fi

# HOW MUCH OF A PAGE THE CRAWL REFUSED TO TOUCH. An over-broad refuse rule does
# not fail — it flags ordinary controls dangerous, the walk skips them, and the
# funnel narrows for a reason no number reports. Live, a URL-scoped `underwrite`
# rule matched against the PAGE url took 20 of 35 hub controls critical and the
# wizard was never entered. Half a page refused is a rule bug, not a safe app.
DANGER_PCT=$(psql_qec "
  SELECT COALESCE(MAX((s->>'danger_controls')::numeric * 100 /
                      NULLIF((s->>'controls_total')::numeric, 0)), 0)::int
  FROM qe_explorations e, jsonb_array_elements(e.stats->'coverage'->'states') s
  WHERE e.exploration_id='$EXPL' AND (s->>'controls_total')::int >= 8;" | tr -d ' ')
if [ "${DANGER_PCT:-0}" -ge 50 ]; then
  say "FAIL  danger ratio: ${DANGER_PCT}% of one page's controls refused (>=50%) — suspect an over-broad refuse rule"
  fail=1
else
  say "OK    danger ratio: max ${DANGER_PCT:-0}% per page"
fi

# TIER-3 LIVENESS. `configured` says the mechanism is wired; an all-tier-1 crawl
# and a crawl with a dead oracle are otherwise indistinguishable.
ORACLE=$(psql_qec "SELECT COALESCE(stats->'coverage'->'advance_oracle'->>'state','') FROM qe_explorations WHERE exploration_id='$EXPL';" | tr -d ' ')
say "INFO  advance_oracle: ${ORACLE:-<unrecorded>}"

# ── 5. The ratchet ─────────────────────────────────────────────────────────
best_of() { python3 -c "
import json,sys
try: b=json.load(open('$BASELINE'))
except Exception: b={}
print(int(b.get(sys.argv[1], 0)))
" "$1"; }

check() {  # metric  current
  local name="$1" cur="$2" best
  best=$(best_of "$name")
  if [ "$cur" -lt "$best" ]; then
    printf 'FAIL  %-18s %s  (regressed from best %s)\n' "$name" "$cur" "$best"
    fail=1
  elif [ "$cur" -gt "$best" ]; then
    printf 'RISE  %-18s %s  (was %s — new floor)\n' "$name" "$cur" "$best"
  elif [ "$best" -eq 0 ]; then
    printf 'GAP   %-18s %s  (never yet achieved — enforced once it works)\n' "$name" "$cur"
  else
    printf 'OK    %-18s %s  (holds at %s)\n' "$name" "$cur" "$best"
  fi
}

say ""
say "--- ratchet (fails only on regression below the best ever seen) ---"
check pages           "$VISITS"
check forms           "$FORMS"
check auto_filled     "$AUTOFILL"
check submitted       "$SUBMITTED"
check flows           "$FLOWS"
check deepest_flow    "$DEEPEST"
check wizard_advances "$ADVANCES"
check tests           "$GENERATED"

# ── 6. Move the floor ──────────────────────────────────────────────────────
# --update-baseline RAISES only, and only on a clean run: today's proven reality
# becomes tomorrow's floor. --rebaseline may also LOWER, and records why.
if [ "$UPDATE_BASELINE" -eq 1 ] && [ "$fail" -eq 0 ]; then
  python3 -c "
import json
try: b=json.load(open('$BASELINE'))
except Exception: b={}
cur = dict(pages=$VISITS, forms=$FORMS, auto_filled=$AUTOFILL,
           submitted=$SUBMITTED, flows=$FLOWS, deepest_flow=$DEEPEST,
           wizard_advances=$ADVANCES, tests=$GENERATED)
for k, v in cur.items():
    b[k] = max(int(b.get(k, 0)), int(v))
json.dump(b, open('$BASELINE','w'), indent=2, sort_keys=True)
open('$BASELINE','a').write('\n')
print('baseline RAISED — commit scripts/golden_crawl_baseline.json')
"
elif [ -n "$REBASELINE_REASON" ]; then
  python3 -c "
import json, sys
try: b=json.load(open('$BASELINE'))
except Exception: b={}
cur = dict(pages=$VISITS, forms=$FORMS, auto_filled=$AUTOFILL,
           submitted=$SUBMITTED, flows=$FLOWS, deepest_flow=$DEEPEST,
           wizard_advances=$ADVANCES, tests=$GENERATED)
lowered = {k: [int(b.get(k, 0)), int(v)] for k, v in cur.items()
           if int(v) < int(b.get(k, 0))}
b.update({k: int(v) for k, v in cur.items()})
# The justification lives WITH the numbers it justifies, so a future reader
# cannot see a lowered floor without also seeing why it was lowered.
b['_rebaselined'] = {
    'reason': '''$REBASELINE_REASON'''[:500],
    'exploration': '$EXPL',
    'lowered': lowered,
}
json.dump(b, open('$BASELINE','w'), indent=2, sort_keys=True)
open('$BASELINE','a').write('\n')
print('baseline RE-BASELINED (lowered: %s)' % (sorted(lowered) or 'none'))
print('reason recorded — commit scripts/golden_crawl_baseline.json')
"
fi

say ""
if [ "$fail" -eq 0 ]; then
  say "GATE PASSED — no funnel regression (exploration $EXPL)"
  exit 0
fi
say "GATE FAILED — the funnel went backwards. Roll back or fix before shipping."
# A DISTINCT CODE FOR "THE FUNNEL REGRESSED", so a caller can tell a VERDICT
# from a FAILURE TO REACH ONE. Sharing exit 1 with "could not dispatch" and
# "crawl never finished" meant a dropped SSH connection was announced as a
# funnel regression — observed live, on a deploy whose funnel had in fact just
# reached its best result ever. A gate that cries wolf gets switched off, and
# then it is not a gate.
exit 3
