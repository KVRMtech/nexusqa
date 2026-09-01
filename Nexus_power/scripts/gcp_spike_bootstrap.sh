#!/usr/bin/env bash
# gcp_spike_bootstrap.sh
#
# One-shot Phase-1 spike-test bootstrap on a fresh GCP GPU VM.
# Run AFTER `gcloud compute ssh` into the VM, with this repo cloned.
#
# What this does:
#   1. Verifies CUDA + Docker + nvidia runtime are working
#   2. Builds .env from .env.example + .env.gcp_gpu.example
#   3. Prompts for the 3-4 secrets that must be set
#   4. Runs the canonical rebuild (base image + every engine)
#   5. Prints the URLs you need to test from your laptop
#
# Idempotent: safe to re-run mid-failure.
#
# Usage:
#   bash scripts/gcp_spike_bootstrap.sh
#
# Or non-interactively (set secrets via env first):
#   NEXUS_JWT_SECRET="..." EARS_HF_TOKEN="..." \
#   bash scripts/gcp_spike_bootstrap.sh --non-interactive

set -euo pipefail

NON_INTERACTIVE=0
if [[ "${1:-}" == "--non-interactive" ]]; then
  NON_INTERACTIVE=1
fi

c_red()    { printf '\033[31m%s\033[0m' "$*"; }
c_green()  { printf '\033[32m%s\033[0m' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m' "$*"; }
c_blue()   { printf '\033[34m%s\033[0m' "$*"; }

step() { printf '\n%s %s\n' "$(c_blue '==>')" "$*"; }
ok()   { printf '    %s %s\n' "$(c_green '✓')"  "$*"; }
warn() { printf '    %s %s\n' "$(c_yellow '!')" "$*"; }
die()  { printf '\n%s %s\n' "$(c_red 'FAIL')" "$*" >&2; exit 1; }

# Move to repo root (the Nexus_power directory containing docker-compose.yml).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# ─── 1. Sanity: GPU + Docker + nvidia-runtime ──────────────────

step "1/5  precheck — GPU, Docker, nvidia-runtime"

command -v nvidia-smi >/dev/null 2>&1 || die "nvidia-smi not found — is this a GPU VM?"
GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
[[ -n "$GPU_NAME" ]] || die "nvidia-smi returned no GPU"
ok "GPU detected: ${GPU_NAME}"

command -v docker >/dev/null 2>&1 || die "docker not found"
docker --version >/dev/null || die "docker daemon not running"
ok "docker $(docker --version | awk '{print $3}' | tr -d ',')"

# nvidia container toolkit / runtime test — should NOT need sudo on
# Deep Learning VMs (they pre-add the user to the docker group).
if ! docker info 2>/dev/null | grep -q "Runtimes:.*nvidia"; then
  warn "nvidia runtime not registered with Docker; trying sudo nvidia-ctk..."
  if command -v nvidia-ctk >/dev/null 2>&1; then
    sudo nvidia-ctk runtime configure --runtime=docker || warn "nvidia-ctk failed (continuing)"
    sudo systemctl restart docker || warn "could not restart docker"
  fi
fi
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1 \
  && ok "Docker + GPU passthrough works" \
  || warn "Docker GPU smoke test failed — engines using cuda will fall back to CPU"

command -v docker compose >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1 \
  || die "docker compose plugin not found"
ok "docker compose available"

# ─── 2. Build .env from templates ───────────────────────────────

step "2/5  build .env from .env.example + .env.gcp_gpu.example"

[[ -f .env.example ]] || die ".env.example missing — wrong directory?"
[[ -f .env.gcp_gpu.example ]] || die ".env.gcp_gpu.example missing — pull latest from repo"

if [[ -f .env ]]; then
  warn ".env exists; backing up to .env.bak.$(date +%s)"
  cp .env ".env.bak.$(date +%s)"
fi

cp .env.example .env
echo "" >> .env
echo "# === Appended by gcp_spike_bootstrap.sh ===" >> .env
cat .env.gcp_gpu.example >> .env
ok ".env composed from templates"

# ─── 3. Secret prompts (only the 3 that matter for the spike) ───

step "3/5  fill in required secrets"

prompt_secret() {
  local var="$1" prompt="$2" default="${3:-}"
  local current
  current="$(grep -E "^${var}=" .env | head -1 | cut -d= -f2- || true)"
  if [[ -n "$current" && "$current" != "change-this-"* && "$current" != "your-"* ]]; then
    ok "${var} already set"
    return 0
  fi
  if [[ "$NON_INTERACTIVE" == "1" ]]; then
    local from_env="${!var:-}"
    [[ -n "$from_env" ]] || die "${var} not set and --non-interactive"
    sed -i.bak "s|^${var}=.*|${var}=${from_env}|" .env
    rm -f .env.bak
    ok "${var} from env"
    return 0
  fi
  echo
  echo "  ${prompt}"
  if [[ -n "$default" ]]; then
    echo "  (press Enter to auto-generate)"
  fi
  read -rsp "  ${var}: " val
  echo
  if [[ -z "$val" && -n "$default" ]]; then
    val="$default"
    ok "auto-generated ${var}"
  fi
  [[ -n "$val" ]] || die "${var} required"
  # Use python to handle special chars safely.
  python3 -c "
import re, sys
path = '.env'
with open(path) as f: data = f.read()
data = re.sub(r'^${var}=.*', '${var}=' + sys.argv[1], data, count=1, flags=re.MULTILINE)
open(path, 'w').write(data)
" "$val"
}

# Generate sane defaults for the things that don't need to be remembered.
RAND_JWT="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')"
RAND_FERNET="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null || echo '')"
RAND_REDIS="$(openssl rand -hex 16 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(16))')"
RAND_PG="$(openssl rand -hex 16 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(16))')"
RAND_NEO="$(openssl rand -hex 16 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(16))')"
RAND_ADMIN_PW="$(openssl rand -hex 12 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(12))')"

prompt_secret NEXUS_JWT_SECRET   "JWT secret (32+ random chars)" "$RAND_JWT"
prompt_secret NEXUS_SECRET_KEY   "platform secret key (32+ random chars)" "$RAND_JWT"
prompt_secret REDIS_PASSWORD     "Redis password" "$RAND_REDIS"
prompt_secret POSTGRES_PASSWORD  "Postgres password" "$RAND_PG"
prompt_secret NEO4J_PASSWORD     "Neo4j password" "$RAND_NEO"
prompt_secret NEXUS_ADMIN_PASSWORD "admin user password (you'll log in with this)" "$RAND_ADMIN_PW"
prompt_secret SHIELD_ENCRYPTION_KEY "Shield Fernet key (cryptography.fernet)" "$RAND_FERNET"
prompt_secret EARS_HF_TOKEN      "Hugging Face token for pyannote diarization (https://hf.co/settings/tokens)"

ok "all required secrets set"

# Save the admin password to a file so it survives the noise above —
# you'll need this to log in to the UI.
ADMIN_PW="$(grep -E '^NEXUS_ADMIN_PASSWORD=' .env | cut -d= -f2-)"
ADMIN_EMAIL="$(grep -E '^NEXUS_ADMIN_EMAIL=' .env | cut -d= -f2-)"
cat > .credentials.txt <<EOF
Nexus QA — admin credentials (spike test)
  Email:    ${ADMIN_EMAIL}
  Password: ${ADMIN_PW}

  UI:           http://EXTERNAL_IP:3000/login
  Gateway API:  http://EXTERNAL_IP:8080
  Platform API: http://EXTERNAL_IP:8091
  Orchestrator: http://EXTERNAL_IP:8100
EOF
chmod 600 .credentials.txt
ok "credentials saved to .credentials.txt (cat it later to grab them)"

# ─── 4. Build + bring up the stack ──────────────────────────────

step "4/5  build images + bring up stack (~15-20 min first time)"

# Use the existing canonical rebuild script — it handles base-image
# race, infra-first ordering, migrations, and health probes for us.
VERBOSE="${VERBOSE:-0}" bash scripts/rebuild_canonical.sh

# ─── 5. Print URLs + smoke probe ────────────────────────────────

step "5/5  ready — connection info"

# Get the VM's external IP from metadata server (no gcloud needed inside).
EXT_IP="$(curl -s -H Metadata-Flavor:Google \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || echo 'unknown')"

echo
echo "$(c_green '╔════════════════════════════════════════════════════════════════╗')"
echo "$(c_green '║  Nexus QA spike stack is UP on GCP.                            ║')"
echo "$(c_green '╚════════════════════════════════════════════════════════════════╝')"
echo
echo "  External IP:  $(c_blue "${EXT_IP}")"
echo
echo "  Client UI:    http://${EXT_IP}:3000/login"
echo "  Gateway:      http://${EXT_IP}:8080"
echo "  Platform API: http://${EXT_IP}:8091"
echo "  Orchestrator: http://${EXT_IP}:8100"
echo
echo "  Login as:     $(cat .credentials.txt | grep Email | awk '{print $2}')"
echo "  Password:     $(cat .credentials.txt | grep Password | awk '{print $2}')"
echo
echo "$(c_yellow '  Reminder: the firewall rule must allow your laptop IP on tcp:3000,8080,8091,8100,8000')"
echo "$(c_yellow '  Or use an SSH tunnel:')"
echo "    gcloud compute ssh nexus-spike --zone=us-central1-a -- -L 3000:localhost:3000"
echo
echo "  Smoke test (audio first, validates the workflow plane):"
echo "    1. Browse to the UI, log in, upload a 30-second WAV. Should complete <2 min."
echo "    2. Then upload your 1-min, 5-min, 30-min videos."
echo "    3. Watch wall times in the UI stage rail."
echo
echo "  To tear down when done: "
echo "    On your laptop:  gcloud compute instances delete nexus-spike --zone=us-central1-a"
echo
