# VKPower — Fresh-Account Redeploy Runbook (BOTH products)

Captured before deleting the old GCP setup (both owner logins lost). **No data is
copied — clean rebuild.** All application CODE is in this git repo; only GCP infra
+ runtime data are discarded. Two products share one codebase + substrate:

| Product | What it is | Frontend | GPU? | Compose overlay |
|---|---|---|---|---|
| **A. VKPower Video Processing Canonical** | video → storyboard → test-case → Playwright pipeline (eyes/ears/spine/shield/brain, neo4j, ollama) | `nexus-client` :3000 | **yes** | `docker-compose.gpu.yml` (prod) / `.canonical.yml` (dev/CPU) |
| **B. VKPower Verdict / QE-Central** | crawl → factory → run → heal → honest verdict; the Persona × Environment matrix (R1–R6) | `nexus-verdict-portal` :5273 | no | `docker-compose.qec.yml` + `.runner.yml` |

**Shared substrate (both):** `nexus-postgres` (DBs `nexus` + `qecentral`), `nexus-redis`, `nexus-platform-api` :8091 (the factory backend), `neo4j` (video only), and this repo. `docker-compose.yml` is the base both overlays extend. Product A is frozen **v1.0-gpu-validated** (commit `9f417e4`, see `FROZEN_RELEASE.md`); the persona work is on branch **`feat/qec-phases-0-6`** (P0–P5 + R1–R6, not merged to `develop`).

---

## 1. Old GCP (deleted 2026-07-26) — reference only

| Project | Role | Billing (card — NOT CLI-accessible) | Fate |
|---|---|---|---|
| `project-5c779aa6-0de2-4c83-b5e` | ran **Verdict** on `verdict-box` | `01EE5B-F96081-34FA93` | DELETE_REQUESTED, billing OFF |
| `project-9a4e132d-6d21-4506-a6b` | earlier held the **Video** VM `nexus-vm`; now empty | `010A8D-0A77B8-542EFE` | left (CLI not Owner; ~$0) |

Old machines: Video = **g2-standard-8 + 1× L4 GPU**, 200 GB pd-ssd, DLVM `common-cu129-ubuntu-2204-nvidia-580`. Verdict = **n2-standard-4**, 100 GB pd-balanced, Ubuntu 22.04, static IP `35.186.147.245` → `sslip.io`. **Card can't be closed from the CLI** (see §6).

---

## 2. Shared prerequisites (new account)

1. **New GCP project + billing.** Enable APIs: Compute Engine, Cloud KMS, IAM, Cloud Resource Manager.
2. **Docker + compose plugin** on each VM; `git clone` this repo (old path `/home/<user>/nexus-src`), branch `feat/qec-phases-0-6`.
3. **If copying a Windows tarball instead of git clone → normalize CRLF first** (bash/Dockerfile break on `\r`): `find . -type f \( -name '*.sh' -o -name 'Dockerfile*' -o -name '*.yml' \) -print0 | xargs -0 sed -i 's/\r$//'`.
4. **Regenerate ALL secrets (none are copied):** `NEXUS_JWT_SECRET`, `POSTGRES_PASSWORD` (role `nexus`, SUPERUSER/bypassrls), `NEO4J_PASSWORD`, `RUNNER_TOKEN`, and a fresh **GCP KMS** keyring+key for envelope encryption (`envelope=gcp_kms`; grant the VM SA `cloudkms.cryptoKeyEncrypterDecrypter`). KMS-encrypted secrets (capture-once auth, persona cards) from the old project **do not decrypt** on a new project — everything re-enters fresh.
5. **DB schema = alembic head + EVERY `scripts/apply_*.sql` that has `IF NOT EXISTS`.** Hard-won lesson: several features shipped via standalone `apply_*.sql`, not numbered migrations, so a plain `alembic upgrade head` misses them (storyboard/surfaces 500, Pages&Forms blank). After `alembic upgrade head`, apply: surface_prefs, e2e_auth_profiles, flywheel_labels, script_versions, e2e_runner_jobs, e2e_run_screenshots, run_reports, heal_events, **and the persona set** `apply_persona_env.sql → _p4 → _p5 → r3` (via `docker cp … nexus-postgres:/tmp/ && psql -U nexus -d nexus -f`). Do NOT run apply_017/018 (those tables come from alembic and the scripts lack IF NOT EXISTS).

---

## 3. Product A — VKPower Video Processing Canonical (GPU VM)

**VM:** `g2-standard-8` + `1× nvidia-l4`, `asia-southeast1-a`, **200 GB pd-ssd**, image family DLVM `common-cu129-ubuntu-2204-nvidia-580` (ships GPU driver + nvidia-container-toolkit). Reserve a static IP.
```
gcloud compute instances create nexus-vm --zone=<ZONE> \
  --machine-type=g2-standard-8 --accelerator=type=nvidia-l4,count=1 \
  --image-family=common-cu129-ubuntu-2204-nvidia-580 --image-project=deeplearning-platform-release \
  --boot-disk-size=200GB --boot-disk-type=pd-ssd --maintenance-policy=TERMINATE
gcloud compute firewall-rules create nexus-allow-app --allow=tcp:3000,tcp:8080,tcp:8091,tcp:8100,tcp:8000 --source-ranges=0.0.0.0/0
```
**GPU on the DLVM:** driver is present; **do NOT apt-upgrade nvidia-container-toolkit** (breaks deps) — just `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`. Install docker-ce manually (DLVM has no Docker). `usermod -aG docker` under sudo adds *root*, so builds need `sudo`.

**Bring up (prod GPU):**
```
export NEXUS_JWT_SECRET=… POSTGRES_USER=nexus POSTGRES_PASSWORD=… NEO4J_PASSWORD=…
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```
(Dev/CPU-only variant: `-f docker-compose.canonical.yml`. Storage: bind `/data/nexus` chown `999:999` via `docker-compose.storage.yml` or the pipeline hits `Permission denied: /data/nexus/eyes` — engines run as uid 999.)

**~15 containers:** redis, postgres, neo4j (7474/7687), ollama (11434, GPU), auth (8000), gateway (8080), platform-api (8091), client (3000), orchestrator (8100), eyes-api/worker (8002…), ears-api/worker (8003, whisper CUDA), spine, shield, brain, runner. Optional `full` profile adds milvus/etcd/minio.

**LLM tiers (keys via stdin→root, NEVER argv/chat):** Tier-1 **Claude `claude-sonnet-4-6`** (`EYES_VISION_TIER1_*`, `LLM_TIER_TIER_PREMIUM_*`, `*_TIER1_*`) → Tier-2 **GPT-4o** (`EYES_VISION_TIER2_*`; router `openai_compat` `…TIER_BALANCED_BASE_URL=https://api.openai.com/v1`) → Tier-3 **ollama** local (`llama3.2-vision:11b`, `llava:7b`, `moondream`, `llama3.2:1b`). GOTCHA: the platform-api LLM router fail-fasts unless `LLM_TIER_TIER_PREMIUM_BASE_URL=https://api.anthropic.com` is *also* set. `ollama pull` all four models after boot.

**Portal:** `http://<ip>:3000`; admin `admin@nexus.local` (tenant `nexus-platform`, role admin) — reset hash in the auth container if seeded with the code default. **Frozen contract:** additive only over `9f417e4`; see `frozen_canonical_pipeline` + `frozen_visual_evidence_v2` (v3.0-frozen-20260621).

---

## 4. Product B — VKPower Verdict / QE-Central (CPU VM)

**VM:** `n2-standard-4`, `asia-southeast1-a`, **100 GB pd-balanced**, **Ubuntu 22.04 LTS**. Reserve a static IP → gives a `<newip>.sslip.io` host (no DNS needed).
```
gcloud compute instances create verdict-box --zone=<ZONE> --machine-type=n2-standard-4 \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --boot-disk-size=100GB --boot-disk-type=pd-balanced
gcloud compute firewall-rules create verdict-demo-ports --allow=tcp:80,tcp:443,tcp:8097 --source-ranges=0.0.0.0/0
```
**Bring up:**
```
docker compose -f docker-compose.yml -f docker-compose.qec.yml -f docker-compose.runner.yml up -d --build
```
**Containers:** platform-api (8091), postgres, redis, **qe-central** (8093), **qe-explorer** (contained crawler), **qec-egress-proxy** (squid — Safe_ports; put test apps on **:80**), **verdict-portal** (nginx, 5273 → HTTPS via a `caddy:2` container `verdict-caddy`, `<ip>.sslip.io { reverse_proxy localhost:5273 }`, auto Let's Encrypt once 80/443 open), **nexus-runner** (Playwright), and demo apps (`nexus-acme-life` :8097, the `vkpowerlife` app). Runtime knobs: `QEC_ADMISSION_BACKEND=memory`, `QEC_DAEMON_LEADER_ELECTION=none`, `QEC_EXPLORER_DISPATCH_ENABLED=true`, `envelope=gcp_kms`, `NEXUS_PLUGIN_MODULES=products.nexus_qa.plugin`.

**Persona × Environment migrations** (order): `apply_persona_env.sql → apply_persona_env_p4.sql → apply_persona_env_p5.sql → apply_persona_env_r3.sql` (all in `Nexus_power/platform/api/scripts`).

**Portal build — on PowerShell or the VM, NEVER git-bash** (API-base string-mangling bug): `cd Nexus_power/verdict-portal && npm ci && npm run build`, then serve `dist/` from the nginx container (index.html `Cache-Control: no-cache`; hashed `/assets` immutable). The new **Personas & Environments** tab + prominent **Run as** are in this build.

**Optional reservation cap** env: `NEXUS_PERSONA_RESERVATION_CAP` (default 50).

---

## 5. Topology choice

- **Split (recommended, cost-optimal — mirrors the old setup):** GPU `nexus-vm` for Product A (start on-demand for video work, stop when idle — GPU is the expensive part), and a small always-on CPU `verdict-box` for Product B. Each has its own postgres.
- **Co-located (simpler ops, pricier):** one GPU VM running both overlay sets (`-f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.qec.yml -f docker-compose.runner.yml`), sharing one postgres/redis/platform-api.

**Cost posture:** stop-when-idle. A stopped VM still bills its disk (+ reserved IP); to hit $0 you must delete the resources/project (that's what we did to the old one).

---

## 6. Old billing / card (cannot be closed from the CLI)

The card lives on billing accounts this CLI login isn't an admin on. Project deletion stops **all usage → no new charges**, but to *remove the card / close the billing account*: recover the billing-admin account, or contact **Google Cloud Billing Support** ([cloud.google.com/contact](https://cloud.google.com/contact) → Billing, explain both owner logins are lost), or ask the bank to block Google charges. Watch the next statement drop toward $0.
