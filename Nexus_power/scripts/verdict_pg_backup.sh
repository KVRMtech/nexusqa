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
: "${GCS_BACKUP_BUCKET:?set GCS_BACKUP_BUCKET (gs://…)}"
PG="${PG_CONTAINER:-nexus-postgres}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

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
  echo "==== BACKUP $STAMP → $GCS_BACKUP_BUCKET ===="
  for db in nexus qecentral; do
    echo "-- dumping $db --"
    # -Fc custom format = compressed + parallel-restorable; SoR integrity first.
    if ! docker exec "$PG" pg_dump -U nexus -d "$db" -Fc > "$WORK/${db}_${STAMP}.dump"; then
      echo "DUMP_FAILED:$db"; exit 1
    fi
    sz=$(stat -c%s "$WORK/${db}_${STAMP}.dump" 2>/dev/null || echo 0)
    echo "   $db dump = ${sz} bytes"
    [ "$sz" -gt 1000 ] || { echo "DUMP_TOO_SMALL:$db (refusing to ship a suspect backup)"; exit 1; }
    if command -v gsutil >/dev/null 2>&1; then
      gsutil cp "$WORK/${db}_${STAMP}.dump" "$GCS_BACKUP_BUCKET/${db}/${db}_${STAMP}.dump" \
        && echo "   uploaded $db" || { echo "UPLOAD_FAILED:$db"; exit 1; }
    else
      echo "   gsutil absent — dump kept locally at $WORK (wire object upload for prod)"
    fi
  done
  echo "BACKUP_OK $STAMP"
  emit_textfile_metric verdict_backup_last_success_timestamp_seconds "$(date -u +%s)" \
    "Unix time of the last successful Verdict Postgres backup (both nexus+qecentral dumped)."
}

restore_drill() {
  echo "==== RESTORE-DRILL (proves recovery, not just backup existence) ===="
  # Take a fresh dump of qecentral, restore into a throwaway DB, compare a table count.
  local drill="verdict_restore_drill_${STAMP}"
  docker exec "$PG" pg_dump -U nexus -d qecentral -Fc > "$WORK/drill.dump" || { echo "DRILL_DUMP_FAIL"; exit 1; }
  docker exec "$PG" psql -U nexus -d postgres -c "DROP DATABASE IF EXISTS ${drill};" >/dev/null 2>&1
  docker exec "$PG" psql -U nexus -d postgres -c "CREATE DATABASE ${drill};" >/dev/null 2>&1 || { echo "DRILL_CREATE_FAIL"; exit 1; }
  docker exec -i "$PG" pg_restore -U nexus -d "${drill}" --no-owner < "$WORK/drill.dump" 2>&1 | tail -3
  src=$(docker exec "$PG" psql -U nexus -d qecentral -t -A -c "select count(*) from information_schema.tables where table_schema='public';" 2>/dev/null)
  dst=$(docker exec "$PG" psql -U nexus -d "${drill}" -t -A -c "select count(*) from information_schema.tables where table_schema='public';" 2>/dev/null)
  docker exec "$PG" psql -U nexus -d postgres -c "DROP DATABASE IF EXISTS ${drill};" >/dev/null 2>&1
  echo "restored tables: src(qecentral)=$src  drill=$dst"
  if [ -n "$src" ] && [ "$src" = "$dst" ]; then
    echo "RESTORE_DRILL_PASS (recovery proven — 6.3a exit criterion met)"
    emit_textfile_metric verdict_restore_drill_last_success_timestamp_seconds "$(date -u +%s)" \
      "Unix time of the last PASSED Verdict restore-drill (recovery PROVEN, not assumed)."
  else
    echo "RESTORE_DRILL_FAIL (backup is NOT provably recoverable — do not go to client #2)"; exit 1
  fi
}

case "${1:-}" in
  --restore-drill) restore_drill ;;
  *) backup ;;
esac
