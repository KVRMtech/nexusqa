#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# QE-Central — deterministic CI database bootstrap (Phase 5.5).
#
# Builds the TWO databases the QE-Central DB-gated tests require, from an empty
# PostgreSQL service container, in a single idempotent, re-runnable pass:
#
#   * nexus     — the VKPower substrate schema (the root alembic chain @ head).
#                 Carries page_visits / page_actions / ground_truth_events /
#                 visual_frames / canonical_artifacts / sessions / tenants with
#                 their FORCE-RLS tenant_isolation policies.  Exposed to tests as
#                 QEC_TEST_DATABASE_URL (superuser — round-trip + create_all) and
#                 QEC_TEST_SUBSTRATE_DATABASE_URL (least-priv qec_substrate role —
#                 the behavioural page_visits RLS proof, which REQUIRES a
#                 non-superuser role because superusers bypass RLS).
#
#   * qecentral — the 21 QE-Central-owned tables (the qec alembic chain @ head)
#                 with ENABLE + FORCE RLS + the tenant_isolation policies + the
#                 one-active-cycle partial unique index.  Exposed to tests as
#                 QEC_TEST_QEC_DATABASE_URL, connected as the least-priv `qec`
#                 role so the RLS isolation test observes real enforcement.
#
# Roles + grants come from the SAME production script the operator runs
# (scripts/qec_db_bootstrap.sql, design R-7) — CI proves that script too.
#
# This is FAIL-CLOSED and STRICT: any step failing aborts the build (set -e).
# It is deliberately env-driven (no hardcoded hosts/passwords) so it runs
# identically in CI and on a laptop pointed at a throwaway Postgres.
#
# Usage (from anywhere; paths are resolved against this script's location):
#   PGHOST=localhost PGPORT=5432 PGUSER=postgres PGPASSWORD=postgres \
#   QEC_ROLE_PASSWORD=... QEC_SUBSTRATE_ROLE_PASSWORD=... \
#     bash scripts/qec_ci_db_setup.sh
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Connection / naming inputs (all overridable; CI-safe dev defaults) ──────
# libpq reads PGHOST/PGPORT/PGUSER/PGPASSWORD directly, so psql needs no flags.
export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-postgres}"          # the service-container SUPERUSER
export PGPASSWORD="${PGPASSWORD:-postgres}"

NEXUS_DB="${NEXUS_DB:-nexus}"
QEC_DB="${QEC_DB:-qecentral}"
QEC_ROLE_PASSWORD="${QEC_ROLE_PASSWORD:-qec_ci_pw}"
QEC_SUBSTRATE_ROLE_PASSWORD="${QEC_SUBSTRATE_ROLE_PASSWORD:-qec_sub_ci_pw}"

# ── Repo layout (this file lives in <repo>/scripts) ─────────────────────────
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${_SCRIPT_DIR}/.." && pwd)}"
QEC_SERVICE_DIR="${REPO_ROOT}/platform/qe-central"

log() { printf '\n\033[1;36m── %s\033[0m\n' "$*"; }

# ── 1. Wait for the server to accept connections (belt-and-braces; the GH
#       service health-check already gates this, but a local run may not). ───
log "Waiting for PostgreSQL at ${PGHOST}:${PGPORT} (user ${PGUSER})"
for _i in $(seq 1 30); do
  if pg_isready -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" >/dev/null 2>&1; then
    echo "PostgreSQL is ready."
    break
  fi
  if [ "${_i}" -eq 30 ]; then
    echo "FATAL: PostgreSQL never became ready." >&2
    exit 1
  fi
  sleep 1
done

# ── 2. Create the empty `nexus` database (idempotent) ───────────────────────
log "Creating database '${NEXUS_DB}' (if absent)"
if ! psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '${NEXUS_DB}'" | grep -q 1; then
  psql -d postgres -c "CREATE DATABASE ${NEXUS_DB}"
  echo "Created ${NEXUS_DB}."
else
  echo "${NEXUS_DB} already exists — reusing."
fi

# ── 3. Apply the VKPower nexus schema to head (substrate tables + RLS) ───────
# env.py reads DATABASE_URL; run from REPO_ROOT so `script_location = alembic`
# resolves.  This is READ-ONLY against VKPower source (we never modify it).
log "Applying VKPower nexus schema (alembic upgrade head)"
(
  cd "${REPO_ROOT}"
  DATABASE_URL="postgresql+asyncpg://${PGUSER}:${PGPASSWORD}@${PGHOST}:${PGPORT}/${NEXUS_DB}" \
    python -m alembic -c "${REPO_ROOT}/alembic.ini" upgrade head
)

# ── 3b. Materialise the substrate tables the alembic chain does NOT own ──────
# A few substrate models (ground_truth_events, surface_prefs, …) are create_all-only
# — the running service binds them to the SDK ``Base`` and relies on create_all, not
# a migration (surface_prefs.py: "binds the SDK's Base so it shares … create_all").
# The bootstrap SQL below GRANTs on them, so they must exist first; without this the
# grant aborts with `relation "ground_truth_events" does not exist`. create_all is
# checkfirst=True, so it only adds the missing tables and never touches an
# alembic-owned one. NOTE: these create_all tables carry no migration-defined RLS —
# tracked separately; the RLS proof itself covers the migration-owned page_visits.
log "Materialising create_all-only substrate tables (ground_truth_events, surface_prefs, …)"
(
  cd "${REPO_ROOT}"
  DATABASE_URL="postgresql+asyncpg://${PGUSER}:${PGPASSWORD}@${PGHOST}:${PGPORT}/${NEXUS_DB}" \
  PYTHONPATH="${REPO_ROOT}/platform/api${PYTHONPATH:+:${PYTHONPATH}}" \
  python - <<'PY'
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from nexus_sdk.db import Base
import nexus_sdk.db.models                      # noqa: F401 — register SDK substrate models
import app.services.storyboard.surface_prefs    # noqa: F401 — bind surface_prefs to Base


async def main() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)   # checkfirst: only missing tables
    await engine.dispose()


asyncio.run(main())
print("substrate create_all complete")
PY
)

# ── 4. Roles + qecentral database + least-privilege grants (production SQL) ──
# scripts/qec_db_bootstrap.sql needs the nexus substrate tables to already
# exist (step 3) because it GRANTs on them; run it as the superuser.  It creates
# `qec` (owner of qecentral) and `qec_substrate` (least-priv nexus writer),
# both NOSUPERUSER — exactly the roles the RLS test must connect through.
log "Running scripts/qec_db_bootstrap.sql (roles + qecentral + grants)"
psql -d "${NEXUS_DB}" \
  -v ON_ERROR_STOP=on \
  -v qec_password="${QEC_ROLE_PASSWORD}" \
  -v qec_substrate_password="${QEC_SUBSTRATE_ROLE_PASSWORD}" \
  -f "${REPO_ROOT}/scripts/qec_db_bootstrap.sql"

# ── 5. Apply the QE-Central schema to head (21 tables + RLS + partial index) ─
# Connect AS the `qec` role (owner) so the tables are qec-owned and FORCE RLS
# governs the app role itself — the posture the RLS test asserts.  env.py reads
# QEC_DATABASE_URL and self-adds the service root to sys.path.
log "Applying QE-Central qecentral schema (alembic upgrade head, as role qec)"
(
  cd "${QEC_SERVICE_DIR}"
  QEC_DATABASE_URL="postgresql+asyncpg://qec:${QEC_ROLE_PASSWORD}@${PGHOST}:${PGPORT}/${QEC_DB}" \
    python -m alembic -c "${QEC_SERVICE_DIR}/alembic_qec/alembic.ini" upgrade head
)

# ── 6. Verify head + table count (fail loudly on a partial build) ───────────
# The EXPECTED head is derived from the migration chain itself (not hardcoded), so
# this never goes stale as migrations land. It was previously pinned to qec_002/22
# and silently aborted the whole security suite the moment qec_003 (app_environments)
# was added — head became qec_003/23 and set -e killed CI before any test ran.
EXPECTED_HEAD="$(
  cd "${QEC_SERVICE_DIR}" &&
  QEC_DATABASE_URL="postgresql+asyncpg://qec:${QEC_ROLE_PASSWORD}@${PGHOST}:${PGPORT}/${QEC_DB}" \
    python -m alembic -c "${QEC_SERVICE_DIR}/alembic_qec/alembic.ini" heads 2>/dev/null \
    | awk 'NR==1{print $1}'
)"
log "Verifying qecentral is at the qec chain head (${EXPECTED_HEAD:-<unknown>})"
HEAD="$(psql -d "${QEC_DB}" -tAc "SELECT version_num FROM alembic_version" | tr -d '[:space:]')"
if [ -z "${EXPECTED_HEAD}" ] || [ "${HEAD}" != "${EXPECTED_HEAD}" ]; then
  echo "FATAL: qecentral alembic head is '${HEAD}', expected chain head '${EXPECTED_HEAD:-<could-not-derive>}'." >&2
  exit 1
fi
# Sanity floor: the qec_001 base creates the 21 core tenant tables and later
# migrations only ADD; a count below the base means a partial/failed build.
MIN_TENANT_TABLES=21
TABLE_COUNT="$(psql -d "${QEC_DB}" -tAc \
  "SELECT count(*) FROM information_schema.tables \
   WHERE table_schema = 'public' AND table_type = 'BASE TABLE' \
     AND table_name <> 'alembic_version'" | tr -d '[:space:]')"
if [ "${TABLE_COUNT}" -lt "${MIN_TENANT_TABLES}" ]; then
  echo "FATAL: qecentral has ${TABLE_COUNT} tenant tables, expected >= ${MIN_TENANT_TABLES}." >&2
  exit 1
fi

log "QE-Central CI databases ready: ${NEXUS_DB} (substrate) + ${QEC_DB} (${TABLE_COUNT} tables @ ${HEAD})"
