#!/usr/bin/env bash
# Nexus QA — single-GPU-pod deployment on RunPod.io.
#
# Runs on the RunPod pod itself (not your laptop). The pod boots with
# Docker + NVIDIA driver pre-installed via the "RunPod PyTorch 2.4"
# template; this script wires up the docker-compose stack with
# production-safe defaults + pulls the LLaVA model into Ollama.
#
# Usage (on the pod, after you've scp'd Nexus_power/ to /workspace):
#   cd /workspace/Nexus_power
#   bash scripts/cloud/deploy_runpod.sh
#
# Time: 20-40 min first run (image builds + Ollama model pull).
# Cost: ~$0.30-0.50/hr while the pod is running.

set -eu

# ─── Pre-flight ──────────────────────────────────────────────

echo "[1/7] Pre-flight checks..."
if ! command -v docker >/dev/null 2>&1; then
  echo "  Docker missing — installing..."
  curl -fsSL https://get.docker.com | bash
fi

if ! command -v docker compose >/dev/null 2>&1 \
    && ! command -v docker-compose >/dev/null 2>&1; then
  echo "  docker-compose plugin missing — installing..."
  apt-get update -y >/dev/null 2>&1
  apt-get install -y docker-compose-plugin >/dev/null 2>&1
fi

# Verify GPU passthrough works
if ! docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1; then
  echo "  GPU passthrough failed. nvidia-container-toolkit may not be configured."
  echo "  Try: sudo apt-get install -y nvidia-container-toolkit && sudo systemctl restart docker"
  exit 1
fi
echo "  GPU passthrough OK."

# ─── .env file with RunPod-friendly defaults ─────────────────

echo "[2/7] Writing .env..."
JWT_SECRET=$(openssl rand -hex 32)
SECRET_KEY=$(openssl rand -hex 32)
DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
cat > .env <<EOF
# RunPod single-VM deployment — all services in-cluster.
NEXUS_ENV=production
NEXUS_JWT_SECRET=${JWT_SECRET}
NEXUS_SECRET_KEY=${SECRET_KEY}

# Local Postgres + Redis (containers in docker-compose).
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_USER=nexus
POSTGRES_PASSWORD=${DB_PASSWORD}
POSTGRES_DB=nexus
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=
NEXUS_REDIS_URL=redis://redis:6379

# Production-safe defaults (architect P0 review + session findings).
# GPU host: bump scenes back up + enable OCR on GPU.
ORCHESTRATOR_DEFAULT_PROCESSING_PROFILE=multimodal
EYES_OCR_MAX_WORKERS=2
EYES_OCR_FRAME_TIMEOUT_SECONDS=30
EYES_OCR_GPU=true
EYES_FAST_SKIP_OCR=false
EYES_MULTIMODAL_MAX_SCENES=20
EYES_LLAVA_PER_SCENE_TIMEOUT_S=45
EYES_ANALYZE_SCENES_STAGE_BUDGET_S=600
EYES_LLAVA_CIRCUIT_THRESHOLD=3

# Ollama is local on the same pod.
EYES_OLLAMA_BASE_URL=http://ollama:11434
EYES_OLLAMA_MODEL=llava:7b
EYES_FAST_OLLAMA_MODEL=llava:7b
NEXUS_ALLOW_DEGRADED_MODE=false

# Tenant admission + queue saturation
NEXUS_QUEUE_SATURATION_THRESHOLD=400
EOF
echo "  .env written. DB password saved."

# ─── docker-compose override for GPU ─────────────────────────

echo "[3/7] Writing GPU compose override..."
cat > docker-compose.override.yml <<'YAML'
# RunPod GPU override — pins eyes/ears engines + Ollama to GPU.
services:
  eyes:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
  ears:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
  ollama:
    image: ollama/ollama:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    volumes:
      - ollama-data:/root/.ollama
    healthcheck:
      test: ["CMD", "ollama", "list"]
      interval: 30s
YAML

# ─── Bring up the stack ──────────────────────────────────────

echo "[4/7] docker compose up (will take 20-30 min on first run for image builds)..."
docker compose pull 2>/dev/null || true
docker compose build base-image
docker compose up -d

# ─── Wait for Ollama + pull LLaVA ────────────────────────────

echo "[5/7] Waiting for Ollama..."
for i in $(seq 1 60); do
  if docker exec nexus-ollama ollama list >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

echo "[6/7] Pulling LLaVA model (~5 GB, 5-10 min on GPU pods)..."
docker exec nexus-ollama ollama pull llava:7b

# ─── Health check ────────────────────────────────────────────

echo "[7/7] Waiting for orchestrator..."
for i in $(seq 1 60); do
  if curl -sf http://localhost:8100/health >/dev/null 2>&1; then
    echo "  Orchestrator healthy."
    break
  fi
  sleep 5
done

PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "<runpod-pod-id>")
cat <<EOF

═══════════════════════════════════════════════════════════════
 Nexus QA — RunPod deployment complete
═══════════════════════════════════════════════════════════════

 Pod public IP : ${PUBLIC_IP}
 Orchestrator  : http://${PUBLIC_IP}:8100
 Client UI     : http://${PUBLIC_IP}:3000
 DB password   : ${DB_PASSWORD}

 IMPORTANT — on RunPod, you must EXPOSE ports 3000 and 8100 in the
 pod's "TCP Port Mappings" section of the web console for the public
 URLs to actually be reachable. RunPod proxies them via a unique
 https://<pod-id>-3000.proxy.runpod.net URL.

 Quick smoke test:
   curl http://localhost:8100/health   # should return 200
═══════════════════════════════════════════════════════════════
EOF
