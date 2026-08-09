# Watched Deploy Runbook — P0–P6 branch → proven

**Purpose:** convert `feat/qec-dynamic-catalog-p0-p6` from *code-complete + tested* to
*proven live*, safely, with you at the keyboard. Every step has a **STOP-and-verify**
gate. Nothing here is irreversible except where flagged 🔒.

**Deploy facts (verified):** deploy = `scripts/deploy.ps1` → push to `mine/develop` →
SSH `verdict-box` → `git pull` → per-plane `docker compose build` + `up -d
--force-recreate`. Data + KEK live on **volumes**, so force-recreate preserves them
(build+recreate, never `docker cp`). This branch does **not** touch `sdk/nexus-sdk`,
so the base image is **not** rebuilt (no `-RebuildBase` needed). Migrations are a
**separate manual step**, not auto-run on container start.

---

## Step 0 — Pre-flight (local, no side effects)
```
# on the branch, confirm green
cd Nexus_power/engines/qe-explorer   && python -m pytest tests/ -q
cd ../../platform/qe-central         && NEXUS_SECRET_KEY=ci NEXUS_JWT_SECRET=ci python -m pytest tests/ -q
```
**GATE:** explorer 598 passed · qe-central 1439 passed / 69 skipped. If not green, stop.

## Step 1 — Land the branch on `develop` (the VM pulls `develop`)
Review the diff, then merge (no force):
```
git checkout develop
git merge --no-ff feat/qec-dynamic-catalog-p0-p6
```
**GATE:** clean merge, working tree matches the reviewed branch.

## Step 2 — 🔒 Back up the qe-central database BEFORE migrating
Migrations `qec_011–014` are **additive only** (new tables/columns), so they cannot
drop data — but take a snapshot anyway. On the VM:
```
# adjust container/db names to your compose; qecentral db, qe-central postgres
docker exec <qec-postgres-container> pg_dump -U <user> qecentral \
  > ~/backups/qecentral_pre_qec014_$(date +%Y%m%d_%H%M%S).sql
```
**GATE:** dump file exists and is non-empty.

## Step 3 — Deploy the code (build + force-recreate)
From the repo root on your Windows box:
```
.\scripts\deploy.ps1 qe-central qe-explorer
```
Watch the SSH output for `nexus-base:dev: no rebuild needed` (expected — no SDK change)
and the two `up -d --force-recreate` lines.
**GATE:** `docker ps` shows `nexus-qe-central` and the explorer healthy; `/health` OK.

## Step 4 — Apply the migrations (qec_011 → 014)
On the VM, in the qe-central compose context:
```
docker compose -f docker-compose.qec.yml run --rm qe-central \
  alembic -c alembic_qec/alembic.ini upgrade head
```
**GATE:** alembic reports upgrading through `qec_014` (current head). Verify:
```
docker compose -f docker-compose.qec.yml run --rm qe-central \
  alembic -c alembic_qec/alembic.ini current      # → qec_014 (head)
```
Confirm the new tables exist (psql): `catalog_questions`, `catalog_versions`,
`personas`, `persona_journeys`, and `journey_branches.reveals` column.

## Step 5 — Live crawl on the DISPOSABLE env (the proof)
Point a crawl at the disposable VKPower env (the same way you ran `f42511b8` /
`baaf37f3`). This is Phase-B, so it needs the disposable attestation + approvals.
After it completes and folds:
**GATE (P1 branching):** on `/apply/lifestyle`, the walk answers the questionnaire;
`journey_branches` rows for those questions carry non-null `reveals` on the walked
option. (Then a planned re-crawl of the "Yes" side captures the other reveals.)

## Step 6 — Verify the three routes (the wired pillars)
With a valid auth token for the tenant/app:
```
# P2 — the app-scoped Master Catalog
GET  /api/v1/qec/apps/{app_id}/catalog
# P3 — generation: supply answers, get the journey
POST /api/v1/qec/apps/{app_id}/catalog/project   {"answers": {"tobacco use": "yes"}}
# P6 — regression: diff the two latest crawls (needs 2 crawls)
GET  /api/v1/qec/apps/{app_id}/catalog/diff
```
**GATE:** `/catalog` returns the deduped question set (form fields + questionnaire
questions); `/catalog/project` shows the tobacco follow-ups in `activated` for
`tobacco use=yes` and in `skipped` for `no`; after a second crawl, `/catalog/diff`
names what changed.

## Step 7 — Persona generation (after the bridge lands)
```
POST /api/v1/qec/apps/{app_id}/personas            {"name":"Tobacco","answers":{"tobacco use":"yes"}}
POST /api/v1/qec/apps/{app_id}/personas/generate    # project ALL personas' journeys
GET  /api/v1/qec/apps/{app_id}/personas             # list personas + their journeys
```
**GATE:** each persona yields a distinct journey (executed/activated/skipped) from the
ONE catalog; provenance is `inferred` until a verifying crawl confirms it.

---

## Rollback
- **Code:** redeploy the previous `develop` commit (`git revert` the merge, deploy).
- **DB:** migrations are additive; to reverse, `alembic ... downgrade qec_010`
  (drops the new tables/column — only if you must). Restore from the Step-2 dump if
  anything looks wrong.
- **Flags:** all new autonomy stays behind existing gates (`branch_walks_enabled`,
  disposable attestation); turning a tenant flag off reverts behavior without a deploy.
