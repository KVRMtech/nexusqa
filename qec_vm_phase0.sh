#!/usr/bin/env bash
# QE-Central Phase-0 VM verification — ISOLATED from the running VKPower stack.
# Creates the qecentral DB + roles, runs qec_001 migration, builds+starts the
# qe-central container, runs the REFUSE matrix R1-R8. Never rebuilds platform-api.
# Never echoes secrets. Idempotent-ish (safe to re-run).
set -uo pipefail
REPO=/home/srika/nexus/Nexus_power
cd "$REPO" || { echo "FATAL: repo missing"; exit 1; }
echo "==== QEC PHASE-0 VM VERIFY @ $(date -u +%FT%TZ) ===="

# --- 0. unpack the synced tree (qe-central NEW files + compose + sql + patched/test files) ---
echo "---- STEP0 unpack ----"
tar -xzf /home/srika/qec-phase0-sync.tar.gz -C "$REPO/.." && echo "UNPACK_OK" || { echo "UNPACK_FAIL"; exit 1; }
ls -d "$REPO/platform/qe-central" && echo "QEC_TREE_PRESENT"

# --- 1. create qecentral DB + least-privilege roles (dev-tier passwords; internal-only pg) ---
echo "---- STEP1 db bootstrap ----"
docker exec -i nexus-postgres psql -U nexus -d postgres -v ON_ERROR_STOP=0 \
  -v qec_password=qec-dev -v qec_substrate_password=qec-substrate-dev \
  < "$REPO/scripts/qec_db_bootstrap.sql" 2>&1 | tail -25
docker exec nexus-postgres psql -U nexus -lqt | cut -d'|' -f1 | grep -qw qecentral \
  && echo "QECENTRAL_DB_OK" || echo "QECENTRAL_DB_FAIL"

# --- 2. build the qe-central image ---
echo "---- STEP2 build image ----"
docker compose --env-file "$REPO/.env" -f "$REPO/docker-compose.qec.yml" build qe-central 2>&1 | tail -15 \
  && echo "BUILD_OK" || { echo "BUILD_FAIL"; }

# --- 3. run the alembic migration (creates 21 qecentral tables + RLS) ---
echo "---- STEP3 migrate ----"
docker compose --env-file "$REPO/.env" -f "$REPO/docker-compose.qec.yml" run --rm \
  -e QEC_DB_PASSWORD=qec-dev -e QEC_SUBSTRATE_DB_PASSWORD=qec-substrate-dev \
  qe-central alembic -c alembic_qec/alembic.ini upgrade head 2>&1 | tail -20
TABLES=$(docker exec nexus-postgres psql -U nexus -d qecentral -t -A -c \
  "select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE';" 2>/dev/null)
echo "QECENTRAL_TABLE_COUNT=$TABLES"
RLS=$(docker exec nexus-postgres psql -U nexus -d qecentral -t -A -c \
  "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relkind='r' and c.relforcerowsecurity;" 2>/dev/null)
echo "QECENTRAL_FORCE_RLS_TABLES=$RLS"

# --- 4. start the qe-central container (harness enabled for the matrix) ---
echo "---- STEP4 up ----"
# Name qe-central explicitly so the current compose's explorer/squid services
# are NOT built/started — the REFUSE matrix only needs qe-central + the factory.
QE_HARNESS_ENABLED=true QEC_DB_PASSWORD=qec-dev QEC_SUBSTRATE_DB_PASSWORD=qec-substrate-dev \
  docker compose --env-file "$REPO/.env" -f "$REPO/docker-compose.qec.yml" up -d qe-central 2>&1 | tail -8
sleep 6
docker ps --filter name=nexus-qe-central --format '{{.Names}} {{.Status}}'
echo "---- health ----"
docker exec nexus-qe-central sh -lc 'curl -s -m 10 http://localhost:8093/health || echo HEALTH_CURL_FAIL' 2>&1 | head -5

# --- 4b. WAIT for the VKPower factory (platform-api) to be reachable ---
# On a cold VM boot the heavy platform-api is not ready for ~30-60s (its own
# healthcheck has start_period:30s). The harness drives the factory chain, so
# a ConnectError here is a not-ready race, not a code fault. Poll from INSIDE
# the qe-central container (same network path the harness uses) up to ~3 min.
echo "---- STEP4b wait for platform-api factory ----"
PAPI_OK=no
for i in $(seq 1 36); do
  if docker exec nexus-qe-central sh -lc 'curl -sf -m 5 http://platform-api:8091/health >/dev/null 2>&1'; then
    echo "PLATFORM_API_READY after ~$((i*5))s"; PAPI_OK=yes; break
  fi
  sleep 5
done
[ "$PAPI_OK" = yes ] || echo "PLATFORM_API_NOT_READY_after_180s (harness may CHAIN_ERROR on generate)"

# --- 4c. clean the harness tenant's prior rows (idempotent re-runs) ---
# The golden fixture's ground_truth_events use deterministic ids; a re-run
# against leftover rows hits a duplicate-key. Wipe the dedicated harness tenant
# first (superuser bypasses RLS) so every run starts clean. FK-safe order.
echo "---- STEP4c clean harness tenant ----"
docker exec nexus-postgres psql -U nexus -d nexus -v ON_ERROR_STOP=0 -q -c "
DELETE FROM page_actions        WHERE tenant_id='qec-harness-tenant';
DELETE FROM page_visits         WHERE tenant_id='qec-harness-tenant';
DELETE FROM ground_truth_events WHERE tenant_id='qec-harness-tenant';
DELETE FROM visual_frames       WHERE tenant_id='qec-harness-tenant';
DELETE FROM surface_prefs       WHERE tenant_id='qec-harness-tenant';
DELETE FROM canonical_artifacts WHERE tenant_id='qec-harness-tenant';
DELETE FROM sessions            WHERE tenant_id='qec-harness-tenant';
" 2>&1 | tail -3
docker exec nexus-postgres psql -U nexus -d qecentral -q -c \
  "DELETE FROM qe_harness_runs; DELETE FROM qe_explorations;" 2>&1 | tail -1
echo "HARNESS_TENANT_CLEANED"

# --- 4d. DIAGNOSTIC: can the harness's OWN http client reach the factory? ---
# (curl above proved the network; this proves httpx — the harness's real client
#  — from the same container, to pinpoint any generate ConnectError.)
echo "---- STEP4d factory reachability (httpx, harness's client) ----"
docker exec nexus-qe-central python - <<'PYEOF' 2>&1 | tail -6
import os, httpx
u = os.environ.get("PLATFORM_API_URL", "?")
print("PLATFORM_API_URL =", u)
for path in ("/health", "/api/v1/test-factory/health"):
    try:
        r = httpx.get(u.rstrip("/") + path, timeout=8.0)
        print(f"httpx GET {path} -> {r.status_code}")
    except Exception as e:
        print(f"httpx GET {path} -> ERR {type(e).__name__}: {str(e)[:160]}")
PYEOF

# --- 5. run the REFUSE matrix R1-R8 in-container ---
echo "---- STEP5 REFUSE matrix ----"
docker exec -e QE_HARNESS_ENABLED=true nexus-qe-central python -m app.harness.runner 2>&1 | tail -60
echo "REFUSE_EXIT=$?"

# --- 6. summary from persisted harness rows ---
echo "---- STEP6 harness verdicts (from qecentral) ----"
docker exec nexus-postgres psql -U nexus -d qecentral -t -A -F'|' -c \
  "select rule_id, verdict from qe_harness_runs order by rule_id;" 2>/dev/null || echo "no verdict rows"
echo "==== QEC PHASE-0 VM VERIFY DONE @ $(date -u +%FT%TZ) ===="
