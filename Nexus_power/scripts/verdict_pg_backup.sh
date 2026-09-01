#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# VKPower Verdict — Postgres backup + restore-drill (Phase 6.3a)
# Dumps BOTH databases the product depends on — `nexus` (VKPower factory +
# substrate + evidence) and `qecentral` (Verdict tables) — and ships them to
# GCS. The restore-drill (--restore-drill) actually RESTORES into a throwaway
# database and verifies row counts, so "we have backups" is PROVEN recoverable,
# not assumed. 6.3a exit criterion: a backup lands AND a restore-drill passes.
#
# Nightly cron:  17 3 * * *  GCS_BACKUP_BUCKET=gs://…  verdict_pg_backup.sh
# Restore drill: verdict_pg_backup.sh --restore-drill
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
PG="${PG_CONTAINER:-nexus-postgres}"
# DB role used for every dump/restore/psql call (prod = the `nexus` owner).
PG_USER="${PG_USER:-nexus}"
# Transport for reaching Postgres:
#   docker (default) — run INSIDE the $PG container (production on the VM);
#   local            — run the client tools directly against the libpq PG* env
#                      (a CI Postgres service container reached over TCP).
# Behaviour is otherwise identical, so CI can exercise the SAME script the
# operator runs (design R-7: prove the production script, never a CI fork).
PG_TRANSPORT="${PG_TRANSPORT:-docker}"
_run()   { if [ "$PG_TRANSPORT" = local ]; then "$@"; else docker exec    "$PG" "$@"; fi; }
_run_i() { if [ "$PG_TRANSPORT" = local ]; then "$@"; else docker exec -i "$PG" "$@"; fi; }
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
# Persistent LOCAL backup dir (always written) + OPTIONAL offsite GCS bucket. When
# GCS_BACKUP_BUCKET is empty the backup is local-only; when set, the local dump is
# ALSO copied to GCS. A configured off-host target is a durability requirement:
# a failed copy makes the run fail and therefore does not refresh the success
# freshness metric as though an off-host backup existed.
LOCAL_BACKUP_DIR="${LOCAL_BACKUP_DIR:-$HOME/db_backups}"
GCS_BACKUP_BUCKET="${GCS_BACKUP_BUCKET:-}"
BACKUP_RETENTION="${BACKUP_RETENTION:-14}"   # keep newest N local dumps per db
mkdir -p "$LOCAL_BACKUP_DIR"

# ── OPT-IN fleet observability (Phase 7): emit a node_exporter textfile metric ──
# When NODE_EXPORTER_TEXTFILE_DIR is set, record the epoch of the last SUCCESSFUL
# backup / restore-drill so Prometheus can alert on staleness
# (VerdictBackupStale / VerdictRestoreDrillMissed in
# infrastructure/observability/verdict/alerts-verdict.yml). When the env var is
# UNSET this is a hard no-op — today's behaviour is preserved byte-for-byte.
# The write is ATOMIC (temp file + rename) so node_exporter never reads a partial
# file, and a write failure degrades to a log line, never a backup failure.
emit_textfile_metric() {
  local dir="${NODE_EXPORTER_TEXTFILE_DIR:-}"
  [ -n "$dir" ] || return 0
  if [ ! -d "$dir" ]; then
    echo "TEXTFILE_DIR_MISSING:$dir (skipping metric emit; create it or unset NODE_EXPORTER_TEXTFILE_DIR)"
    return 0
  fi
  local name="$1" value="$2" help="$3"
  local tmp="$dir/.${name}.$$.tmp" final="$dir/${name}.prom"
  if {
        printf '# HELP %s %s\n' "$name" "$help"
        printf '# TYPE %s gauge\n' "$name"
        printf '%s %s\n' "$name" "$value"
     } > "$tmp" 2>/dev/null && mv -f "$tmp" "$final" 2>/dev/null; then
    echo "TEXTFILE_METRIC_WRITTEN:${name}=${value} -> ${final}"
  else
    rm -f "$tmp" 2>/dev/null || true
    echo "TEXTFILE_METRIC_SKIPPED:${name} (write failed; check dir perms)"
  fi
}

backup() {
  echo "==== BACKUP $STAMP → local:$LOCAL_BACKUP_DIR${GCS_BACKUP_BUCKET:+ + $GCS_BACKUP_BUCKET} ===="
  for db in nexus qecentral; do
    echo "-- dumping $db --"
    out="$LOCAL_BACKUP_DIR/${db}_${STAMP}.dump"
    # -Fc custom format = compressed + parallel-restorable; SoR integrity first.
    if ! _run pg_dump -U "$PG_USER" -d "$db" -Fc > "$out"; then
      echo "DUMP_FAILED:$db"; exit 1
    fi
    sz=$(stat -c%s "$out" 2>/dev/null || echo 0)
    echo "   $db dump = ${sz} bytes -> $out"
    [ "$sz" -gt 1000 ] || { echo "DUMP_TOO_SMALL:$db (refusing to keep a suspect backup)"; rm -f "$out"; exit 1; }
    # An explicitly configured GCS destination is the off-host system of
    # record. Do not report a local-only run as a successful production backup.
    if [ -n "$GCS_BACKUP_BUCKET" ]; then
      command -v gsutil >/dev/null 2>&1 || {
        echo "GCS_COPY_UNAVAILABLE:$db (gsutil is required for $GCS_BACKUP_BUCKET)"; exit 1;
      }
      if ! gsutil cp "$out" "$GCS_BACKUP_BUCKET/${db}/${db}_${STAMP}.dump" >/dev/null 2>&1; then
        echo "GCS_COPY_FAILED:$db (local dump retained; off-host backup failed)"; exit 1
      fi
      echo "   copied $db to $GCS_BACKUP_BUCKET"
    fi
    # Retention: keep the newest N local dumps per db.
    ls -1t "$LOCAL_BACKUP_DIR/${db}_"*.dump 2>/dev/null | tail -n +"$((BACKUP_RETENTION+1))" | xargs -r rm -f
  done
  echo "BACKUP_OK $STAMP"
  emit_textfile_metric verdict_backup_last_success_timestamp_seconds "$(date -u +%s)" \
    "Unix time of the last successful Verdict Postgres backup (both nexus+qecentral dumped)."
}

# Emit "tablename rowcount" for every public base table in $1 via ONE dynamic query
# (2 psql calls total). EXACT counts — never pg_stat estimates (which are 0 until
# ANALYZE and would green-wash an empty restore as "matching").
counts_for() {
  local db="$1" gen
  gen=$(_run psql -U "$PG_USER" -d "$db" -t -A -c \
    "select coalesce(string_agg(format('select %L as t, count(*) as n from public.%I', tablename, tablename), ' union all '), 'select null::text as t, 0 as n where false') from pg_tables where schemaname='public';" 2>/dev/null)
  [ -n "$gen" ] || return 0
  _run psql -U "$PG_USER" -d "$db" -t -A -F' ' -c "$gen" 2>/dev/null
}

alembic_head() {
  _run psql -U "$PG_USER" -d "$1" -t -A -c \
    "select version_num from alembic_version;" 2>/dev/null | tr -d '[:space:]'
}

# Restore ONE db's fresh dump into a throwaway and PROVE recovery: (1) the restored
# DB is at the SAME alembic head as the source, and (2) no table that has rows in
# the source is EMPTY in the restore (catches the "schema-present but data-missing"
# green-wash the old table-count check let through). Returns non-zero on failure.
drill_one() {
  # Lowercase the throwaway DB name: an UNQUOTED `CREATE DATABASE` folds identifiers to
  # lowercase, but pg_restore/-d would use the original mixed-case stamp (…T…Z) and fail
  # "database does not exist" — making every table read as a (false) empty restore.
  local db="$1" drill
  drill=$(printf 'verdict_restore_drill_%s_%s' "$db" "$STAMP" | tr 'A-Z' 'a-z')
  echo "-- restore-drill: $db --"
  local dump="$WORK/drill_${db}.dump" latest
  if [ -n "$GCS_BACKUP_BUCKET" ]; then
    command -v gsutil >/dev/null 2>&1 || { echo "DRILL_GCS_UNAVAILABLE:$db"; return 1; }
    latest=$(gsutil ls "$GCS_BACKUP_BUCKET/${db}/${db}_"*.dump 2>/dev/null | sort | tail -n 1)
    [ -n "$latest" ] || { echo "DRILL_GCS_DUMP_MISSING:$db"; return 1; }
    echo "   restoring off-host copy: $latest"
    gsutil cp "$latest" "$dump" >/dev/null 2>&1 || { echo "DRILL_GCS_COPY_FAIL:$db"; return 1; }
  else
    # Development/offline drill: use the newest retained local dump. A fresh
    # dump is only the compatibility fallback when no local backup exists.
    latest=$(ls -1t "$LOCAL_BACKUP_DIR/${db}_"*.dump 2>/dev/null | head -n 1)
    if [ -n "$latest" ]; then
      cp "$latest" "$dump" || { echo "DRILL_LOCAL_COPY_FAIL:$db"; return 1; }
    else
      _run pg_dump -U "$PG_USER" -d "$db" -Fc > "$dump" || { echo "DRILL_DUMP_FAIL:$db"; return 1; }
    fi
  fi
  _run psql -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS ${drill};" >/dev/null 2>&1
  _run psql -U "$PG_USER" -d postgres -c "CREATE DATABASE ${drill};" >/dev/null 2>&1 || { echo "DRILL_CREATE_FAIL:$db"; return 1; }
  _run_i pg_restore -U "$PG_USER" -d "${drill}" --no-owner < "$dump" 2>&1 | tail -3

  local rc=0
  # (1) alembic head must match — the restore preserved the schema revision.
  local src_head dst_head
  src_head="$(alembic_head "$db")"; dst_head="$(alembic_head "$drill")"
  echo "   alembic head: src=${src_head:-<none>} drill=${dst_head:-<none>}"
  if [ -z "$dst_head" ] || [ "$src_head" != "$dst_head" ]; then
    echo "   DRILL_ALEMBIC_HEAD_MISMATCH:$db"; rc=1
  fi

  # (2) per-table row counts — no table with rows in src may be EMPTY in the drill.
  local empty_restores=0 total_dst=0 dst_tables=0 t
  # set +u for this block: expanding an EMPTY associative array (${!A[@]} / ${#A[@]})
  # trips 'unbound variable' under set -u on this bash; the :- defaults keep it safe.
  set +u
  declare -A SRC=() DST=()
  while read -r t n; do [ -n "$t" ] && SRC["$t"]="$n"; done < <(counts_for "$db")
  while read -r t n; do [ -n "$t" ] && DST["$t"]="$n"; done < <(counts_for "$drill")
  dst_tables=${#DST[@]}
  for t in "${!SRC[@]}"; do
    local s="${SRC[$t]:-0}" d="${DST[$t]:-0}"
    total_dst=$(( total_dst + d ))
    if [ "$s" -gt 0 ] && [ "$d" -eq 0 ]; then
      echo "   EMPTY_RESTORE:$db.$t (src=$s drill=0)"; empty_restores=$(( empty_restores + 1 ))
    fi
  done
  set -u
  echo "   restored rows total(drill)=$total_dst  tables=$dst_tables  empty_restores=$empty_restores"
  [ "$empty_restores" -eq 0 ] || rc=1

  _run psql -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS ${drill};" >/dev/null 2>&1
  return $rc
}

restore_drill() {
  echo "==== RESTORE-DRILL (proves recovery — alembic head + per-table rows) ===="
  # Cover BOTH the qecentral tables AND the nexus factory/evidence DB (verdict_events
  # hash chain etc.) — a Certificate of Correctness that references unrecoverable
  # evidence is worthless. Configurable via DRILL_DBS; defaults to both.
  local dbs="${DRILL_DBS:-qecentral nexus}" failed=0 db
  for db in $dbs; do
    drill_one "$db" || failed=1
  done
  if [ "$failed" -eq 0 ]; then
    echo "RESTORE_DRILL_PASS (recovery PROVEN for: $dbs — alembic head + no empty restore)"
    emit_textfile_metric verdict_restore_drill_last_success_timestamp_seconds "$(date -u +%s)" \
      "Unix time of the last PASSED Verdict restore-drill (recovery PROVEN: alembic head + per-table rows)."
  else
    echo "RESTORE_DRILL_FAIL (backup is NOT provably recoverable — do not go to client #2)"; exit 1
  fi
}

case "${1:-}" in
  --restore-drill) restore_drill ;;
  *) backup ;;
esac
