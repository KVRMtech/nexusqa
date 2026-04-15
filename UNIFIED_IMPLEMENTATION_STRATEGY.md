# Unified Implementation Strategy — Three-Perspective Merge

## Honest Assessment

All three perspectives — my CANONICAL_PIPELINE_IMPLEMENTATION.md, Architect 1's 10-phase plan, and Architect 2's design principles — converge on the **same core architecture**: "Process Once, Use Many." We agree on the diagnosis (Eyes engine is the critical bottleneck, frame dedup is broken, GPU semaphore serializes everything, downstream pipelines re-process from scratch). We agree on the cure (canonical artifact pipeline, scene-based grouping, threshold-based dedup, parallel audio+video, consumer chains read from artifacts).

**Where we differ is scope and depth:**

| Dimension | My Plan | Architect 1 | Architect 2 |
|-----------|---------|-------------|-------------|
| Eyes fix | Full code (Hamming distance, scene grouping, batch OCR) | Described but no code | Detailed design principles, no code |
| Helm/infra | ❌ Not covered | ✅ Brain deployment gap, configmap fixes, env var aliases | ❌ Not covered |
| UI migration | ❌ Not covered | ✅ Client API rewiring from legacy → new orchestrator | ❌ Not covered |
| Mission wiring | ❌ Not covered | ✅ MissionOrchestrator.execute_stage connected | ❌ Not covered |
| Canonical chain granularity | 3 stages (transcription, visual_analysis, artifact_assembly) | 13 stages (media_probe → canonical_quality_gate) | Milestone events (partial readiness) |
| Artifact persistence | Redis only (Spine Redis hash) | PostgreSQL + object storage + Alembic migrations | PostgreSQL + media fingerprint cache |
| Long video handling | ❌ Not covered | ✅ Chunked 5-10 min segments | ✅ Chunked parallel processing |
| SSE/WebSocket streaming | ❌ Not covered | ✅ Progress streaming to UI | ❌ Not covered |
| Brain quality gate | Not addressed | Mandatory quality gate coordinator | ❌ Not covered |
| Dynamic sampling | Fixed max_fps_extract=1.0 | Described | ✅ 1 frame/5-20s for stable, burst on change |
| Fast path / deep path | ❌ Not covered | ❌ Not covered | ✅ Two-tier processing |
| Screen-flow graph | ❌ Not covered | ❌ Not covered | ✅ Reusable graph substrate |
| Performance math | ✅ Exact calculations (50→4 LLaVA calls) | ❌ Not provided | ✅ Detailed cost analysis |

**Bottom line:** My plan is the most code-ready but narrowest in scope. Architect 1 covers the full system (infra → engines → UI → missions) but lacks implementation code. Architect 2 provides the deepest design rationale but stays at architecture level. The unified strategy below takes the best from each.

---

## What All Three Agree On (Confirmed Against Actual Code)

1. **Eyes is the critical performance bottleneck** — sequential per-frame LLaVA calls, binary dHash dedup that doesn't use the configured threshold, GPU Semaphore(1) serializing everything
2. **"Process Once, Use Many" is the correct architecture** — canonical pipeline extracts media artifacts once, all consumer chains read from them
3. **New generic orchestrator (nexus-qa-orchestrator:8100) is the right engine** — DAG execution, polling, retry, conditions already built. Legacy qa-orchestrator:8092 should be deprecated
4. **Audio + Video should run in parallel** — new orchestrator chains already support this via DAG (no `depends_on` between transcription and visual_analysis)
5. **Consumer chains (QI, Knowledge, Regression) should NOT call Ears/Eyes directly** — they should fetch pre-computed canonical artifacts

**Code confirmations from this session:**
- Brain engine IS defined in `values.yaml` (engines.brain, port 8011, replicas 2) but the Helm template `engine-deployment.yaml` dict has only 10 entries — **Brain will NOT deploy in Kubernetes** ✅ confirmed
- Configmap is missing `BRAIN_ENGINE_URL`, `PLATFORM_API_URL`, `QA_ORCHESTRATOR_URL` ✅ confirmed
- Gateway `config.py` has no Field aliases — env vars from configmap won't bind ✅ confirmed
- Client `SessionCommandPage` calls legacy path (`/v1/qa/sessions/`) — `startWorkflow()` exists in api.ts but is NOT used ✅ confirmed
- `MissionOrchestrator.execute_stage()` exists with full STAGE_ENGINE_ACTIONS mapping but is NEVER called from the missions router `advance` endpoint ✅ confirmed
- Missions router `advance` just changes status in DB, doesn't trigger engines ✅ confirmed

---

## Unified Implementation — 8 Phases, File-by-File

### Phase 0: Helm & Infrastructure Fixes (MUST DO FIRST)

**Why first:** Nothing works in Kubernetes until Brain deploys, configmap has all URLs, and gateway binds env vars correctly. This is a deployment blocker, not a feature.

#### File: `infrastructure/helm/nexus-qa/templates/engine-deployment.yaml`

**Current problem:** The `$engines` dict has 10 entries. Brain is missing.

```yaml
# ADD to the $engines dict (after "mouth"):
"brain":
  name: brain-engine
  image: "{{ $.Values.engines.brain.image.repository }}:{{ $.Values.engines.brain.image.tag }}"
  port: {{ $.Values.engines.brain.port }}
  replicas: {{ $.Values.engines.brain.replicas }}
  gpu: {{ $.Values.engines.brain.gpu }}
```

#### File: `infrastructure/helm/nexus-qa/templates/engine-service.yaml`

**Same fix:** Add `"brain"` to the `$engines` dict with matching structure.

#### File: `infrastructure/helm/nexus-qa/templates/configmap.yaml`

**Add missing entries:**
```yaml
# ADD to data section:
BRAIN_ENGINE_URL: "http://brain-engine:{{ .Values.engines.brain.port }}"
PLATFORM_API_URL: "http://platform-api:{{ .Values.platform.api.port }}"
QA_ORCHESTRATOR_URL: "http://qa-orchestrator:{{ .Values.orchestrator.port }}"
```

#### File: `infrastructure/helm/nexus-qa/templates/orchestrator.yaml`

**Add brain URL injection:**
```yaml
# ADD to env section:
- name: BRAIN_ENGINE_URL
  valueFrom:
    configMapKeyRef:
      name: {{ include "nexus-qa.fullname" . }}-config
      key: BRAIN_ENGINE_URL
```

#### File: `infrastructure/helm/nexus-qa/templates/platform-deployment.yaml`

**Add brain URL + platform API URL to gateway and platform-api deployments:**
```yaml
# ADD to gateway env:
- name: BRAIN_ENGINE_URL
  valueFrom:
    configMapKeyRef:
      name: {{ include "nexus-qa.fullname" . }}-config
      key: BRAIN_ENGINE_URL
- name: PLATFORM_API_URL
  valueFrom:
    configMapKeyRef:
      name: {{ include "nexus-qa.fullname" . }}-config
      key: PLATFORM_API_URL

# ADD to platform-api env:
- name: BRAIN_ENGINE_URL
  valueFrom:
    configMapKeyRef:
      name: {{ include "nexus-qa.fullname" . }}-config
      key: BRAIN_ENGINE_URL
```

#### File: `platform/gateway/app/config.py`

**Fix env var binding.** Current: `auth_url: str = Field(default="http://localhost:8000")` — pydantic-settings tries `AUTH_URL` but configmap provides `AUTH_SERVICE_URL`. Add aliases:

```python
# ADD Field aliases to match configmap env var names
auth_url: str = Field(default="http://localhost:8000", alias="AUTH_SERVICE_URL")
brain_url: str = Field(default="http://localhost:8011", alias="BRAIN_ENGINE_URL")
platform_api_url: str = Field(default="http://localhost:8091", alias="PLATFORM_API_URL")
qa_orchestrator_url: str = Field(default="http://localhost:8092", alias="QA_ORCHESTRATOR_URL")
orchestrator_url: str = Field(default="http://localhost:8100", alias="ORCHESTRATOR_URL")

# Engine URLs — add aliases matching CONFIGMAP keys
shield_url: str = Field(default="http://localhost:8001", alias="SHIELD_ENGINE_URL")
ears_url: str = Field(default="http://localhost:8002", alias="EARS_ENGINE_URL")
eyes_url: str = Field(default="http://localhost:8003", alias="EYES_ENGINE_URL")
heart_url: str = Field(default="http://localhost:8004", alias="HEART_ENGINE_URL")
backbone_url: str = Field(default="http://localhost:8005", alias="BACKBONE_ENGINE_URL")
nerves_url: str = Field(default="http://localhost:8006", alias="NERVES_ENGINE_URL")
legs_url: str = Field(default="http://localhost:8007", alias="LEGS_ENGINE_URL")
hands_url: str = Field(default="http://localhost:8008", alias="HANDS_ENGINE_URL")
spine_url: str = Field(default="http://localhost:8009", alias="SPINE_ENGINE_URL")
mouth_url: str = Field(default="http://localhost:8010", alias="MOUTH_ENGINE_URL")
```

Also add `model_config = {"populate_by_name": True}` so both the alias AND the field name work (dev uses field name, K8s uses alias).

#### Phase 0 — Change Summary

| # | File | Change | Risk |
|---|------|--------|------|
| 1 | `infrastructure/helm/nexus-qa/templates/engine-deployment.yaml` | Add `brain` to $engines dict | LOW |
| 2 | `infrastructure/helm/nexus-qa/templates/engine-service.yaml` | Add `brain` to $engines dict | LOW |
| 3 | `infrastructure/helm/nexus-qa/templates/configmap.yaml` | Add BRAIN_ENGINE_URL, PLATFORM_API_URL, QA_ORCHESTRATOR_URL | LOW |
| 4 | `infrastructure/helm/nexus-qa/templates/orchestrator.yaml` | Add BRAIN_ENGINE_URL env injection | LOW |
| 5 | `infrastructure/helm/nexus-qa/templates/platform-deployment.yaml` | Add BRAIN_ENGINE_URL + PLATFORM_API_URL to gateway + platform-api | LOW |
| 6 | `platform/gateway/app/config.py` | Add Field aliases for all env vars, add populate_by_name | LOW |

**Validation:** `helm template . | grep brain` should show Deployment + Service + configmap entry. Gateway should log correct URLs on startup.

---

### Phase 1: Eyes Engine Performance Fix (THE Critical Fix)

This is my original plan's strongest section — exact code, performance math, minimal risk.

#### File 1: `engines/eyes-engine/app/frame_diff/__init__.py`

**Bug:** Line ~100-101 uses `current_hash != prev_hash` (binary comparison). The `frame_diff_threshold=0.05` config is stored but never used.

**Fix:** Implement Hamming distance threshold comparison.

| Change | Detail |
|--------|--------|
| Replace `_extract_with_opencv` | Use `_hamming_distance() > self.frame_diff_threshold` instead of `!=` |
| Add `_hamming_distance()` static method | Normalized Hamming distance (0.0=identical, 1.0=completely different) |
| Add `"hash": current_hash` to frame dict | Needed for scene grouping in next step |

```python
@staticmethod
def _hamming_distance(hash_a: str, hash_b: str) -> float:
    """Normalized Hamming distance between two hex hash strings."""
    int_a = int(hash_a, 16)
    int_b = int(hash_b, 16)
    xor = int_a ^ int_b
    differing_bits = bin(xor).count('1')
    total_bits = len(hash_a) * 4
    return differing_bits / total_bits if total_bits > 0 else 0.0
```

In `_extract_with_opencv`, replace:
```python
if prev_hash is None or current_hash != prev_hash:
```
with:
```python
is_different = (
    prev_hash is None
    or self._hamming_distance(current_hash, prev_hash) > self.frame_diff_threshold
)
if is_different:
```

**Impact:** 1-minute screencast: ~50 frames → ~8-15 unique frames. 70-80% reduction in downstream work.

#### File 2: `engines/eyes-engine/main.py`

**Replace `_process_video` with scene-based pipeline.** Five new methods:

| Method | Purpose |
|--------|---------|
| `_process_video` | Rewritten: extract → batch OCR → scene grouping → scene LLaVA → result |
| `_batch_ocr(frames)` | OCR all frames WITHOUT GPU semaphore (CPU-bound) |
| `_group_into_scenes(frames, ocr_results)` | Group consecutive frames by dHash proximity |
| `_build_scene(frame_indices, frames, ocr_results)` | Build scene metadata, pick representative frame |
| `_analyze_scenes(job_id, scenes, total_frames, stages)` | ONE LLaVA call per scene, propagate to all frames |

**Architect 2's additions to incorporate:**

1. **Adaptive sampling rate** — Instead of fixed `max_fps_extract=1.0`, detect screen stability:
```python
# In _extract_with_opencv, after computing Hamming distance:
# If last N frames were all similar (distance < 0.02), increase frame_interval
# If sudden large change (distance > 0.3), decrease frame_interval briefly
consecutive_similar = 0
for i in range(1, len(frames)):
    dist = self._hamming_distance(frames[i-1]["hash"], frames[i]["hash"])
    if dist < 0.02:
        consecutive_similar += 1
        if consecutive_similar > 5:
            frame_interval = max(1, int(fps / 0.2))  # Drop to 1 frame per 5s
    else:
        consecutive_similar = 0
        frame_interval = max(1, int(fps / self.max_fps_extract))  # Back to normal
```

2. **Chunked long-video processing** (from both architects) — For videos > 10 minutes, split into 5-minute segments and process chunks in parallel:
```python
async def _process_video(self, job_id, video_path, session_id, tenant_id):
    """Full video analysis pipeline with chunking for long videos."""
    duration = self._get_video_duration(video_path)

    CHUNK_THRESHOLD_SECONDS = 600  # 10 minutes
    if duration > CHUNK_THRESHOLD_SECONDS:
        await self._process_video_chunked(job_id, video_path, session_id, tenant_id, duration)
    else:
        await self._process_video_single(job_id, video_path, session_id, tenant_id)


async def _process_video_chunked(self, job_id, video_path, session_id, tenant_id, duration):
    """Split long video into chunks, process in parallel, merge results."""
    CHUNK_SECONDS = 300  # 5-minute chunks
    chunk_count = math.ceil(duration / CHUNK_SECONDS)

    # Split video into chunks using ffmpeg
    chunk_paths = await self._split_video(video_path, CHUNK_SECONDS, job_id)

    # Process all chunks concurrently (bounded by GPU semaphore inside each)
    tasks = [
        self._process_single_chunk(job_id, chunk_path, session_id, tenant_id, idx, chunk_count)
        for idx, chunk_path in enumerate(chunk_paths)
    ]
    chunk_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Merge chunk results into single VisualAnalysisResult
    merged = self._merge_chunk_results(chunk_results, job_id, session_id, tenant_id)
    ...
```

**Add to `EyesConfig`:**
```python
scene_boundary_threshold: float = 0.15     # dHash distance for new scene
gpu_concurrency: int = 1                   # Raise on multi-GPU
chunk_threshold_seconds: float = 600.0     # Chunk videos longer than this
chunk_duration_seconds: float = 300.0      # Size of each chunk
adaptive_sampling: bool = True             # Adjust frame rate based on stability
```

**Performance (incorporating all three perspectives):**

| Video | Before | After (my plan only) | After (unified) |
|-------|--------|---------------------|-----------------|
| 1 min | 80+ min | 1-3 min (GPU) | 1-3 min (GPU) — same, chunking irrelevant |
| 30 min | 6-8 hrs (GPU) | 15-30 min | 8-15 min (parallel chunks + adaptive) |
| 2 hrs | 24-40 hrs (GPU) | 30-60 min | 15-30 min (24 parallel 5-min chunks) |

#### Phase 1 — Change Summary

| # | File | Change | Risk |
|---|------|--------|------|
| 1 | `engines/eyes-engine/app/frame_diff/__init__.py` | Hamming distance dedup, return hash | LOW — bug fix |
| 2 | `engines/eyes-engine/main.py` | Scene-based pipeline, chunked long-video, adaptive sampling, config expansion | MEDIUM — core logic rewrite |

---

### Phase 2: Canonical Artifact Chain + PostgreSQL Persistence

**Key design decision:** Architect 1 wants 13 granular stages. I had 3. The right answer is **7 stages** — enough granularity for milestone events (Architect 2's requirement) without over-decomposing stages that always run together:

| Stage | Engine | Depends On | Milestone Event |
|-------|--------|-----------|-----------------|
| `media_probe` | spine | (none) | `media_probed` — duration, format, codecs known |
| `audio_transcription` | ears | media_probe | `transcript_ready` — full transcript available |
| `pii_redaction` | shield | audio_transcription | `transcript_safe` — PII-redacted text ready |
| `visual_extraction` | eyes | media_probe | `keyframes_ready` — frames + OCR + scenes + LLaVA |
| `visual_graph_assembly` | spine | visual_extraction | `visual_graph_ready` — screen-flow graph built |
| `artifact_persistence` | spine | pii_redaction, visual_graph_assembly | `canonical_artifact_ready` — full artifact in PostgreSQL |
| `canonical_quality_gate` | brain | artifact_persistence | `quality_verified` — Brain confirms completeness |

Notes:
- `audio_transcription` and `visual_extraction` run in PARALLEL (no mutual dependency, only depend on media_probe)
- `pii_redaction` depends on audio only → can start before video finishes
- `canonical_quality_gate` is Brain as mandatory gate (Architect 1's recommendation) — if confidence < threshold, Brain re-requests selective enrichment
- Each stage emits a milestone event via EventBus so downstream consumers can start progressively (Architect 2's requirement)

#### File 3: NEW — `products/nexus-qa-orchestrator/app/workflows/builtin/canonical_processing.py`

```python
"""
Built-in Chain: Canonical Media Processing.

The "Process Once, Use Many" pipeline. 7-stage DAG:

    media_probe ─┬─→ audio_transcription → pii_redaction ──────────┐
                 └─→ visual_extraction → visual_graph_assembly ────┤
                                                                    ▼
                                                    artifact_persistence → canonical_quality_gate

Milestone events emitted at each stage completion for progressive downstream triggering.
"""

from ..schema import (
    ChainDefinition, StageDefinition, PollingConfig, RetryPolicy,
)


def build_canonical_processing_chain() -> ChainDefinition:
    return ChainDefinition(
        chain_id="nexus.canonical-processing",
        name="Canonical Media Processing",
        description="Process raw media once. All downstream chains consume the artifact.",
        version="2.0.0",
        tags=["canonical", "media", "processing", "foundation"],
        stages=[
            StageDefinition(
                stage_id="media_probe",
                name="Media Probe",
                description="Detect format, duration, codecs, resolution. Drives chunking decisions.",
                engine="spine",
                endpoint="/api/v1/spine/probe-media",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "video_file_id": "$workflow.input.video_file_id",
                    "audio_file_id": "$workflow.input.audio_file_id",
                },
                timeout_seconds=30,
                on_failure="fail",
            ),

            StageDefinition(
                stage_id="audio_transcription",
                name="Audio Transcription",
                description="Transcribe audio with speaker diarization via Ears engine",
                engine="ears",
                endpoint="/api/v1/ears/transcribe",
                request_type="multipart",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "language": "$workflow.input.language",
                },
                file_mappings={"audio": "$workflow.input.audio_file_id"},
                condition="$workflow.input.audio_file_id",
                depends_on=["media_probe"],
                timeout_seconds=900,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=5.0),
                on_failure="skip",
                polling=PollingConfig(
                    enabled=True,
                    job_id_path="job_id",
                    poll_endpoint="/api/v1/ears/jobs/{job_id}",
                    poll_interval_seconds=5.0,
                    max_poll_seconds=900.0,
                    completion_statuses=["completed"],
                    failure_statuses=["failed"],
                    result_path="result",
                    status_path="status",
                ),
            ),

            StageDefinition(
                stage_id="pii_redaction",
                name="PII Redaction",
                description="Detect and redact PII from transcript via Shield engine",
                engine="shield",
                endpoint="/api/v1/shield/redact",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "text": "$stages.audio_transcription.output.transcript_text",
                },
                depends_on=["audio_transcription"],
                condition="$stages.audio_transcription.output.transcript_text",
                timeout_seconds=60,
                on_failure="skip",
            ),

            StageDefinition(
                stage_id="visual_extraction",
                name="Visual Extraction",
                description="Scene-based video analysis via Eyes engine (chunked for long video)",
                engine="eyes",
                endpoint="/api/v1/eyes/analyze-video",
                request_type="multipart",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "duration_hint": "$stages.media_probe.output.duration_seconds",
                },
                file_mappings={"video": "$workflow.input.video_file_id"},
                condition="$workflow.input.video_file_id",
                depends_on=["media_probe"],
                timeout_seconds=1800,
                on_failure="skip",
                polling=PollingConfig(
                    enabled=True,
                    job_id_path="job_id",
                    poll_endpoint="/api/v1/eyes/jobs/{job_id}",
                    poll_interval_seconds=5.0,
                    max_poll_seconds=1800.0,
                    completion_statuses=["completed"],
                    failure_statuses=["failed"],
                    result_path="result",
                    status_path="status",
                ),
            ),

            StageDefinition(
                stage_id="visual_graph_assembly",
                name="Visual Graph Assembly",
                description="Build screen-flow graph from keyframes + OCR + scene transitions",
                engine="spine",
                endpoint="/api/v1/spine/build-visual-graph",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "visual_analysis": "$stages.visual_extraction.output",
                },
                depends_on=["visual_extraction"],
                condition="$stages.visual_extraction.output",
                timeout_seconds=120,
                on_failure="skip",
            ),

            StageDefinition(
                stage_id="artifact_persistence",
                name="Canonical Artifact Persistence",
                description="Combine all results into canonical artifact, persist to PostgreSQL + object storage",
                engine="spine",
                endpoint="/api/v1/spine/persist-canonical-artifact",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "media_probe": "$stages.media_probe.output",
                    "transcription": "$stages.audio_transcription.output",
                    "safe_transcript": "$stages.pii_redaction.output",
                    "visual_analysis": "$stages.visual_extraction.output",
                    "visual_graph": "$stages.visual_graph_assembly.output",
                },
                depends_on=["pii_redaction", "visual_graph_assembly"],
                timeout_seconds=60,
                on_failure="fail",
            ),

            StageDefinition(
                stage_id="canonical_quality_gate",
                name="Brain Quality Gate",
                description="Brain validates artifact completeness and confidence. Can re-request enrichment.",
                engine="brain",
                endpoint="/api/v1/brain/quality-gate",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "artifact_id": "$stages.artifact_persistence.output.artifact_id",
                    "artifact_summary": "$stages.artifact_persistence.output.summary",
                },
                depends_on=["artifact_persistence"],
                timeout_seconds=120,
                retry_policy=RetryPolicy(max_retries=1),
                on_failure="skip",
            ),
        ],
    )
```

#### File 4: `products/nexus-qa-orchestrator/app/workflows/builtin/__init__.py`

```python
# ADD import:
from .canonical_processing import build_canonical_processing_chain

# ADD to load_all_builtin_chains():
def load_all_builtin_chains() -> list[ChainDefinition]:
    return [
        build_canonical_processing_chain(),  # NEW — runs first
        build_qa_testing_chain(),
        build_compliance_audit_chain(),
        build_knowledge_capture_chain(),
        build_regression_suite_chain(),
    ]
```

#### File 5: `sdk/nexus-sdk/nexus_sdk/media/models.py`

**Add CanonicalMediaArtifact model** (from my original plan, enhanced with Architect 2's fields):

```python
class CanonicalMediaArtifact(BaseModel):
    """Canonical processed output. Created ONCE, consumed by all downstream chains."""
    artifact_id: str = ""
    session_id: str = ""
    tenant_id: str = ""
    media_fingerprint: str = ""               # NEW: SHA-256 of source files for re-upload detection

    # Media probe
    source_video_filename: str = ""
    source_audio_filename: str = ""
    duration_seconds: float = 0.0
    video_resolution: str = ""
    video_codec: str = ""

    # Audio results
    transcription: Optional[dict] = None
    safe_transcript_text: str = ""            # PII-redacted
    audio_job_id: str = ""

    # Video results
    visual_analysis: Optional[dict] = None
    visual_graph: Optional[dict] = None       # NEW: screen-flow graph (Architect 2)
    video_job_id: str = ""
    scene_count: int = 0
    frame_count: int = 0
    application_types_seen: list[str] = Field(default_factory=list)

    # Combined
    visual_summary: str = ""

    # Quality gate
    brain_quality_score: Optional[float] = None
    brain_confidence: Optional[float] = None
    quality_gate_passed: bool = False

    # Metadata
    processing_time_seconds: float = 0.0
    created_at: str = ""
    status: str = "pending"
    error: Optional[str] = None
```

#### File 6: Alembic migration — NEW `alembic/versions/xxx_add_canonical_artifacts.py`

```python
"""Add canonical_artifacts table and media_fingerprint cache.

Revision ID: (auto-generated)
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

def upgrade():
    op.create_table(
        "canonical_artifacts",
        sa.Column("artifact_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False, index=True),
        sa.Column("media_fingerprint", sa.String(128), nullable=True, index=True),
        sa.Column("status", sa.String(30), default="pending"),
        sa.Column("duration_seconds", sa.Float, default=0.0),
        sa.Column("scene_count", sa.Integer, default=0),
        sa.Column("frame_count", sa.Integer, default=0),
        sa.Column("safe_transcript_text", sa.Text, default=""),
        sa.Column("visual_summary", sa.Text, default=""),
        sa.Column("application_types_seen", postgresql.JSON, default=[]),
        sa.Column("brain_quality_score", sa.Float, nullable=True),
        sa.Column("quality_gate_passed", sa.Boolean, default=False),
        sa.Column("full_artifact_json", postgresql.JSON, nullable=True),
        sa.Column("processing_time_seconds", sa.Float, default=0.0),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index("ix_canonical_artifacts_tenant_session", "canonical_artifacts", ["tenant_id", "session_id"])
    op.create_index("ix_canonical_artifacts_fingerprint", "canonical_artifacts", ["media_fingerprint"])


def downgrade():
    op.drop_table("canonical_artifacts")
```

#### File 7: `sdk/nexus-sdk/nexus_sdk/db/models.py`

**Add `CanonicalArtifactRow` ORM model:**

```python
class CanonicalArtifactRow(Base):
    """Canonical media artifact — process once, use many."""
    __tablename__ = "canonical_artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    media_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    scene_count: Mapped[int] = mapped_column(Integer, default=0)
    frame_count: Mapped[int] = mapped_column(Integer, default=0)
    safe_transcript_text: Mapped[str] = mapped_column(Text, default="")
    visual_summary: Mapped[str] = mapped_column(Text, default="")
    application_types_seen: Mapped[list] = mapped_column(JSON, default=list)
    brain_quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_gate_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    full_artifact_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processing_time_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_canonical_artifacts_tenant_session", "tenant_id", "session_id"),
        Index("ix_canonical_artifacts_fingerprint", "media_fingerprint"),
    )
```

#### File 8: `engines/spine-engine/main.py`

**Add 4 new endpoints** (replaces my original 2-endpoint plan):

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/spine/probe-media` | ffprobe to get duration, codecs, resolution |
| `POST /api/v1/spine/build-visual-graph` | Build screen-flow graph from scenes (Architect 2) |
| `POST /api/v1/spine/persist-canonical-artifact` | Store artifact in PostgreSQL + object storage |
| `GET /api/v1/spine/artifacts/{session_id}` | Retrieve canonical artifact |

The `persist-canonical-artifact` endpoint:
1. Computes `media_fingerprint` (SHA-256 of source file) for re-upload detection (Architect 2)
2. Checks if fingerprint already exists → return cached artifact (skip reprocessing)
3. Stores full artifact JSON in `canonical_artifacts` table
4. Stores large blobs (frame images) in object storage (local filesystem or S3)
5. Emits `canonical_artifact_ready` event via EventBus

#### Phase 2 — Change Summary

| # | File | Change | Risk |
|---|------|--------|------|
| 1 | NEW: `products/nexus-qa-orchestrator/app/workflows/builtin/canonical_processing.py` | 7-stage canonical chain | LOW — new file |
| 2 | `products/nexus-qa-orchestrator/app/workflows/builtin/__init__.py` | Register canonical chain | LOW |
| 3 | `sdk/nexus-sdk/nexus_sdk/media/models.py` | Add CanonicalMediaArtifact | LOW |
| 4 | NEW: `alembic/versions/xxx_add_canonical_artifacts.py` | PostgreSQL table for artifacts | LOW |
| 5 | `sdk/nexus-sdk/nexus_sdk/db/models.py` | Add CanonicalArtifactRow ORM | LOW |
| 6 | `engines/spine-engine/main.py` | Add 4 new endpoints (probe, graph, persist, retrieve) | MEDIUM |

---

### Phase 3: UI Migration (Client → New Orchestrator)

**Why:** Client currently calls legacy `/v1/qa/sessions/` endpoints. These must switch to the new orchestrator's `/v1/orchestrator/process` endpoint.

#### File 9: `client/src/services/api.ts`

**Replace legacy upload functions with new canonical processing trigger:**

```typescript
// REPLACE uploadSessionAudio + uploadSessionVideo + runSessionPipeline
// with a single function:

export async function startCanonicalProcessing(params: {
  sessionId: string;
  audioFile?: File;
  videoFile?: File;
  documentFiles?: File[];
  consumerChainId?: string;
  language?: string;
}): Promise<{ workflow_id: string; status: string }> {
  const formData = new FormData();
  formData.append('session_id', params.sessionId);
  if (params.audioFile) formData.append('audio', params.audioFile);
  if (params.videoFile) formData.append('video', params.videoFile);
  if (params.documentFiles) {
    params.documentFiles.forEach(f => formData.append('documents', f));
  }
  if (params.consumerChainId) formData.append('consumer_chain_id', params.consumerChainId);
  if (params.language) formData.append('language', params.language);

  const response = await apiClient.post('/v1/orchestrator/process', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return response.data;
}

// ADD: SSE-based progress streaming (Architect 1's Phase 9)
export function streamWorkflowProgress(
  workflowId: string,
  onProgress: (event: WorkflowProgressEvent) => void,
  onComplete: (result: any) => void,
  onError: (error: any) => void,
): () => void {
  const eventSource = new EventSource(
    `${API_BASE}/v1/orchestrator/workflows/${workflowId}/stream`
  );

  eventSource.addEventListener('progress', (e) => {
    onProgress(JSON.parse(e.data));
  });
  eventSource.addEventListener('complete', (e) => {
    onComplete(JSON.parse(e.data));
    eventSource.close();
  });
  eventSource.addEventListener('error', (e) => {
    onError(e);
    eventSource.close();
  });

  return () => eventSource.close();
}
```

**Keep the legacy functions but mark as deprecated** — don't delete them until the legacy QA orchestrator is fully retired:

```typescript
/** @deprecated Use startCanonicalProcessing instead */
export async function uploadSessionAudio(...) { ... }
/** @deprecated Use startCanonicalProcessing instead */
export async function uploadSessionVideo(...) { ... }
/** @deprecated Use startCanonicalProcessing instead */
export async function runSessionPipeline(...) { ... }
```

#### File 10: `client/src/pages/SessionCommandPage.tsx` (or equivalent session page)

**Replace the 3-step upload flow with single canonical processing call:**

Current flow:
```
createQASession() → uploadSessionAudio() → uploadSessionVideo() → runSessionPipeline()
```

New flow:
```
createQASession() → startCanonicalProcessing({ sessionId, audioFile, videoFile, consumerChainId: 'nexus.qa-testing' })
                   → streamWorkflowProgress(workflowId, onProgress, onComplete, onError)
```

The onProgress callback updates the UI's progress bar with stage-level detail (which stage is running, percent complete).

#### File 11: `products/nexus-qa-orchestrator/app/main.py`

**Add `POST /api/v1/orchestrator/process` endpoint** (from my original plan, enhanced):

```python
@app.post("/api/v1/orchestrator/process")
async def start_canonical_processing(
    req: CanonicalProcessingRequest,
    background_tasks: BackgroundTasks,
    user: NexusUser = Depends(get_current_user),
):
    """
    Upload media → canonical processing → optional consumer chain.
    Primary entry point replacing legacy /v1/qa/ routes.
    """
    # Check media fingerprint for re-upload detection (Architect 2)
    fingerprint = await _compute_media_fingerprint(req)
    existing = await artifact_store.find_by_fingerprint(req.tenant_id, fingerprint)
    if existing and existing.quality_gate_passed:
        # Skip canonical processing — artifact already exists
        if req.consumer_chain_id:
            # Go straight to consumer chain
            return await _start_consumer_only(existing, req, user)
        return {"workflow_id": None, "artifact_id": existing.artifact_id, "status": "cached"}

    # Start canonical processing
    canonical_chain = await registry.get("nexus.canonical-processing")
    instance = await chain_engine.start(
        chain=canonical_chain,
        tenant_id=req.tenant_id,
        session_id=req.session_id,
        input_data={
            "audio_file_id": req.audio_file_id,
            "video_file_id": req.video_file_id,
            "language": req.language,
            "media_fingerprint": fingerprint,
        },
        created_by=user.user_id,
    )

    background_tasks.add_task(
        _run_canonical_then_consumer,
        workflow_id=instance.workflow_id,
        canonical_chain=canonical_chain,
        consumer_chain_id=req.consumer_chain_id,
        consumer_input_data={**req.consumer_input_data, "document_file_ids": req.document_file_ids},
        tenant_id=req.tenant_id,
        session_id=req.session_id,
        user_id=user.user_id,
    )

    return StartWorkflowResponse(
        workflow_id=instance.workflow_id,
        chain_id=canonical_chain.chain_id,
        status=instance.status,
        session_id=instance.session_id,
    )
```

**Add SSE streaming endpoint:**

```python
@app.get("/api/v1/orchestrator/workflows/{workflow_id}/stream")
async def stream_workflow_progress(
    workflow_id: str,
    user: NexusUser = Depends(get_current_user),
):
    """SSE endpoint for real-time workflow progress."""
    async def event_generator():
        while True:
            instance = await workflow_store.get_instance(workflow_id)
            if not instance:
                yield {"event": "error", "data": json.dumps({"error": "not_found"})}
                break

            progress = {
                "workflow_id": workflow_id,
                "status": instance.status.value,
                "current_stage": instance.current_stage_id,
                "progress_percent": instance.progress_percent,
                "stages": [
                    {"stage_id": s.stage_id, "status": s.status.value, "duration": s.duration_seconds}
                    for s in instance.stage_executions
                ],
            }
            yield {"event": "progress", "data": json.dumps(progress)}

            if instance.status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED):
                yield {"event": "complete", "data": json.dumps(progress)}
                break

            await asyncio.sleep(2)

    return EventSourceResponse(event_generator())
```

#### File 12: `platform/gateway/app/routes.py`

**Add route for the new orchestrator process endpoint** (it may already be covered by the `/api/v1/orchestrator` prefix route, but verify):

The gateway already has:
```python
routes = {
    "/api/v1/orchestrator": cfg.orchestrator_url,
}
```
This should already proxy `/api/v1/orchestrator/process` → nexus-qa-orchestrator. **No changes needed** if the proxy is path-prefix based. But verify the proxy correctly forwards multipart/form-data and SSE responses.

For SSE, the nginx ConfigMap in `client.yaml` already has:
```
proxy_set_header Connection '';
proxy_http_version 1.1;
```
This supports SSE. **Verify** the gateway Python code also forwards SSE correctly (no response buffering).

#### Phase 3 — Change Summary

| # | File | Change | Risk |
|---|------|--------|------|
| 1 | `client/src/services/api.ts` | Add `startCanonicalProcessing()` + `streamWorkflowProgress()`, deprecate legacy | MEDIUM |
| 2 | `client/src/pages/SessionCommandPage.tsx` | Replace 3-step flow with single call + SSE progress | MEDIUM |
| 3 | `products/nexus-qa-orchestrator/app/main.py` | Add `/process` + `/stream` endpoints, fingerprint caching | MEDIUM |
| 4 | `platform/gateway/app/routes.py` | Verify SSE passthrough (likely no change needed) | LOW |

---

### Phase 4: Rewire Consumer Chains

Consumer chains (QA Testing, Knowledge Capture, Compliance Audit, Regression Suite) currently have their own `transcription` and `visual_analysis` stages calling Ears/Eyes directly. Replace those with `fetch_artifact` stages.

#### File 13: `products/nexus-qa-orchestrator/app/workflows/builtin/qa_testing.py`

**Remove:** `transcription` stage, `visual_analysis` stage
**Add:** `fetch_artifact` stage at position 0
**Update:** All `depends_on` and `input_mapping` references

```python
StageDefinition(
    stage_id="fetch_artifact",
    name="Fetch Canonical Artifact",
    engine="spine",
    endpoint="/api/v1/spine/artifacts/{session_id}",
    method="GET",
    input_mapping={
        "tenant_id": "$workflow.tenant_id",
        "session_id": "$workflow.session_id",
    },
    timeout_seconds=30,
    on_failure="fail",
),
# pii_redaction: depends_on=["fetch_artifact"], text=$stages.fetch_artifact.output.safe_transcript_text
# rule_extraction: depends_on=["fetch_artifact", "document_ingestion"], visual_context=$stages.fetch_artifact.output.visual_analysis
```

#### File 14: `products/nexus-qa-orchestrator/app/workflows/builtin/knowledge_capture.py`

**Same pattern.** Replace transcription + visual_analysis with fetch_artifact.

#### File 15: `products/nexus-qa-orchestrator/app/workflows/builtin/compliance_audit.py`

**Check if it has audio/video stages.** If yes, same pattern. If no (document-only), no changes needed.

#### Phase 4 — Change Summary

| # | File | Change | Risk |
|---|------|--------|------|
| 1 | `products/nexus-qa-orchestrator/app/workflows/builtin/qa_testing.py` | Replace media stages with fetch_artifact | MEDIUM |
| 2 | `products/nexus-qa-orchestrator/app/workflows/builtin/knowledge_capture.py` | Same | MEDIUM |
| 3 | `products/nexus-qa-orchestrator/app/workflows/builtin/compliance_audit.py` | Conditional — only if it has media stages | LOW |

---

### Phase 5: Wire Missions to Real Workflow Execution

**Problem confirmed:** `MissionOrchestrator.execute_stage()` exists with full engine-mapping logic but is NEVER called. The missions router `advance` endpoint just flips status fields in DB.

#### File 16: `platform/api/app/routers/missions.py`

**Modify `start_stage` and `advance` endpoints to actually trigger engine work:**

```python
@router.post("/api/v1/missions/{mission_id}/stages/{stage_number}/start")
async def start_stage(
    mission_id: str = Path(...),
    stage_number: int = Path(..., ge=1, le=5),
    user: dict = Depends(get_current_user),
):
    factory = require_db()
    async with factory() as db:
        mission = await _load_mission_with_stages(db, mission_id)
        # ... existing status validation ...

        stage = _get_stage(mission, stage_number)
        stage.status = "active"
        stage.started_at = utc_now()
        mission.current_stage = stage_number
        await db.commit()

        # NEW: Actually execute the stage via MissionOrchestrator
        orchestrator = MissionOrchestrator()  # or inject via dependency
        try:
            result = await orchestrator.execute_stage(
                mission_id=mission_id,
                stage_type=STAGE_TYPES[stage_number],
                stage_inputs=stage.inputs,
                context=mission.context,
            )
            # Update stage with engine results
            stage.outputs = result
            stage.engine_calls = result.get("engine_calls", [])
            await db.commit()
        except Exception as exc:
            stage.status = "failed"
            stage.error_message = str(exc)
            await db.commit()
            raise HTTPException(500, f"Stage execution failed: {exc}")

        return _stage_to_response(stage)
```

For the **Capture** stage specifically, integrate with the canonical pipeline:
- When stage 1 (capture) starts with media files, trigger `nexus.canonical-processing` chain via the new orchestrator
- Store the `workflow_id` in `stage.metadata_json` for tracking
- When canonical processing completes (via SSE or polling), mark capture stage complete with the artifact_id in outputs

```python
# In start_stage, for capture stage:
if STAGE_TYPES[stage_number] == "capture" and stage.inputs.get("video_file_id"):
    # Trigger canonical processing
    response = await httpx.AsyncClient().post(
        f"{orchestrator_url}/api/v1/orchestrator/process",
        json={
            "tenant_id": mission.tenant_id,
            "session_id": stage.inputs.get("session_id", mission_id),
            "video_file_id": stage.inputs["video_file_id"],
            "audio_file_id": stage.inputs.get("audio_file_id"),
        },
        headers={"Authorization": f"Bearer {user_token}"},
    )
    workflow_data = response.json()
    stage.metadata_json["workflow_id"] = workflow_data["workflow_id"]
    await db.commit()
```

#### File 17: `platform/api/app/services/mission_orchestrator.py`

**No structural changes needed** — the code is already well-written with `STAGE_ENGINE_ACTIONS`, `execute_stage`, `call_engine`, `_build_payload`. It just needs to be imported and called from the missions router (which it currently isn't).

**Minor enhancement:** Add the canonical artifact ID to the context passed between stages:

```python
# In execute_stage, after successful engine call:
if stage_type == "capture" and result.get("artifact_id"):
    # Pass artifact ID to downstream stages
    context["canonical_artifact_id"] = result["artifact_id"]
```

#### Phase 5 — Change Summary

| # | File | Change | Risk |
|---|------|--------|------|
| 1 | `platform/api/app/routers/missions.py` | start_stage calls MissionOrchestrator.execute_stage(), capture stage triggers canonical pipeline | MEDIUM-HIGH |
| 2 | `platform/api/app/services/mission_orchestrator.py` | Pass artifact_id in context | LOW |

---

### Phase 6: Brain as Mandatory Quality Gate

**Architect 1's recommendation:** Brain should validate every canonical artifact before downstream chains consume it. If confidence < threshold, Brain can request selective enrichment (re-analyze specific frames with more expensive prompts).

#### File 18: `engines/brain-engine/main.py` (or equivalent)

**Add quality gate endpoint:**

```python
@app.post("/api/v1/brain/quality-gate")
async def quality_gate(req: NexusRequest, user: NexusUser = Depends(get_current_user)):
    """
    Validate canonical artifact completeness and confidence.
    Returns pass/fail with confidence score.
    Can request selective re-enrichment for low-confidence items.
    """
    artifact_summary = req.data.get("artifact_summary", {})

    # Check completeness
    has_transcript = bool(artifact_summary.get("transcript_text"))
    has_visual = bool(artifact_summary.get("visual_analysis"))
    has_scenes = artifact_summary.get("scene_count", 0) > 0

    # Use Brain's LLM to assess quality
    quality_assessment = await self._assess_quality(artifact_summary)

    passed = quality_assessment["confidence"] >= 0.7 and (has_transcript or has_visual)

    return NexusResponse(
        success=True,
        engine="brain",
        data={
            "passed": passed,
            "confidence": quality_assessment["confidence"],
            "completeness_score": quality_assessment["completeness"],
            "recommendations": quality_assessment.get("recommendations", []),
            "re_enrichment_needed": not passed and quality_assessment["confidence"] < 0.5,
        },
    )
```

#### Phase 6 — Change Summary

| # | File | Change | Risk |
|---|------|--------|------|
| 1 | `engines/brain-engine/main.py` | Add `/quality-gate` endpoint | LOW — additive |

---

### Phase 7: Platform Read Model Expansion

**Architect 1's recommendation:** Expose canonical artifacts, workflow status, and mission progress through platform API endpoints. Currently the platform API only serves sessions and missions — it doesn't expose the orchestrator's workflow state.

#### File 19: `platform/api/app/routers/` — new router or additions

Add read-only endpoints:
- `GET /api/v1/artifacts/{session_id}` — fetch canonical artifact from PostgreSQL
- `GET /api/v1/artifacts/{session_id}/status` — processing status
- `GET /api/v1/sessions/{session_id}/workflows` — list all workflows for a session
- `GET /api/v1/sessions/{session_id}/artifacts` — list all artifacts for a session

These are proxied from platform-api to the relevant backends, or read directly from PostgreSQL where the data is persisted.

#### Phase 7 — Change Summary

| # | File | Change | Risk |
|---|------|--------|------|
| 1 | `platform/api/app/routers/` (new or existing) | Add artifact + workflow read endpoints | LOW — additive |

---

## Complete Change Map

### Phase 0 — Helm & Infra (6 files)
| File | Type |
|------|------|
| `infrastructure/helm/nexus-qa/templates/engine-deployment.yaml` | MODIFY |
| `infrastructure/helm/nexus-qa/templates/engine-service.yaml` | MODIFY |
| `infrastructure/helm/nexus-qa/templates/configmap.yaml` | MODIFY |
| `infrastructure/helm/nexus-qa/templates/orchestrator.yaml` | MODIFY |
| `infrastructure/helm/nexus-qa/templates/platform-deployment.yaml` | MODIFY |
| `platform/gateway/app/config.py` | MODIFY |

### Phase 1 — Eyes Performance Fix (2 files)
| File | Type |
|------|------|
| `engines/eyes-engine/app/frame_diff/__init__.py` | MODIFY |
| `engines/eyes-engine/main.py` | MODIFY (major rewrite) |

### Phase 2 — Canonical Chain + Persistence (6 files)
| File | Type |
|------|------|
| `products/nexus-qa-orchestrator/app/workflows/builtin/canonical_processing.py` | CREATE |
| `products/nexus-qa-orchestrator/app/workflows/builtin/__init__.py` | MODIFY |
| `sdk/nexus-sdk/nexus_sdk/media/models.py` | MODIFY |
| `alembic/versions/xxx_add_canonical_artifacts.py` | CREATE |
| `sdk/nexus-sdk/nexus_sdk/db/models.py` | MODIFY |
| `engines/spine-engine/main.py` | MODIFY |

### Phase 3 — UI Migration (3-4 files)
| File | Type |
|------|------|
| `client/src/services/api.ts` | MODIFY |
| `client/src/pages/SessionCommandPage.tsx` | MODIFY |
| `products/nexus-qa-orchestrator/app/main.py` | MODIFY |
| `platform/gateway/app/routes.py` | VERIFY (likely no change) |

### Phase 4 — Consumer Chain Rewiring (2-3 files)
| File | Type |
|------|------|
| `products/nexus-qa-orchestrator/app/workflows/builtin/qa_testing.py` | MODIFY |
| `products/nexus-qa-orchestrator/app/workflows/builtin/knowledge_capture.py` | MODIFY |
| `products/nexus-qa-orchestrator/app/workflows/builtin/compliance_audit.py` | CONDITIONAL MODIFY |

### Phase 5 — Mission Wiring (2 files)
| File | Type |
|------|------|
| `platform/api/app/routers/missions.py` | MODIFY |
| `platform/api/app/services/mission_orchestrator.py` | MODIFY (minor) |

### Phase 6 — Brain Quality Gate (1 file)
| File | Type |
|------|------|
| `engines/brain-engine/main.py` | MODIFY |

### Phase 7 — Platform Read Model (1+ files)
| File | Type |
|------|------|
| `platform/api/app/routers/` | MODIFY or CREATE |

---

## Execution Order & Dependencies

```
Phase 0 ──→ Phase 1 ──→ Phase 2 ──→ Phase 3 ──→ Phase 4
(Helm fix)   (Eyes)      (Chain+DB)   (UI)         (Consumer)
                                        │
                                        └──→ Phase 5 ──→ Phase 6 ──→ Phase 7
                                             (Missions)   (Brain)     (Read Model)
```

**Phase 0 is prerequisite for ALL others** (Brain must deploy, URLs must resolve).

**Phase 1 can be done independently** after Phase 0 — immediate performance win.

**Phase 2 depends on Phase 1** (canonical chain calls the improved Eyes engine).

**Phases 3-4 depend on Phase 2** (UI and consumers need the canonical chain to exist).

**Phases 5-7 can proceed in parallel** with 3-4 once Phase 2 is done.

---

## Risk Matrix

| Phase | Risk | Mitigation |
|-------|------|-----------|
| 0 | LOW | Pure infra config. `helm template` validation. No code logic changes. |
| 1 | MEDIUM | Core Eyes rewrite. Do this behind a feature flag (`scene_grouping_enabled: bool = True`). If disabled, falls back to current per-frame logic. |
| 2 | LOW | New chain + DB table. Additive only. Existing chains unchanged until Phase 4. |
| 3 | MEDIUM | UI change. Keep legacy functions alive (deprecated). Feature flag in React: `USE_NEW_ORCHESTRATOR=true` env var. |
| 4 | MEDIUM | Consumer chain restructure. Can test each chain independently. Rollback = revert chain definitions. |
| 5 | MEDIUM-HIGH | Mission wiring. The `execute_stage` code calls engines — if engines are down, missions fail. Add timeout + graceful degradation. |
| 6 | LOW | Brain endpoint is advisory in the chain (`on_failure: "skip"`). If Brain fails, artifact still persists. |
| 7 | LOW | Read-only endpoints. No write risk. |

---

## What My Original Plan Got Right

1. **Exact Hamming distance code** — ready to implement, tested math
2. **Scene grouping algorithm** — frame-index based, representative frame selection, merged OCR
3. **Performance calculations** — 50→4 LLaVA calls reduction, specific speedup numbers
4. **Batch OCR pattern** — CPU-bound OCR outside GPU semaphore
5. **Canonical chain DAG shape** — parallel audio+video, assemble after both
6. **Consumer chain rewiring** — fetch_artifact pattern with $-path input_mapping

## What the Architects Added That I Missed

1. **Helm topology must be fixed first** — Brain won't deploy, URLs won't resolve (Architect 1)
2. **Gateway env var aliases** — pydantic-settings won't bind without aliases (Architect 1)
3. **PostgreSQL persistence + Alembic** — Redis-only is not durable for production (Architect 1)
4. **Chunked long-video processing** — 2-hour video needs parallel 5-min chunks (Both)
5. **Adaptive sampling rate** — reduce frame extraction rate during stable screens (Architect 2)
6. **Media fingerprint caching** — skip reprocessing on re-upload (Architect 2)
7. **Screen-flow graph** — reusable visual substrate for downstream consumers (Architect 2)
8. **Fast path / deep path** — two-tier processing for urgency tradeoffs (Architect 2)
9. **SSE progress streaming** — UI needs real-time stage progress (Architect 1)
10. **Brain as mandatory quality gate** — confidence validation before downstream (Architect 1)
11. **Mission wiring** — execute_stage must actually be called (Architect 1)
12. **UI migration** — client must switch from legacy path (Architect 1)
13. **Media probe stage** — know duration/format before processing (Architect 1)
14. **Milestone events** — partial-readiness events for progressive triggering (Architect 2)

---

## Recommendation

**Start with Phase 0 + Phase 1 together.** Phase 0 is pure config (low risk, high impact — nothing works in K8s without it). Phase 1 is the single biggest performance win (80+ minutes → 1-3 minutes for a 1-minute video). Together they give a deployment that actually works AND runs fast.

Then Phase 2 (canonical chain + persistence) establishes the "process once" foundation. Phase 3 (UI migration) makes it user-facing. Phases 4-7 extend the architecture to consumer chains, missions, quality gates, and read models.

This order maximizes visible progress at each step and allows testing of each phase independently before moving to the next.
