#!/usr/bin/env bash
# =============================================================================
# dr_drill.sh — rehearsed backup -> restore -> verify (Phase 2).
#
# The customer's proof-of-behavior evidence IS the product, so "we have backups"
# is not enough — a restore must be REHEARSED and the data proven intact. This
# drill, against the deployed Postgres:
#   1. records the row counts of the evidence tables (the truth to preserve),
#   2. pg_dumps each database,
#   3. restores each dump into a fresh database on the same server,
#   4. re-counts and FAILS on any mismatch,
#   5. reports the wall-clock restore time (an RTO measurement).
#
# Non-destructive: it restores into <db>_drill databases and drops them after.
#
# Requires: kubectl (to reach the postgres pod) + a running verdict install.
# Usage:  NS=verdict scripts/dr_drill.sh
#         PG_POD=<pod> PG_USER=postgres scripts/dr_drill.sh   # overrides
# =============================================================================
set -euo pipefail

NS="${NS:-verdict}"
PG_USER="${PG_USER:-postgres}"
# Databases whose evidence must survive a restore. Discovered, then filtered to
# those that actually exist — no hardcoded assumption that all are present.
CANDIDATE_DBS="${DR_DBS:-qecentral nexus}"
# Evidence tables to count as the integrity witness (present-subset is used).
WITNESS_TABLES="${DR_WITNESS_TABLES:-page_visits factory_test_cases ground_truth_events test_run test_run_step app_cycles}"

command -v kubectl >/dev/null 2>&1 || { echo "FATAL: kubectl required." >&2; exit 1; }

PG_POD="${PG_POD:-$(kubectl -n "$NS" get pod -l app.kubernetes.io/component=postgres -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)}"
[ -n "$PG_POD" ] || { echo "FATAL: no postgres pod found in ns/$NS (set PG_POD)." >&2; exit 1; }

psql() { kubectl -n "$NS" exec -i "$PG_POD" -- psql -U "$PG_USER" -tAX "$@"; }
echo "== postgres pod: $PG_POD (ns/$NS) =="

# databases that exist
EXISTING="$(psql -c "SELECT datname FROM pg_database WHERE datname = ANY (string_to_array('$(echo "$CANDIDATE_DBS" | tr ' ' ',')', ','));" 2>/dev/null || true)"
[ -n "$EXISTING" ] || { echo "FATAL: none of [$CANDIDATE_DBS] exist on this server." >&2; exit 1; }

count_witness() { # $1 = db  -> "table=count" lines for tables that exist
  local db="$1"
  for t in $WITNESS_TABLES; do
    local n
    n="$(kubectl -n "$NS" exec -i "$PG_POD" -- psql -U "$PG_USER" -d "$db" -tAX \
         -c "SELECT to_regclass('public.$t') IS NOT NULL;" 2>/dev/null | tr -d '[:space:]')"
    [ "$n" = "t" ] || continue
    local c
    c="$(kubectl -n "$NS" exec -i "$PG_POD" -- psql -U "$PG_USER" -d "$db" -tAX \
         -c "SELECT count(*) FROM public.$t;" 2>/dev/null | tr -d '[:space:]')"
    echo "$t=$c"
  done
}

fail=0
for db in $EXISTING; do
  echo ""
  echo "== database: $db =="
  before="$(count_witness "$db")"
  echo "  evidence before:"; echo "$before" | sed 's/^/    /'

  drill="${db}_drill"
  t0=$(date +%s)
  echo "  backup -> restore into $drill ..."
  kubectl -n "$NS" exec -i "$PG_POD" -- sh -c "
    set -e
    psql -U '$PG_USER' -c \"DROP DATABASE IF EXISTS ${drill};\" >/dev/null
    psql -U '$PG_USER' -c \"CREATE DATABASE ${drill};\" >/dev/null
    pg_dump -U '$PG_USER' -d '${db}' -Fc | pg_restore -U '$PG_USER' -d '${drill}' --no-owner --no-acl
  " >/dev/null 2>&1 || { echo "  RESTORE FAILED for $db" >&2; fail=1; continue; }
  t1=$(date +%s)

  after="$(NS="$NS" PG_POD="$PG_POD" bash -c "$(declare -f count_witness); WITNESS_TABLES='$WITNESS_TABLES' PG_USER='$PG_USER' count_witness '$drill'")"
  echo "  evidence after restore:"; echo "$after" | sed 's/^/    /'

  if [ "$before" = "$after" ]; then
    echo "  OK — evidence identical after restore (RTO for $db: $((t1 - t0))s)"
  else
    echo "  MISMATCH — restored evidence differs from source for $db" >&2; fail=1
  fi
  kubectl -n "$NS" exec -i "$PG_POD" -- psql -U "$PG_USER" -c "DROP DATABASE IF EXISTS ${drill};" >/dev/null 2>&1 || true
done

echo ""
if [ "$fail" = "0" ]; then
  echo "DR_DRILL: PASS — every database backed up and restored with byte-for-row-count identical evidence."
else
  echo "DR_DRILL: FAIL — a restore failed or the restored evidence did not match." >&2
fi
exit "$fail"
