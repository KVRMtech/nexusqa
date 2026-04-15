# Nexus QA — Architect Summary: Canonical Pipeline, Docker & Model Changes

**Date:** April 1, 2026  
**Status:** All phases COMPLETE — Signed off with 14/14 deployment checks + 13/13 E2E tests passing

---

## 1. What Was Done (7-Phase "Canonical-Only Product Narrowing")

### Phase 1: Product Mode Flag
- Added `NEXUS_PRODUCT_MODE` env var (backend) and `VITE_PRODUCT_MODE` (frontend)
- Default: `canonical` — restricts the entire platform to canonical processing only
- Created `client/src/productMode.ts` — single source of truth for UI mode gating

### Phase 2: UI Route & Sidebar Narrowing
- `App.tsx` — route filtering: canonical mode hides non-canonical routes
- `AppLayout.tsx` — sidebar: only shows canonical-relevant menu items
- `engineStore.ts` — health polling restricted to 5 canonical engines (ears, eyes, shield, spine, brain)
- `api.ts` — API calls scoped to canonical endpoints only

### Phase 3: Session Workspace Hardening
- Orchestrator enforces `session_id` for canonical processing (400 if missing)
- Media fingerprint dedup: SHA-256 fingerprint → Spine cache lookup → skip reprocessing on duplicate uploads

### Phase 4: Orchestrator Canonical-Only Enforcement
- Chain listing filtered: only `nexus.canonical-processing` shown in canonical mode
- Workflow start blocked: 403 for any non-canonical chain_id
- Provenance fields injected: `source_type`, `source_filename`, `created_by`

### Phase 5: Runtime Startup Profile
- `scripts/start_all_services.py` — `--profile canonical|full` flag
- `docker-compose.yml` — 7 non-canonical services gated with `profiles: ["full"]`

### Phase 6: Validation & Ops Alignment
- `scripts/validate_deployment.py` — `--profile canonical` checks 9 services, `--profile full` checks 15
- Canonical services: auth, shield, ears, eyes, spine, brain, gateway, platform-api, orchestrator

### Phase 7: Docker Deployment & Stabilization
- Created `docker-compose.canonical.yml` — dedicated canonical-only compose file
- All services containerized and running in Docker

---

## 2. Docker Architecture

### Canonical Compose (`docker-compose.canonical.yml`) — 13 Services

| Service | Container | Port | Purpose |
|---------|-----------|------|---------|
| **auth-service** | nexus-auth | 8000 | JWT authentication |
| **shield** | nexus-shield | 8001 | PII detection & redaction |
| **ears** | nexus-ears | 8002 | Audio transcription (Whisper) |
| **eyes** | nexus-eyes | 8003 | Visual analysis (Llama Vision) |
| **spine** | nexus-spine | 8009 | Document parsing, embedding, storage |
| **brain** | nexus-brain | 8011 | LLM reasoning & orchestration |
| **gateway** | nexus-gateway | 8080 | API gateway / reverse proxy |
| **platform-api** | nexus-platform-api | 8091 | Platform REST API |
| **orchestrator** | nexus-orchestrator | 8100 | Workflow orchestrator |
| **neo4j** | nexus-neo4j | 7474/7687 | Graph database |
| **ollama** | nexus-ollama | 11434 | Local LLM inference server |
| **client** | nexus-client | 3000 | React frontend |
| **base-image** | nexus-base-builder | — | Shared Python base image builder |

### Non-Canonical Services (gated by `profiles: ["full"]` in docker-compose.yml)

| Service | Port | Why excluded |
|---------|------|-------------|
| heart | 8004 | Rule reasoning — not in canonical chain |
| backbone | 8005 | Knowledge graph — not in canonical chain |
| nerves | 8006 | Event bus — not in canonical chain |
| legs | 8007 | Browser automation — not in canonical chain |
| hands | 8008 | Test execution — not in canonical chain |
| mouth | 8010 | Report generation — not in canonical chain |
| qa-orchestrator | 8092 | Legacy orchestrator — replaced by orchestrator |

### Infrastructure Dependencies

| Component | Source | Connection |
|-----------|--------|------------|
| PostgreSQL (5432) | External (hybrid-pipeline stack) | `host.docker.internal:5432` |
| Redis (6379) | External (hybrid-pipeline stack) | `host.docker.internal:6379` |
| Neo4j (7687) | Docker container in canonical compose | `neo4j:7687` |
| Ollama (11434) | Docker container in canonical compose | `ollama:11434` |

### Dockerfile Fixes Applied

| File | Fix |
|------|-----|
| `engines/ears-engine/Dockerfile` | `RUN mkdir -p /app/service/data/audio && chown -R nexus:nexus /app/service/data` |
| `engines/eyes-engine/Dockerfile` | `RUN mkdir -p /app/service/data/frames && chown -R nexus:nexus /app/service/data` |
| `engines/spine-engine/Dockerfile` | `RUN mkdir -p /data/nexus/documents /data/nexus/orchestrator/uploads && chown -R nexus:nexus /data/nexus` |
| `products/nexus-qa-orchestrator/Dockerfile` | `RUN mkdir -p /data/nexus/orchestrator/uploads && chown -R nexus:nexus /data/nexus` |
| `platform/api/Dockerfile` | Added `COPY cache.py /app/service/cache.py` (missing module) |

---

## 3. LLM Model Configuration

### What's Actually Running Now (Canonical — Tier 3 Only)

| Engine | Model | Size | Provider | Purpose |
|--------|-------|------|----------|---------|
| **Brain** | `llama3.2:1b` | 1.3 GB | Ollama | Meta-reasoning, session coordination |
| **Eyes** | `llama3.2-vision:11b` | 7.9 GB | Ollama | Screenshot/video frame visual analysis |
| Heart* | `llama3.2:1b` | — | Ollama | *Not running (non-canonical)* |

**Key facts:**
- Zero cloud API keys in any container (no ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)
- Zero Tier 1/2 environment variables set
- 100% local inference via Ollama — no data leaves the deployment

### Eyes Model Change (Completed Today)

| Before | After | Reason |
|--------|-------|--------|
| `llava:7b` (4.7 GB) | `llama3.2-vision:11b` (7.9 GB) | **Enterprise provenance** — Meta (US) origin vs. research lab models. Stronger OCR, document understanding. Enterprise security teams won't flag Meta Llama. |

**Files updated for model change:** `engines/eyes-engine/main.py`, `engines/eyes-engine/app/vision/__init__.py`, `docker-compose.canonical.yml`, `docker-compose.yml`, `.env.example`, `scripts/start_all_services.py`, `scripts/setup_ollama.py`, `infrastructure/docker/.env.tiers.example`, `infrastructure/helm/nexus-qa/values.yaml`, `engines/brain-engine/app/tier_manager/manager.py`, `tests/engines/test_eyes_modules.py`

### Full 3-Tier Model Matrix (Recommended Production Stack)

#### LLM Engines (use `TieredLLMRouter` with automatic failover)

| Engine | Tier 1 (Cloud PRIMARY) | Tier 2 (Cloud FALLBACK) | Tier 3 (LOCAL) |
|--------|----------------------|------------------------|---------------|
| **Brain** | Anthropic / `claude-opus-4-20250514` | OpenAI / `gpt-4o` | Ollama / `llama3.1:70b` |
| **Heart** | Anthropic / `claude-opus-4-20250514` | OpenAI / `gpt-4o` | Ollama / `llama3.1:70b` |

#### Planned LLM Engines (future integration)

| Engine | Tier 1 | Tier 2 | Tier 3 |
|--------|--------|--------|--------|
| **Eyes** | Google / `gemini-3-pro` | OpenAI / `gpt-4o` | Ollama / `llama3.2-vision:11b` (Meta) |
| **Hands** | OpenAI / `gpt-4o-mini` | Anthropic / `claude-haiku` | Ollama / `llama3.1:8b` |
| **Mouth** | Anthropic / `claude-haiku` | OpenAI / `gpt-4o-mini` | Ollama / `llama3.1:8b` |
| **Spine** | OpenAI / `text-embedding-3-large` | Ollama / `nomic-embed-text` | Ollama / `nomic-embed-text` |

#### Tooling Engines (non-LLM, library-based)

| Engine | Tier 1 | Tier 2 | Tier 3 |
|--------|--------|--------|--------|
| **Ears** | whisper-v3-large (local) | Azure AI Speech (cloud) | whisper-base (local) |
| **Shield** | Azure AI Language (cloud) | Presidio + GLiNER (local) | Presidio (local) |
| **Backbone** | Neo4j + Milvus (local) | Qdrant (local) | Neo4j + Milvus (local) |
| **Legs** | Playwright (local) | Selenium (local) | Playwright (local) |
| **Nerves** | API integrations (local) | API integrations (local) | API integrations (local) |

### SDK Tier Routing Logic (`sdk/nexus-sdk/nexus_sdk/llm/tiered.py`)

- `TieredProviderConfig.from_engine(engine_name)` reads `{ENGINE}_TIER{1,2,3}_PROVIDER` env vars
- Only instantiates tiers where the env var is set (absent = skipped)
- `TieredLLMRouter.generate()` tries tiers in order: Tier 1 → Tier 2 → Tier 3
- On failure, logs warning and falls to next tier
- If ALL tiers fail, raises `RuntimeError`
- **In canonical mode:** Only `TIER3` env vars are set → router has exactly 1 tier → always Ollama

### Security Note on Tier Protection

The canonical compose protects against cloud invocation purely via **environment configuration** (no Tier 1/2 env vars set). There is no hard code guard in the SDK that blocks cloud tiers in canonical mode. If someone adds `BRAIN_TIER1_PROVIDER=anthropic` + API key in the future, the router WILL prefer it. Consider adding an explicit guard if this is a compliance requirement.

---

## 4. Deployment Validation Results

### Deployment Check (14/14 PASSED)
```
✅ alembic migration at 009_semantic_completeness
✅ auth-service (8000) healthy
✅ shield (8001) healthy
✅ ears (8002) healthy
✅ eyes (8003) healthy
✅ spine (8009) healthy
✅ brain (8011) healthy
✅ gateway (8080) healthy
✅ platform-api (8091) healthy
✅ orchestrator (8100) healthy
✅ postgres schema validated
✅ spine DB connectivity
✅ gateway routing
✅ auth endpoint
```

### E2E Tests (13/13 PASSED, ~93 seconds)
```
✅ test_canonical_chain_listed
✅ test_non_canonical_chains_hidden
✅ test_session_lifecycle
✅ test_workflow_start_requires_session
✅ test_canonical_processing_upload
✅ test_non_canonical_chain_blocked
✅ test_media_fingerprint_dedup
✅ test_engine_health_all_canonical
✅ test_brain_llm_tiered
✅ test_eyes_visual_analyzer_real
✅ test_spine_document_store
✅ test_shield_pii_detection
✅ test_gateway_proxy_routing
```

---

## 5. Files Modified (Complete List)

### New Files Created
- `docker-compose.canonical.yml` — Canonical-only Docker Compose
- `client/src/productMode.ts` — Product mode configuration

### Configuration Files Modified
- `docker-compose.yml` — Added `NEXUS_PRODUCT_MODE`, `profiles: ["full"]`
- `.env` — `CLIENT_PORT=3000`
- `.env.example` — `EYES_OLLAMA_MODEL=llama3.2-vision:11b`
- `infrastructure/docker/.env.tiers.example` — Eyes T3 model updated
- `infrastructure/helm/nexus-qa/values.yaml` — Eyes model updated
- `sdk/nexus-sdk/pyproject.toml` — Added sqlalchemy, asyncpg, alembic, bcrypt, python-multipart

### Engine/Service Code Modified
- `engines/eyes-engine/main.py` — Default model → `llama3.2-vision:11b`
- `engines/eyes-engine/app/vision/__init__.py` — Default model → `llama3.2-vision:11b`
- `engines/brain-engine/app/tier_manager/manager.py` — Eyes T3 model updated
- `products/nexus-qa-orchestrator/app/main.py` — Canonical enforcement logic

### Frontend Modified
- `client/src/App.tsx` — Route filtering by product mode
- `client/src/layouts/AppLayout.tsx` — Sidebar filtering by product mode
- `client/src/stores/engineStore.ts` — Health polling scoped to active engines
- `client/src/services/api.ts` — API scoped to canonical endpoints

### Scripts Modified
- `scripts/start_all_services.py` — `--profile` flag, model defaults
- `scripts/validate_deployment.py` — `--profile canonical` support
- `scripts/setup_ollama.py` — Model list updated

### Dockerfiles Fixed
- `engines/ears-engine/Dockerfile` — Permission fix
- `engines/eyes-engine/Dockerfile` — Permission fix
- `engines/spine-engine/Dockerfile` — Permission fix
- `products/nexus-qa-orchestrator/Dockerfile` — Permission fix
- `platform/api/Dockerfile` — Missing file fix

### Tests Modified
- `tests/engines/test_eyes_modules.py` — Updated default model assertion

---

## 6. Open Items / Recommendations for Architect Review

1. **Heart Tier 1 model cost:** Currently spec'd as `claude-opus-4` — recommend downgrading to `claude-sonnet-4` (rule extraction is structured work, not open-ended reasoning)
2. **Spine Tier 2 = Tier 3:** Both `nomic-embed-text` — no real fallback diversity. Consider `bge-large-en-v1.5` for T2
3. **Brain/Heart Tier 3 = `llama3.1:70b`:** Requires ~40GB VRAM — may be impractical for on-prem. Current canonical deployment uses `llama3.2:1b` (1.3GB). Consider `llama3.1:8b` as a realistic T3 spec
4. **Config drift:** `.env.tiers.example` references `gpt-5` / `claude-opus-4-5`, while `manager.py` references `gpt-4o` / `claude-opus-4` — should align to one source of truth
5. **Canonical mode SDK guard:** No hard block on cloud tiers in canonical mode — consider adding if compliance requires it
6. **Old model cleanup:** `llava:7b` (4.7GB) still cached in Ollama — can be removed with `docker exec nexus-ollama ollama rm llava:7b`
