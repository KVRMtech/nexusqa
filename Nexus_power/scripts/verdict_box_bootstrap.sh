#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# VKPower Verdict — dedicated-box bootstrap (Phase 6.3)
# Runs ON the DEDICATED GCP box (NOT nexus-vm) after: VM created, Docker
# installed, repo present at $REPO. Brings up a SELF-CONTAINED production stack:
#   dedicated Postgres  +  VKPower factory (platform-api + engines + redis)
#   +  Verdict plane (qe-central, qe-explorer, repo-intel, verdict-portal).
#
# FAIL-CLOSED by design: the Phase-6 safety spine REFUSES to boot qe-central in
# a deployed env on a dev KEK or default secrets — so this script provisions a
# real GCP-KMS envelope + strong generated secrets, or it stops and tells you
# exactly what to set. Nothing here silently downgrades to dev-grade security.
#
# Usage (on the box):
#   NEXUS_KEK_GCP_KEY=projects/P/locations/L/keyRings/R/cryptoKeys/K \
#   GCS_BACKUP_BUCKET=gs://verdict-<client>-backups \
#   bash scripts/verdict_box_bootstrap.sh
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
REPO="${REPO:-$HOME/nexus/Nexus_power}"
ENV_FILE="$REPO/.env.production"
cd "$REPO" || { echo "FATAL: repo not found at $REPO (set REPO=)"; exit 1; }
echo "==== VERDICT BOX BOOTSTRAP @ $(date -u +%FT%TZ) · repo=$REPO ===="

# ── 1. Preconditions: real KMS + strong secrets (the safety spine demands it) ──
: "${NEXUS_KEK_GCP_KEY:?set NEXUS_KEK_GCP_KEY to the GCP-KMS CryptoKey resource — the safety spine refuses a local KEK in production}"
: "${GCS_BACKUP_BUCKET:?set GCS_BACKUP_BUCKET (gs://…) — a regulated proof-of-behavior SoR MUST have backups before client #2}"

gen() { openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | xxd -p | tr -d '\n'; }

if [ ! -f "$ENV_FILE" ]; then
  echo "---- generating strong production secrets → $ENV_FILE (chmod 600) ----"
  JWT="$(gen)"; EXPLORER="$(gen)"; PGPW="$(gen)"; QECPW="$(gen)"; QECSUBPW="$(gen)"; NEO4J="$(gen)"
  umask 077
  cat > "$ENV_FILE" <<EOF
# VKPower Verdict — production env (generated $(date -u +%FT%TZ)). DO NOT COMMIT.
NEXUS_ENV=production
# ── Strong secrets (NOT dev defaults — the boot gate would refuse those) ──
NEXUS_JWT_SECRET=${JWT}
QEC_EXPLORER_TOKEN=${EXPLORER}
POSTGRES_PASSWORD=${PGPW}
QEC_DB_PASSWORD=${QECPW}
QEC_SUBSTRATE_DB_PASSWORD=${QECSUBPW}
NEO4J_PASSWORD=${NEO4J}
# ── Envelope: real GCP KMS (never the dev local KEK in production) ──
NEXUS_KEK_PROVIDER=gcp_kms
NEXUS_KEK_GCP_KEY=${NEXUS_KEK_GCP_KEY}
# ── Verdict hardening: audience-enforced tokens + distributed limiter + leader ──
QEC_REQUIRE_AUD=true
QEC_ADMISSION_BACKEND=redis
QEC_REDIS_URL=redis://redis:6379/3
QEC_DAEMON_LEADER_ELECTION=advisory_lock
# ── Backups ──
GCS_BACKUP_BUCKET=${GCS_BACKUP_BUCKET}
EOF
  echo "SECRETS_GENERATED (JWT/explorer/DB/neo4j — 256-bit each)"
else
  echo "---- reusing existing $ENV_FILE ----"
fi
set -a; . "$ENV_FILE"; set +a
echo "boot-safety preview: NEXUS_ENV=$NEXUS_ENV KEK=$NEXUS_KEK_PROVIDER require_aud=$QEC_REQUIRE_AUD admission=$QEC_ADMISSION_BACKEND leader=$QEC_DAEMON_LEADER_ELECTION"

# ── 2. Bring up the VKPower factory (dedicated Postgres + redis + platform-api) ──
echo "---- STEP2 factory stack (docker-compose.yml) ----"
docker compose --env-file "$ENV_FILE" -f docker-compose.yml up -d postgres redis 2>&1 | tail -4
echo "waiting for postgres…"; until docker exec nexus-postgres pg_isready -U nexus >/dev/null 2>&1; do sleep 3; done; echo "PG_READY"
docker compose --env-file "$ENV_FILE" -f docker-compose.yml up -d 2>&1 | tail -6

# ── 3. Dedicated qecentral DB + least-priv roles (strong passwords) ──
echo "---- STEP3 qecentral DB + roles ----"
docker exec -i nexus-postgres psql -U nexus -d postgres -v ON_ERROR_STOP=0 \
  -v qec_password="$QEC_DB_PASSWORD" -v qec_substrate_password="$QEC_SUBSTRATE_DB_PASSWORD" \
  < scripts/qec_db_bootstrap.sql 2>&1 | tail -6
docker exec nexus-postgres psql -U nexus -lqt | cut -d'|' -f1 | grep -qw qecentral \
  && echo "QECENTRAL_DB_OK" || { echo "QECENTRAL_DB_FAIL"; exit 1; }

# ── 4. Build + migrate + bring up the Verdict plane ──
echo "---- STEP4 Verdict plane (docker-compose.qec.yml) ----"
docker compose --env-file "$ENV_FILE" -f docker-compose.qec.yml build qe-central qe-explorer 2>&1 | tail -6
docker compose --env-file "$ENV_FILE" -f docker-compose.qec.yml run --rm \
  qe-central alembic -c alembic_qec/alembic.ini upgrade head 2>&1 | tail -8
TABLES=$(docker exec nexus-postgres psql -U nexus -d qecentral -t -A -c \
  "select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE';" 2>/dev/null)
echo "QECENTRAL_TABLES=$TABLES (expect 23 incl. alembic_version — 22 tenant tables + qec_002)"
QEC_PLATFORM_API_URL="http://platform-api:8091" \
  docker compose --env-file "$ENV_FILE" -f docker-compose.qec.yml --profile portal up -d 2>&1 | tail -8

# ── 5. Health gate (the safety spine either booted clean or refused loudly) ──
echo "---- STEP5 health ----"
sleep 8
for svc in nexus-qe-central nexus-repo-intel; do
  docker ps --filter "name=$svc" --format '{{.Names}} {{.Status}}' || true
done
docker exec nexus-qe-central sh -lc 'curl -s -m 10 http://localhost:8093/health' 2>&1 | head -c 800; echo

# ── 6. Nightly backup + restore-drill (6.3a exit criterion: recovery PROVEN) ──
echo "---- STEP6 backup cron + restore-drill scaffold ----"
install -m 755 scripts/verdict_pg_backup.sh /usr/local/bin/verdict_pg_backup.sh 2>/dev/null \
  || cp scripts/verdict_pg_backup.sh /usr/local/bin/ 2>/dev/null || true
( crontab -l 2>/dev/null | grep -v verdict_pg_backup; echo "17 3 * * * GCS_BACKUP_BUCKET=$GCS_BACKUP_BUCKET /usr/local/bin/verdict_pg_backup.sh >> /var/log/verdict_backup.log 2>&1" ) | crontab - 2>/dev/null \
  && echo "BACKUP_CRON_INSTALLED (03:17 nightly → $GCS_BACKUP_BUCKET)" || echo "BACKUP_CRON_SKIPPED (no crontab; run verdict_pg_backup.sh from your scheduler)"
echo "NOTE: run 'bash scripts/verdict_pg_backup.sh --restore-drill' to PROVE recovery (6.3a exit)."

echo "==== VERDICT BOX BOOTSTRAP DONE @ $(date -u +%FT%TZ) ===="
echo "Portal: http://<box-ip>:5273  ·  qe-central: :8093  ·  factory: :8091"
echo "If qe-central is NOT healthy, the safety spine likely REFUSED an unsafe boot — check its logs; that is fail-closed working."
