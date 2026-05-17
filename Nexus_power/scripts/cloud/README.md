# Nexus QA — GCP Deployment (single GPU VM)

Production-shape testing on GCP: 1 × Compute Engine VM with a T4 GPU,
running the same docker-compose stack as local but with managed
Cloud SQL + Memorystore Redis + GCS object storage.

## Cost ceiling

| Resource | Always-on | Stopped |
|---|---|---|
| VM (T4, n1-standard-8, SSD 200GB) | ~$0.95/hr ≈ $23/day | $0 (compute), $2/day (SSD) |
| Cloud SQL `db-g1-small` | ~$0.85/day | same |
| Memorystore Redis 1GB Basic | ~$1.15/day | same |
| GCS storage | <$0.10/day | same |
| **Total always-on** | **~$25/day** | **~$4/day** |

Your $300 free credit covers ~12 days always-on or ~75 days
"few hours per day". Stop the VM between sessions:
```bash
gcloud compute instances stop nexus-vm --zone us-central1-a
```

## Prerequisites

1. GCP account with $300 credit activated.
2. GCP project created (any name — script uses the project ID).
3. **Cloud Shell** opened (browser-based, gcloud pre-installed) — easier
   than installing gcloud CLI locally. Top-right toolbar of
   https://console.cloud.google.com → terminal icon.

## Step 1 — Provision

In Cloud Shell:

```bash
# Verify your project ID
gcloud config list

# If project ID differs from what you expect, set it:
gcloud config set project YOUR-PROJECT-ID

# Clone or upload this directory to Cloud Shell, then:
chmod +x provision_gcp.sh
PROJECT_ID=$(gcloud config get-value project) ./provision_gcp.sh
```

Wait ~10 min. Script prints VM IP, DB IP, Redis IP, GCS bucket, and
DB password at the end. Save these.

## Step 2 — Push code to the VM

Three options:

**A. Git push (recommended for ongoing dev):**
1. Push your local branch to a private GitHub repo
2. Set `REPO_URL=git@github.com:you/nexus-power.git` in `deploy_on_vm.sh`

**B. scp from local machine (one-shot):**
```bash
gcloud compute scp --recurse Nexus_power/ nexus-vm:~/nexus/ \
  --zone us-central1-a --project YOUR-PROJECT-ID
```

**C. Build images locally and push to Artifact Registry:**
```bash
gcloud auth configure-docker us-central1-docker.pkg.dev
docker tag nexus-base:dev us-central1-docker.pkg.dev/PROJECT/repo/nexus-base:dev
docker push us-central1-docker.pkg.dev/PROJECT/repo/nexus-base:dev
# repeat for each service image
```

Option B is fastest for first deploy. ~5 min upload.

## Step 3 — Deploy

```bash
bash deploy_on_vm.sh <VM_IP> <DB_IP> <REDIS_IP> <BUCKET> <DB_PASSWORD>
```

This SSHs into the VM, installs Docker + NVIDIA container toolkit,
pulls the LLaVA model into Ollama (~15 min on first run), and brings
up the docker-compose stack.

Time: 30-60 min for first run (mostly model pulls + image builds).

## Step 4 — Validate

From your laptop (or anywhere):

```bash
# Replace VM_IP with the IP from provision_gcp.sh output
curl http://VM_IP:8100/health

# Open the UI
open http://VM_IP:3000
```

Then re-run the same 10-upload validation we did locally — expect:

| Metric | Local CPU | GCP T4 GPU | Improvement |
|---|---|---|---|
| OCR per workflow | ~380s | ~15-30s | 12-25× |
| analyze_scenes | 60s (circuit broke) | 30-60s (real LLaVA) | full enrichment |
| Total wall time | ~9 min | ~2-3 min | 3-4× |
| Quality_gate outcome | mostly `pass_with_warnings` | mostly `pass` | clean runs |

## Step 5 — Teardown when done

```bash
# Stop everything (preserves data, stops billing on compute):
gcloud compute instances stop nexus-vm --zone us-central1-a

# Resume later:
gcloud compute instances start nexus-vm --zone us-central1-a

# Full destroy:
bash teardown_gcp.sh   # (TODO: write this when needed)
```

## Troubleshooting

**"GPU not accessible from Docker"** — the NVIDIA driver install
runs at first boot and takes 2-3 min. SSH in, run `nvidia-smi`, and
re-run the deploy script when the driver responds.

**Cloud SQL "could not connect"** — the script authorizes the VM's
external IP. If you stopped and started the VM, the IP may have
changed (unless you reserved a static IP). Re-run:
```bash
NEW_IP=$(gcloud compute instances describe nexus-vm --zone us-central1-a \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
gcloud sql instances patch nexus-postgres \
  --authorized-networks="${NEW_IP}/32"
```

**Ollama model pull stuck** — `docker logs ollama-init` shows progress.
LLaVA-7b is ~5GB. On GCP's network this typically takes 10-15 min.
