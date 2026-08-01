#!/usr/bin/env bash
# Apply every additive schema script, in order, idempotently.
#
# Each apply_*.sql is additive and guarded (ADD COLUMN IF NOT EXISTS / CREATE TABLE
# IF NOT EXISTS), so running this repeatedly is a no-op. It exists because nothing
# else applied them: each was run by hand, which is fine until one is missed — and a
# missed column is not a quiet degradation, it is a 500 on every write of the column
# the ORM already knows about. That is exactly how apply_card_contract.sql could have
# shipped unapplied.
#
#   bash apply_all.sh                 # inside the api container / with psql on PATH
#   CONTAINER=nexus-postgres bash apply_all.sh   # from the VM host, via docker
#
# Order is dependency order: the base table before the columns added to it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_USER="${DB_USER:-nexus}"
DB_NAME="${DB_NAME:-nexus}"
CONTAINER="${CONTAINER:-}"

FILES=(
  apply_persona_env.sql          # base: recipes, personas, credentials, environments
  apply_persona_env_p4.sql       # environment posture / production flags / data_epoch
  apply_persona_env_p5.sql       # credential verified_epoch
  apply_persona_env_r3.sql       # scoped certification
  apply_login_type_key.sql       # recipe fleet-reuse key
  apply_login_domain.sql         # recipe login domain
  apply_card_contract.sql        # card -> which login it was checked against (F2/F3)
  apply_heal_events.sql
  apply_run_reports.sql
)

run_sql() {
  local f="$1"
  if [ -n "$CONTAINER" ]; then
    docker cp "$HERE/$f" "$CONTAINER:/tmp/$f" >/dev/null
    docker exec "$CONTAINER" psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -f "/tmp/$f"
  else
    psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -f "$HERE/$f"
  fi
}

for f in "${FILES[@]}"; do
  if [ ! -f "$HERE/$f" ]; then
    echo "  SKIP    $f (not present in this tree)"
    continue
  fi
  printf '  apply   %s\n' "$f"
  run_sql "$f" >/dev/null
done

echo "ALL SCHEMA SCRIPTS APPLIED"
