# Nexus QA — Canonical-First Production Runbook

> **Purpose**: Operate the Nexus QA platform in production with the canonical
> media processing pipeline as the primary ingestion path.  Every KT session
> flows through the 7-stage canonical chain before any downstream consumer
> chain (QA testing, compliance audit, knowledge capture) can fire.

> **Primary validation path**: The **client UI** (`http://localhost:5173`) is the
> certified production ingestion path. Operators should use the UI for session
> creation, media upload, and artifact review. The curl/API examples below are
> for troubleshooting and automation only — they are NOT the certification path.

---

## 1  Architecture at a Glance

```
                        ┌─────────────┐
                        │  Gateway    │ :8080
                        │  (routing)  │
                        └──────┬──────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
  Platform API :8091    Orchestrator :8100    QA Orchestrator :8092
  (sessions, artifacts,  (canonical chain,     (product-specific
   tenants, missions)     workflow engine)       orchestration)
        │
        ▼
  ┌──────────────────────────────────────────────────┐
  │            16 Engine Services (8000–8011)         │
  │                                                    │
  │  Auth :8000   Shield :8001   Ears :8002            │
  │  Eyes :8003   Heart  :8004   Backbone :8005        │
  │  Nerves:8006  Legs   :8007   Hands :8008           │
  │  Spine :8009  Mouth  :8010   Brain :8011           │
  └──────────────────────────────────────────────────┘
        │          │          │
        ▼          ▼          ▼
   PostgreSQL   Redis      Neo4j
   :5432        :6379      :7687
```

## 2  Canonical Processing Pipeline

Every KT session media upload triggers the **7-stage canonical chain**:

```
media_probe ─┬─→ audio_transcription → pii_redaction ──────────────┐
             └─→ visual_extraction → visual_graph_assembly ────────┤
                                                                   ▼
                                         artifact_persistence → canonical_quality_gate
```

| Stage | Engine | Timeout | On Failure | Purpose |
|-------|--------|---------|------------|---------|
| 1 media_probe | Spine :8009 | 30s | fail | FFprobe media metadata (zero-copy) |
| 2 audio_transcription | Ears :8002 | 900s | skip | Whisper v3 Large + diarization |
| 3 pii_redaction | Shield :8001 | 60s | skip | PII detection and redaction |
| 4 visual_extraction | Eyes :8003 | 1800s | skip | Frame extraction, OCR, LLaVA analysis |
| 5 visual_graph_assembly | Spine :8009 | 120s | skip | Screen-flow graph from keyframes |
| 6 artifact_persistence | Spine :8009 | 60s | fail | Write canonical artifact to PostgreSQL |
| 7 canonical_quality_gate | Brain :8011 | 120s | fail | Heuristic + LLM semantic scoring |

**Key invariant**: Artifact status on platform-api (`GET /api/v1/artifacts/{id}/status`)
is the single authoritative completion signal. Do not use workflow status as a proxy.

## 3  Starting the Platform

### 3.1  Infrastructure (Docker)

```powershell
# Start PostgreSQL, Redis, Neo4j, Ollama
docker compose -f docker-compose.dev.yml up -d

# Pull AI models (first time only, ~6GB)
python scripts/setup_ollama.py
```

### 3.2  Application Services

```powershell
# Start all 16 services with correct env vars
python scripts/start_all_services.py

# Verify all healthy
python scripts/health_check.py
```

### 3.3  Database Migrations

```powershell
# Check current migration
python scripts/migrate.py status

# Verify schema is at head
python scripts/migrate.py check

# Apply any pending migrations
python scripts/migrate.py upgrade

# Current head: 009_semantic_completeness
```

## 4  Stopping the Platform

```powershell
# Graceful stop (SIGTERM → wait → force-kill)
python scripts/start_all_services.py --stop
```

## 5  Health Monitoring

### 5.1  Quick Health Check

```powershell
# All services at once
python scripts/health_check.py

# Single service
curl http://localhost:8009/health
```

### 5.2  Critical Health Indicators

| Check | Healthy | Action if Unhealthy |
|-------|---------|---------------------|
| Spine `modes.database` | `"postgresql"` | Restart spine; check Postgres connection |
| Brain `modes.llm` | `"ollama"` | Run `scripts/setup_ollama.py`; check Ollama |
| Brain `modes.semantic_scoring` | `"real"` | If `"degraded"`, LLM is stub — fix LLM_BACKEND |
| Brian `modes.llm_model` | `"llama3.2:1b"` | Check Ollama models; pull if missing |
| Ears Whisper model | loaded | Check GPU memory; restart ears |
| Eyes LLaVA model | loaded | Check Ollama; restart eyes |
| Platform API DB | connected | Check Postgres; run `python scripts/migrate.py check` |

### 5.3  Spine DB Pool (Critical)

Spine engine must report `"database": "postgresql"` in its health modes.
If `"database": "unavailable"`, artifacts will not be persisted and all
downstream artifact queries return 404.

```powershell
curl http://localhost:8009/health | jq '.modes.database'
# Must return: "postgresql"
```

## 6  Common Operations

### 6.1  Process a KT Session (UI — Primary Path)

1. Open the client at `http://localhost:5173`
2. Create a new KT session (title, tenant, session type)
3. Upload audio and/or video files via the session page
4. Monitor progress via the session timeline / artifact status panel
5. Review the completed canonical artifact (transcript, visual scenes, quality score)

The UI drives the same Orchestrator → Engine pipeline described in Section 2.
Artifact status in the UI is the authoritative completion signal.

### 6.2  Process a KT Session (API — Automation / Troubleshooting)

```bash
# Upload audio + video for canonical processing
curl -X POST http://localhost:8100/api/v1/orchestrator/process \
  -H "Authorization: Bearer $TOKEN" \
  -F "audio=@recording.wav" \
  -F "video=@screen.mp4" \
  -F "session_id=$SESSION_ID"
```

The response includes `workflow_id`. Poll until terminal:

```bash
curl http://localhost:8100/api/v1/orchestrator/workflows/$WF_ID \
  -H "Authorization: Bearer $TOKEN"
# status: "completed" | "failed" | "needs_review"
```

### 6.3  Check Artifact Status (Authoritative Completion Signal)

```bash
curl http://localhost:8091/api/v1/artifacts/$ARTIFACT_ID/status \
  -H "Authorization: Bearer $TOKEN"
```

Returns: `status`, `quality_gate_passed`, `has_real_transcript`,
`has_visual_semantics`, `semantic_completeness_score`, `completed_at`.

## 6.5  Secondary Ingestion Paths (Spine)

The following endpoints are **supported but secondary** ingestion modes.
They feed into the Spine document pipeline (parsing, chunking) — *not*
the 7-stage canonical media chain. Use them for supplementary material
(BRDs, policies, specs) that enriches a session's knowledge context.

### 6.5.1  Document Upload (Non-Media)

```bash
# Upload a BRD, policy doc, or specification (non-media, Spine-only)
curl -X POST http://localhost:8009/api/v1/spine/ingest \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@policy_document.pdf" \
  -F "tenant_id=my-tenant" \
  -F "session_id=$SESSION_ID"
```

### 6.5.2  URL-Based Ingestion

```bash
# Ingest media from a URL (e.g., cloud storage presigned URL)
curl -X POST http://localhost:8009/api/v1/spine/ingest-url \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://storage.example.com/recording.wav",
    "tenant_id": "my-tenant",
    "session_id": "sess-123",
    "source_type": "url"
  }'
```

## 7  Troubleshooting

### Workflow Stuck in Processing

```sql
-- Check workflow stage states
SELECT workflow_id, status, stages
FROM workflows
WHERE status = 'processing'
  AND created_at < NOW() - INTERVAL '30 minutes';
```

### Artifacts Not Persisting (404 on status)

1. Check Spine health: `curl http://localhost:8009/health` — verify `modes.database = postgresql`
2. If database is `unavailable`, restart Spine with correct `POSTGRES_*` env vars
3. Check `canonical_artifacts` table directly:
   ```sql
   SELECT COUNT(*) FROM canonical_artifacts
   WHERE created_at > NOW() - INTERVAL '1 hour';
   ```

### Quality Gate Always Failing

1. Check Brain LLM availability: `curl http://localhost:8011/health`
2. Verify Ollama is running: `curl http://localhost:11434/api/tags`
3. Short media (<5s) will always score low on duration_adequacy
4. Audio-only uploads (no video) cap completeness at 0.7

### JWT Authentication Failures (401)

All services must use the same `NEXUS_JWT_SECRET`. When starting manually,
ensure this env var matches what `start_all_services.py` sets:

```powershell
$env:NEXUS_JWT_SECRET = "test-secret-do-not-use-in-production"  # dev only
```

## 8  Environment Variables

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `NEXUS_JWT_SECRET` | `dev-jwt-secret-key...` | YES (production) | JWT signing key |
| `POSTGRES_HOST` | `localhost` | no | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | no | PostgreSQL port |
| `POSTGRES_USER` | `nexus` | no | Database user |
| `POSTGRES_PASSWORD` | `change-me` | YES | Database password |
| `POSTGRES_DB` | `nexus` | no | Database name |
| `REDIS_HOST` | `localhost` | no | Redis host |
| `REDIS_PORT` | `6379` | no | Redis port |
| `LLM_BACKEND` | `ollama` | no | Brain LLM backend |
| `OLLAMA_MODEL` | `llama3.2:1b` | no | Default LLM model |
| `NEXUS_ENV` | `development` | no | Environment tier |

## 9  Database Schema (Alembic Migrations)

```
001_initial               — Users, tenants, audit_log
002_platform_api_tables   — Sessions, knowledge, missions
003_test_cases            — Generated test cases
004_media_processing      — Media processing jobs
005_qi_portal             — QI portal tables
006_canonical_artifacts   — Canonical artifact table
007_mission_stage_wf      — Mission→workflow linkage
008_canonical_provenance  — workflow_id, source_type, source_filename
009_semantic_completeness — has_real_transcript, has_visual_semantics, score
```

To check current revision:
```powershell
python scripts/migrate.py check
# Expected: 009_semantic_completeness (head)
```

To apply pending:
```powershell
python scripts/migrate.py upgrade
```

## 10  Production Checklist

- [ ] `NEXUS_JWT_SECRET` set to a strong random secret (≥32 chars)
- [ ] `POSTGRES_PASSWORD` set to production credentials
- [ ] PostgreSQL connection pool sized (default 20+10 overflow per service)
- [ ] Alembic at `009_semantic_completeness` (head)
- [ ] All 16 services report healthy
- [ ] Spine reports `database: postgresql` in health modes
- [ ] Brain LLM backend is not `stub`
- [ ] Ollama models pulled (`llama3.2:1b`, `llava:7b`)
- [ ] Redis accessible (or services degrade gracefully to in-memory)
- [ ] Monitoring stack deployed (Prometheus + Grafana + Loki)
- [ ] Network policies applied (brain-engine included)
- [ ] TLS termination configured at gateway
