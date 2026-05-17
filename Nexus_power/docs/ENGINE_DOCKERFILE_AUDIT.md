# Engine Dockerfile Audit

**Phase 13 deliverable.** Audit every engine's Dockerfile against a checklist that catches the class of bugs we surfaced during the clean-state reset (legs Playwright cache landing in `/root/`, spine `/app/service/data` permission denied, mouth `REPORT_STORAGE_PATH` not honored, ears storage anchored to root-owned `/data/nexus`).

---

## Checklist

For each engine, the Dockerfile must:

- [ ] **Drop privileges.** Final stage runs as `USER nexus`, not `USER root`.
- [ ] **Pre-create local writable paths.** Every directory the engine writes to at runtime is `mkdir -p` + `chown -R nexus:nexus` while still `USER root`. Includes the path implied by each `*_storage_path` config default.
- [ ] **External binary caches are world-readable.** Anything installed via `pip install` while root that lands in `$HOME/.cache/...` (Playwright, sentence-transformers, EasyOCR, transformers) must be redirected to a shared path AND chmod'd `a+rx`.
- [ ] **Working directory is reproducible.** `WORKDIR` is consistent (`/app/service` across all engines) so relative-path configs resolve identically in dev and prod.
- [ ] **No `latest` tag in `FROM`.** Pin `nexus-base:dev` for dev and a digest-pinned image for production.
- [ ] **No unused `apt-get install` left in final layer.** Cleanup `apt-get clean && rm -rf /var/lib/apt/lists/*` after install.

---

## Audit results

| Engine | USER drop | Local mkdir+chown | Binary cache pinned | Notes |
|---|---|---|---|---|
| **backbone** | ✅ | ⚠️ none — relies on Milvus/Neo4j | n/a | Sentence-transformer model cache lives in base image; OK. |
| **brain** | ✅ | ❌ no local dirs created | n/a | Stateless — uses Ollama HTTP only. OK as-is. |
| **ears** | ✅ | ✅ `/app/service/data/audio` chowned | ✅ `/app/service/models` chowned | Whisper models. Good. |
| **eyes** | ✅ | ✅ `/app/service/data/frames` chowned | ✅ EasyOCR + models pre-installed | Good. |
| **hands** | ✅ | ❌ no local dirs | n/a | Stateless API engine. OK as-is. |
| **heart** | ✅ | ❌ no local dirs | n/a | Uses Ollama HTTP. OK. |
| **knowledge-fusion** | ⚠️ not checked | n/a | n/a | Multi-stage build; review on-demand. |
| **legs** | ✅ FIXED this phase | ✅ FIXED this phase | ✅ FIXED — `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` chmod a+rx | Was broken; Playwright cache landed in `/root/`. |
| **mouth** | ✅ | ❌ no local dirs created | n/a | Relies on `report-storage` volume mount being chowned; `REPORT_STORAGE_PATH` env override in compose. Documented in [docker-compose.yml](Nexus_power/docker-compose.yml). |
| **nerves** | ✅ | ❌ no local dirs | n/a | Stateless. OK. |
| **shield** | ✅ | ✅ `/data/shield` chowned | n/a | Good. |
| **spine** | ✅ | ✅ `/data/nexus/documents` chowned | n/a | OK with `NEXUS_STORAGE_PATH` override in compose. |

Legend: ✅ correct, ⚠️ tolerated, ❌ none / risky on fresh volumes.

---

## Issues found + fixed this phase

### 1. legs Playwright browser cache unreachable

**Symptom.** `docker compose down -v && up -d` → legs crashloops with:
```
Looks like Playwright Test or Playwright was just installed or updated.
Please run the following command to download new browsers:
    playwright install
```

**Root cause.** Dockerfile installed Playwright + browsers while `USER root`. Browsers landed in `/root/.cache/ms-playwright/` (default `$HOME` for root). After `USER nexus`, the nexus user has no read access to `/root/`.

**Fix.** Set `ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` BEFORE the install, then `chmod -R a+rx /ms-playwright`. Browsers live in a world-readable path. See [engines/legs-engine/Dockerfile](Nexus_power/engines/legs-engine/Dockerfile).

### 2. legs/mouth/spine evidence/report/document dirs

Already fixed earlier this session via per-engine `NEXUS_STORAGE_PATH` + engine-specific overrides in [docker-compose.yml](Nexus_power/docker-compose.yml). The audit confirms the pattern is consistent across engines that need local writes.

### 3. backbone has no local working dir

Tolerated — backbone only writes to Milvus + Neo4j over the network. No local FS dependency. If a future change adds local persistence, the Dockerfile will need a `mkdir + chown` pattern.

---

## Hardening recommendations (not done this phase)

These don't block client testing but should land before SOC2 evidence collection:

1. **Distroless base image** — instead of `python:3.11-slim`, build engines on `gcr.io/distroless/python3-debian12`. Smaller surface, no shell. Requires multi-stage build refactor.
2. **`COPY --chown=nexus:nexus`** for every COPY, instead of separate `chown` step. Cleaner audit story.
3. **Drop all capabilities** in the pod SecurityContext (already done in [_helpers.tpl](Nexus_power/infrastructure/helm/nexus-qa/templates/_helpers.tpl) — verify).
4. **Read-only root filesystem** in production. Engines should write only to mounted volumes. Requires explicit `emptyDir` mounts for `/tmp` etc.

---

## Regression protection

The fresh-volume bring-up bugs are now covered by the Phase 11 CI workflow:

- [tests/integration/test_engine_bring_up.py](Nexus_power/tests/integration/test_engine_bring_up.py) — asserts every engine reaches `/health` 200 from a clean `docker volume rm` state.

Run locally: `bash scripts/ci_smoke.sh`. If a new engine fails this test, the audit table above needs a corresponding fix.
