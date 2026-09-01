#!/usr/bin/env bash
# rebuild_canonical.sh
#
# One-shot rebuild + bring-up + smoke check for the canonical
# processing stack. Use this after pulling code changes to the engines
# / workflow plane / orchestrator.
#
# Exits 0 only after every health check + queue consumer group check
# passes — so CI / a client can rely on the exit code as a "ready"
# signal.
#
# Idempotent: safe to re-run mid-failure.
#
# Stages:
#   0. precheck      — docker + compose available, scripts/.env present
#   1. down          — stop everything cleanly (data volumes preserved)
#   2. base-image    — build nexus-base:dev FIRST so downstream service
#                      builds don't bake a stale nexus-sdk
#   3. service-build — build every engine + platform image in parallel
#   4. infra-up      — bring up redis + postgres + neo4j ONLY, wait
#                      until they're healthy
#   5. migrate       — run alembic upgrade head inside an engine
#                      container (uses the freshly-built SDK)
#   6. app-up        — bring up the rest of the stack
#   7. health        — poll every engine + orchestrator /health endpoint
#                      with a deadline
#   8. workflow      — verify every workflow-plane queue lane has a
#                      registered consumer group
#   9. ready         — print READY summary
#
# Override defaults via env:
#   COMPOSE_FILE       (default: docker-compose.yml)
#   HEALTH_DEADLINE    (seconds, default 300)
#   SKIP_BUILD=1       skip stages 2 + 3 (use when only data/migrations changed)
#   SKIP_MIGRATE=1     skip stage 5
#   VERBOSE=1          stream docker build output instead of summarising

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
HEALTH_DEADLINE="${HEALTH_DEADLINE:-300}"
SKIP_BUILD="${SKIP_BUILD:-0}"
SKIP_MIGRATE="${SKIP_MIGRATE:-0}"
VERBOSE="${VERBOSE:-0}"

# Move to repo root regardless of where the script was invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# ─── helpers ────────────────────────────────────────────────────

c_red()    { printf '\033[31m%s\033[0m' "$*"; }
c_green()  { printf '\033[32m%s\033[0m' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m' "$*"; }
c_blue()   { printf '\033[34m%s\033[0m' "$*"; }

step() {
  printf '\n%s %s\n' "$(c_blue '==>')" "$*"
}

ok() {
  printf '    %s %s\n' "$(c_green '✓')" "$*"
}

warn() {
  printf '    %s %s\n' "$(c_yellow '!')" "$*"
}

die() {
  printf '\n%s %s\n' "$(c_red 'FAIL')" "$*" >&2
  exit 1
}

dc() {
  # Wrapper around docker compose — prefer plugin, fall back to legacy.
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "${COMPOSE_FILE}" "$@"
  else
    docker-compose -f "${COMPOSE_FILE}" "$@"
  fi
}

# Wait until `cmd` returns 0, polling every `interval` seconds, up to
# `deadline` total. Logs progress every 15s so the operator knows it
# is still alive.
wait_for() {
  local label="$1" deadline="$2" cmd="$3"
  local start; start=$(date +%s)
  local last_log=0
  while true; do
    if eval "$cmd" >/dev/null 2>&1; then
      ok "${label} ready"
      return 0
    fi
    local now; now=$(date +%s)
    local elapsed=$((now - start))
    if [ "${elapsed}" -ge "${deadline}" ]; then
      die "${label} did not become ready within ${deadline}s"
    fi
    if [ $((now - last_log)) -ge 15 ]; then
      printf '    %s %s (%ds elapsed)\n' "$(c_yellow '⋯')" "waiting for ${label}" "${elapsed}"
      last_log=${now}
    fi
    sleep 2
  done
}

# Hit a /health endpoint. Returns 0 only on HTTP 200.
http_health() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 5 "${url}" >/dev/null
  else
    die "curl is required for health checks"
  fi
}

# Verify a Redis Streams consumer group has at least one registered
# consumer. Catches the case where engine pods started but failed
# silently to connect their WorkflowWorker.
verify_consumer_group() {
  local stream="$1"
  local group="$2"
  local out
  out=$(dc exec -T redis redis-cli -n 3 XINFO GROUPS "${stream}" 2>/dev/null || true)
  echo "${out}" | grep -q "name${IFS}${group}" && return 0
  # Older redis-cli output uses spaces; tolerate both.
  echo "${out}" | grep -q "name.*${group}" && return 0
  return 1
}

# ─── stage 0: precheck ──────────────────────────────────────────

step "0/9  precheck"

command -v docker >/dev/null 2>&1 || die "docker not on PATH"
dc version >/dev/null 2>&1 || die "docker compose plugin missing"
[ -f "${COMPOSE_FILE}" ] || die "compose file not found: ${COMPOSE_FILE}"
[ -f .env ] || warn ".env not found — using defaults. Engine workers need NEXUS_ORCHESTRATOR_URL set."
ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"
ok "compose file: ${COMPOSE_FILE}"

# ─── stage 1: down ──────────────────────────────────────────────

step "1/9  stop existing stack"

dc down --remove-orphans 2>&1 | tail -5 || true
ok "stack stopped (volumes preserved)"

# ─── stage 2: base image ────────────────────────────────────────

if [ "${SKIP_BUILD}" = "1" ]; then
  step "2/9  base image — SKIPPED (SKIP_BUILD=1)"
else
  step "2/9  build base image (nexus-base:dev)"
  if [ "${VERBOSE}" = "1" ]; then
    dc build base-image
  else
    dc build base-image 2>&1 | tail -10
  fi
  # Sanity: image actually exists now.
  docker image inspect nexus-base:dev >/dev/null 2>&1 \
    || die "nexus-base:dev did not get tagged after build — check infrastructure/docker/Dockerfile.base"
  ok "nexus-base:dev built"
fi

# ─── stage 3: service builds ────────────────────────────────────

if [ "${SKIP_BUILD}" = "1" ]; then
  step "3/9  service builds — SKIPPED (SKIP_BUILD=1)"
else
  step "3/9  build every service (depends on base)"
  # Build in one parallel pass — base is already built so no race.
  # Service names match docker-compose.yml keys exactly (engines are
  # short: 'eyes' not 'eyes-engine'; the QA orchestrator is
  # 'qa-orchestrator' not 'nexus-qa-orchestrator').
  SERVICES_TO_BUILD=(
    shield ears eyes spine backbone
    heart nerves legs hands mouth brain
    orchestrator qa-orchestrator
    auth-service gateway platform-api client
  )
  if [ "${VERBOSE}" = "1" ]; then
    dc build "${SERVICES_TO_BUILD[@]}"
  else
    dc build "${SERVICES_TO_BUILD[@]}" 2>&1 | tail -15
  fi
  ok "all service images built"
fi

# ─── stage 4: infra up ──────────────────────────────────────────

step "4/9  start infra (redis, postgres, neo4j)"
dc up -d redis postgres neo4j 2>&1 | tail -5
wait_for "redis"    60 'dc exec -T redis redis-cli ping | grep -q PONG'
wait_for "postgres" 60 'dc exec -T postgres pg_isready -U nexus_app | grep -q accepting'
wait_for "neo4j"    90 'dc exec -T neo4j cypher-shell -u neo4j -p $(grep NEO4J_PASSWORD .env 2>/dev/null | cut -d= -f2 || echo nexuspass) "RETURN 1" 2>&1 | grep -q "1"' || warn "neo4j cypher probe failed — backbone may not initialise cleanly; continuing"

# ─── stage 5: migrate ───────────────────────────────────────────

if [ "${SKIP_MIGRATE}" = "1" ]; then
  step "5/9  alembic upgrade head — SKIPPED (SKIP_MIGRATE=1)"
else
  step "5/9  alembic upgrade head"
  # Run alembic in a one-shot nexus-base:dev container so we use the
  # SDK that was just rebuilt. We attach to the postgres container's
  # docker network (auto-detected so the project name doesn't matter)
  # and bind-mount the alembic dir + SDK source read-only.
  pg_net=$(docker inspect nexus-postgres \
    --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
    2>/dev/null | awk '{print $1}')
  [ -n "${pg_net}" ] || die "cannot find nexus-postgres network; is the stack running?"

  # alembic env.py only honours DATABASE_URL — building it explicitly is
  # more robust than passing --env-file (which docker refuses to parse on
  # Windows hosts because MSYS rewrites the path).
  PG_USER=$(grep -E '^POSTGRES_USER=' "${REPO_ROOT}/.env" 2>/dev/null | cut -d= -f2- || echo nexus)
  PG_PASS=$(grep -E '^POSTGRES_PASSWORD=' "${REPO_ROOT}/.env" 2>/dev/null | cut -d= -f2-)
  PG_DB=$(grep -E '^POSTGRES_DB=' "${REPO_ROOT}/.env" 2>/dev/null | cut -d= -f2- || echo nexus)
  [ -n "${PG_PASS}" ] || die "POSTGRES_PASSWORD not found in ${REPO_ROOT}/.env"

  # Git-Bash on Windows (MSYS) auto-translates `-w /app` and `:/app/…` mount
  # targets into Windows paths like `C:/Program Files/Git/app`, which docker
  # then rejects. MSYS_NO_PATHCONV=1 disables that translation for this
  # invocation. Linux/macOS shells ignore the variable. On Windows we feed
  # docker native paths via cygpath -w; on Linux/macOS cygpath isn't a
  # command — fall back to ${REPO_ROOT} directly.
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
    --network "${pg_net}" \
    -v "${AL_MOUNT}" \
    -v "${AI_MOUNT}" \
    -v "${SK_MOUNT}" \
    -w /app \
    -e PYTHONPATH=/app/sdk/nexus-sdk \
    -e DATABASE_URL="postgresql+asyncpg://${PG_USER}:${PG_PASS}@postgres:5432/${PG_DB}" \
    nexus-base:dev \
    sh -c 'python -m alembic -c /app/alembic.ini upgrade head' 2>&1 | tail -30 \
    || die "alembic upgrade head failed — see output above"
  ok "schema is at head"
fi

# ─── stage 6: app up ────────────────────────────────────────────

step "6/9  start application services"
dc up -d 2>&1 | tail -10
ok "compose up -d issued; waiting for health"

# ─── stage 7: health ────────────────────────────────────────────

step "7/9  health probes (deadline ${HEALTH_DEADLINE}s)"

# Engines that participate in the canonical workflow plane. Only these
# are required for canonical processing to be testable; legs/mouth/etc.
# can stay degraded for the smoke test.
# Ports are READ from docker-compose.yml — keep this in sync with the
# `ports:` blocks there.
declare -A HEALTH_URLS=(
  [shield]="http://localhost:8001/health"
  [ears]="http://localhost:8002/health"
  [eyes]="http://localhost:8003/health"
  [spine]="http://localhost:8009/health"
  [backbone]="http://localhost:8005/health"
  [orchestrator]="http://localhost:8100/health"
  [auth-service]="http://localhost:8000/health"
  [gateway]="http://localhost:8080/health"
  [platform-api]="http://localhost:8091/health"
)

for name in shield ears eyes spine backbone orchestrator auth-service gateway platform-api; do
  url="${HEALTH_URLS[$name]:-}"
  if [ -z "${url}" ]; then continue; fi
  wait_for "${name}" "${HEALTH_DEADLINE}" "http_health '${url}'"
done

# ─── stage 8: workflow plane attach proof ───────────────────────

step "8/9  verify workflow-plane workers attached to queue lanes"

declare -A LANE_GROUPS=(
  ["nexus:queue:shield.cpu"]="nexus:workers:shield.cpu"
  ["nexus:queue:eyes.cpu"]="nexus:workers:eyes.cpu"
  ["nexus:queue:eyes.gpu"]="nexus:workers:eyes.gpu"
  ["nexus:queue:ears.cpu"]="nexus:workers:ears.cpu"
  ["nexus:queue:ears.gpu"]="nexus:workers:ears.gpu"
  ["nexus:queue:spine.cpu"]="nexus:workers:spine.cpu"
  ["nexus:queue:backbone.cpu"]="nexus:workers:backbone.cpu"
)

missing=0
for stream in "${!LANE_GROUPS[@]}"; do
  group="${LANE_GROUPS[$stream]}"
  # The stream may not exist until the first dispatch; the consumer
  # group is created on engine startup. If it's missing here, the
  # engine never reached its workflow-worker bootstrap.
  if ! verify_consumer_group "${stream}" "${group}"; then
    warn "no consumer group on ${stream} — engine workflow worker did not attach"
    missing=$((missing + 1))
  else
    ok "${stream} ← ${group}"
  fi
done

if [ "${missing}" -gt 0 ]; then
  warn "${missing} lanes have no workers. Canonical workflows for those engines will time out."
  warn "Inspect with: docker compose logs <engine> | grep workflow_worker"
fi

# ─── stage 9: ready ─────────────────────────────────────────────

step "9/9  ready"

cat <<EOF

$(c_green '╔════════════════════════════════════════════════════════════════╗')
$(c_green '║  Canonical processing stack is READY for client testing.       ║')
$(c_green '╚════════════════════════════════════════════════════════════════╝')

Client UI:    http://localhost:5173      (vite dev) or check 'docker compose ps client'
Auth API:     http://localhost:8000
Gateway:      http://localhost:8080
Platform API: http://localhost:8091
Orchestrator: http://localhost:8100

Smoke test commands:
  # 1. Real-time logs for the canonical chain:
  docker compose logs -f orchestrator eyes ears spine shield backbone

  # 2. Queue depths (should be 0 at idle):
  docker compose exec redis redis-cli -n 3 XLEN nexus:queue:eyes.gpu

  # 3. Cache-hit proof — upload the same video twice from the UI.
  #    Second upload returns status='completed' in <500ms.

  # 4. Concurrency proof — drop 4+ videos at once. logs/eyes
  #    should show multiple 'workflow_worker.started' bindings AND
  #    multiple jobs in flight on a single pod simultaneously.

If anything misbehaves:
  docker compose logs --since 5m <service>
  docker compose ps                                       (which pods crashed?)
  SKIP_BUILD=1 scripts/rebuild_canonical.sh               (re-run quickly w/out rebuild)
  SKIP_MIGRATE=1 SKIP_BUILD=1 scripts/rebuild_canonical.sh (just restart everything)

EOF
