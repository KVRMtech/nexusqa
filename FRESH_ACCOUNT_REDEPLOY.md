# Fresh-Account Redeploy Runbook

Captured before deleting the old GCP setup (both owner logins lost). **No data is
copied — this is a clean rebuild.** The application CODE is entirely in this git
repo; only the GCP infrastructure + runtime data are being discarded.

---

## 1. What the OLD setup was (for reference)

**GCP projects (being deleted):**
| Project | Role | Billing acct (card — NOT CLI-accessible) |
|---|---|---|
| `project-5c779aa6-0de2-4c83-b5e` | the whole deployment | `01EE5B-F96081-34FA93` |
| `project-9a4e132d-6d21-4506-a6b` | empty (no resources) | `010A8D-0A77B8-542EFE` |

**The one VM (`verdict-box`) ran everything in Docker:**
- Machine: **n2-standard-4**, zone **asia-southeast1-a**, **Ubuntu 22.04 LTS**, **100 GB pd-balanced** boot disk.
- Static IP: **35.186.147.245** (→ `sslip.io` hostnames, no DNS needed).
- Default compute service account: `673682582349-compute@developer.gserviceaccount.com`.
- Firewall: GCP defaults + `verdict-demo-ports` = **tcp:80, 443, 8097**. (Platform-api :8091 is internal to the docker network.)
- Storage bucket: `verdict-backups-673682582349` (backups — discarded).

**Docker containers on the VM (topology lives in this repo's compose files):**
| Container | Purpose | Port |
|---|---|---|
| `nexus-platform-api` | FastAPI backend (`/app/service/app`) | 8091 (internal) |
| `nexus-postgres` | Postgres — DBs `nexus` + `qecentral` | 5432 (internal) |
| `nexus-qe-central` | QE-Central bridge / crawler | internal |
| `nexus-verdict-portal` | nginx serving the React portal | 80/443 |
| `nexus-runner` | Playwright runner | internal |
| venkata test app (`vkpowerlife`) | the demo app under test | 80 (sslip.io vhost) |
| squid proxy | egress control (Safe_ports → test apps on :80) | internal |

**URLs:** portal `https://35-186-147-245.sslip.io/` · app `https://vkpowerlife.35-186-147-245.sslip.io/` · tenant `__platform__`.

**Compose sources of truth (in this repo):** `Nexus_power/docker-compose.yml` +
`.canonical.yml` + `.qec.yml` + `.runner.yml` + `.override.yml`. Per-feature
`deploy_*.sh` scripts at the repo root show how individual pieces were pushed.

---

## 2. Rebuild on the NEW account (clean, fresh test)

1. **New GCP project + billing** on the new account. Enable APIs: Compute Engine,
   Cloud KMS, IAM, Cloud Resource Manager.
2. **VM** — same spec is plenty:
   ```
   gcloud compute instances create verdict-box \
     --zone=<ZONE> --machine-type=n2-standard-4 \
     --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
     --boot-disk-size=100GB --boot-disk-type=pd-balanced
   gcloud compute addresses create verdict-box-ip --region=<REGION>   # then assign to the VM
   gcloud compute firewall-rules create verdict-demo-ports \
     --allow=tcp:80,tcp:443,tcp:8097 --direction=INGRESS --source-ranges=0.0.0.0/0
   ```
   The new static IP gives a new `<newip>.sslip.io` host — update the app/portal base URLs to it.
3. **Docker** — install docker + compose plugin on the VM; `git clone` this repo to
   `/home/<user>/nexus-src` (old path was `/home/srika/nexus`).
4. **Secrets / env (regenerate — none are copied):**
   - `NEXUS_JWT_SECRET` (new random secret)
   - `RUNNER_TOKEN` (new)
   - Postgres role `nexus` password (SUPERUSER, as before)
   - **GCP KMS envelope key** — *re-provision on the new project* (KMS keys are
     region+project bound; the envelope/`EnvelopeService` needs a fresh keyring +
     key and the SA granted `cloudkms.cryptoKeyEncrypterDecrypter`). This is the
     one piece that always needs re-doing on a new GCP — see `deploy_kms.sh`.
5. **Bring the stack up:** `docker compose -f docker-compose.yml -f docker-compose.canonical.yml -f docker-compose.qec.yml -f docker-compose.runner.yml up -d --build` (mirror the old override set).
6. **Apply the persona × environment migrations** (in `Nexus_power/platform/api/scripts`, via `psql -U nexus -d nexus -f`):
   `apply_persona_env.sql` → `apply_persona_env_p4.sql` → `apply_persona_env_p5.sql` → `apply_persona_env_r3.sql`
   (plus the other `apply_*.sql` the app uses: auth_profiles, heal_events, run_reports, etc.).
7. **Build the portal on PowerShell/VM, never git-bash** (API-base mangling bug):
   `cd Nexus_power/verdict-portal && npm ci && npm run build`, then serve `dist/` from nginx.
8. **Fresh smoke test** — crawl the venkata app, generate, run a persona, verify the
   new **Personas & Environments** portal tab. No old data expected; everything reseeds.

**Gotchas carried over (from memory):** re-provision KMS on new GCP; test apps on
port 80 (squid `Safe_ports`); recreating a container reverts any `docker cp`'d
files — prefer image rebuilds; the branch `feat/qec-phases-0-6` holds the P0–P5 +
R1–R6 work (not merged to `develop`).

---

## 3. Old billing / card (cannot be closed from the CLI)

The card lives on billing accounts this CLI login is not an admin on. Deleting the
projects stops **all usage → no new charges**, but to actually remove the card /
close the billing account you must either recover the billing-admin account, or
contact **Google Cloud Billing Support** ([cloud.google.com/contact](https://cloud.google.com/contact) → Billing), or
ask the bank to block Google charges.
