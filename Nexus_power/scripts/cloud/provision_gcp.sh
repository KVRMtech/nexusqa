#!/usr/bin/env bash
# Nexus QA — GCP single-VM provisioning (Cloud Shell friendly).
#
# Creates:
#   - 1 × VM with NVIDIA L4 GPU (default) or T4 fallback
#   - 1 × Cloud SQL Postgres (db-g1-small)
#   - 1 × Memorystore Redis (1GB Basic)
#   - 1 × GCS bucket for object storage
#   - Firewall rules for orchestrator (8100) + client UI (3000)
#   - Service account with minimal IAM
#
# Cost ceiling: ~$25/day with everything always-on. Stop the VM
# between sessions to drop to ~$2/day (Cloud SQL + Redis idle).
#
# Idempotent: safe to re-run. Existing resources are skipped.
#
# Lessons baked in (from earlier provisioning attempts):
#   - Image family `pytorch-latest-gpu` no longer exists; using
#     `common-cu129-ubuntu-2204-nvidia-580` which Google currently
#     maintains. `discover_image_family` falls back if Google bumps
#     versions again.
#   - gcloud's `--allow` rejects `tcp:8100,3000,8080` (treats 3000 as
#     IP protocol). Prefix every port with `tcp:`.
#   - Free-trial billing rejects GPUs (TPU only). Upgrade billing
#     account first.
#   - T4 routinely out of stock; L4 is newer + more available.
#     Script tries L4 first across us-central1 zones, then T4 fallback.
#
# Usage (in Cloud Shell):
#   PROJECT_ID=<your-project-id> ./provision_gcp.sh
#
# Optional overrides:
#   GPU_TYPE=nvidia-tesla-t4 ./provision_gcp.sh   # force T4 instead of L4
#   ZONES="us-east4-a us-east4-b" ./provision_gcp.sh

set -eu

# ─── CONFIG (override via env) ────────────────────────────────

PROJECT_ID="${PROJECT_ID:-REPLACE_WITH_YOUR_PROJECT_ID}"
REGION="${REGION:-us-central1}"
VM_NAME="${VM_NAME:-nexus-vm}"
DB_INSTANCE="${DB_INSTANCE:-nexus-postgres}"
REDIS_INSTANCE="${REDIS_INSTANCE:-nexus-redis}"
GCS_BUCKET="${GCS_BUCKET:-${PROJECT_ID}-nexus-artifacts}"
SA_NAME="${SA_NAME:-nexus-vm-sa}"
DISK_SIZE_GB="${DISK_SIZE_GB:-200}"
IMAGE_PROJECT="${IMAGE_PROJECT:-deeplearning-platform-release}"
DB_PASSWORD_FILE="${DB_PASSWORD_FILE:-/tmp/nexus_db_password}"

# GPU selection:
#   GPU_TYPE=nvidia-l4 (default) — needs g2-standard-* machine type
#   GPU_TYPE=nvidia-tesla-t4    — needs n1-standard-* machine type
GPU_TYPE="${GPU_TYPE:-nvidia-l4}"

if [[ "$GPU_TYPE" == "nvidia-l4" ]]; then
  MACHINE_TYPE="${MACHINE_TYPE:-g2-standard-8}"  # L4 is bundled into g2
  ZONES_DEFAULT="us-central1-a us-central1-b us-central1-c us-east4-a us-east4-b us-east4-c"
else
  MACHINE_TYPE="${MACHINE_TYPE:-n1-standard-8}"
  ZONES_DEFAULT="us-central1-a us-central1-b us-central1-c us-central1-f us-east1-c us-east1-d us-west1-a us-west1-b"
fi
ZONES="${ZONES:-$ZONES_DEFAULT}"

# ─── Guard rails ──────────────────────────────────────────────

if [[ "$PROJECT_ID" == "REPLACE_WITH_YOUR_PROJECT_ID" ]]; then
  echo "ERROR: set PROJECT_ID. Example:" >&2
  echo "  PROJECT_ID=your-project ./provision_gcp.sh" >&2
  exit 1
fi
if ! command -v gcloud >/dev/null 2>&1; then
  echo "ERROR: gcloud CLI not found. Use Cloud Shell or install gcloud." >&2
  exit 1
fi

gcloud config set project "$PROJECT_ID" >/dev/null
echo "Provisioning Nexus QA stack on project ${PROJECT_ID} (GPU: ${GPU_TYPE})"

# ─── Discover current Deep Learning VM image family ──────────
#
# Google renames image families when CUDA/driver versions change
# (e.g. `pytorch-latest-gpu` → `common-cu129-ubuntu-2204-nvidia-580`).
# Discover the newest `common-cu*-ubuntu-22*` family so the VM-create
# step doesn't fail on a stale literal.

discover_image_family() {
  local f
  f="$(gcloud compute images list \
        --project="$IMAGE_PROJECT" \
        --filter="family ~ '^common-cu.*ubuntu-2204'" \
        --format='value(family)' 2>/dev/null \
      | sort -u | tail -1)"
  echo "$f"
}

IMAGE_FAMILY="${IMAGE_FAMILY:-$(discover_image_family)}"
if [[ -z "$IMAGE_FAMILY" ]]; then
  IMAGE_FAMILY="common-cu129-ubuntu-2204-nvidia-580"   # last-known-good
fi
echo "Using image family: $IMAGE_FAMILY"

# ─── Enable required APIs ─────────────────────────────────────

echo "[1/8] Enabling APIs..."
gcloud services enable \
  compute.googleapis.com \
  sqladmin.googleapis.com \
  redis.googleapis.com \
  storage.googleapis.com \
  servicenetworking.googleapis.com \
  --project="$PROJECT_ID" --quiet

# ─── Service account ──────────────────────────────────────────

echo "[2/8] Service account..."
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$SA_EMAIL" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="Nexus VM service account" \
    --project="$PROJECT_ID" --quiet
fi
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

# ─── Firewall rules ───────────────────────────────────────────

echo "[6/8] Firewall rules..."
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

# ─── GPU VM with multi-zone fallback ──────────────────────────

echo "[7/8] GPU VM..."
# Skip if VM already exists in any zone.
EXISTING_ZONE="$(gcloud compute instances list \
  --project="$PROJECT_ID" \
  --filter="name=$VM_NAME" \
  --format='value(zone.basename())' 2>/dev/null | head -1)"

if [[ -n "$EXISTING_ZONE" ]]; then
  echo "  VM ${VM_NAME} already exists in zone ${EXISTING_ZONE}; skipping create."
  ZONE="$EXISTING_ZONE"
else
  ZONE=""
  for Z in $ZONES; do
    echo "  → trying zone $Z..."
    # Build the create command. L4 uses g2-standard-* (GPU built in);
    # T4 uses n1-standard-* with explicit --accelerator.
    CREATE_ARGS=(
      compute instances create "$VM_NAME"
      --machine-type="$MACHINE_TYPE"
      --zone="$Z"
      --maintenance-policy=TERMINATE
      --image-family="$IMAGE_FAMILY"
      --image-project="$IMAGE_PROJECT"
      --boot-disk-size="${DISK_SIZE_GB}GB"
      --boot-disk-type=pd-ssd
      --service-account="$SA_EMAIL"
      --scopes=cloud-platform
      --metadata=install-nvidia-driver=True
      --tags=nexus-vm
      --project="$PROJECT_ID"
      --quiet
    )
    if [[ "$GPU_TYPE" != "nvidia-l4" ]]; then
      CREATE_ARGS+=(--accelerator="count=1,type=${GPU_TYPE}")
    fi
    if gcloud "${CREATE_ARGS[@]}" 2>/tmp/vm_err; then
      ZONE="$Z"
      break
    fi
    # Surface specific failure reasons up front.
    err="$(cat /tmp/vm_err)"
    if grep -q "ZONE_RESOURCE_POOL_EXHAUSTED" <<<"$err"; then
      echo "    out of stock in $Z, trying next zone..."
    elif grep -qE "Quota .* exceeded" <<<"$err"; then
      echo "    QUOTA EXCEEDED — file a support case at https://console.cloud.google.com/support/cases"
      echo "    Request: NVIDIA_${GPU_TYPE^^}_GPUS in region ${REGION%-*}, limit 1"
      exit 2
    elif grep -q "free tier where non-TPU accelerators" <<<"$err"; then
      echo "    BILLING TIER blocks GPUs — upgrade billing at https://console.cloud.google.com/billing"
      exit 3
    else
      echo "    other error in $Z: $(tail -c 200 /tmp/vm_err)"
    fi
  done
fi

if [[ -z "$ZONE" ]]; then
  echo
  echo "ERROR: ${GPU_TYPE} unavailable in all attempted zones. Options:"
  echo "  - Wait 30-60 min and re-run (capacity comes back)"
  echo "  - Try different GPU: GPU_TYPE=nvidia-tesla-t4 ./provision_gcp.sh"
  echo "  - Try different region: ZONES='asia-southeast1-a' ./provision_gcp.sh"
  exit 4
fi

VM_IP="$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
  --project="$PROJECT_ID" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"

# ─── Authorize VM IP on Cloud SQL ─────────────────────────────

echo "[8/8] Authorize VM IP on Cloud SQL..."
if [[ -n "$VM_IP" ]]; then
  gcloud sql instances patch "$DB_INSTANCE" \
    --authorized-networks="${VM_IP}/32" \
    --project="$PROJECT_ID" --quiet >/dev/null
else
  echo "  WARNING: VM has no external IP — Cloud SQL authorize skipped."
fi

# ─── Summary ──────────────────────────────────────────────────

cat <<EOF

═══════════════════════════════════════════════════════════════
 Nexus QA — GCP provisioning complete
═══════════════════════════════════════════════════════════════

 VM            ${VM_NAME}  (${VM_IP})
 Zone          ${ZONE}
 GPU           ${GPU_TYPE}  (${MACHINE_TYPE})
 Cloud SQL     ${DB_INSTANCE}  (${DB_IP})
 Redis         ${REDIS_INSTANCE}  (${REDIS_IP}:6379)
 GCS bucket    gs://${GCS_BUCKET}
 DB password   ${DB_PASSWORD}
               (saved to ${DB_PASSWORD_FILE})

 SSH command   gcloud compute ssh ${VM_NAME} --zone ${ZONE} --project ${PROJECT_ID}

 Next step:    bash deploy_on_vm.sh \\
                 "${VM_NAME}" "${ZONE}" \\
                 "${DB_IP}" "${REDIS_IP}" \\
                 "${GCS_BUCKET}" "${DB_PASSWORD}"

 Cost watch    Stop the VM when not testing:
               gcloud compute instances stop ${VM_NAME} --zone ${ZONE}
               Resume:
               gcloud compute instances start ${VM_NAME} --zone ${ZONE}

 Note          VM is in ${ZONE}; remember this for all future gcloud
               compute commands (the zone is not project-default).
═══════════════════════════════════════════════════════════════
EOF
