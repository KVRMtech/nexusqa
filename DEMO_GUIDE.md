# Nexus QA — Real E2E Production Demo Guide

## Quick Start (3 Commands)

```powershell
# 1. Start infrastructure + Ollama
docker compose -f docker-compose.dev.yml up -d

# 2. Pull AI models (one-time, ~6GB total)
python scripts/setup_ollama.py

# 3. Start all services & run demo
python scripts/start_all_services.py
python scripts/demo_e2e.py --demo full
```

---

## Prerequisites

### Hardware Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| RAM | 16 GB | 32 GB |
| Disk | 20 GB free | 40 GB free |
| CPU | 4 cores | 8+ cores |
| GPU | Not required | NVIDIA GPU (for faster inference) |

### Software Requirements

| Software | Version | Purpose |
|----------|---------|---------|
| Python | 3.10+ | Backend services |
| Docker Desktop | 24+ | Infrastructure (Redis, Postgres, Neo4j, Ollama) |
| Node.js | 18+ | Frontend client (optional for UI demo) |
| Git | 2.x | Source control |

### No API Keys Required!

The entire platform runs **100% on-premise** using Ollama for AI inference. No cloud API keys needed.

| Component | Provider | Cost |
|-----------|----------|------|
| LLM (Heart Engine) | Ollama + llama3.2:1b | **Free** |
| Vision (Eyes Engine) | Ollama + llava:7b | **Free** |
| Speech-to-Text | faster-whisper (local) | **Free** |
| Embeddings | sentence-transformers | **Free** |
| PII Detection | Regex-based (local) | **Free** |
| Database | PostgreSQL (Docker) | **Free** |
| Cache | Redis (Docker) | **Free** |
| Knowledge Graph | Neo4j (Docker) | **Free** |
| Vector Store | Milvus (Docker) | **Free** |

**Optional paid providers** (if you prefer cloud AI):

```env
# To use OpenAI instead of Ollama:
LLM_PROVIDER=openai
LLM_API_KEY=sk-your-openai-key
LLM_MODEL=gpt-4o

# To use Anthropic:
LLM_PROVIDER=anthropic
LLM_API_KEY=sk-ant-your-key
LLM_MODEL=claude-sonnet-4-20250514

# To use Azure OpenAI:
LLM_PROVIDER=azure
LLM_API_KEY=your-azure-key
AZURE_ENDPOINT=https://your-resource.openai.azure.com
LLM_MODEL=gpt-4o

# For speaker diarization (optional):
HF_TOKEN=hf_your-huggingface-token
```

---

## Step-by-Step Setup

### Step 1: Install Python Dependencies

```powershell
cd nexus-qa
python -m venv .venv
.venv\Scripts\activate

# Install SDK + dependencies
pip install -e sdk/nexus-sdk/
pip install httpx pytest pytest-asyncio faker
```

### Step 2: Start Infrastructure

```powershell
# Starts Redis, PostgreSQL, Neo4j, and Ollama
docker compose -f docker-compose.dev.yml up -d

# Verify infrastructure is running
docker ps
# Expected: nexus-redis, nexus-postgres, nexus-neo4j, nexus-ollama
```

### Step 3: Pull AI Models

```powershell
# Interactive model setup (shows progress & verification)
python scripts/setup_ollama.py

# Or pull manually:
docker exec nexus-ollama ollama pull llama3.2:1b    # ~1.3 GB
docker exec nexus-ollama ollama pull llava:7b         # ~4.7 GB

# Verify models are ready:
docker exec nexus-ollama ollama list
```

### Step 4: Start All Services

```powershell
# Option A: Local development mode (all services as Python processes)
python scripts/start_all_services.py
# Starts: auth(8000), shield(8001), ears(8002), eyes(8003), heart(8004),
#          backbone(8005), nerves(8006), legs(8007), hands(8008),
#          spine(8009), mouth(8010), brain(8011), orchestrator(8100)
# Note: platform-api(8091) needs to be started separately if using this method

# Option B: Full Docker production stack (all 15 services + monitoring)
docker compose -f infrastructure/docker/docker-compose.yml up -d --build
```

### Step 5: Verify Services

```powershell
# Quick health check
python scripts/health_check.py

# Expected output:
#   auth-service     :8000  UP
#   shield           :8001  UP
#   ears             :8002  UP
#   eyes             :8003  UP
#   heart            :8004  UP
#   backbone         :8005  UP
#   nerves           :8006  UP
#   legs             :8007  UP
#   hands            :8008  UP
#   spine            :8009  UP
#   mouth            :8010  UP
#   brain            :8011  UP
#   gateway          :8080  UP
#   platform-api     :8091  UP
#   orchestrator     :8100  UP
#   15/15 services healthy
```

### Step 6: Run the Demo

```powershell
# Quick demo — Shield + Heart engines only (~30 seconds)
python scripts/demo_e2e.py --demo quick

# Full demo — All 11 engines in sequence (~2-5 minutes)
python scripts/demo_e2e.py --demo full

# Orchestrator demo — Full chain pipeline (~3-10 minutes)
python scripts/demo_e2e.py --demo orchestrator
```

---

## Demo Modes

### Quick Demo (`--demo quick`)
**Duration:** ~30 seconds
**Engines Used:** Shield, Heart

1. **Authentication** — Login, get JWT token
2. **PII Detection** — Analyze transcript for sensitive data (SSN, credit cards, emails, addresses)
3. **PII Redaction** — Replace PII with safe tokens, generate mapping ID
4. **Rule Extraction** — AI extracts business rules from redacted transcript
5. **Test Generation** — AI generates test cases (happy path, boundary, negative, edge case)
6. **AI Q&A** — Ask a natural language question about the transcript

### Full Demo (`--demo full`)
**Duration:** ~2-5 minutes
**Engines Used:** All 11

Everything in Quick Demo, plus:
7. **Synthetic Data** — Hands Engine generates realistic test patient profiles
8. **Knowledge Graph** — Backbone Engine stores rules in Neo4j + Milvus, semantic search
9. **Report Generation** — Mouth Engine generates HTML executive summary
10. **Test Case CRUD** — Platform API creates, reads, exports, and deletes test cases

### Orchestrator Demo (`--demo orchestrator`)
**Duration:** ~3-10 minutes
**Engines Used:** Ears, Shield, Eyes, Heart, Backbone (via chain)

1. **Lists available chains** — Shows all 4 built-in pipelines
2. **Starts Knowledge-Capture chain** — 5-stage DAG pipeline
3. **Polls workflow progress** — Real-time status updates
4. **Dashboard summary** — Aggregate workflow statistics

---

## Demo Scenario: Online Pharmacy Platform

The demo uses a realistic scenario — an online pharmacy ordering system:

- **Transcript**: SME walkthrough of medication search, prescription validation, insurance processing, checkout, and refill management
- **PII Data**: Names, SSN, addresses, credit cards, emails, phone numbers, insurance IDs
- **Business Rules**: Prescription requirements, DEA schedule compliance, insurance eligibility, credit card tokenization, controlled substance ID verification, refill management
- **Test Cases**: E2E tests covering happy path, boundary conditions, negative scenarios, and edge cases

---

## Architecture Overview

```
                    ┌─────────────────────┐
                    │   React Frontend    │ :3080
                    │    (Nexus Client)   │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │      Gateway        │ :8080
                    │  (Auth + Routing)   │
                    └──────────┬──────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         │                     │                     │
    ┌────▼─────┐    ┌─────────▼────────┐     ┌──────▼─────┐
    │  Platform │    │   Orchestrator   │     │ Auth Svc   │
    │   API     │    │ (Chain Engine)   │     │ (JWT/RBAC) │
    │  :8091    │    │    :8100         │     │  :8000     │
    └───────────┘    └────────┬─────────┘     └────────────┘
                              │
    ┌─────────── Engine Layer (11 AI Engines) ───────────┐
    │                                                     │
    │  Shield :8001  — PII Detection & Redaction          │
    │  Ears   :8002  — Speech-to-Text (Whisper)           │
    │  Eyes   :8003  — Vision Analysis (LLaVA)            │
    │  Heart  :8004  — AI Reasoning (LLM)                 │
    │  Backbone :8005 — Knowledge Graph (Neo4j+Milvus)    │
    │  Nerves :8006  — Integration Hub (Jira/Slack)       │
    │  Legs   :8007  — Test Execution (Playwright)        │
    │  Hands  :8008  — Synthetic Data Generation          │
    │  Spine  :8009  — Document Processing                │
    │  Mouth  :8010  — Report Generation                  │
    │  Brain  :8011  — Intelligent Coordinator            │
    │                                                     │
    └─────────────────────────────────────────────────────┘
                              │
    ┌─────────── Infrastructure Layer ───────────────────┐
    │                                                     │
    │  Redis    :6379  — Cache & Message Broker           │
    │  Postgres :5432  — Relational Database              │
    │  Neo4j    :7474  — Knowledge Graph                  │
    │  Milvus   :19530 — Vector Database                  │
    │  Ollama   :11434 — Local LLM Server                 │
    │                                                     │
    └─────────────────────────────────────────────────────┘
```

---

## Troubleshooting

### Ollama not responding
```powershell
# Check if Ollama Docker container is running
docker ps | Select-String ollama

# Restart Ollama
docker restart nexus-ollama

# Verify Ollama API
Invoke-WebRequest http://localhost:11434/api/tags | Select-Object StatusCode
```

### Model not found
```powershell
# List installed models
docker exec nexus-ollama ollama list

# Pull missing model
docker exec nexus-ollama ollama pull llama3.2:1b
docker exec nexus-ollama ollama pull llava:7b
```

### Service startup failures
```powershell
# Check if ports are in use
netstat -ano | Select-String ":8001|:8004|:8080"

# Check service logs
python scripts/start_all_services.py --stop  # Stop all
python scripts/start_all_services.py          # Restart
```

### Out of memory
- Reduce model size: Use `llama3.2:1b` instead of `llama3.1:8b`
- Close other applications to free RAM
- Increase Docker Desktop memory limit (Settings → Resources)

### Heart Engine slow responses
- First request may take 30-60s (model loading)
- Subsequent requests are faster (model stays in memory)
- GPU acceleration significantly improves speed

---

## Running with Cloud LLM (Optional)

If you prefer using OpenAI, Anthropic, or Azure instead of local Ollama:

```powershell
# Set environment before starting services
$env:LLM_PROVIDER = "openai"
$env:LLM_API_KEY = "sk-your-openai-api-key"
$env:LLM_MODEL = "gpt-4o"

# Then start services normally
python scripts/start_all_services.py
python scripts/demo_e2e.py --demo full
```

This switches the Heart Engine from Ollama to OpenAI. All other engines remain local.

---

## Built-in Orchestrator Chains

| Chain ID | Name | Stages | Purpose |
|----------|------|--------|---------|
| `nexus.qa-testing` | Full QA Pipeline | 11 | Complete: transcribe → redact → analyze → extract → generate → execute → report |
| `nexus.compliance-audit` | Compliance Audit | 5 | Document → redact → extract rules → knowledge check → compliance report |
| `nexus.knowledge-capture` | Knowledge Capture | 5 | Transcribe → redact → visual analysis → extract rules → store in knowledge graph |
| `nexus.regression-suite` | Regression Suite | 6 | Fetch rules → generate tests → generate data → execute → report → notify |

---

## Stopping Everything

```powershell
# Stop Python services
python scripts/start_all_services.py --stop

# Stop Docker infrastructure
docker compose -f docker-compose.dev.yml down

# Stop full production stack
docker compose -f infrastructure/docker/docker-compose.yml down

# Clean up all data (WARNING: destroys all data)
docker compose -f docker-compose.dev.yml down -v
```
