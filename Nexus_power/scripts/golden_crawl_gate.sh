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
# It is IMMUTABLE during a gate run (M0.4 / T-GT-04): a plain run reads it and
# never writes it. Mutable per-host bookkeeping lives in an untracked runtime
# state file. The ratchet itself lives in scripts/gate_baseline.py so it can be
# unit-tested — it used to be three python heredocs whose metric lists drifted.
#
# Usage (on the VM):
#   scripts/golden_crawl_gate.sh <app_id> [--update-baseline]
#
# ── EXIT CODES ARE THE ROLLBACK DECISION (M0.4 / T-GT-05) ───────────────────
#   0  PASS             — no funnel regression.
#   2  usage error      — the gate was invoked wrongly. No verdict.
#   3  REGRESSION       — a funnel metric went backwards. ROLL BACK.
#   4  HOST UNAVAILABLE — the infrastructure could not support a verdict (disk,
#                         dead container, no DB). NOT a verdict on the build, and
#                         explicitly NOT a rollback trigger: reverting a healthy
#                         deployment because monitoring was down is an outage we
#                         caused ourselves.
#   1  APP UNHEALTHY    — the build was reachable but could not produce a crawl:
#                         dispatch refused, crawl failed/stalled. That IS a
#                         statement about the build. ROLL BACK.
# The final line of output is always `GATE_VERDICT=<PASS|REGRESSION|APP_UNHEALTHY
# |HOST_UNAVAILABLE|USAGE>` so a caller that only captured stdout — or whose SSH
# dropped before the exit code arrived — can still tell these apart.
set -u

EXIT_PASS=0
EXIT_APP_UNHEALTHY=1
EXIT_USAGE=2
EXIT_REGRESSION=3
EXIT_HOST_UNAVAILABLE=4

# Every exit goes through here so the machine-readable verdict can never drift
# from the exit code — they are emitted by the same statement.
finish() {  # verdict  code
  printf 'GATE_VERDICT=%s\n' "$1"
  exit "$2"
}

APP_ID="${1:-}"
UPDATE_BASELINE=0
REBASELINE_REASON=""
REUSE_EXPL=""
case "${2:-}" in
  --update-baseline) UPDATE_BASELINE=1 ;;
  # EVALUATE A CRAWL THAT ALREADY RAN. The assertions are pure functions of a
  # recorded exploration, so re-running them should not cost another 20-minute
  # crawl. This is what makes the CANARY cheap enough to be routine: raise a
  # floor above the observed value, re-evaluate the same evidence, and watch the
  # gate go red — a real end-to-end assertion run, no deploy, no re-crawl.
  --exploration)
    REUSE_EXPL="${3:-}"
    if [ -z "$REUSE_EXPL" ]; then
      echo "--exploration requires an exploration id" >&2
      finish USAGE $EXIT_USAGE
    fi ;;
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
      finish USAGE $EXIT_USAGE
    fi ;;
esac

if [ -z "$APP_ID" ]; then
  echo "usage: $0 <app_id> [--update-baseline | --rebaseline \"reason\" | --exploration <id>]" >&2
  finish USAGE $EXIT_USAGE
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
#: ONE BASELINE FILE PER APP. The floors are an APP's contract, not the
#: product's: pages/forms/submits mean nothing across two different
#: applications. The golden app keeps golden_crawl_baseline.json unchanged, so
#: nothing about today's gate moves; every other app is judged against its own
#: golden_crawl_baseline.<app_id>.json.
#:
#: Named FLOORS_* rather than BASELINE_*: a contract test requires every line
#: mentioning $BASELINE to be an assignment or a $RATCHET call, and "$BASELINE_APP"
#: contains "$BASELINE" as a substring. The test is right - the baseline has one
#: owner - so the variables are named not to collide with it.
FLOORS_DEFAULT="$HERE/golden_crawl_baseline.json"
FLOORS_APP="$HERE/golden_crawl_baseline.$APP_ID.json"
BASELINE="$FLOORS_DEFAULT"
RATCHET="$HERE/gate_baseline.py"
QEC=nexus-qe-central
PG=nexus-postgres
#: The control that is the ENTRANCE to this app's deepest funnel. Per-app by
#: nature — the gate is an app's contract — and overridable so a matrix row
#: names its own door rather than inheriting Summit's.
GATE_ENTRY_CONTROL="${GATE_ENTRY_CONTROL:-New Application}"
POLL_SECONDS="${GOLDEN_POLL_SECONDS:-60}"
#: 55 x 60s. Summit's full crawl has been measured twice at ~43 min end to end
#: (2026-09-02), so the budget stays well clear of it - a healthy run must never
#: be terminated as a stall.
MAX_POLLS="${GOLDEN_MAX_POLLS:-55}"

#: WHICH APP THIS BASELINE BELONGS TO.
#:
#: golden_crawl_baseline.json is ONE flat set of floors - it is not keyed by
#: app. The gate accepts any app_id, so pointing it at a different application
#: silently ratchets that app's funnel against THIS app's numbers. Measured
#: 2026-09-02 by running the gate on a vkpowerlife app: the SAME run reported
#: deepest_flow 9 (was 4) and wizard_advances 25 (was 3) as RISES while forms 3
#: (best 8) and catalog_questions 35 (best 83) came out as FAILS. Rises and
#: falls together in one run is the signature of two different applications,
#: not a funnel going backwards - but the verdict was REGRESSION, which inside
#: a deploy means an automatic rollback of a blameless build.
#:
#: The worse form is silent: a mismatched app that happened to run clean under
#: --update-baseline would RAISE these floors to another app's numbers, and the
#: real golden app would then fail its own gate for ever. That did not happen -
#: raising needs --update-baseline AND a clean run, and the baseline was checked
#: byte-unchanged afterwards - but nothing prevented it.
#:
#: Held here rather than in the baseline: that file has exactly one writer (the
#: ratchet), and the gate's contract tests enforce it. Override per run.
GOLDEN_APP_EXPECTED="${GOLDEN_APP_EXPECTED:-86203785-1fed-4930-8edf-d83988adafab}"

say() { printf '%s\n' "$*"; }
psql_qec() { docker exec "$PG" psql -U nexus -d qecentral -A -t -c "$1" 2>/dev/null | tr -d '\r'; }

# ── 0. HOST HEALTH (Track 0.4) ──────────────────────────────────────────────
# A crawl on an unhealthy host produces a CONFUSING result, not an obviously
# broken one: a full disk truncates evidence, a dead container refuses dispatch
# in a way that reads like an app problem, and hundreds of exited containers
# quietly eat the loop device table. Failing fast here costs 2 seconds and saves
# the investigation that a half-degraded crawl always triggers.
say "=== GOLDEN CRAWL GATE ==="
say "app: $APP_ID"

# WHICH FLOORS JUDGE THIS APP — resolved BEFORE a 40-minute crawl is spent.
#
#   its own baseline exists   -> use it
#   it IS the golden app      -> the original file, exactly as before
#   --rebaseline was passed   -> create its baseline from this run
#   otherwise                 -> refuse, and say how to onboard it
#
# The refusal is HOST_UNAVAILABLE, never REGRESSION: "no floors exist for your
# app" is the ABSENCE of a verdict, and deploy.ps1 maps that to abort-without-
# rollback. Measured 2026-09-02, before this existed: running the gate on a
# vkpowerlife app against Summit's floors reported deepest_flow 9 (was 4) as a
# RISE and forms 3 (best 8) as a FAIL in the SAME run - rises and falls together
# being the signature of two different applications - and the verdict was
# REGRESSION, which inside a deploy rolls back a blameless build.
if [ -f "$FLOORS_APP" ]; then
  BASELINE="$FLOORS_APP"
  say "floors: golden_crawl_baseline.$APP_ID.json (this app's own)"
elif [ "$APP_ID" = "$GOLDEN_APP_EXPECTED" ]; then
  BASELINE="$FLOORS_DEFAULT"
  say "floors: golden_crawl_baseline.json (the golden app)"
elif [ -n "$REBASELINE_REASON" ]; then
  BASELINE="$FLOORS_APP"
  say "floors: golden_crawl_baseline.$APP_ID.json — CREATING from this run"
else
  say ""
  say "GATE ABORTED — no baseline exists for app '$APP_ID'."
  say "  Floors are an APP's contract. Judging this app against another's"
  say "  reports a REGRESSION that is really just a different application,"
  say "  so no verdict about this build is possible. NOT rolling back."
  say ""
  say "  To put this app under the gate, take one clean crawl as its floor:"
  say "    bash scripts/golden_crawl_gate.sh $APP_ID --rebaseline \"onboarding <client>\""
  say "  That writes golden_crawl_baseline.$APP_ID.json, reviewable in git,"
  say "  and every later run is judged against it alone."
  finish HOST_UNAVAILABLE $EXIT_HOST_UNAVAILABLE
fi

# ONE definition of healthy, shared with the pre-swap preflight deploy.ps1 runs
# (scripts/host_health.sh). A preflight that checked something subtly different
# from the gate would admit a deploy through a door the gate then refuses.
. "$HERE/host_health.sh"
if ! host_health "$QEC" "$PG" nexus-qe-explorer; then
  say ""
  say "GATE ABORTED — the HOST is unhealthy. This is not a verdict on the build."
  # T-GT-05. This used to exit 1, which deploy.ps1 read as "the gate reached no
  # verdict" and answered with a rollback. So a full disk on the VM reverted a
  # deployment that was, as far as anyone could tell, perfectly healthy — an
  # outage manufactured by our own monitoring. Infrastructure failures abort;
  # they do not revert. Exit 4 says exactly that and nothing else.
  say "The deployment is NOT rolled back: a monitoring failure is not a build failure."
  say "Fix the host, then re-run the gate to obtain a verdict."
  finish HOST_UNAVAILABLE $EXIT_HOST_UNAVAILABLE
fi

if [ -n "$REUSE_EXPL" ]; then
  EXPL="$REUSE_EXPL"
  say "exploration: $EXPL  (re-evaluating a recorded crawl — no dispatch)"
else
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
  # The containers were confirmed running by host_health above, so a refused or
  # failed dispatch is the BUILD refusing work — a statement about this deploy.
  finish APP_UNHEALTHY $EXIT_APP_UNHEALTHY
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
      finish APP_UNHEALTHY $EXIT_APP_UNHEALTHY ;;
  esac
  sleep "$POLL_SECONDS"
done
if [ "$STATUS" != "completed" ]; then
  say "FAIL — crawl did not reach a terminal state within the budget"
  finish APP_UNHEALTHY $EXIT_APP_UNHEALTHY
fi
fi   # end: dispatch-and-wait (skipped by --exploration)

# ── 2b. The evidence must EXIST before it can be judged ─────────────────────
# If postgres dies between the crawl and the read, every metric reads 0, every
# floor "regresses", and the gate announces a total funnel collapse — rolling
# back a healthy deploy on the strength of a database that was not answering.
# A missing exploration row is an INFRASTRUCTURE fact, not a funnel verdict.
EXPL_ROWS=$(psql_qec "SELECT count(*) FROM qe_explorations WHERE exploration_id='$EXPL';" | tr -d ' ')
if [ "${EXPL_ROWS:-0}" != "1" ]; then
  say ""
  say "GATE ABORTED — exploration '$EXPL' is not readable (rows=${EXPL_ROWS:-<no answer>})."
  say "Metrics cannot be read, so no verdict about the funnel is possible."
  say "The deployment is NOT rolled back: an unreadable substrate is not a regression."
  finish HOST_UNAVAILABLE $EXIT_HOST_UNAVAILABLE
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
      COALESCE((stats->'coverage'->>'forms_confirmed')::int, 0),
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
CONFIRMED="${5:-0}"; AUTOFILL="${6:-0}"; FLOWS="${7:-0}"; DEEPEST="${8:-0}"
ADVANCES="${9:-0}"

# Posture + auth are pass/fail facts, not counts.
TRAVERSAL=$(psql_qec "SELECT COALESCE(stats->'coverage'->>'traversal','') FROM qe_explorations WHERE exploration_id='$EXPL';" | tr -d ' ')
AUTH_BLOCKED=$(psql_qec "SELECT COALESCE(stats->'coverage'->>'auth_blocked','false') FROM qe_explorations WHERE exploration_id='$EXPL';" | tr -d ' ')

say ""
say "--- funnel ---"
printf '  %-22s %s\n' traversal "$TRAVERSAL" pages "$VISITS" forms "$FORMS" \
  auto_filled "$AUTOFILL" submitted "$SUBMITTED" confirmed "$CONFIRMED" flows "$FLOWS" \
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

# THE DROPDOWNS WERE ANSWERED, not merely "29 fields were". A portal-rendered
# choice reads back empty, its fill is discarded, and the total barely moves
# because text fields carried it — so auto_filled alone cannot see the widget
# class that keeps breaking. Ratcheted like any other floor.
SELECTS=$(psql_qec "SELECT COALESCE((stats->'coverage'->'filled_by_kind'->>'select')::int, 0) FROM qe_explorations WHERE exploration_id='$EXPL';" | tr -d ' ')

# THE ONE DOOR INTO THE FUNNEL. A danger RATIO catches a refuse rule that went
# broad; only the NAMES catch a rule that took out the single control a funnel
# depends on. Live, a URL-scoped `underwrite` rule flagged `New Application` -
# the only entrance to the five-step wizard - and the walk simply stopped, with
# every other number looking reasonable.
GUARDED=$(psql_qec "
  SELECT count(*) FROM qe_explorations e,
       jsonb_array_elements(e.stats->'coverage'->'states') st,
       jsonb_array_elements_text(st->'danger_names') nm
  WHERE e.exploration_id='$EXPL' AND lower(nm) = lower('$GATE_ENTRY_CONTROL');" | tr -d ' ')
if [ "${GUARDED:-0}" -gt 0 ]; then
  say "FAIL  entry control: '$GATE_ENTRY_CONTROL' is danger-flagged — the funnel's own door is refused"
  fail=1
else
  say "OK    entry control: '$GATE_ENTRY_CONTROL' not danger-flagged"
fi

# TIER-3 LIVENESS. `configured` says the mechanism is wired; an all-tier-1 crawl
# and a crawl with a dead oracle are otherwise indistinguishable.
ORACLE=$(psql_qec "SELECT COALESCE(stats->'coverage'->'advance_oracle'->>'state','') FROM qe_explorations WHERE exploration_id='$EXPL';" | tr -d ' ')
say "INFO  advance_oracle: ${ORACLE:-<unrecorded>}"

# ── 4b. CATALOG COMPLETENESS (Track M0.4 / T-GT-06) ─────────────────────────
# The Master Catalog is what this product actually delivers — the deduped set of
# questions an application asks. Every floor above measures the CRAWL; none
# measured the ARTEFACT. So a fold change that dropped a question class, or a
# dedup key that collapsed distinct questions into one, shrank the catalog while
# pages/forms/flows all held and the gate went green. The catalog is app-scoped
# and deduped by stable question_id, so its size is directly comparable across
# crawls and belongs in the ratchet like any other floor.
CATALOG=$(psql_qec "SELECT count(*) FROM catalog_questions WHERE app_id='$APP_ID';" | tr -d ' ')
if [ -z "$CATALOG" ]; then
  # An unanswered count is not a count of zero. Reporting it as 0 would fake a
  # total catalog collapse and trigger a rollback on a missing table.
  say ""
  say "GATE ABORTED — catalog_questions could not be counted for app '$APP_ID'."
  say "The deployment is NOT rolled back: an unreadable table is not a shrunk catalog."
  finish HOST_UNAVAILABLE $EXIT_HOST_UNAVAILABLE
fi
say "INFO  catalog_questions: $CATALOG"

# ── 5. The ratchet ─────────────────────────────────────────────────────────
#: A floor may sit unmet while the capability that feeds it is still being
#: built — but not forever, and not silently. After this many gate runs, or this
#: many days since it was first seen unmet, an unenforced floor fails the gate:
#: either the capability landed and the floor should hold, or it did not, and
#: that is the news.
GAP_MAX_RUNS="${GOLDEN_GAP_MAX_RUNS:-3}"
GAP_MAX_DAYS="${GOLDEN_GAP_MAX_DAYS:-7}"

# ONE JSON object of everything measured. The ratchet lives in gate_baseline.py,
# which owns the metric list — so a metric cannot be evaluated here and silently
# missing from the writer that persists it, which is exactly how selects_filled
# and forms_confirmed spent their entire lives at floor 0 (T-GT-03).
CURRENT_JSON=$(printf '{"pages":%s,"forms":%s,"auto_filled":%s,"selects_filled":%s,"forms_confirmed":%s,"submitted":%s,"flows":%s,"deepest_flow":%s,"wizard_advances":%s,"tests":%s,"catalog_questions":%s}' \
  "${VISITS:-0}" "${FORMS:-0}" "${AUTOFILL:-0}" "${SELECTS:-0}" "${CONFIRMED:-0}" \
  "${SUBMITTED:-0}" "${FLOWS:-0}" "${DEEPEST:-0}" "${ADVANCES:-0}" "${GENERATED:-0}" \
  "${CATALOG:-0}")

say ""
say "--- ratchet (fails only on regression below the best ever seen) ---"
RATCHET_ERR=$(mktemp)
python3 "$RATCHET" evaluate --baseline "$BASELINE" --current "$CURRENT_JSON" 2>"$RATCHET_ERR"
if [ $? -ne 0 ]; then fail=1; fi
GAPS=$(sed -n 's/^GAPS=//p' "$RATCHET_ERR" | head -1)
rm -f "$RATCHET_ERR"

# ── 6. Move the floor ──────────────────────────────────────────────────────
# EXPLICIT COMMANDS ONLY (T-GT-04). A plain gate run never writes the baseline;
# --update-baseline RAISES only, and only on a clean run, so today's proven
# reality becomes tomorrow's floor. --rebaseline may also LOWER, and records why.
if [ "$UPDATE_BASELINE" -eq 1 ] && [ "$fail" -eq 0 ]; then
  python3 "$RATCHET" raise --baseline "$BASELINE" --current "$CURRENT_JSON"
elif [ -n "$REBASELINE_REASON" ]; then
  python3 "$RATCHET" rebaseline --baseline "$BASELINE" --current "$CURRENT_JSON" \
    --reason "$REBASELINE_REASON" --exploration "$EXPL"
fi

say ""

# -- 6. UNENFORCED FLOORS ARE NOT PASSES (Step 3) ---------------------------
# Rule 1: a GAP prints as NOT YET ENFORCED and never counts as green.
# Rule 2: a GAP self-enforces the moment a crawl carries its data -- that is the
#         existing RISE branch, since any value above a zero floor raises it.
# Rule 3: a floor still unmet after GAP_MAX_RUNS runs or GAP_MAX_DAYS days turns
#         the gate RED. Live case: forms_confirmed sat at 0 behind a PASSING
#         gate while nine submits fired and the application confirmed none.
if [ -n "$GAPS" ]; then
  say ""
  say "--- unenforced floors ---"
fi
# THE BOOKKEEPING GOES TO RUNTIME STATE, NOT THE BASELINE (T-GT-04). This block
# used to json.dump() the run counters straight back into the git-tracked
# baseline, so every single gate run — including a pure read-only evaluation —
# left the working tree dirty, and the next `git pull` on the VM hit a conflict
# on a file whose only changed content was a counter no reviewer can act on.
GAP_ARGS=""
for _g in $GAPS; do GAP_ARGS="$GAP_ARGS --gap $_g"; done
GAP_VERDICT=$(python3 "$RATCHET" gaps --baseline "$BASELINE" \
  --max-runs "$GAP_MAX_RUNS" --max-days "$GAP_MAX_DAYS" $GAP_ARGS)
if [ -n "$GAP_VERDICT" ]; then
  say "FAIL  a floor that has never been met is overdue:"
  printf '%s\n' "$GAP_VERDICT" | tr '|' '\n' | sed 's/^/        /'
  say "      Either the capability landed and the floor should hold, or it did not."
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  say "GATE PASSED — no funnel regression (exploration $EXPL)"
  finish PASS $EXIT_PASS
fi
say "GATE FAILED — the funnel went backwards. Roll back or fix before shipping."
# A DISTINCT CODE FOR "THE FUNNEL REGRESSED", so a caller can tell a VERDICT
# from a FAILURE TO REACH ONE. Sharing exit 1 with "could not dispatch" and
# "crawl never finished" meant a dropped SSH connection was announced as a
# funnel regression — observed live, on a deploy whose funnel had in fact just
# reached its best result ever. A gate that cries wolf gets switched off, and
# then it is not a gate.
finish REGRESSION $EXIT_REGRESSION
