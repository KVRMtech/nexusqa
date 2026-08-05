#!/usr/bin/env bash
set -euo pipefail

# Deploy Summit Life Carrier Admin to the GCP VM.
# Run from the repo root on the VM (/home/srika/nexus-src).
#
# Usage:
#   bash scripts/deploy_summit_life_carrier.sh
#
# This builds the Docker image and starts it via docker-compose.qec.yml
# under the 'grounds' profile, then reloads Caddy with the new vhost.

COMPOSE_FILE="docker-compose.qec.yml"
SERVICE="summit-life-carrier"

echo "=== Building $SERVICE ==="
docker compose -f "$COMPOSE_FILE" --profile grounds build "$SERVICE"

echo "=== Starting $SERVICE ==="
docker compose -f "$COMPOSE_FILE" --profile grounds up -d "$SERVICE"

echo "=== Verifying container ==="
sleep 3
if docker ps --filter "name=nexus-summit-life-carrier" --format '{{.Status}}' | grep -q "Up"; then
    echo "Container nexus-summit-life-carrier is UP"
else
    echo "ERROR: Container failed to start. Logs:"
    docker logs nexus-summit-life-carrier --tail 30
    exit 1
fi

echo "=== Health check ==="
for i in 1 2 3 4 5; do
    if curl -sf -o /dev/null http://localhost:3002/portal/sign-in; then
        echo "Health check PASSED (attempt $i)"
        break
    fi
    if [ "$i" -eq 5 ]; then
        echo "ERROR: Health check failed after 5 attempts"
        docker logs nexus-summit-life-carrier --tail 20
        exit 1
    fi
    sleep 2
done

echo "=== Updating Caddy ==="
# Copy updated Caddyfile to where Caddy reads it
if [ -f /home/srika/Caddyfile ]; then
    cp infrastructure/caddy/Caddyfile /home/srika/Caddyfile
    docker exec verdict-caddy caddy reload --config /etc/caddy/Caddyfile 2>/dev/null \
        || echo "Note: Caddy reload via exec failed — restart the container if needed"
else
    echo "WARNING: /home/srika/Caddyfile not found. Copy manually:"
    echo "  cp infrastructure/caddy/Caddyfile /home/srika/Caddyfile"
    echo "  docker restart verdict-caddy"
fi

echo ""
echo "=== DEPLOYED ==="
echo "Internal:  http://localhost:3002"
echo "External:  https://summitlife-admin.136-85-106-73.sslip.io"
echo ""
echo "To verify externally:"
echo "  curl -k https://summitlife-admin.136-85-106-73.sslip.io/portal/sign-in"
