# VKPower Verdict — Dedicated-Box Deploy Runbook (Phase 6.3)

**Goal:** stand up a **self-contained production Verdict stack on a dedicated GCP box** (separate from `nexus-vm`), fail-closed, backed-up, ready to run a real client's regression tests. Then drive the first live crawl (6.1).

**Why a dedicated box:** it structurally removes the #1 FATAL risk (shared-Postgres blast radius) — Verdict gets its own Postgres, its own KMS key, its own lifecycle. VKPower's `nexus-vm` is never touched.

---

## 0. One-time: what you provision (the founder-only steps)

These create cloud infra, so they run from *your* shell (not the agent's sandbox).

```bash
PROJECT=<your-gcp-project>;  ZONE=asia-southeast1-a;  BOX=verdict-box

# 1. Dedicated VM (4 vCPU / 16 GB is comfortable for factory + Verdict + a browser)
gcloud compute instances create $BOX --project=$PROJECT --zone=$ZONE \
  --machine-type=e2-standard-4 --boot-disk-size=120GB \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --scopes=cloud-platform            # cloud-platform ⇒ the box can reach KMS + GCS

# 2. GCP KMS key for the envelope (the safety spine REFUSES a local KEK in prod)
gcloud kms keyrings create verdict --location=$ZONE --project=$PROJECT 2>/dev/null || true
gcloud kms keys create verdict-kek --location=$ZONE --keyring=verdict \
  --purpose=encryption --project=$PROJECT 2>/dev/null || true
#   → resource name (copy this):
#   projects/$PROJECT/locations/$ZONE/keyRings/verdict/cryptoKeys/verdict-kek

# 3. GCS backup bucket (a regulated SoR MUST have backups before client #2)
gsutil mb -p $PROJECT -l $ZONE gs://verdict-<client>-backups

# 4. Firewall: expose ONLY the portal (5273) + factory API (8091) to your IPs
gcloud compute firewall-rules create verdict-web --project=$PROJECT \
  --allow=tcp:5273,tcp:8091 --source-ranges=<your-office-cidr>
```

---

## 1. On the box: install Docker + get the code

```bash
gcloud compute ssh $BOX --project=$PROJECT --zone=$ZONE
# then, on the box:
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git openssl
sudo usermod -aG docker $USER && newgrp docker
# get the repo (once you've pushed it) — clone from your GitHub, or scp the tree:
git clone https://github.com/Venkatareddy2012/nexus-power-snapshot.git ~/nexus && \
  mv ~/nexus ~/nexus-src && mkdir -p ~/nexus && mv ~/nexus-src ~/nexus/Nexus_power
export REPO=$HOME/nexus/Nexus_power
```

*(This is exactly why the **push** matters — CI runs from it, and the box clones from it.)*

---

## 2. One command: bootstrap the whole stack (fail-closed)

```bash
NEXUS_KEK_GCP_KEY=projects/$PROJECT/locations/$ZONE/keyRings/verdict/cryptoKeys/verdict-kek \
GCS_BACKUP_BUCKET=gs://verdict-<client>-backups \
bash $REPO/scripts/verdict_box_bootstrap.sh
```

What it does (all in `scripts/verdict_box_bootstrap.sh`):
1. **Generates strong 256-bit secrets** (JWT, explorer HMAC, DB passwords) into `.env.production` — never the dev defaults the boot gate would refuse.
2. Sets the production posture: `NEXUS_ENV=production`, `NEXUS_KEK_PROVIDER=gcp_kms`, `QEC_REQUIRE_AUD=true`, `QEC_ADMISSION_BACKEND=redis`, `QEC_DAEMON_LEADER_ELECTION=advisory_lock` — every Phase-5.5/6 hardening knob *on*.
3. Brings up the **VKPower factory** (dedicated Postgres + redis + platform-api + engines).
4. Creates the **dedicated `qecentral` DB** + least-privilege roles (strong passwords) and runs the migration (21 tables + FORCE RLS).
5. Brings up the **Verdict plane** (qe-central, qe-explorer, repo-intel) + the **portal** (`--profile portal`).
6. Health-checks; installs the **nightly backup cron**.

> **Fail-closed is a feature:** if you forget the KMS key or a secret is weak, `qe-central` **refuses to boot** and says why. That's the safety spine working — an unsafe production start is impossible by design.

---

## 3. Verify (the 6.3a exit gates)

```bash
# a) service healthy + KEK is production-grade (not degraded)
docker exec nexus-qe-central sh -lc 'curl -s http://localhost:8093/health' | python3 -m json.tool
#    expect: status "healthy", kek.is_production_grade true

# b) RESTORE-DRILL — proves the backup is recoverable, not just present
GCS_BACKUP_BUCKET=gs://verdict-<client>-backups bash $REPO/scripts/verdict_pg_backup.sh --restore-drill
#    expect: RESTORE_DRILL_PASS

# c) the REFUSE matrix on the real stack (same proof as nexus-vm, now on the box)
QE_HARNESS_ENABLED=true docker exec nexus-qe-central python -m app.harness.runner
#    expect: R1..R8 REFUSED_CORRECTLY, baseline PASS_BASELINE, exit 0

# d) portal is up
open http://<box-ip>:5273     # the Verdict Command Center, live
```

---

## 4. The first real crawl (6.1) — the product's defining moment

Once the box is healthy, onboard the first target (start with a proving ground):

```bash
# Enable live crawling + point the explorer at Aegis (or your first client's non-prod app)
# via the portal's Onboard wizard (the 6 buckets) OR the API:
TOKEN=$(docker exec nexus-qe-central python -c "from app.service_token import mint_service_jwt; print(mint_service_jwt('pilot-tenant'))")
curl -s -X POST http://localhost:8093/api/v1/qec/apps -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Aegis","base_url":"http://aegis:8096", ...six buckets... , "env_attestation":{"env_kind":"disposable","attested_by":"you"}}'
# then trigger a crawl-backed cycle; watch it in the portal's fleet + Verdict Ledger.
```

Exit: **a real URL → certified running regression suite**, visible in the portal, with `guard_blocks > 0` (the safety fence proven on a live browser) and a published per-stack discovery-recall number.

---

## Ordered founder checklist

1. `git push` the branch (backs up + fires CI).
2. Provision (§0): VM + KMS key + GCS bucket + firewall.
3. On the box (§1): install Docker, clone the repo.
4. `verdict_box_bootstrap.sh` (§2) — one command → live stack.
5. Verify (§3): health + restore-drill + REFUSE matrix + portal.
6. First crawl (§4) → certified suite in the portal.

After that, onboarding client #2…#20 is repeating §4 per app — the platform is multi-tenant from the DB up.
