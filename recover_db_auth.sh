#!/usr/bin/env bash
# Recover qe-central DB auth WITHOUT changing any DB role (repo-intel still uses the
# real password). Pull the real qec / qec_substrate passwords from the healthy
# nexus-repo-intel container's env, pin them into Nexus_power/.env, and recreate
# qe-central. The password values NEVER reach stdout — only booleans are printed.
set -uo pipefail
cd /home/srika/nexus-src/Nexus_power || { echo "NO_REPO"; exit 1; }

extract_pw() {  # $1 = env var name, $2 = role name in the URL
  local raw; raw=$(docker exec nexus-repo-intel printenv "$1" 2>/dev/null || true)
  [ -n "$raw" ] || return 1
  # If it's a URL, pull the password between 'role:' and '@'; else use raw as-is.
  if printf '%s' "$raw" | grep -q "://$2:"; then
    printf '%s' "$raw" | sed -E "s|.*://$2:([^@]*)@.*|\1|"
  else
    printf '%s' "$raw"
  fi
}

QP=$(extract_pw QEC_DB_PASSWORD qec || true)
[ -n "${QP:-}" ] || QP=$(extract_pw QEC_DATABASE_URL qec || true)
SP=$(extract_pw QEC_SUBSTRATE_DB_PASSWORD qec_substrate || true)
[ -n "${SP:-}" ] || SP=$(extract_pw NEXUS_DATABASE_URL_SUBSTRATE qec_substrate || true)

[ -n "${QP:-}" ] || { echo "FAIL: could not recover qec password"; exit 1; }
[ -n "${SP:-}" ] || { echo "FAIL: could not recover qec_substrate password"; exit 1; }
echo "recovered qec_pw=$([ -n "$QP" ] && echo yes) substrate_pw=$([ -n "$SP" ] && echo yes)"

# Rewrite .env preserving all other keys (e.g. QEC_REAPER_TICK_SECONDS), values via printf.
touch .env
grep -v -E '^(QEC_DB_PASSWORD|QEC_SUBSTRATE_DB_PASSWORD)=' .env > .env.new 2>/dev/null || true
printf 'QEC_DB_PASSWORD=%s\n' "$QP" >> .env.new
printf 'QEC_SUBSTRATE_DB_PASSWORD=%s\n' "$SP" >> .env.new
mv .env.new .env
chmod 600 .env
echo "env keys now: $(grep -oE '^[A-Z_]+=' .env | tr '\n' ' ')"

echo "=== RECREATE qe-central with corrected DB auth ==="
docker compose -f docker-compose.qec.yml up -d --force-recreate --no-deps qe-central 2>&1 | tail -3

echo "=== HEALTH + DB WAIT ==="
ok=""
for i in $(seq 1 18); do
  sleep 5
  code=$(docker exec nexus-qe-central sh -c 'curl -s -o /dev/null -w "%{http_code}" http://localhost:8093/health' 2>/dev/null || echo "-")
  dberr=$(docker logs --since 20s nexus-qe-central 2>&1 | grep -c 'db_qec_failed' || echo 0)
  echo "  [$i] health=$code db_qec_failed_in_last_20s=$dberr"
  if [ "$code" = "200" ] && [ "$dberr" = "0" ] && [ "$i" -ge 3 ]; then ok=1; break; fi
done

echo "=== RESULT ==="
if [ -n "$ok" ]; then
  echo "DB_AUTH_RECOVERED"
  docker logs --since 90s nexus-qe-central 2>&1 | grep -iE 'qec.reaper|qe_central.started|db_qec_failed' | tail -5
else
  echo "STILL_FAILING — last db logs:"; docker logs --tail 8 nexus-qe-central 2>&1 | tail -8
fi
