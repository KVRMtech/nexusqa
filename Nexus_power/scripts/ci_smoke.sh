#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Nexus QA — CI smoke test
# ─────────────────────────────────────────────────────────────────────
# Brings up the full stack from zero, runs alembic, asserts every
# canonical workflow lane has a consumer attached, and verifies backbone
# is in real-Milvus mode (NOT degraded). Used by the integration CI
# workflow and runnable locally for pre-PR verification.
#
# Side effects: wipes the local dev data volumes (preserves ollama-data
# + model-cache to skip the 15-min LLM re-download). Run only when
# you're OK with losing dev workflow state.
#
# Usage:
#   bash scripts/ci_smoke.sh                      # full bring-up
#   SKIP_VOLUME_WIPE=1 bash scripts/ci_smoke.sh   # keep existing data
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
COMPOSE_FILE="${REPO_ROOT}/docker-compose.yml"

c_red()   { printf '\033[31m%s\033[0m' "$*"; }
c_green() { printf '\033[32m%s\033[0m' "$*"; }
c_blue()  { printf '\033[34m%s\033[0m' "$*"; }
step()    { echo; echo "$(c_blue '==>') $*"; }
ok()      { echo "    $(c_green '✓') $*"; }
fail()    { echo "    $(c_red 'FAIL') $*"; exit 1; }

# ─── precheck ────────────────────────────────────────────────────────
step "0/6  precheck"
command -v docker >/dev/null || fail "docker not on PATH"
[ -f "${COMPOSE_FILE}" ] || fail "docker-compose.yml not found at ${COMPOSE_FILE}"
ok "docker available"
ok "compose file: ${COMPOSE_FILE}"

dc() { docker compose -f "${COMPOSE_FILE}" "$@"; }

# ─── volume wipe ─────────────────────────────────────────────────────
step "1/6  reset data state"
if [ "${SKIP_VOLUME_WIPE:-0}" = "1" ]; then
  ok "skipping volume wipe (SKIP_VOLUME_WIPE=1)"
else
  dc --profile full down >/dev/null 2>&1 || true
  for v in nexus_power_audio-storage nexus_power_document-storage \
           nexus_power_etcd-data nexus_power_frame-storage \
           nexus_power_milvus-data nexus_power_minio-data \
           nexus_power_neo4j-data nexus_power_postgres-data \
           nexus_power_redis-data nexus_power_upload-storage \
           nexus_power_evidence-storage nexus_power_report-storage; do
    docker volume rm "$v" >/dev/null 2>&1 || true
  done
  ok "data volumes wiped (ollama-data + model-cache preserved)"
fi

# ─── infra up ────────────────────────────────────────────────────────
step "2/6  infra services"
dc --profile full up -d redis postgres neo4j etcd minio milvus >/dev/null
deadline=$(( $(date +%s) + 240 ))
while true; do
  all_healthy=1
  for c in nexus-postgres nexus-redis nexus-neo4j nexus-milvus; do
    s=$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo "missing")
    [ "$s" = "healthy" ] || { all_healthy=0; break; }
  done
  [ "$all_healthy" = "1" ] && break
  [ "$(date +%s)" -gt "$deadline" ] && fail "infra not healthy within 240s"
  sleep 5
done
ok "infra healthy"

# ─── alembic ─────────────────────────────────────────────────────────
step "3/6  alembic upgrade head"
PG_PASS=$(grep '^POSTGRES_PASSWORD=' "${REPO_ROOT}/.env" 2>/dev/null | cut -d= -f2- || echo "nexus-dev")
PG_USER=$(grep '^POSTGRES_USER=' "${REPO_ROOT}/.env" 2>/dev/null | cut -d= -f2- || echo "nexus")
PG_DB=$(grep '^POSTGRES_DB=' "${REPO_ROOT}/.env" 2>/dev/null | cut -d= -f2- || echo "nexus")

if command -v cygpath >/dev/null 2>&1; then
  REPO_NATIVE=$(cygpath -w "${REPO_ROOT}")
  AL_MOUNT="${REPO_NATIVE}\\alembic:/app/alembic:ro"
  AI_MOUNT="${REPO_NATIVE}\\alembic.ini:/app/alembic.ini:ro"
  SK_MOUNT="${REPO_NATIVE}\\sdk:/app/sdk:ro"
else
  AL_MOUNT="${REPO_ROOT}/alembic:/app/alembic:ro"
  AI_MOUNT="${REPO_ROOT}/alembic.ini:/app/alembic.ini:ro"
  SK_MOUNT="${REPO_ROOT}/sdk:/app/sdk:ro"
fi

MSYS_NO_PATHCONV=1 docker run --rm \
  --network nexus_power_nexus \
  -v "${AL_MOUNT}" -v "${AI_MOUNT}" -v "${SK_MOUNT}" \
  -w /app \
  -e PYTHONPATH=/app/sdk/nexus-sdk \
  -e DATABASE_URL="postgresql+asyncpg://${PG_USER}:${PG_PASS}@postgres:5432/${PG_DB}" \
  nexus-base:dev \
  sh -c 'python -m alembic -c /app/alembic.ini upgrade head' \
  2>&1 | tail -5 \
  || fail "alembic upgrade head failed — see output above"
ok "schema at head"

# ─── app up ──────────────────────────────────────────────────────────
step "4/6  application services"
dc --profile full up -d >/dev/null
ok "compose up issued"

# ─── health ──────────────────────────────────────────────────────────
step "5/6  per-engine health"
deadline=$(( $(date +%s) + 300 ))
declare -A urls=(
  [shield]="http://localhost:8001/health"
  [ears]="http://localhost:8002/health"
  [eyes]="http://localhost:8003/health"
  [spine]="http://localhost:8009/health"
  [backbone]="http://localhost:8005/health"
  [orchestrator]="http://localhost:8100/health"
  [auth]="http://localhost:8000/health"
  [gateway]="http://localhost:8080/health"
  [platform-api]="http://localhost:8091/health"
)
for name in "${!urls[@]}"; do
  url="${urls[$name]}"
  while true; do
    code=$(curl -sm 3 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)
    [ "$code" = "200" ] && break
    [ "$(date +%s)" -gt "$deadline" ] && fail "$name not healthy (last code: $code)"
    sleep 4
  done
  ok "$name: 200"
done

# ─── attach proof ────────────────────────────────────────────────────
step "6/6  workflow lanes + backbone vector store"
missing=0
for lane in shield.cpu eyes.cpu eyes.gpu ears.cpu ears.gpu \
            spine.cpu spine.gpu backbone.cpu; do
  out=$(docker exec nexus-redis redis-cli -n 3 XINFO GROUPS \
        "nexus:queue:${lane}" 2>&1)
  if echo "$out" | grep -q "^name"; then
    ok "nexus:queue:${lane} has consumer group"
  else
    missing=$((missing + 1))
    echo "    $(c_red 'FAIL') nexus:queue:${lane} — no consumer group"
  fi
done
[ "$missing" -gt 0 ] && fail "${missing} lanes missing consumer groups"

vec=$(curl -sm 5 http://localhost:8005/health \
      | python -c "import sys,json; print(json.load(sys.stdin).get('modes',{}).get('vector_store',''))" \
      2>/dev/null)
case "$vec" in
  milvus*) ok "backbone vector_store: ${vec}" ;;
  *) fail "backbone in degraded mode: ${vec!r}" ;;
esac

echo
echo "$(c_green '═══') CI smoke complete — stack ready for testing $(c_green '═══')"
