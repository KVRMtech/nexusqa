#!/usr/bin/env bash
# Clean recovery pass: sudo-overlay the full local app tree (fixes the partial overlay
# left by root-owned files), normalize ownership to srika so future deploys don't hit
# permission-denied, then rebuild + recreate + health-wait.
set -uo pipefail
SRC=/home/srika/nexus-src/Nexus_power/platform/qe-central
COMPOSE=/home/srika/nexus-src/Nexus_power/docker-compose.qec.yml

# Full overlay with sudo (root-owned files are now writable).
sudo cp -rf ~/qec_app_full/app/. "$SRC/app/" || { echo "OVERLAY_FAIL"; exit 1; }
sudo chown -R srika:srika "$SRC/app" || echo "CHOWN_WARN"
echo "OVERLAY_DONE  ClientAppEnvironmentRow=$(grep -c 'class ClientAppEnvironmentRow' "$SRC/app/db/models.py")"
echo "models.py lines: $(wc -l < "$SRC/app/db/models.py")"

echo "=== BUILD ==="
docker compose -f "$COMPOSE" build qe-central 2>&1 | tail -4 || { echo "BUILD_FAIL"; exit 1; }

echo "=== RECREATE ==="
docker compose -f "$COMPOSE" up -d --force-recreate --no-deps qe-central 2>&1 | tail -3 || { echo "RECREATE_FAIL"; exit 1; }

echo "=== HEALTH WAIT ==="
ok=""
for i in $(seq 1 18); do
  sleep 5
  st=$(docker ps -a --filter name=nexus-qe-central --format '{{.Status}}')
  code=$(docker exec nexus-qe-central sh -c 'curl -s -o /dev/null -w "%{http_code}" http://localhost:8093/health' 2>/dev/null || echo "-")
  echo "  [$i] status=$st health=$code"
  [ "$code" = "200" ] && { ok=1; break; }
done

echo "=== RESULT ==="
if [ -n "$ok" ]; then
  echo "RECOVERED"
  docker exec nexus-qe-central sh -c 'ls app/services/crawl_diagnosis.py app/controlplane/reaper.py' 2>&1
  echo "reaper env: $(docker exec nexus-qe-central sh -c 'env | grep QEC_REAPER_TICK')"
  docker logs --tail 50 nexus-qe-central 2>&1 | grep -iE 'reaper|qe_central.started' | tail -4
else
  echo "STILL_DOWN — last logs:"
  docker logs --tail 18 nexus-qe-central 2>&1 | tail -18
fi
