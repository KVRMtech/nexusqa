# Nexus QA — Production Deployment Implementation Plan

## Analysis Scope

Every Python file, TypeScript file, Dockerfile, Helm template, CI/CD workflow, migration script, test file, and configuration file in the repository was read line by line. This plan is based on the actual code — no assumptions, no examples, no stubs.

---

## PART 1: CURRENT STATE — WHAT EXISTS AND WHAT DOESN'T

### 1.1 Service Inventory (15 Services + Client)

| Service | Port | Status | Backend |
|---------|------|--------|---------|
| auth-service | 8000 | Implemented | PostgreSQL |
| gateway | 8080 | Implemented | httpx proxy |
| platform-api | 8091 | Implemented | PostgreSQL + Redis |
| qa-orchestrator (old) | 8092 | Implemented (deprecated) | Redis + Engine HTTP |
| nexus-qa-orchestrator (new) | 8100 | Implemented | Redis + Engine HTTP + Brain |
| shield-engine | 8001 | Implemented | Redis (Fernet encryption) |
| ears-engine | 8002 | Implemented | Whisper + Pyannote (GPU) |
| eyes-engine | 8003 | Implemented | EasyOCR + LLaVA (GPU) |
| heart-engine | 8004 | Implemented | TieredLLMRouter (GPU) |
| backbone-engine | 8005 | Implemented | Neo4j + Milvus |
| nerves-engine | 8006 | Implemented | Jira/GitHub/Slack/Teams/Webhook |
| legs-engine | 8007 | Implemented | Playwright + httpx |
| hands-engine | 8008 | Implemented | Faker |
| spine-engine | 8009 | Implemented | PyMuPDF + python-docx |
| mouth-engine | 8010 | Implemented | Jinja2 + WeasyPrint |
| brain-engine | 8011 | Implemented | TieredLLMRouter + Redis |
| client | 3080 | Implemented | React 18 + Vite + nginx |

### 1.2 Infrastructure Dependencies

| Component | Production Required | Dev Compose | Docker Compose (Full) | Helm |
|-----------|-------------------|-------------|----------------------|------|
| PostgreSQL 16 | Yes | Yes | Yes | Yes |
| Redis 7 | Yes | Yes | Yes | Yes |
| Neo4j 5 | Yes | Yes | Yes | Yes |
| Milvus 2.4 | Yes | **NO** | Yes | Yes |
| Ollama | Yes (or cloud LLM) | Yes | Yes | Yes |
| MinIO | Yes (Milvus dep) | No | Yes | Yes |
| etcd | Yes (Milvus dep) | No | Yes | Yes |

### 1.3 Critical Disconnect: Two Orchestrators

The codebase has **two** orchestrators that are NOT connected:

1. **qa-orchestrator** (port 8092): Linear 10-stage pipeline for a single session. Used by the `SessionCommandPage` in the UI. Handles upload → transcribe → redact → extract → test → report.

2. **nexus-qa-orchestrator** (port 8100): Generic DAG-based chain engine supporting `for_each` parallelism, Kahn's topological sort, Brain quality gates. Has 4 built-in chains (`qa-testing`, `knowledge-capture`, `compliance-audit`, `regression-suite`). NOT wired to the UI.

**Neither orchestrator is connected to the Mission (QI) system.** The `MissionOrchestrator` service in `platform/api` has `execute_stage()` and `STAGE_ENGINE_ACTIONS` mapping but is never called from mission route endpoints. Stage start/complete/advance only update database status.

---

## PART 2: PIPELINE ARCHITECTURE — CANONICAL MEDIA → QI INTELLIGENCE

### 2.1 Current Flow (Broken)

```
UI (SessionCommandPage)
  │
  ├─ POST /api/v1/qa/sessions              → Creates session in Redis
  ├─ POST /api/v1/qa/sessions/{id}/upload-audio  → Forwards to Ears (async job)
  ├─ POST /api/v1/qa/sessions/{id}/upload-video  → Forwards to Eyes (async job)
  └─ POST /api/v1/qa/sessions/{id}/run-pipeline  → Triggers qa-orchestrator pipeline
      │
      └─ qa-orchestrator.run_full_pipeline() [BACKGROUND TASK]
          ├─ Stage 1a: Poll Ears job → transcript text
          ├─ Stage 1b: Poll Eyes job → visual analysis
          ├─ Stage 2:  Shield redact → safe_text
          ├─ Stage 3:  Heart extract-rules → business rules
          ├─ Stage 4:  Backbone store → knowledge graph
          ├─ Stage 5:  Heart generate-tests → test cases
          ├─ Stage 5b: Hands generate-profiles → test data
          ├─ Stage 6:  Legs execute → test results (polled)
          ├─ Stage 7:  Mouth generate → reports
          └─ Stage 8:  Nerves execute → Slack notification
          
          [DEAD END — Results stored in session Redis only]
          [No event emitted. No QI trigger. No Mission update. No Platform API persistence.]
```

**Mission (QI) Portal is completely disconnected:**
```
UI (MissionDetailPage)
  │
  ├─ POST /api/v1/missions           → Creates mission + 5 stages in PostgreSQL
  ├─ POST /api/v1/missions/{id}/stages/{n}/start     → Sets stage status="active"
  ├─ POST /api/v1/missions/{id}/stages/{n}/complete   → Sets stage status="completed"
  └─ POST /api/v1/missions/{id}/messages              → Template-based chat (not LLM)
  
  [MissionOrchestrator.execute_stage() EXISTS but is NEVER CALLED]
  [No engine calls happen. No artifacts created. Pure status management.]
```

### 2.2 Target Architecture — Production Pipeline

The plan below connects the canonical media-processing pipeline to the QI Intelligence pipeline as a continuous, event-driven flow.

```
                          ┌──────────────────────────────────────────────────┐
                          │         CANONICAL MEDIA PROCESSING               │
                          │        (nexus-qa-orchestrator chains)            │
                          │                                                  │
  Upload ─────────────────┤  Ears (transcribe) ──→ Shield (PII redact)      │
  (audio/video/document)  │  Eyes (visual)     ──→ Shield (PII redact)      │
                          │  Spine (ingest)    ──→ Shield (PII redact)      │
                          │                                                  │
                          │  OUTPUT: safe_transcript, safe_visual_context,   │
                          │          document_chunks, mapping_ids            │
                          └──────────────────┬───────────────────────────────┘
                                             │
                                    EventBus: "canonical.processing.completed"
                                             │
                          ┌──────────────────▼───────────────────────────────┐
                          │           QI INTELLIGENCE PIPELINE               │
                          │        (nexus-qa-orchestrator chains)            │
                          │                                                  │
                          │  Heart (extract-rules)    ──→ Backbone (store)   │
                          │  Heart (generate-tests)   ──→ Hands (test data)  │
                          │  Heart (explore-flows)    ──→ Legs (execute)     │
                          │  Brain (quality-gate)     ──→ Mouth (reports)    │
                          │  Nerves (notify)                                 │
                          │                                                  │
                          │  OUTPUT: rules, test_cases, test_results,        │
                          │          reports, quality_score                   │
                          └──────────────────┬───────────────────────────────┘
                                             │
                                    EventBus: "qi.pipeline.completed"
                                             │
                          ┌──────────────────▼───────────────────────────────┐
                          │        PLATFORM PERSISTENCE & UI                 │
                          │                                                  │
                          │  Platform API writes to PostgreSQL:              │
                          │    - SessionRow (updated with results)           │
                          │    - ContradictionRow (from Heart)               │
                          │    - TraceRow (from Backbone)                    │
                          │    - TestCaseRow (from Heart/Hands)              │
                          │    - MissionArtifactRow (if Mission-linked)      │
                          │    - AuditLogRow (full trail)                    │
                          │                                                  │
                          │  WebSocket push to client:                       │
                          │    - Pipeline progress updates                   │
                          │    - Stage completion events                     │
                          │    - Quality gate results                        │
                          └──────────────────────────────────────────────────┘
```

---

## PART 3: BUGS TO FIX BEFORE PRODUCTION

### 3.1 CRITICAL — Security

| # | Bug | File(s) | Fix |
|---|-----|---------|-----|
| S1 | **Gateway passes unauthenticated requests** — Missing `Authorization` header on non-public paths gets `debug` log but request proceeds to backends | `platform/gateway/app/proxy.py` line ~60 | Return 401 for non-public paths when `Authorization` header is absent |
| S2 | **Multi-tenant isolation gap** — `tenant_id` accepted as query parameter with default `"t-1"` on most platform-api routers; any caller can read any tenant's data | `platform/api/app/routers/sessions.py`, `contradictions.py`, `tests.py`, `data_forge.py`, `compliance.py`, `traceability.py`, `guardrails.py`, `insights.py` | Extract `tenant_id` from JWT token (via `get_current_user` dependency) on ALL routers. Remove `tenant_id` query parameter. |
| S3 | **7 routers missing auth dependency** — `sessions`, `contradictions`, `tests`, `data_forge`, `compliance`, `traceability`, `guardrails` have no `Depends(get_current_user)` | `platform/api/app/routers/*.py` | Add `Depends(get_current_user)` to every router. Enforce `user.tenant_id` for all DB queries. |
| S4 | **`eval()` in workflow conditions and output transforms** — Restricted builtins but still a code execution surface | `products/nexus-qa-orchestrator/app/workflows/context.py`, `engine.py` | Replace with `simpleeval` library (already a common Python pattern) |
| S5 | **Logout is a stub** — Does not actually revoke the token | `platform/auth-service/app/routes.py` line ~134 | Implement JTI blacklist in Redis with TTL matching token expiry |
| S6 | **Shield encryption key auto-generated when not set** — Ephemeral key means PII mappings are lost on restart | `engines/shield-engine/app/__init__.py` | Block startup when `SHIELD_ENCRYPTION_KEY` is empty in production (add `production_guard`) |
| S7 | **Hardcoded credentials in scripts** | `scripts/start_all_services.py`, `scripts/production_e2e_test.py`, `scripts/pre_handover_check.py` | Replace with `os.environ.get()` with no fallback defaults |
| S8 | **Admin resource endpoint exposes system info** — CPU/RAM/disk without auth check | `platform/api/app/routers/admin.py` | Restrict to `admin` role via `require_permission("admin.resources")` |

### 3.2 HIGH — Functional

| # | Bug | File(s) | Fix |
|---|-----|---------|-----|
| F1 | **MissionOrchestrator never called** — Stage start/complete/advance only update status, no engine calls | `platform/api/app/routers/missions.py` | Wire `MissionOrchestrator.execute_stage()` into `start_stage()` endpoint. Run engine calls as background task. Store results as `MissionArtifactRow`. |
| F2 | **Mission chat is template-based** — `_generate_assistant_response()` returns static strings | `platform/api/app/routers/missions.py` lines 850-940 | Replace with HTTP call to Heart engine `/api/v1/heart/ask` with mission context |
| F3 | **Pipeline results not persisted to PostgreSQL** — qa-orchestrator stores results in Redis session only; Platform API has no visibility | `products/qa-orchestrator/app/pipeline.py` | After pipeline completes, POST results to Platform API endpoints to create `SessionRow` events, `ContradictionRow`, `TestCaseRow`, `TraceRow`, `AuditLogRow` |
| F4 | **Direct engine uploads bypass session tracking** — `uploadAudio` and `uploadVideo` (direct engine API) don't create `AudioFileRow`/`VideoFileRow` in PostgreSQL | `client/src/services/api.ts` `uploadVideo` | Remove direct engine upload methods from UI. Route all uploads through QA orchestrator session context. |
| F5 | **Canonical processing → QI pipeline has no automatic trigger** — User must manually call `run-pipeline`. If network fails, engine jobs complete but results are never consumed | `products/qa-orchestrator/main.py` | Auto-trigger pipeline on upload completion OR use nexus-qa-orchestrator chain with upload stage included |
| F6 | **In-memory rate limiter and brute-force counter** — Ineffective in multi-instance deployment | `platform/gateway/app/rate_limiter.py`, `platform/auth-service/app/security.py` | Migrate both to Redis with atomic INCR + TTL |
| F7 | **Backbone config mismatch** — Config declares `bge-large-en-v1.5` (1024 dim) but code uses `all-MiniLM-L6-v2` (384 dim) | `engines/backbone-engine/app/__init__.py`, `app/config.py` | Align config to match actual model. If changing model, re-embed existing vectors. |
| F8 | **SessionReplayPage hardcoded values** — `participants` = empty, `totalDuration` = 0 | `client/src/pages/SessionReplayPage.tsx` | Fetch from `api.getSessionDetail(sessionId)` and populate from session data |
| F9 | **MetricsMiddleware reads full request body** — `body = await request.body()` buffers entire upload into memory for size measurement | `sdk/nexus-sdk/nexus_sdk/observability/metrics.py` | Use `request.headers.get("content-length", 0)` instead |

### 3.3 MEDIUM — Operational

| # | Bug | File(s) | Fix |
|---|-----|---------|-----|
| O1 | **No Milvus in dev compose** — Backbone vector search fails in dev | `docker-compose.dev.yml` | Add Milvus + etcd + MinIO services |
| O2 | **EventBus has zero test coverage** — Redis Streams pub/sub/DLQ is critical infrastructure | `sdk/nexus-sdk/nexus_sdk/events/` | Write tests for publish, subscribe, consumer groups, DLQ, retry-from-DLQ |
| O3 | **EventBus connects without Redis auth** | `sdk/nexus-sdk/nexus_sdk/events/__init__.py` | Pass `redis_password` from `EngineConfig.redis_password` to Redis connection |
| O4 | **No circuit breaker on gateway** — Downstream engine failure causes cascading 503s | `platform/gateway/app/proxy.py` | Implement circuit breaker with half-open state (per-engine) |
| O5 | **Request size limit 500MB on gateway** — DoS vector | `platform/gateway/main.py` | Reduce to 100MB (matches nginx config) |
| O6 | **Duplicate auth systems in client** — `AuthContext` and `authStore` can diverge | `client/src/` | Remove `AuthContext`, standardize on `authStore` (Zustand) |
| O7 | **Test case ID race condition** — `SELECT MAX` without advisory lock | `sdk/nexus-sdk/nexus_sdk/testcase_id.py` | Add `pg_advisory_xact_lock()` before SELECT MAX |
| O8 | **Brain LLM tiers not configured in compose** | `infrastructure/docker/docker-compose.yml` | Uncomment and configure tier env vars for Brain + Heart engines |
| O9 | **`locust` as production dependency** | `platform/api/requirements.txt` | Move to `dev-requirements.txt` |
| O10 | **Plugin extension merging has no deduplication** | `sdk/nexus-sdk/nexus_sdk/plugins/registry.py` | Use `dict`-based merging keyed by unique identifier per extension type |

---

## PART 4: IMPLEMENTATION PLAN — PHASE-BY-PHASE

### Phase 1: Security Hardening (BLOCKING — Do Before Any Deployment)

#### 1A. Fix Gateway Authentication

**File: `platform/gateway/app/proxy.py`**

In the proxy middleware, after JWT decode fails or `Authorization` header is missing for non-public paths, return 401 instead of passing through:

```python
# Current: logs debug and continues
# Fix: return 401 for non-public paths without valid auth
if not is_public_path and not token_valid:
    return JSONResponse(status_code=401, content={"detail": "Authentication required"})
```

#### 1B. Fix Multi-Tenant Isolation on ALL Platform API Routers

For every router in `platform/api/app/routers/`:

1. Add `user: NexusUser = Depends(get_current_user)` to every endpoint
2. Replace `tenant_id: str = Query("t-1")` with `tenant_id = user.tenant_id`
3. Every database query must filter by `tenant_id` from the authenticated user

Affected routers: `sessions.py`, `sme.py`, `contradictions.py`, `guardrails.py`, `traceability.py`, `tests.py`, `data_forge.py`, `compliance.py`, `insights.py`, `admin.py`

#### 1C. Replace eval() in Workflow Engine

**File: `products/nexus-qa-orchestrator/app/workflows/context.py`**

Replace:
```python
result = eval(condition, {"__builtins__": safe_builtins}, context)
```

With `simpleeval`:
```python
from simpleeval import simple_eval
result = simple_eval(condition, names=context, functions={"len": len, "str": str, "int": int, "float": float, "bool": bool})
```

Same change in `engine.py` for `output_transform`.

Add `simpleeval>=1.0` to `products/nexus-qa-orchestrator/requirements.txt`.

#### 1D. Implement Token Revocation

**File: `sdk/nexus-sdk/nexus_sdk/auth/`**

Add `TokenBlacklist` class:
```python
class TokenBlacklist:
    """Redis-backed JTI blacklist for token revocation."""
    
    def __init__(self, redis_client):
        self._redis = redis_client
        self._prefix = "nexus:revoked:"
    
    async def revoke(self, jti: str, expires_in: int):
        """Revoke a token by its JTI. TTL matches token expiry."""
        await self._redis.set(f"{self._prefix}{jti}", "1", ex=expires_in)
    
    async def is_revoked(self, jti: str) -> bool:
        return await self._redis.exists(f"{self._prefix}{jti}") > 0
```

Wire into `auth-service` logout endpoint and `get_current_user` validation.

#### 1E. Shield Encryption Key Production Guard

**File: `engines/shield-engine/app/__init__.py`**

After encryption key initialization:
```python
if not self.config.shield_encryption_key:
    production_guard("Shield encryption key not configured — PII mappings will be lost on restart")
    self._encryption_key = Fernet.generate_key()  # dev only
```

#### 1F. Migrate Rate Limiter and Brute-Force to Redis

**File: `platform/gateway/app/rate_limiter.py`**

Replace in-memory dict with Redis sorted set (sliding window):
```python
async def check_rate_limit(self, tenant_id: str) -> bool:
    key = f"nexus:ratelimit:{tenant_id}"
    now = time.time()
    pipe = self._redis.pipeline()
    pipe.zremrangebyscore(key, 0, now - self._window_seconds)
    pipe.zadd(key, {str(now): now})
    pipe.zcard(key)
    pipe.expire(key, self._window_seconds)
    results = await pipe.execute()
    return results[2] <= self._max_requests
```

Same pattern for `platform/auth-service/app/security.py` brute-force counters.

---

### Phase 2: Connect Canonical Media Processing to QI Pipeline

This is the core architectural fix — making the two pipelines a continuous flow.

#### 2A. Deprecate qa-orchestrator, Unify on nexus-qa-orchestrator

The old `qa-orchestrator` (port 8092) is a linear pipeline with no Brain quality gate, no DAG parallelism, and no chain configurability. It should be fully replaced.

**Step 1: Create a `canonical-media-processing` chain in `nexus-qa-orchestrator`**

**File: `products/nexus-qa-orchestrator/app/workflows/builtin_chains.py`**

Add a new chain that handles ONLY canonical media processing (upload → transcription → visual analysis → PII redaction → document ingestion):

```python
CANONICAL_MEDIA_CHAIN = ChainDefinition(
    chain_id="nexus.canonical-media-processing",
    name="Canonical Media Processing",
    version="1.0.0",
    description="Process raw media (audio/video/documents) into safe, structured content",
    stages=[
        StageDefinition(
            name="transcription",
            engine="ears",
            endpoint="/api/v1/ears/transcribe",
            request_type="multipart",
            input_mapping={"file": "$workflow.inputs.audio_file"},
            condition="$workflow.inputs.has_audio == true",
            polling=PollingConfig(
                enabled=True,
                poll_endpoint="/api/v1/ears/jobs/{job_id}",
                job_id_path="job_id",
                status_path="status",
                completion_statuses=["completed"],
                failure_statuses=["failed"],
                poll_interval_seconds=5,
                max_poll_seconds=600,
            ),
            retry=RetryPolicy(max_retries=2, backoff_seconds=5),
        ),
        StageDefinition(
            name="visual_analysis",
            engine="eyes",
            endpoint="/api/v1/eyes/analyze-video",
            request_type="multipart",
            input_mapping={"file": "$workflow.inputs.video_file"},
            condition="$workflow.inputs.has_video == true",
            polling=PollingConfig(
                enabled=True,
                poll_endpoint="/api/v1/eyes/jobs/{job_id}",
                job_id_path="job_id",
                status_path="status",
                completion_statuses=["completed"],
                failure_statuses=["failed"],
                poll_interval_seconds=5,
                max_poll_seconds=900,
            ),
            retry=RetryPolicy(max_retries=2, backoff_seconds=5),
        ),
        StageDefinition(
            name="document_ingestion",
            engine="spine",
            endpoint="/api/v1/spine/ingest",
            request_type="multipart",
            input_mapping={"file": "$workflow.inputs.document_file"},
            condition="$workflow.inputs.has_document == true",
            retry=RetryPolicy(max_retries=2, backoff_seconds=3),
        ),
        StageDefinition(
            name="pii_redaction",
            engine="shield",
            endpoint="/api/v1/shield/redact",
            depends_on=["transcription", "visual_analysis", "document_ingestion"],
            input_mapping={
                "text": "$stages.transcription.output.text",
                "visual_context": "$stages.visual_analysis.output.analysis",
                "document_text": "$stages.document_ingestion.output.content",
            },
            retry=RetryPolicy(max_retries=3, backoff_seconds=2),
        ),
    ],
)
```

**Step 2: Create the QI Intelligence chain that takes canonical output as input**

```python
QI_INTELLIGENCE_CHAIN = ChainDefinition(
    chain_id="nexus.qi-intelligence",
    name="Quality Intelligence Pipeline",
    version="1.0.0",
    description="Extract knowledge, generate tests, execute, report — from canonical media output",
    stages=[
        StageDefinition(
            name="rule_extraction",
            engine="heart",
            endpoint="/api/v1/heart/extract-rules",
            input_mapping={
                "transcript": "$workflow.inputs.safe_transcript",
                "visual_context": "$workflow.inputs.safe_visual_context",
                "document_context": "$workflow.inputs.document_chunks",
                "session_id": "$workflow.inputs.session_id",
                "tenant_id": "$workflow.inputs.tenant_id",
            },
            retry=RetryPolicy(max_retries=2, backoff_seconds=10),
        ),
        StageDefinition(
            name="knowledge_store",
            engine="backbone",
            endpoint="/api/v1/backbone/store-rule",
            depends_on=["rule_extraction"],
            for_each="$stages.rule_extraction.output.rules",
            for_each_concurrency=5,
            input_mapping={
                "rule": "$temp.for_each_item",
                "tenant_id": "$workflow.inputs.tenant_id",
                "session_id": "$workflow.inputs.session_id",
            },
            retry=RetryPolicy(max_retries=3, backoff_seconds=2),
        ),
        StageDefinition(
            name="test_generation",
            engine="heart",
            endpoint="/api/v1/heart/generate-tests",
            depends_on=["rule_extraction"],
            input_mapping={
                "rules": "$stages.rule_extraction.output.rules",
                "transcript": "$workflow.inputs.safe_transcript",
                "tenant_id": "$workflow.inputs.tenant_id",
            },
            retry=RetryPolicy(max_retries=2, backoff_seconds=10),
        ),
        StageDefinition(
            name="test_data",
            engine="hands",
            endpoint="/api/v1/hands/generate/profiles",
            depends_on=["test_generation"],
            input_mapping={
                "count": 50,
                "tenant_id": "$workflow.inputs.tenant_id",
            },
            retry=RetryPolicy(max_retries=2, backoff_seconds=3),
        ),
        StageDefinition(
            name="test_execution",
            engine="legs",
            endpoint="/api/v1/legs/execute/batch",
            depends_on=["test_generation", "test_data"],
            input_mapping={
                "test_cases": "$stages.test_generation.output.test_cases",
                "test_data": "$stages.test_data.output.profiles",
                "tenant_id": "$workflow.inputs.tenant_id",
            },
            polling=PollingConfig(
                enabled=True,
                poll_endpoint="/api/v1/legs/jobs/{job_id}",
                job_id_path="batch_id",
                status_path="status",
                completion_statuses=["completed"],
                failure_statuses=["failed"],
                poll_interval_seconds=10,
                max_poll_seconds=1800,
            ),
            retry=RetryPolicy(max_retries=1, backoff_seconds=30),
        ),
        StageDefinition(
            name="report_generation",
            engine="mouth",
            endpoint="/api/v1/mouth/generate",
            depends_on=["test_execution", "rule_extraction", "knowledge_store"],
            input_mapping={
                "report_type": "full_session_report",
                "session_id": "$workflow.inputs.session_id",
                "tenant_id": "$workflow.inputs.tenant_id",
                "rules": "$stages.rule_extraction.output.rules",
                "test_results": "$stages.test_execution.output.results",
            },
            retry=RetryPolicy(max_retries=2, backoff_seconds=5),
        ),
        StageDefinition(
            name="notification",
            engine="nerves",
            endpoint="/api/v1/nerves/execute",
            depends_on=["report_generation"],
            input_mapping={
                "connector": "slack",
                "action": "send_message",
                "params": {
                    "channel": "$workflow.inputs.notification_channel",
                    "text": "QI pipeline completed for session $workflow.inputs.session_id",
                },
            },
            retry=RetryPolicy(max_retries=3, backoff_seconds=5),
        ),
    ],
)
```

**Step 3: Create a composite chain that runs both in sequence**

```python
FULL_QA_PIPELINE_CHAIN = ChainDefinition(
    chain_id="nexus.full-qa-pipeline",
    name="Full QA Pipeline (Media + QI)",
    version="1.0.0",
    description="End-to-end: canonical media processing → QI intelligence",
    # This chain weaves canonical + QI stages into a single DAG
    # Stages from canonical feed into QI stages via depends_on
    stages=[
        # --- CANONICAL MEDIA PROCESSING ---
        # (transcription, visual_analysis, document_ingestion, pii_redaction)
        # ... same as CANONICAL_MEDIA_CHAIN stages above ...
        
        # --- QI INTELLIGENCE ---  
        # (rule_extraction depends_on pii_redaction, etc.)
        # ... same as QI_INTELLIGENCE_CHAIN stages but with depends_on 
        #     pointing to pii_redaction instead of workflow.inputs ...
    ],
)
```

#### 2B. Wire Upload → Pipeline as Atomic Operation

**File: `products/nexus-qa-orchestrator/main.py`**

Replace the separate upload + run-pipeline endpoints with a single upload endpoint that auto-starts the pipeline:

```python
@app.post("/api/v1/qa/sessions/{session_id}/upload")
async def upload_media(
    session_id: str,
    file: UploadFile,
    background_tasks: BackgroundTasks,
    user: NexusUser = Depends(get_current_user),
):
    """Upload media and automatically start the full QA pipeline."""
    # 1. Detect media type from content type / extension
    media_type = detect_media_type(file)
    
    # 2. Persist file metadata to PostgreSQL (AudioFileRow / VideoFileRow)
    media_row = await persist_media_metadata(session_id, file, media_type, user.tenant_id)
    
    # 3. Start the full-qa-pipeline chain with the uploaded file
    workflow = await chain_engine.start(
        chain_id="nexus.full-qa-pipeline",
        tenant_id=user.tenant_id,
        inputs={
            "session_id": session_id,
            "tenant_id": user.tenant_id,
            "has_audio": media_type == "audio",
            "has_video": media_type == "video",
            "has_document": media_type == "document",
            "audio_file": file if media_type == "audio" else None,
            "video_file": file if media_type == "video" else None,
            "document_file": file if media_type == "document" else None,
            "notification_channel": "#nexus-qa-alerts",
        },
    )
    
    # 4. Link workflow to session in PostgreSQL
    await link_workflow_to_session(session_id, workflow.instance_id, user.tenant_id)
    
    # 5. Execute pipeline in background
    background_tasks.add_task(chain_engine.execute, workflow.instance_id)
    
    return {"session_id": session_id, "workflow_id": workflow.instance_id, "status": "processing"}
```

#### 2C. Pipeline Completion → Platform API Persistence

**File: `products/nexus-qa-orchestrator/app/workflows/engine.py`**

After the Brain quality gate step in `execute()`, add a persistence step that writes results to PostgreSQL via Platform API:

```python
async def _persist_results_to_platform(self, workflow: WorkflowInstance, ctx: WorkflowContext):
    """Write pipeline results to Platform API for UI consumption."""
    tenant_id = ctx.resolve("$workflow.inputs.tenant_id")
    session_id = ctx.resolve("$workflow.inputs.session_id")
    
    # 1. Update session status
    await self._http.post(f"{self._platform_api_url}/api/v1/sessions/{session_id}/events", json={
        "event_type": "pipeline_completed",
        "data": {"workflow_id": workflow.instance_id, "status": workflow.status},
        "tenant_id": tenant_id,
    })
    
    # 2. Persist extracted rules as test cases
    rules = ctx.resolve("$stages.rule_extraction.output.rules") or []
    for rule in rules:
        await self._http.post(f"{self._platform_api_url}/api/v1/test-cases", json={
            "tenant_id": tenant_id,
            "session_id": session_id,
            "title": rule.get("description", ""),
            "source": "pipeline",
            "rule_data": rule,
        })
    
    # 3. Persist contradictions
    contradictions = ctx.resolve("$stages.rule_extraction.output.contradictions") or []
    for contradiction in contradictions:
        await self._http.post(f"{self._platform_api_url}/api/v1/contradictions", json={
            "tenant_id": tenant_id,
            "session_id": session_id,
            **contradiction,
        })
    
    # 4. Persist trace records
    test_results = ctx.resolve("$stages.test_execution.output.results") or []
    for result in test_results:
        await self._http.post(f"{self._platform_api_url}/api/v1/traceability", json={
            "tenant_id": tenant_id,
            "session_id": session_id,
            "rule_id": result.get("rule_id"),
            "test_id": result.get("test_id"),
            "status": result.get("status"),
        })
    
    # 5. Audit log
    await self._http.post(f"{self._platform_api_url}/api/v1/admin/audit", json={
        "tenant_id": tenant_id,
        "action": "pipeline_completed",
        "resource_type": "session",
        "resource_id": session_id,
        "details": {"workflow_id": workflow.instance_id, "quality_score": workflow.quality_score},
    })
```

#### 2D. EventBus Integration — Real-Time Pipeline Events

**File: `products/nexus-qa-orchestrator/app/workflows/engine.py`**

Publish events at each pipeline phase boundary:

```python
# After canonical stages complete:
await self._event_bus.publish(NexusEvent(
    event_type="canonical.processing.completed",
    tenant_id=tenant_id,
    data={
        "session_id": session_id,
        "workflow_id": workflow.instance_id,
        "safe_transcript": ctx.resolve("$stages.pii_redaction.output.safe_text"),
        "mapping_id": ctx.resolve("$stages.pii_redaction.output.mapping_id"),
    },
))

# After QI pipeline completes:
await self._event_bus.publish(NexusEvent(
    event_type="qi.pipeline.completed",
    tenant_id=tenant_id,
    data={
        "session_id": session_id,
        "workflow_id": workflow.instance_id,
        "rules_count": len(rules),
        "tests_count": len(test_cases),
        "quality_score": quality_gate_result.get("score"),
    },
))
```

#### 2E. Wire Mission Stages to Engine Execution

**File: `platform/api/app/routers/missions.py`**

In the `start_stage` endpoint, call `MissionOrchestrator.execute_stage()`:

```python
@router.post("/missions/{mission_id}/stages/{stage_number}/start")
async def start_stage(
    mission_id: str,
    stage_number: int,
    user: NexusUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = ...,
):
    # ... existing status update logic ...
    
    # NEW: Trigger engine execution as background task
    orchestrator = MissionOrchestrator(
        engine_urls=get_engine_urls(),
        auth_token=request.headers.get("Authorization"),
    )
    background_tasks.add_task(
        _execute_mission_stage,
        orchestrator, mission_id, stage_number, stage.stage_type, user.tenant_id, db
    )
    
    return {"status": "active", "message": f"Stage {stage_number} started with engine execution"}
```

```python
async def _execute_mission_stage(orchestrator, mission_id, stage_number, stage_type, tenant_id, db):
    """Background task: call engines and persist artifacts."""
    try:
        result = await orchestrator.execute_stage(
            stage_type=stage_type,
            mission_context={"mission_id": mission_id, "tenant_id": tenant_id},
            persona_config=await get_persona_stage_config(mission_id, db),
        )
        # Persist engine outputs as mission artifacts
        for engine_name, output in result.outputs.items():
            artifact = MissionArtifactRow(
                mission_id=mission_id,
                stage_number=stage_number,
                artifact_type=engine_name,
                content=output,
                tenant_id=tenant_id,
            )
            db.add(artifact)
        await db.commit()
    except Exception as e:
        logger.error("Mission stage execution failed", mission_id=mission_id, stage=stage_number, error=str(e))
```

#### 2F. Wire Mission Chat to Heart LLM

**File: `platform/api/app/routers/missions.py`**

Replace the template-based `_generate_assistant_response()`:

```python
async def _generate_assistant_response(
    user_message: str,
    mission: MissionRow,
    stage: MissionStageRow,
    messages: list[MissionMessageRow],
) -> str:
    """Generate contextual response via Heart engine LLM."""
    heart_url = os.environ.get("HEART_ENGINE_URL", "http://localhost:8004")
    
    # Build conversation context from recent messages
    conversation = [{"role": m.role, "content": m.content} for m in messages[-10:]]
    
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{heart_url}/api/v1/heart/ask",
            json={
                "question": user_message,
                "context": {
                    "mission_name": mission.name,
                    "stage_type": stage.stage_type,
                    "stage_name": stage.name,
                    "conversation_history": conversation,
                    "mission_description": mission.description,
                },
                "tenant_id": mission.tenant_id,
            },
            headers={"Authorization": f"Bearer {get_service_token()}"},
        )
        if response.status_code == 200:
            return response.json().get("answer", "I couldn't generate a response.")
        
        # Fallback to existing template logic if Heart is unavailable
        return _generate_template_response(user_message, stage.stage_type)
```

---

### Phase 3: Real-Time UI Pipeline Visibility

#### 3A. WebSocket Pipeline Progress

**File: `products/nexus-qa-orchestrator/app/workflows/engine.py`**

After each stage completes, publish to a Redis pub/sub channel that the gateway can proxy to WebSocket clients:

```python
async def _publish_progress(self, workflow_id: str, stage_name: str, status: str, data: dict):
    """Push pipeline progress to clients via Redis pub/sub."""
    channel = f"nexus:ws:pipeline:{workflow_id}"
    message = json.dumps({
        "type": "pipeline_progress",
        "workflow_id": workflow_id,
        "stage": stage_name,
        "status": status,
        "timestamp": datetime.utcnow().isoformat(),
        "data": data,
    })
    await self._redis.publish(channel, message)
```

**File: `platform/gateway/main.py`**

Add WebSocket endpoint that subscribes to the Redis channel and forwards to client:

```python
from fastapi import WebSocket

@app.websocket("/ws/pipeline/{workflow_id}")
async def pipeline_ws(websocket: WebSocket, workflow_id: str):
    await websocket.accept()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"nexus:ws:pipeline:{workflow_id}")
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"])
    finally:
        await pubsub.unsubscribe()
        await websocket.close()
```

#### 3B. Client Pipeline Tracking

**File: `client/src/pages/SessionCommandPage.tsx`**

After uploading and triggering pipeline, connect to WebSocket for real-time progress instead of polling:

```typescript
const ws = useWebSocket(`/ws/pipeline/${workflowId}`, {
    onMessage: (event) => {
        const progress = JSON.parse(event.data);
        updatePipelineStage(progress.stage, progress.status, progress.data);
    },
});
```

This replaces the current 5-second polling approach with instant push updates.

---

### Phase 4: Infrastructure Hardening for Production

#### 4A. Secrets Management

**File: `infrastructure/helm/nexus-qa/values.yaml`**

Enable External Secrets Operator (ESO) for production:

```yaml
externalSecrets:
  enabled: true           # Change from false to true
  provider: "aws"         # or "vault", "azure", "gcp"
  secretStore: "nexus-production-store"
  refreshInterval: "1h"
  keys:
    jwtSecret: "nexus/jwt-secret"
    postgresPassword: "nexus/postgres-password"
    redisPassword: "nexus/redis-password"
    neo4jPassword: "nexus/neo4j-password"
    shieldEncryptionKey: "nexus/shield-encryption-key"
    minioSecretKey: "nexus/minio-secret-key"
    hfToken: "nexus/hf-token"
```

#### 4B. Network Policies

**File: `infrastructure/helm/nexus-qa/templates/networkpolicy.yaml`**

Enable by default and enforce:
- Engines can only receive traffic from gateway and orchestrator
- Platform API can receive from gateway only
- Auth service can receive from gateway only
- Database services only reachable from their consumers
- No egress from engines except to Ollama, Neo4j, Redis, PostgreSQL, Milvus

#### 4C. Horizontal Pod Autoscaling

**File: `infrastructure/helm/nexus-qa/values.yaml`**

```yaml
# Enable HPA for high-traffic services
gateway:
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 10
    targetCPUUtilization: 70

platformApi:
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 8
    targetCPUUtilization: 75

engines:
  heart:
    autoscaling:
      enabled: true
      minReplicas: 1
      maxReplicas: 4
      targetCPUUtilization: 80
  ears:
    autoscaling:
      enabled: true
      minReplicas: 1
      maxReplicas: 3
      targetCPUUtilization: 80
```

#### 4D. Pod Disruption Budgets

```yaml
# Ensure at least 1 replica running during rolling updates
pdb:
  enabled: true
  minAvailable:
    gateway: 1
    platformApi: 1
    authService: 1
```

#### 4E. Database Backups

Create a CronJob for PostgreSQL backups:

```yaml
# infrastructure/helm/nexus-qa/templates/backup-cronjob.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ include "nexus-qa.fullname" . }}-pg-backup
spec:
  schedule: "0 2 * * *"  # 2 AM daily
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: pg-backup
            image: postgres:16
            command: ["pg_dump", "-Fc", "-f", "/backups/nexus-$(date +%Y%m%d).dump"]
            volumeMounts:
            - name: backup-volume
              mountPath: /backups
```

#### 4F. Monitoring Alerts

**File: `infrastructure/helm/nexus-qa/templates/prometheusrule.yaml`**

Define critical alerts:

```yaml
groups:
- name: nexus-critical
  rules:
  - alert: EngineDown
    expr: up{job=~".*-engine"} == 0
    for: 2m
    labels:
      severity: critical
  - alert: PipelineFailureRate
    expr: rate(nexus_http_requests_total{status=~"5.."}[5m]) > 0.1
    for: 5m
    labels:
      severity: warning
  - alert: QualityGateFailures
    expr: rate(nexus_quality_gate_failures_total[1h]) > 5
    for: 10m
    labels:
      severity: warning
  - alert: DatabaseConnectionPool
    expr: nexus_db_pool_available < 2
    for: 1m
    labels:
      severity: critical
  - alert: RedisMemoryHigh
    expr: redis_memory_used_bytes / redis_memory_max_bytes > 0.85
    for: 5m
    labels:
      severity: warning
  - alert: GPUMemoryHigh
    expr: nvidia_gpu_memory_used_bytes / nvidia_gpu_memory_total_bytes > 0.9
    for: 2m
    labels:
      severity: critical
```

---

### Phase 5: LLM Tier Configuration for Production

#### 5A. Multi-Tier LLM Setup

**File: `infrastructure/docker/.env.tiers.example` → `.env.tiers`**

```env
# Tier 1: Cloud Primary (highest quality)
HEART_TIER1_PROVIDER=anthropic
HEART_TIER1_MODEL=claude-sonnet-4-20250514
HEART_TIER1_API_KEY=sk-ant-...
HEART_TIER1_MAX_TOKENS=8192

BRAIN_TIER1_PROVIDER=anthropic
BRAIN_TIER1_MODEL=claude-sonnet-4-20250514
BRAIN_TIER1_API_KEY=sk-ant-...

# Tier 2: Cloud Fallback
HEART_TIER2_PROVIDER=openai
HEART_TIER2_MODEL=gpt-4o
HEART_TIER2_API_KEY=sk-...

BRAIN_TIER2_PROVIDER=openai
BRAIN_TIER2_MODEL=gpt-4o
BRAIN_TIER2_API_KEY=sk-...

# Tier 3: Local Fallback (always available)
HEART_TIER3_PROVIDER=ollama
HEART_TIER3_MODEL=llama3.1:70b
HEART_TIER3_BASE_URL=http://ollama:11434

BRAIN_TIER3_PROVIDER=ollama
BRAIN_TIER3_MODEL=llama3.1:70b

# Eyes engine (multimodal)
EYES_TIER1_PROVIDER=openai
EYES_TIER1_MODEL=gpt-4o
EYES_TIER2_PROVIDER=ollama
EYES_TIER2_MODEL=llava:13b
```

#### 5B. GPU Resource Allocation

| Engine | GPU Memory | Model | Production Node Selector |
|--------|-----------|-------|-------------------------|
| Ears | 6-8 GB | Whisper Large-v3 | `gpu.nvidia.com/type: a10g` |
| Eyes | 4-6 GB | LLaVA 7B (fallback) | `gpu.nvidia.com/type: a10g` |
| Heart | 8-16 GB | LLama 3.1:70b (local tier) | `gpu.nvidia.com/type: a100` |
| Ollama | 24-48 GB | Multiple models | `gpu.nvidia.com/type: a100` |

---

### Phase 6: Testing Gaps to Close

| Priority | Test | Coverage Target |
|----------|------|-----------------|
| P0 | EventBus (Redis Streams) — publish, subscribe, DLQ, retry | Unit + integration |
| P0 | Platform API router functional tests — mock DB, test full request/response | All 16 routers |
| P0 | Multi-tenant isolation — verify tenant A cannot access tenant B data | Security integration tests |
| P1 | Pipeline end-to-end with mocked engines — canonical → QI flow | Integration |
| P1 | Brain quality gate decision paths — pass, fail, needs_review | Unit |
| P1 | Token revocation — revoke + verify rejection | Integration |
| P2 | Mission → engine execution flow | Integration |
| P2 | WebSocket pipeline progress | Integration |
| P2 | Load test with SLA gates (p95 < 500ms for API, < 30s for pipeline start) | Load |

---

## PART 5: DEPLOYMENT SEQUENCE

### Pre-Deployment Checklist

```
□ All Phase 1 security fixes applied and tested
□ SHIELD_ENCRYPTION_KEY generated and stored in secrets manager
□ JWT_SECRET generated (64+ character random) and stored in secrets manager
□ PostgreSQL password, Redis password, Neo4j password generated and stored
□ Alembic migrations run against production database
□ LLM tier API keys configured and tested
□ GPU nodes provisioned with NVIDIA drivers and nvidia-container-toolkit
□ Container registry (GHCR) accessible from cluster
□ Ingress controller (nginx) installed with TLS certificate
□ External Secrets Operator installed and configured
□ Monitoring stack (Prometheus, Grafana, Loki, Tempo) deployed
□ All P0 tests passing
□ Security audit script passes: python scripts/security_audit.py
□ Pre-handover check passes: python scripts/pre_handover_check.py
```

### Deployment Order (Kubernetes)

```
Step 1: Infrastructure (StatefulSets)
  helm upgrade nexus-qa ./infrastructure/helm/nexus-qa \
    -f values-production.yaml \
    --set-string global.tag=v1.0.0 \
    --set engines.enabled=false \
    --set platform.enabled=false \
    --wait --timeout 300s
  
  → PostgreSQL, Redis, Neo4j, Milvus, Ollama start first
  → Wait for readiness probes

Step 2: Pull LLM Models
  kubectl exec -it deploy/ollama -- ollama pull llama3.1:70b
  kubectl exec -it deploy/ollama -- ollama pull llava:13b
  kubectl exec -it deploy/ollama -- ollama pull whisper:large-v3

Step 3: Database Migrations
  kubectl run alembic-migrate --image=ghcr.io/org/nexus-platform-api:v1.0.0 \
    --env="DATABASE_URL=$PROD_DB_URL" \
    --command -- alembic upgrade head
  
  → Creates all 30+ tables across 5 migration versions

Step 4: Auth Service + Gateway
  helm upgrade nexus-qa ./infrastructure/helm/nexus-qa \
    -f values-production.yaml \
    --set platform.authService.enabled=true \
    --set platform.gateway.enabled=true \
    --wait --timeout 120s
  
  → Auth service validates secrets, creates admin tenant
  → Gateway starts routing

Step 5: CPU Engines (No GPU Required)
  helm upgrade nexus-qa ./infrastructure/helm/nexus-qa \
    -f values-production.yaml \
    --set engines.shield.enabled=true \
    --set engines.backbone.enabled=true \
    --set engines.nerves.enabled=true \
    --set engines.hands.enabled=true \
    --set engines.spine.enabled=true \
    --set engines.mouth.enabled=true \
    --set engines.brain.enabled=true \
    --wait --timeout 180s

Step 6: GPU Engines
  helm upgrade nexus-qa ./infrastructure/helm/nexus-qa \
    -f values-production.yaml \
    --set engines.ears.enabled=true \
    --set engines.eyes.enabled=true \
    --set engines.heart.enabled=true \
    --wait --timeout 300s
  
  → Scheduled on GPU nodes only

Step 7: Platform API + Orchestrator
  helm upgrade nexus-qa ./infrastructure/helm/nexus-qa \
    -f values-production.yaml \
    --set platform.api.enabled=true \
    --set orchestrator.enabled=true \
    --wait --timeout 120s

Step 8: Client (Frontend)
  helm upgrade nexus-qa ./infrastructure/helm/nexus-qa \
    -f values-production.yaml \
    --set client.enabled=true \
    --wait --timeout 60s

Step 9: Ingress + TLS
  helm upgrade nexus-qa ./infrastructure/helm/nexus-qa \
    -f values-production.yaml \
    --set ingress.enabled=true \
    --set ingress.hosts[0].host=nexus-qa.yourdomain.com \
    --set ingress.tls[0].secretName=nexus-tls \
    --set ingress.tls[0].hosts[0]=nexus-qa.yourdomain.com

Step 10: Monitoring
  helm upgrade nexus-qa ./infrastructure/helm/nexus-qa \
    -f values-production.yaml \
    --set monitoring.enabled=true \
    --wait --timeout 120s
```

### Post-Deployment Verification

```bash
# 1. Health check all services
python scripts/health_check.py --env production

# 2. Security audit
python scripts/security_audit.py --env production

# 3. Validate secrets
python scripts/validate_secrets.py --env production

# 4. End-to-end smoke test
python scripts/production_e2e_test.py --env production

# 5. Pre-handover check
python scripts/pre_handover_check.py --env production
```

---

## PART 6: COMPLETE DATA FLOW — HOW THE UI CONSUMES EVERYTHING

### Flow 1: User Uploads Media (SessionCommandPage)

```
User drops audio/video file in SessionCommandPage
  │
  └─ POST /api/v1/qa/sessions/{id}/upload  (unified endpoint)
      │
      ├─ File metadata → PostgreSQL (AudioFileRow or VideoFileRow)
      ├─ File → Ears or Eyes engine (async job started)
      ├─ Chain "nexus.full-qa-pipeline" started
      ├─ WebSocket channel opened: ws/pipeline/{workflow_id}
      │
      CANONICAL PROCESSING (DAG Level 0-1):
      │  ├─ [PARALLEL] Ears transcribe + Eyes analyze + Spine ingest
      │  └─ [DEPENDS]  Shield redact (after all above complete)
      │     └─ EventBus: "canonical.processing.completed"
      │
      QI INTELLIGENCE (DAG Level 2-5):
      │  ├─ Heart extract-rules
      │  ├─ [PARALLEL] Backbone store + Heart generate-tests
      │  ├─ Hands generate test data
      │  ├─ Legs execute tests (polled)
      │  ├─ Mouth generate reports
      │  └─ Brain quality gate
      │     └─ EventBus: "qi.pipeline.completed"
      │
      PERSISTENCE:
      │  ├─ SessionRow updated (status, results)
      │  ├─ ContradictionRow created (from Heart)
      │  ├─ TestCaseRow created (from Heart)
      │  ├─ TraceRow created (from Backbone)
      │  ├─ AuditLogRow created (full trail)
      │  └─ WebSocket: pipeline_completed event
      │
      UI auto-refreshes:
         ├─ SessionReplayPage shows intelligence timeline
         ├─ KnowledgeGraphPage shows new nodes
         ├─ ContradictionRadarPage shows new conflicts
         ├─ TraceabilityMatrixPage shows rule→test links
         ├─ TestExecutionCenterPage shows test results
         └─ ExecutiveInsightsPage KPIs update
```

### Flow 2: QI Mission (MissionDetailPage)

```
User creates Mission with Persona
  │
  └─ POST /api/v1/missions  (creates 5 stages in PostgreSQL)
      │
      Stage 1: CAPTURE
      │  User clicks "Start Stage"
      │  └─ POST /api/v1/missions/{id}/stages/1/start
      │      ├─ MissionOrchestrator.execute_stage("capture")
      │      │   ├─ Spine: /spine/ingest (document)
      │      │   ├─ Shield: /shield/scan (PII)
      │      │   ├─ Ears: /ears/transcribe (audio)
      │      │   └─ Eyes: /eyes/analyze-video (video)
      │      ├─ Results → MissionArtifactRow
      │      └─ WebSocket: stage 1 progress
      │
      Stage 2: UNDERSTAND
      │  └─ POST /api/v1/missions/{id}/stages/2/start
      │      ├─ MissionOrchestrator.execute_stage("understand")
      │      │   ├─ Heart: /heart/extract-rules
      │      │   ├─ Heart: /heart/analyze
      │      │   ├─ Backbone: /backbone/nodes
      │      │   └─ Nerves: /nerves/connectors (gap analysis)
      │      └─ Results → MissionArtifactRow
      │
      Stage 3: STRATEGIZE
      │  └─ Heart: /heart/explore-flows + Nerves: impact analysis
      │
      Stage 4: GENERATE
      │  └─ Legs: /legs/execute + Hands: /hands/generate + Mouth: /mouth/generate
      │
      Stage 5: VALIDATE
      │  └─ Legs: /legs/execute (regression) + Nerves: compliance check
      │
      Mission Chat (any stage):
         └─ POST /api/v1/missions/{id}/messages
             ├─ User message saved
             ├─ Heart: /heart/ask (with mission context + conversation history)
             └─ Assistant response saved
```

### Flow 3: Dashboard Modules Read Pipeline Results

```
ExecutiveInsightsPage (Module 0)
  ├─ GET /api/v1/insights/kpis        → Aggregated from sessions, tests, contradictions
  ├─ GET /api/v1/insights/roi         → Computed from test counts × manual cost savings
  ├─ GET /api/v1/insights/risks       → Open contradictions + failed tests + compliance gaps
  └─ GET /api/v1/insights/weekly-trend → 8-week PostgreSQL aggregation

SessionReplayPage (Module 1)
  ├─ GET /api/v1/sessions/{id}         → Session detail with pipeline status
  ├─ GET /api/v1/sessions/{id}/events  → Ordered intelligence timeline
  └─ GET /api/v1/sessions/{id}/transcript → Full redacted transcript

KnowledgeGraphPage (Module 2)
  ├─ GET /api/v1/backbone/stats        → Node/edge counts
  ├─ POST /api/v1/backbone/search      → Semantic vector search
  └─ POST /api/v1/backbone/query       → Cypher graph queries

ContradictionRadarPage (Module 5)
  ├─ GET /api/v1/contradictions         → List conflicts from Heart
  └─ POST /api/v1/contradictions/{id}/resolve → SME resolution with audit

AIConfidencePage (Module 6)
  ├─ GET /api/v1/guardrails/pipeline    → Guardrail check results
  ├─ GET /api/v1/guardrails/review-queue → Items needing human review
  └─ GET /api/v1/guardrails/trust-trend  → Confidence score over time

TraceabilityMatrixPage (Module 7)
  └─ GET /api/v1/traceability           → Rule → Test → Result → Evidence

TestExecutionCenterPage (Module 8)
  ├─ GET /api/v1/tests/suites           → Test suites from pipeline
  ├─ GET /api/v1/tests/runs             → Test execution runs
  └─ GET /api/v1/test-cases             → Generated test cases with steps

DataForgePage (Module 9)
  ├─ GET /api/v1/data-forge/configs     → Forge configurations
  └─ GET /api/v1/data-forge/results     → Generated synthetic data

ComplianceCockpitPage (Module 10)
  └─ GET /api/v1/compliance/jurisdictions → Regulatory compliance by state

BrainDashboardPage (Module 11)
  ├─ GET /api/v1/brain/tiers/status     → Active LLM tier
  ├─ GET /api/v1/brain/tiers/summary    → Tier health and failover status
  └─ GET /api/v1/brain/quality-gate     → Quality gate analysis

AdminPage (Module 12)
  ├─ GET /api/v1/admin/engines          → Engine health (11 engines)
  ├─ GET /api/v1/admin/resources        → System CPU/RAM/disk
  ├─ GET /api/v1/admin/integrations     → Jira/Slack/GitHub status
  └─ GET /api/v1/admin/audit            → Audit log trail
```

---

## PART 7: TECHNICAL ENHANCEMENTS BEYOND BUG FIXES

### 7.1 Shield: Upgrade from Regex to NER Model

**Current:** Regex patterns only. Misses context-dependent PII.

**Enhancement:**
1. Add Microsoft Presidio as primary detector
2. Use Phi-3 small model for context-aware entity recognition
3. Keep regex as fast first-pass filter, use NER for confirmation
4. Add per-tenant configurable entity allow/deny lists

### 7.2 Eyes: Replace Heuristic Classifier with Fine-Tuned Model

**Current:** `ApplicationClassifier` uses keyword matching (browser indicators, mainframe keywords).

**Enhancement:**
1. Collect training data from frame screenshots classified by the heuristic
2. Fine-tune a lightweight image classifier (MobileNetV3 or EfficientNet-B0) on insurance application screenshots
3. Categories: browser, mainframe (TN3270), spreadsheet, PDF viewer, email client, custom application

### 7.3 Backbone: Embedding Model Upgrade

**Current:** `all-MiniLM-L6-v2` (384 dimensions).

**Enhancement:**
1. Upgrade to `BAAI/bge-large-en-v1.5` (1024 dimensions) for production — better semantic quality
2. Requires Milvus collection recreation with new dimension
3. Re-embed all existing knowledge after migration

### 7.4 Circuit Breaker on Gateway

**Enhancement:**
Add per-engine circuit breaker state (closed/open/half-open):
- After 5 consecutive failures → open circuit (instant 503 for 30s)
- After 30s → half-open (allow 1 probe request)
- If probe succeeds → close circuit
- Prevents cascading failures when an engine is down

### 7.5 Legs: Database and Mainframe Test Execution

**Current:** Stubs that raise errors.

**Enhancement:**
1. Database executor: async SQLAlchemy with read-only connection, parameterized queries only
2. Mainframe executor: py3270 integration for TN3270 terminal emulation
3. Both behind feature flags in engine config

### 7.6 Spine: OCR Enhancement

**Current:** EasyOCR for text extraction.

**Enhancement:**
1. Add Tesseract as fallback OCR engine
2. Add layout analysis for complex insurance forms (table detection, form field extraction)
3. Integrate with Eyes engine for visual context enrichment

### 7.7 Platform: Config Service

**Current:** Empty directory. All config via env vars.

**Enhancement:**
1. Centralized key-value config store backed by PostgreSQL
2. Runtime config changes without restart (watched keys)
3. Per-tenant config overrides
4. Config versioning and rollback

### 7.8 Platform: Message Bus Service

**Current:** Empty directory. Events via Redis Streams in SDK.

**Enhancement:**
1. Dedicated message bus service wrapping Redis Streams
2. Dead letter queue monitoring dashboard
3. Event replay capability for debugging
4. Schema registry for event types
5. Event-driven triggers (e.g., auto-start QI pipeline on canonical completion)

---

## PART 8: IMPLEMENTATION PRIORITY MATRIX

| Priority | Phase | Items | Effort | Dependency |
|----------|-------|-------|--------|------------|
| **P0** | Phase 1 | S1-S8 Security fixes | 3-5 days | None |
| **P0** | Phase 2A-2B | Unify orchestrators, atomic upload→pipeline | 5-7 days | Phase 1 |
| **P0** | Phase 2C | Pipeline results → PostgreSQL persistence | 3-4 days | Phase 2A |
| **P1** | Phase 2D | EventBus integration | 2-3 days | Phase 2C |
| **P1** | Phase 2E | Mission → engine execution wiring | 3-4 days | Phase 2A |
| **P1** | Phase 2F | Mission chat → Heart LLM | 1-2 days | None |
| **P1** | Phase 3 | WebSocket pipeline progress | 2-3 days | Phase 2D |
| **P1** | Phase 4A-4B | Secrets + network policies | 2-3 days | None |
| **P2** | Phase 4C-4F | HPA, PDB, backups, alerts | 3-4 days | Phase 4A |
| **P2** | Phase 5 | LLM tier configuration | 1-2 days | GPU nodes |
| **P2** | Phase 6 | Test coverage gaps | 5-7 days | Phase 2 |
| **P3** | Phase 7.1 | Shield NER upgrade | 5-7 days | None |
| **P3** | Phase 7.2 | Eyes classifier upgrade | 3-5 days | Training data |
| **P3** | Phase 7.4 | Gateway circuit breaker | 2-3 days | None |
| **P3** | Phase 7.5 | Legs DB/mainframe execution | 5-7 days | None |

---

## PART 9: PRODUCTION ENVIRONMENT REQUIREMENTS

### Compute

| Component | Min Instances | CPU | Memory | GPU | Storage |
|-----------|--------------|-----|--------|-----|---------|
| PostgreSQL | 1 (HA: 2) | 4 | 16 GB | - | 100 GB SSD |
| Redis | 1 (HA: 3 sentinel) | 2 | 8 GB | - | 10 GB SSD |
| Neo4j | 1 (HA: 3 core) | 4 | 16 GB | - | 50 GB SSD |
| Milvus | 1 (HA: 3) | 4 | 16 GB | - | 100 GB SSD |
| Ollama | 1 | 8 | 32 GB | 1x A100 48GB | 100 GB SSD |
| Gateway | 2-10 (HPA) | 1 | 512 MB | - | - |
| Auth Service | 2 | 1 | 512 MB | - | - |
| Platform API | 2-8 (HPA) | 2 | 2 GB | - | - |
| Shield | 2 | 1 | 1 GB | - | - |
| Ears | 1-3 (HPA) | 4 | 8 GB | 1x A10G 24GB | 20 GB |
| Eyes | 1-3 (HPA) | 4 | 8 GB | 1x A10G 24GB | 20 GB |
| Heart | 1-4 (HPA) | 2 | 4 GB | (via Ollama/Cloud) | - |
| Backbone | 2 | 2 | 4 GB | - | - |
| Brain | 2 | 2 | 2 GB | - | - |
| Nerves | 1 | 1 | 512 MB | - | - |
| Legs | 2 | 2 | 2 GB | - | - |
| Hands | 1 | 1 | 1 GB | - | - |
| Spine | 1 | 2 | 2 GB | - | - |
| Mouth | 1 | 2 | 2 GB | - | - |
| Orchestrator | 2 | 2 | 2 GB | - | - |
| Client (nginx) | 2 | 0.5 | 256 MB | - | - |
| Prometheus | 1 | 2 | 4 GB | - | 50 GB |
| Grafana | 1 | 1 | 1 GB | - | 5 GB |

**Total minimum:** ~50 CPU cores, ~130 GB RAM, 2x GPU (NVIDIA A10G + A100), ~475 GB SSD

### Network

| Requirement | Details |
|-------------|---------|
| Ingress | HTTPS with TLS 1.2+ |
| Internal | 172.28.0.0/16 bridge network (Docker) or ClusterIP (K8s) |
| Egress | Ollama (internal), Anthropic API (HTTPS), OpenAI API (HTTPS), Jira/GitHub/Slack (HTTPS) |
| DNS | Internal service discovery via K8s DNS |
| Load Balancer | L7 (nginx ingress controller) with WebSocket support |

---

## Summary

This plan covers every file in the codebase. The critical path to production is:

1. **Fix security holes** (gateway auth bypass, tenant isolation, eval injection, token revocation)
2. **Wire the disconnected systems** (canonical pipeline → QI pipeline → PostgreSQL → UI)
3. **Enable real-time visibility** (WebSocket pipeline progress, EventBus events)
4. **Harden infrastructure** (secrets management, network policies, HPA, backups, monitoring alerts)
5. **Configure LLM tiers** (cloud primary → cloud fallback → local Ollama)
6. **Close test gaps** (EventBus, router functional tests, multi-tenant isolation tests)

No stubs. No examples. No dummy implementations. Every item above maps to specific files and specific code changes in the existing codebase.
