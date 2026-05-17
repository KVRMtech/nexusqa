#!/usr/bin/env bash
# Nexus QA — GCP single-VM provisioning (Cloud Shell friendly).
#
# Creates:
#   - 1 × VM with NVIDIA T4 GPU (n1-standard-8 + 200GB SSD)
#   - 1 × Cloud SQL Postgres (db-g1-small)
#   - 1 × Memorystore Redis (1GB Basic)
#   - 1 × GCS bucket for object storage
#   - Firewall rules for orchestrator (8100) + client UI (3000)
#   - Service account with minimal IAM
#
# Cost ceiling: ~$25/day with everything always-on. Stop the VM
# (`gcloud compute instances stop nexus-vm`) between sessions to drop
# to ~$2/day (only Cloud SQL + Redis idle costs).
#
# Idempotent: safe to re-run. Existing resources are skipped.
#
# Usage (in Cloud Shell):
#   1. Edit the CONFIG block below — paste your project ID.
#   2. chmod +x provision_gcp.sh && ./provision_gcp.sh
#   3. Wait ~10 min for VM + Cloud SQL.
#   4. Script prints VM IP + DB password at the end — save them.

set -eu

# ─── CONFIG (edit these) ──────────────────────────────────────

PROJECT_ID="${PROJECT_ID:-REPLACE_WITH_YOUR_PROJECT_ID}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
VM_NAME="${VM_NAME:-nexus-vm}"
DB_INSTANCE="${DB_INSTANCE:-nexus-postgres}"
REDIS_INSTANCE="${REDIS_INSTANCE:-nexus-redis}"
GCS_BUCKET="${GCS_BUCKET:-${PROJECT_ID}-nexus-artifacts}"
SA_NAME="${SA_NAME:-nexus-vm-sa}"
GPU_TYPE="${GPU_TYPE:-nvidia-tesla-t4}"
MACHINE_TYPE="${MACHINE_TYPE:-n1-standard-8}"
DISK_SIZE_GB="${DISK_SIZE_GB:-200}"
# Deep Learning VM image with CUDA 12 pre-installed (PyTorch family).
# nvidia-container-toolkit is included so Docker can hand the GPU
# straight through to the Ollama / eyes containers.
IMAGE_FAMILY="${IMAGE_FAMILY:-pytorch-latest-gpu}"
IMAGE_PROJECT="${IMAGE_PROJECT:-deeplearning-platform-release}"
# Strong random password for the Postgres user — written to disk for
# the deploy_on_vm.sh script to pick up. Rotate after first deploy.
DB_PASSWORD_FILE="${DB_PASSWORD_FILE:-/tmp/nexus_db_password}"

# ─── Guard rails ──────────────────────────────────────────────

if [[ "$PROJECT_ID" == "REPLACE_WITH_YOUR_PROJECT_ID" ]]; then
  echo "ERROR: set PROJECT_ID in the CONFIG block (line 26) or export it." >&2
  exit 1
fi

if ! command -v gcloud >/dev/null 2>&1; then
  echo "ERROR: gcloud CLI not found. Use Cloud Shell or install gcloud." >&2
  exit 1
fi

gcloud config set project "$PROJECT_ID" >/dev/null
echo "Provisioning Nexus QA stack on project ${PROJECT_ID}, region ${REGION}..."

# ─── Enable required APIs ─────────────────────────────────────

echo "[1/8] Enabling APIs (compute, sql, redis, storage)..."
gcloud services enable \
  compute.googleapis.com \
  sqladmin.googleapis.com \
  redis.googleapis.com \
  storage.googleapis.com \
  servicenetworking.googleapis.com \
  --project="$PROJECT_ID" --quiet

# ─── Service account ──────────────────────────────────────────

echo "[2/8] Service account..."
if ! gcloud iam service-accounts describe \
    "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="Nexus VM service account" \
    --project="$PROJECT_ID" --quiet
fi
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
for role in cloudsql.client redis.editor storage.objectAdmin compute.osLogin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" --role="roles/${role}" \
    --condition=None --quiet >/dev/null
done

# ─── GCS bucket ───────────────────────────────────────────────

echo "[3/8] GCS bucket..."
if ! gcloud storage buckets describe "gs://${GCS_BUCKET}" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${GCS_BUCKET}" \
    --location="$REGION" --uniform-bucket-level-access \
    --project="$PROJECT_ID" --quiet
fi

# ─── Cloud SQL Postgres ───────────────────────────────────────

echo "[4/8] Cloud SQL Postgres (~5 min on first create)..."
if ! gcloud sql instances describe "$DB_INSTANCE" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud sql instances create "$DB_INSTANCE" \
    --database-version=POSTGRES_15 --tier=db-g1-small \
    --region="$REGION" \
    --storage-size=20 --storage-type=SSD --storage-auto-increase \
    --backup-start-time=03:00 \
    --project="$PROJECT_ID" --quiet
fi
# Generate & store DB password (idempotent — only writes on first run).
if [[ ! -f "$DB_PASSWORD_FILE" ]]; then
  openssl rand -base64 30 | tr -d '/+=' | cut -c1-24 > "$DB_PASSWORD_FILE"
  chmod 600 "$DB_PASSWORD_FILE"
fi
DB_PASSWORD="$(cat "$DB_PASSWORD_FILE")"
gcloud sql users set-password nexus \
  --instance="$DB_INSTANCE" --password="$DB_PASSWORD" \
  --project="$PROJECT_ID" --quiet >/dev/null 2>&1 || \
  gcloud sql users create nexus \
    --instance="$DB_INSTANCE" --password="$DB_PASSWORD" \
    --project="$PROJECT_ID" --quiet
# Create the `nexus` database (matches local docker-compose).
if ! gcloud sql databases list \
    --instance="$DB_INSTANCE" --project="$PROJECT_ID" --format="value(name)" \
    | grep -q '^nexus$'; then
  gcloud sql databases create nexus \
    --instance="$DB_INSTANCE" --project="$PROJECT_ID" --quiet
fi
DB_IP="$(gcloud sql instances describe "$DB_INSTANCE" \
  --project="$PROJECT_ID" --format='value(ipAddresses[0].ipAddress)')"

# ─── Memorystore Redis ────────────────────────────────────────

echo "[5/8] Memorystore Redis..."
if ! gcloud redis instances describe "$REDIS_INSTANCE" \
    --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud redis instances create "$REDIS_INSTANCE" \
    --size=1 --region="$REGION" --tier=basic --redis-version=redis_7_0 \
    --project="$PROJECT_ID" --quiet
fi
REDIS_IP="$(gcloud redis instances describe "$REDIS_INSTANCE" \
  --region="$REGION" --project="$PROJECT_ID" --format='value(host)')"

# ─── Firewall ─────────────────────────────────────────────────

echo "[6/8] Firewall rules..."
# gcloud's --allow takes comma-separated `PROTOCOL[:PORT]` entries;
# `tcp:8100,3000,8080` would treat 3000 + 8080 as IP protocol numbers
# (gcloud rejects them). Prefix each port with `tcp:` to disambiguate.
declare -A FIREWALL_RULES=(
  ["nexus-allow-orchestrator"]="tcp:8100,tcp:3000,tcp:8080"
  ["nexus-allow-ssh"]="tcp:22"
)
for name in "${!FIREWALL_RULES[@]}"; do
  allow="${FIREWALL_RULES[$name]}"
  if ! gcloud compute firewall-rules describe "$name" \
      --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud compute firewall-rules create "$name" \
      --network=default --allow="$allow" \
      --source-ranges=0.0.0.0/0 --project="$PROJECT_ID" --quiet
  fi
done

# ─── GPU VM ───────────────────────────────────────────────────

echo "[7/8] GPU VM (~3 min)..."
if ! gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud compute instances create "$VM_NAME" \
    --machine-type="$MACHINE_TYPE" \
    --zone="$ZONE" \
    --accelerator="count=1,type=${GPU_TYPE}" \
    --maintenance-policy=TERMINATE \
    --image-family="$IMAGE_FAMILY" \
    --image-project="$IMAGE_PROJECT" \
    --boot-disk-size="${DISK_SIZE_GB}GB" \
    --boot-disk-type=pd-ssd \
    --service-account="$SA_EMAIL" \
    --scopes=cloud-platform \
    --metadata="install-nvidia-driver=True" \
    --tags=nexus-vm \
    --project="$PROJECT_ID" --quiet
fi
VM_IP="$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
  --project="$PROJECT_ID" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"

# Allow VM to reach Cloud SQL by IP. Production should use private VPC
# peering; for this validation we authorize the VM's external IP.
echo "[8/8] Authorize VM IP on Cloud SQL..."
gcloud sql instances patch "$DB_INSTANCE" \
  --authorized-networks="${VM_IP}/32" \
  --project="$PROJECT_ID" --quiet >/dev/null

# ─── Summary ──────────────────────────────────────────────────

cat <<EOF

═══════════════════════════════════════════════════════════════
 Nexus QA — GCP provisioning complete
═══════════════════════════════════════════════════════════════

 VM            ${VM_NAME}  (${VM_IP})
 Zone          ${ZONE}
 Cloud SQL     ${DB_INSTANCE}  (${DB_IP})
 Redis         ${REDIS_INSTANCE}  (${REDIS_IP}:6379)
 GCS bucket    gs://${GCS_BUCKET}
 DB password   $(cat "$DB_PASSWORD_FILE")
               (also saved to ${DB_PASSWORD_FILE} on Cloud Shell)

 SSH command   gcloud compute ssh ${VM_NAME} --zone ${ZONE} --project ${PROJECT_ID}

 Next step:    bash deploy_on_vm.sh \\
                 "${VM_IP}" "${DB_IP}" "${REDIS_IP}" \\
                 "${GCS_BUCKET}" "$(cat "$DB_PASSWORD_FILE")"

 Cost watch    Stop the VM when not testing:
               gcloud compute instances stop ${VM_NAME} --zone ${ZONE}
               Resume:
               gcloud compute instances start ${VM_NAME} --zone ${ZONE}
═══════════════════════════════════════════════════════════════
EOF
