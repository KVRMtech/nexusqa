# Nexus QA — Deploy Runbook (GCP VM)

How the live portal at `http://34.124.214.109:3000` is deployed and kept reproducible.
Grounded in the 2026-06-08 reproducibility audit (see also the deploy-lineage notes).

> **Host:** `nexus-vm` @ `asia-southeast1-a`. Repo on VM: `/home/harik/nexus/Nexus_power`
> (owned by `harik` — `scp` to your home, then `sudo cp`/`sudo docker cp`). Docker needs `sudo`.

---

## 1. Two deploy lineages — do NOT mix them

| Service | Lineage | How to deploy | Rebuild safe? |
|---|---|---|---|
| **nexus-platform-api** (`/api/v1/*`, port 8091) | **repo-rebuild** (since 2026-06-10, Phase C — was docker-cp) — built from the VM repo via `platform/api/Dockerfile` | `docker compose build platform-api && docker compose up -d --force-recreate --no-deps platform-api` (rebuild `base-image` FIRST if you changed `sdk/`). See §4. | ✅ Recreate is now the intended path (the trap is gone). |
| **nexus-client** (portal UI, port 3000) | **repo-rebuild** — built from the VM repo via `Dockerfile.client` | `docker compose build client && docker compose up -d client` | ✅ Rebuild is the intended path. |
| **nexus-runner** (Playwright exec) | compose | `docker-compose.runner.yml` (standalone, external net `nexus_power_nexus`) | ✅ |

**Reproducibility status (audited 2026-06-08):** of 111 deployed `*.py` files under `/app/service`,
**97 are byte-identical to the local repo** (CRLF-normalized) and the rest are this-session's changes
or whitespace. The local repo is the de-facto source of truth; the *VM's* git checkout is the stale one.
A clean `platform-api` rebuild **from the local repo** would reproduce (and quietly fix) prod — the only
prod-only delta found was a dormant broken keyword-only marker in `frame_annotator.py` that the repo lacks.

---

## 2. Backend (platform-api) — the docker-cp deploy

```bash
# 1. Stage changed files onto the VM (from your machine)
gcloud compute scp <file.py> nexus-vm:/tmp/deploy/ --zone=asia-southeast1-a

# 2. Copy into the running container at the matching path under /app/service/app
sudo docker cp /tmp/deploy/<file.py> nexus-platform-api:/app/service/app/<relative/path.py>

# 3. Syntax-check WITHOUT writing bytecode (the nexus user can't write __pycache__)
sudo docker exec nexus-platform-api python -c \
  "import ast,sys;[ast.parse(open(p).read(),p) for p in sys.argv[1:]];print('AST_OK')" \
  /app/service/app/<relative/path.py>

# 4. Restart and verify
sudo docker restart nexus-platform-api
sudo docker ps  --format '{{.Names}}\t{{.Status}}' | grep platform-api    # → healthy
sudo docker logs --tail 25 nexus-platform-api | grep -iE 'startup complete|error|traceback'
```

Ignore the pre-existing `frame_annotator_init_failed … Permission denied: /app/service/data`
warning (`frame_annotator_ready=False`) — it is unrelated to app deploys.

**Verify a code path on real data** without HTTP/JWT by exercising it through the app's own session
(set `PYTHONPATH=/app/service`, call `init_db(PlatformAPIConfig())`, then the service fn inside a
`tenant_scoped_session`). This was used to confirm the extraction-health output on 6 live artifacts.

---

## 3. Frontend (client) — repo-rebuild on the VM

> **NEVER build the Vite client from a Windows git-bash shell** — MSYS rewrites the
> `VITE_API_BASE=/api` build arg into a `C:/Program Files/Git/api` path and login breaks.
> Build on the VM (Docker/Linux) where `VITE_API_BASE` comes from the compose build arg.

```bash
# Update the file in the VM repo (back it up first — reversible)
F=/home/harik/nexus/Nexus_power/client/src/...
sudo cp $F $F.bak && sudo cp /tmp/<file> $F

# Rebuild + redeploy ONLY the client (other services stay Running, no stale-base race)
sudo bash -c 'cd /home/harik/nexus/Nexus_power && docker compose build client && docker compose up -d client'

# Verify the new code actually shipped in the bundle
sudo docker exec nexus-client sh -c "grep -rl '<a string from your change>' /usr/share/nginx/html/assets/"
```

Run the build via a background/synchronous SSH invocation, not a remote `&` — a detached
`gcloud ssh` can be killed mid-build.

---

## 4. platform-api recreate is now SAFE (reconciled 2026-06-10, Phase C)

The repo was reconciled from the running container (incl. the **SDK** — `sdk/nexus-sdk/nexus_sdk/db/models.py`
had `CombinationReserveRow` the base lacked, which crash-looped the first cutover) and platform-api now
runs a clean source-built image. `docker compose build platform-api && up -d --force-recreate --no-deps platform-api`
is the intended deploy. **If you change `sdk/`, rebuild `base-image` FIRST** (the SDK is installed into
`nexus-base:dev`, not `/app/service`). Before any cutover: `docker commit` a `rollback-<date>` image, build,
then GATE — `diff` the new image's `/app/service` **and** `site-packages/nexus_sdk` vs the running container
(expect only ~14 orphan top-level scratch scripts; SDK drift must be 0) + an import-proof. Rollback = retag the
commit → `:latest` + recreate. **Env:** `.env` is older than all containers, so a recreate injects identical
secrets; verify `stat .env` mtime < container `Created` before recreating (don't `docker exec env` — classifier
blocks reading prod secrets). After a `.env`/secret change, force-recreate the **whole** stack (services keep
CREATE-time env otherwise → stale-JWT/DB 401s). **VM:** a TERMINATED VM (likely spot preemption) recovers via
`gcloud compute instances start nexus-vm`; containers auto-restart (`restart: unless-stopped`).

---

## 5. Secrets — provide via environment, never commit

Config defaults live in [`platform/api/app/config.py`](platform/api/app/config.py) (`PlatformAPIConfig`).
Override in prod via env (compose `environment:` / `.env`), never in code:

| Setting | Env var | Dev default (CHANGE in prod) |
|---|---|---|
| JWT signing secret | `NEXUS_JWT_SECRET` | `dev-jwt-secret-change-me` |
| Postgres password | `POSTGRES_PASSWORD` | `nexus-dev` |
| Postgres host/user/db | `POSTGRES_HOST` / `POSTGRES_USER` / `POSTGRES_DB` | `localhost` / `nexus` / `nexus` |
| Redis password | `REDIS_PASSWORD` | `""` |

- All four services must share the **same** `NEXUS_JWT_SECRET` or tokens fail cross-service.
- Never print, log, or commit a real secret value. Keep `.env` out of git.

---

## 6. DB migrations on prod (additive only, EXPLICIT auth each time)

Alembic does **not** run at startup. Apply an additive table by piping idempotent SQL through the
postgres container, mirroring migration 036's RLS block:

```bash
sudo docker cp x.sql nexus-postgres:/tmp/x.sql
sudo docker exec nexus-postgres psql -U nexus -d nexus -v ON_ERROR_STOP=1 -f /tmp/x.sql
# Verify: SELECT to_regclass('public.<t>');  SELECT policyname FROM pg_policies WHERE tablename='<t>';
```

Every new tenant-scoped table needs: `ENABLE`+`FORCE ROW LEVEL SECURITY` and a `tenant_isolation`
policy `USING/WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true))`
(`tenant_scoped_session` sets that GUC). **A prod DB migration requires explicit user authorization
every time** — the auto-mode classifier blocks it even after a general "proceed".

---

## 7. Base image reproducibility

`infrastructure/docker/Dockerfile.base` (`FROM python:3.11-slim@sha256:…`) is now **digest-pinned**
so future clean rebuilds are deterministic. Known gaps to close for full reproducibility:

- The base currently deployed (`nexus-base:dev`, `sha256:95bb54a8…`, built 2026-05-31) **predates the
  pin** — it used whatever `3.11-slim` was current then. The pin governs future rebuilds only.
- `apt-get install build-essential curl git` and the SDK install are **not version-pinned** — pin
  apt package versions + freeze SDK deps for byte-reproducibility.
- `platform/api/Dockerfile` takes `ARG BASE_IMAGE=nexus-base:latest`; the running base is tagged `:dev`.
