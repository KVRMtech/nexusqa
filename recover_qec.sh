#!/usr/bin/env bash
# Recover the crash-looping qe-central: overlay the FULL local app/ tree onto the stale
# VM source (fixes the ClientAppEnvironmentRow ImportError + any other divergence),
# rebuild, recreate, and verify with a proper health-wait.
set -uo pipefail
SRC=/home/srika/nexus-src/Nexus_power/platform/qe-central
COMPOSE=/home/srika/nexus-src/Nexus_power/docker-compose.qec.yml

cd /home/srika/nexus-src/Nexus_power || { echo "NO_REPO"; exit 1; }

# 1) Back up the stale source, then overlay the known-good local tree.
ts=$(date -u +%Y%m%dT%H%M%SZ)
cp -r "$SRC/app" "$SRC/app.bak.$ts" && echo "BACKED_UP app -> app.bak.$ts"
rm -rf ~/qec_app_full && mkdir -p ~/qec_app_full
tar -xzf ~/qec_app_full.tgz -C ~/qec_app_full || { echo "EXTRACT_FAIL"; exit 1; }
cp -r ~/qec_app_full/app/. "$SRC/app/" || { echo "OVERLAY_FAIL"; exit 1; }
echo "OVERLAY_DONE  (ClientAppEnvironmentRow present: $(grep -c 'class ClientAppEnvironmentRow' "$SRC/app/db/models.py"))"

# 2) Rebuild ONLY qe-central (bakes the corrected source in).
echo "=== BUILD ==="
docker compose -f "$COMPOSE" build qe-central 2>&1 | tail -4 || { echo "BUILD_FAIL"; exit 1; }

# 3) Recreate ONLY qe-central.
echo "=== RECREATE ==="
docker compose -f "$COMPOSE" up -d --force-recreate --no-deps qe-central 2>&1 | tail -3 || { echo "RECREATE_FAIL"; exit 1; }

# 4) Health-wait with retries (don't declare success early like last time).
echo "=== HEALTH WAIT ==="
ok=""
for i in $(seq 1 18); do
  sleep 5
  st=$(docker ps -a --filter name=nexus-qe-central --format '{{.Status}}')
  code=$(docker exec nexus-qe-central sh -c 'curl -s -o /dev/null -w "%{http_code}" http://localhost:8093/health' 2>/dev/null || echo "-")
  echo "  [$i] status=$st health=$code"
  if [ "$code" = "200" ]; then ok=1; break; fi
done

echo "=== RESULT ==="
if [ -n "$ok" ]; then
  echo "RECOVERED"
  echo "phase code baked in: $(docker exec nexus-qe-central sh -c 'ls app/services/crawl_diagnosis.py app/controlplane/reaper.py 2>&1' )"
  echo "reaper env: $(docker exec nexus-qe-central sh -c 'env | grep QEC_REAPER_TICK')"
  docker logs --tail 40 nexus-qe-central 2>&1 | grep -iE 'reaper|qe_central.started' | tail -3
else
  echo "STILL_DOWN — last logs:"
  docker logs --tail 15 nexus-qe-central 2>&1 | tail -15
fi
