#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# qec_ci_dr_seed.sh — plant a known data row in EACH product database so the
# restore-drill proves DATA recovery, not just an empty-schema round-trip.
#
# The restore-drill (scripts/verdict_pg_backup.sh --restore-drill) fails if any
# table that has rows in the source is EMPTY after restore. With freshly-migrated
# databases only `alembic_version` carries rows, so that check is vacuous. This
# seeds one real row into a root table of each DB — the substrate `tenants`
# (nexus, the evidence SoR) and `client_apps` (qecentral) — turning the drill
# into a genuine "the seeded row survives a backup+restore" proof.
#
# Idempotent (ON CONFLICT DO NOTHING) and connects as the libpq superuser, which
# bypasses RLS so the insert needs no tenant GUC. Env-driven; no hardcoded host.
#
# Usage (CI): PGHOST=localhost PGUSER=postgres PGPASSWORD=... bash scripts/qec_ci_dr_seed.sh
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-postgres}"
export PGPASSWORD="${PGPASSWORD:-postgres}"
NEXUS_DB="${NEXUS_DB:-nexus}"
QEC_DB="${QEC_DB:-qecentral}"

# A stable probe identity so re-runs are idempotent and the row is obvious.
PROBE_TENANT="ci-dr-probe-tenant"
PROBE_APP="ci-dr-probe-app"

log() { printf '\n\033[1;36m── %s\033[0m\n' "$*"; }

log "Seeding substrate ${NEXUS_DB}.tenants (evidence SoR root)"
psql -d "${NEXUS_DB}" -v ON_ERROR_STOP=on -c "
  INSERT INTO tenants (tenant_id, name, domain)
  VALUES ('${PROBE_TENANT}', 'CI DR probe', '${PROBE_TENANT}.example.test')
  ON CONFLICT (tenant_id) DO NOTHING;"

log "Seeding ${QEC_DB}.client_apps (control-plane root)"
# client_apps NOT-NULL columns carry Python-side defaults (not server defaults),
# so a raw INSERT must supply them explicitly; JSONB defaults are '{}'.
psql -d "${QEC_DB}" -v ON_ERROR_STOP=on -c "
  INSERT INTO client_apps (
    app_id, tenant_id, name, base_url, canonical_host,
    answer_key, env_attestation, fences, repo_binding, schedule, budgets,
    latest_artifact_id, baseline_fingerprint_id, status, created_at, updated_at)
  VALUES (
    '${PROBE_APP}', '${PROBE_TENANT}', 'CI DR probe app',
    'https://${PROBE_TENANT}.example.test', '${PROBE_TENANT}.example.test',
    '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
    '', '', 'active', now(), now())
  ON CONFLICT (app_id) DO NOTHING;"

# Prove the rows are actually present (fail loudly if a seed silently did nothing).
T="$(psql -d "${NEXUS_DB}" -tAc "SELECT count(*) FROM tenants WHERE tenant_id = '${PROBE_TENANT}'" | tr -d '[:space:]')"
A="$(psql -d "${QEC_DB}"   -tAc "SELECT count(*) FROM client_apps WHERE app_id = '${PROBE_APP}'" | tr -d '[:space:]')"
if [ "${T}" != "1" ] || [ "${A}" != "1" ]; then
  echo "FATAL: seed did not land (tenants=${T}, client_apps=${A})." >&2
  exit 1
fi
echo "SEED_OK: ${NEXUS_DB}.tenants(+1) ${QEC_DB}.client_apps(+1)"
