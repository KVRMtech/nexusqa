#!/usr/bin/env bash
# Production posture for qe-central:
#   1. add QEC_REAPER_TICK_SECONDS to the compose env block (idempotent)
#   2. set it in .env
#   3. REBUILD only qe-central so the phase code is BAKED IN (docker-cp'd code is
#      lost on any recreate; and never `up --build` the stack — base-image race)
#   4. force-recreate only qe-central
set -uo pipefail
cd /home/srika/nexus-src/Nexus_power || { echo "NO_REPO"; exit 1; }

COMPOSE=docker-compose.qec.yml
cp -n "$COMPOSE" "${COMPOSE}.bak.$(date -u +%Y%m%dT%H%M%SZ)" 2>/dev/null || true

# 1) idempotent insert of the reaper env var right after QEC_ADMISSION_BACKEND
if grep -q "QEC_REAPER_TICK_SECONDS" "$COMPOSE"; then
  echo "COMPOSE_ALREADY_HAS_REAPER_VAR"
else
  python3 - <<'PY'
import re, io
p = "docker-compose.qec.yml"
s = open(p, encoding="utf-8").read()
anchor = "      QEC_ADMISSION_BACKEND: ${QEC_ADMISSION_BACKEND:-memory}\n"
add = ("      # Phase-0 stale-crawl reaper: terminalizes orphaned crawls (crashed worker /\n"
       "      # lost callback) into an honest 'stalled' state. <=0 or unset => INERT.\n"
       "      QEC_REAPER_TICK_SECONDS: ${QEC_REAPER_TICK_SECONDS:-0}\n")
if anchor not in s:
    raise SystemExit("ANCHOR_NOT_FOUND")
s = s.replace(anchor, anchor + add, 1)
open(p, "w", encoding="utf-8").write(s)
print("COMPOSE_PATCHED")
PY
fi

# 2) .env value (idempotent)
touch .env
if grep -q '^QEC_REAPER_TICK_SECONDS=' .env; then
  sed -i 's/^QEC_REAPER_TICK_SECONDS=.*/QEC_REAPER_TICK_SECONDS=300/' .env
else
  echo 'QEC_REAPER_TICK_SECONDS=300' >> .env
fi
echo "ENV_SET: $(grep '^QEC_REAPER_TICK_SECONDS=' .env)"

# 3) rebuild ONLY qe-central (bakes the phase code into the image)
echo "=== BUILD (qe-central only) ==="
docker compose -f "$COMPOSE" build qe-central 2>&1 | tail -6 || { echo "BUILD_FAIL"; exit 1; }

# 4) recreate ONLY qe-central
echo "=== RECREATE ==="
docker compose -f "$COMPOSE" up -d --force-recreate --no-deps qe-central 2>&1 | tail -4 || { echo "RECREATE_FAIL"; exit 1; }

sleep 14
echo "=== VERIFY ==="
docker ps --filter name=nexus-qe-central --format '{{.Names}}|{{.Status}}'
echo "phase code baked in?"
docker exec nexus-qe-central sh -c 'ls /app/service/app/services/crawl_diagnosis.py /app/service/app/controlplane/reaper.py' 2>&1
echo "reaper env in container:"
docker exec nexus-qe-central sh -c 'env | grep QEC_REAPER'
echo "health:"
docker exec nexus-qe-central sh -c 'curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8093/health'
echo "reaper boot log:"
docker logs --tail 40 nexus-qe-central 2>&1 | grep -iE 'reaper|qe_central.started' | tail -4
echo "PRODUCTION_ENABLE_DONE"
