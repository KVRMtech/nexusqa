#!/usr/bin/env bash
# Sync the RLS-aware reaper into the VM source, rebuild qe-central, recreate (safe now:
# .env pins QEC_DB_PASSWORD), and VERIFY the 4 orphaned crawls actually get reaped.
set -uo pipefail
SRC=/home/srika/nexus-src/Nexus_power/platform/qe-central
COMPOSE=/home/srika/nexus-src/Nexus_power/docker-compose.qec.yml

cp ~/reaper.py "$SRC/app/controlplane/reaper.py" || { echo "COPY_FAIL"; exit 1; }
echo "reaper synced; per-tenant reap present: $(grep -c '_tenant_ids' "$SRC/app/controlplane/reaper.py")"

echo "=== BUILD ==="
docker compose -f "$COMPOSE" build qe-central 2>&1 | tail -3 || { echo "BUILD_FAIL"; exit 1; }
echo "=== RECREATE (env now pinned in .env) ==="
docker compose -f "$COMPOSE" up -d --force-recreate --no-deps qe-central 2>&1 | tail -3 || { echo "RECREATE_FAIL"; exit 1; }

echo "=== WAIT for DB connect ==="
for i in $(seq 1 18); do
  sleep 5
  code=$(docker exec nexus-qe-central sh -c 'curl -s -o /dev/null -w "%{http_code}" http://localhost:8093/health' 2>/dev/null || echo "-")
  conn=$(docker logs --since 25s nexus-qe-central 2>&1 | grep -c 'db_qec_connected')
  echo "  [$i] health=$code db_connected=$conn"
  [ "$code" = "200" ] && [ "$conn" -gt 0 ] && break
done

echo "=== TRIGGER a reap immediately (don't wait 300s) ==="
docker exec -w /app/service nexus-qe-central python -c "
import asyncio
from app.controlplane.reaper import reap_stale_explorations
print('reaped:', asyncio.run(reap_stale_explorations()))
"
echo "=== orphan states after reap (pending should be 0) ==="
docker exec nexus-postgres psql -U nexus -d qecentral -t -A -F'|' -c "select status,count(*) from qe_explorations group by status order by 1;"
echo "=== a reaped row's honest reason ==="
docker exec nexus-postgres psql -U nexus -d qecentral -t -A -F'|' -c "select status, left(error,55) from qe_explorations where status='stalled' order by finished_at desc limit 2;"
echo "REAPER_REDEPLOY_DONE"
