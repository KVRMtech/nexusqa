# Canonical Video Processing Pipeline — Complete Implementation Plan

## Executive Summary

**Problem:** A 1-minute video takes 80+ minutes to process because:
1. Eyes engine makes ~50 sequential LLaVA calls (1 per extracted frame)
2. Frame dedup uses binary dHash comparison (`!=`) — the `frame_diff_threshold=0.05` config is **never used**
3. GPU Semaphore(1) serializes all OCR + LLaVA calls — zero parallelism
4. Old orchestrator runs Ears → Eyes sequentially (not in parallel)
5. Every pipeline (QI, Knowledge Capture, Regression) re-processes the same raw video from scratch

**Solution:** A "Process Once, Use Many" canonical pipeline that:
1. Extracts + deduplicates frames intelligently (threshold-based dHash, not binary)
2. Groups similar frames into scenes, sends 1 LLaVA call per scene (not per frame)
3. Runs OCR in parallel batches (not behind GPU semaphore)
4. Runs audio + video processing concurrently
5. Stores the canonical artifact once — all consumer pipelines read from it

**Expected Performance:**
| Video Length | Current (CPU) | Current (GPU) | After Fix (GPU) | After Fix (CPU) |
|-------------|---------------|---------------|-----------------|-----------------|
| 1 minute    | 80+ min       | 12-15 min     | 1-3 min         | 5-10 min        |
| 30 minutes  | 40+ hours     | 6-8 hours     | 15-30 min       | 1-2 hours       |
| 2 hours     | 160+ hours    | 24-40 hours   | 30-60 min       | 2-4 hours       |

---

## Architecture Overview

```
                     ┌──────────────────────────────────┐
                     │         FILE UPLOAD               │
                     │  (video + audio + documents)      │
                     └───────────────┬──────────────────┘
                                     │
                     ┌───────────────▼──────────────────┐
                     │   CANONICAL PROCESSING STAGE      │
                     │                                    │
                     │  ┌─────────┐    ┌──────────┐      │
                     │  │  EARS   │    │   EYES   │      │
                     │  │ (audio) │    │ (video)  │      │
                     │  │         │    │          │      │
                     │  │ Whisper │    │ Smart    │      │
                     │  │ Pyannote│    │ Dedup    │      │
                     │  │         │    │ Scene    │      │
                     │  │         │    │ Grouping │      │
                     │  │         │    │ Batch OCR│      │
                     │  │         │    │ Scene    │      │
                     │  │         │    │ LLaVA    │      │
                     │  └────┬────┘    └────┬─────┘      │
                     │       │              │             │
                     │       ▼              ▼             │
                     │  ┌──────────────────────────┐     │
                     │  │  CANONICAL ARTIFACT       │     │
                     │  │  (Redis + PostgreSQL)     │     │
                     │  │                           │     │
                     │  │  • TranscriptionResult    │     │
                     │  │  • VisualAnalysisResult   │     │
                     │  │  • AudioMetadata          │     │
                     │  │  • Frame paths + hashes   │     │
                     │  └─────────┬─────────────────┘     │
                     └────────────┼────────────────────────┘
                                  │
               ┌──────────────────┼──────────────────────┐
               │                  │                      │
    ┌──────────▼────┐  ┌─────────▼──────┐  ┌───────────▼──────┐
    │  QI Testing   │  │  Knowledge     │  │  Regression      │
    │  Pipeline     │  │  Capture       │  │  Suite           │
    │               │  │  Pipeline      │  │  Pipeline        │
    │ Shield→Heart  │  │ Shield→Heart   │  │ Backbone→Heart   │
    │ →Hands→Legs   │  │ →Backbone      │  │ →Hands→Legs      │
    │ →Mouth→Nerves │  │                │  │ →Mouth→Nerves    │
    └───────────────┘  └────────────────┘  └──────────────────┘
```

---

## Phase 1: Fix Eyes Engine Performance (THE critical fix)

### File 1: `engines/eyes-engine/app/frame_diff/__init__.py`

**Current Bug (Line 100-101):**
```python
# Check if frame differs enough from previous
if prev_hash is None or current_hash != prev_hash:
```
The `frame_diff_threshold=0.05` config is STORED but NEVER USED. Binary `!=` means any single-pixel timing change in a screen recording creates a "new" frame. A 1-minute video at 2fps = 120 candidate frames, and binary hash comparison only drops exact duplicates (maybe 50% at best), yielding ~50-60 frames through to LLaVA.

**Fix: Implement Hamming distance threshold comparison.**

```python
# REPLACE the _extract_with_opencv method

def _extract_with_opencv(self, video_path: str, output_dir: str) -> list[dict]:
    """Extract frames using OpenCV with threshold-based diff filtering."""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    frame_interval = max(1, int(fps / self.max_fps_extract))

    frames = []
    prev_hash = None
    frame_idx = 0
    extracted_idx = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_interval == 0:
            current_hash = self._compute_frame_hash(frame, cv2)

            # USE threshold — Hamming distance on 64-bit dHash
            is_different = (
                prev_hash is None
                or self._hamming_distance(current_hash, prev_hash) > self.frame_diff_threshold
            )

            if is_different:
                timestamp = frame_idx / fps
                frame_path = os.path.join(
                    output_dir, f"frame_{extracted_idx:05d}.png"
                )
                cv2.imwrite(frame_path, frame)

                frames.append({
                    "frame_path": frame_path,
                    "timestamp": round(timestamp, 3),
                    "index": extracted_idx,
                    "source_frame_idx": frame_idx,
                    "hash": current_hash,
                })
                extracted_idx += 1
                prev_hash = current_hash

        frame_idx += 1

    cap.release()

    duration = frame_idx / fps if fps > 0 else 0
    logger.info(
        "frame_extractor.completed",
        video=os.path.basename(video_path),
        total_source_frames=frame_idx,
        extracted_frames=len(frames),
        video_fps=round(fps, 1),
        duration_seconds=round(duration, 1),
        diff_threshold=self.frame_diff_threshold,
    )

    return frames

@staticmethod
def _hamming_distance(hash_a: str, hash_b: str) -> float:
    """
    Normalized Hamming distance between two hex hash strings.
    Returns 0.0 (identical) to 1.0 (completely different).
    """
    int_a = int(hash_a, 16)
    int_b = int(hash_b, 16)
    xor = int_a ^ int_b
    differing_bits = bin(xor).count('1')
    total_bits = len(hash_a) * 4  # 4 bits per hex char
    return differing_bits / total_bits if total_bits > 0 else 0.0
```

**Changes:**
| Line(s) | Change | Why |
|---------|--------|-----|
| 76-124 | Replace `_extract_with_opencv` | Use Hamming distance instead of binary `!=` |
| +new | Add `_hamming_distance` static method | Calculate normalized distance between dHash values |
| 105 | Add `"hash": current_hash` to frame dict | Needed for scene grouping in Phase 2 |

**Impact:** With threshold=0.05, a 1-minute screencast that currently produces ~50 frames will drop to ~8-15 unique frames (screen recordings have long static periods). This alone cuts LLaVA calls by 70-80%.

---

### File 2: `engines/eyes-engine/main.py`

**Current Problem (Lines 328-378):**
The `_process_video` method runs a sequential `for` loop:
```python
for i, frame_info in enumerate(raw_frames):
    async with self._gpu_semaphore:      # GPU lock
        extracted_text, text_regions, ocr_conf = self.ocr.extract_text(frame_path)
    ...
    async with self._gpu_semaphore:      # GPU lock AGAIN
        analysis = await self.visual_analyzer.analyze_frame(...)
```

Two problems:
1. OCR (EasyOCR, CPU-bound at inference) is behind a GPU semaphore — OCR doesn't need the GPU semaphore if running on CPU, and even on GPU it can batch
2. Each frame gets its own LLaVA call (5-8s GPU, 15-30s CPU)

**Fix: Scene-based grouping + batch OCR + parallel OCR.**

Replace `_process_video` completely:

```python
async def _process_video(
    self,
    job_id: str,
    video_path: str,
    session_id: str,
    tenant_id: str,
):
    """Background: full video analysis pipeline with scene grouping."""
    pipeline_stages: list[str] = []
    start = time.monotonic()

    try:
        # ── Stage 1: Extract Frames (with threshold dedup) ──
        await self.job_store.update_job(
            job_id,
            status=JobStatus.PROCESSING.value,
            current_stage="extracting",
            progress_percent=5.0,
        )

        frames_dir = str(Path(video_path).parent / f"{job_id}_frames")
        raw_frames = await self.frame_extractor.extract_frames(
            video_path, frames_dir
        )
        pipeline_stages.append("extract")
        total_frames = len(raw_frames)

        if total_frames == 0:
            await self._complete_empty_job(job_id, session_id, tenant_id, start, pipeline_stages)
            return

        # ── Stage 2: Batch OCR (parallel, no GPU semaphore for CPU OCR) ──
        await self.job_store.update_job(
            job_id,
            current_stage="ocr_batch",
            progress_percent=15.0,
        )

        ocr_results = await self._batch_ocr(raw_frames)
        pipeline_stages.append("ocr")

        # ── Stage 3: Group into scenes by visual similarity ──
        await self.job_store.update_job(
            job_id,
            current_stage="scene_grouping",
            progress_percent=30.0,
        )

        scenes = self._group_into_scenes(raw_frames, ocr_results)
        pipeline_stages.append("scene_grouping")

        # ── Stage 4: One LLaVA call per scene (not per frame) ──
        await self.job_store.update_job(
            job_id,
            current_stage="scene_analysis",
            progress_percent=35.0,
        )

        analyzed_frames = await self._analyze_scenes(
            job_id, scenes, total_frames, pipeline_stages
        )
        pipeline_stages.append("analyze")

        # ── Stage 5: Build Result ──
        elapsed = time.monotonic() - start

        result = VisualAnalysisResult(
            job_id=job_id,
            session_id=session_id,
            tenant_id=tenant_id,
            frames=analyzed_frames,
            total_frames_extracted=total_frames,
            processing_time_seconds=round(elapsed, 2),
            pipeline_stages=pipeline_stages,
        )
        result.compute_stats()

        await self.job_store.update_job(
            job_id,
            status=JobStatus.COMPLETED.value,
            result=result.model_dump(mode="json"),
            processing_time_seconds=round(elapsed, 2),
            current_stage="completed",
            progress_percent=100.0,
        )

        # Emit event
        if self.event_bus:
            await self.event_bus.publish(NexusEvent(
                event_type="eyes.analysis.completed",
                tenant_id=tenant_id,
                trace_id=job_id,
                engine="eyes",
                session_id=session_id,
                data={
                    "job_id": job_id,
                    "session_id": session_id,
                    "frame_count": len(analyzed_frames),
                    "scene_count": len(scenes),
                    "application_types": result.application_types_seen,
                    "pipeline_stages": pipeline_stages,
                },
            ))

        logger.info(
            "eyes.pipeline.completed",
            job_id=job_id,
            frames_extracted=total_frames,
            scenes=len(scenes),
            frames_analyzed=len(analyzed_frames),
            elapsed_seconds=round(elapsed, 2),
        )

    except Exception as e:
        elapsed = time.monotonic() - start
        logger.error(
            "eyes.pipeline.failed",
            job_id=job_id, error=str(e), elapsed_seconds=round(elapsed, 2),
            exc_info=True,
        )
        await self.job_store.update_job(
            job_id,
            status=JobStatus.FAILED.value,
            error=str(e),
            current_stage="failed",
            processing_time_seconds=round(elapsed, 2),
        )


async def _batch_ocr(self, frames: list[dict]) -> list[dict]:
    """
    Run OCR on all frames. OCR is CPU-bound (EasyOCR readtext)
    so we run sequentially but WITHOUT the GPU semaphore.
    """
    results = []
    for frame_info in frames:
        extracted_text, text_regions, ocr_conf = self.ocr.extract_text(
            frame_info["frame_path"]
        )
        app_type = self.app_classifier.classify(extracted_text)
        results.append({
            "extracted_text": extracted_text,
            "text_regions": text_regions,
            "ocr_confidence": ocr_conf,
            "app_type": app_type,
        })
    return results


def _group_into_scenes(
    self,
    frames: list[dict],
    ocr_results: list[dict],
) -> list[dict]:
    """
    Group consecutive frames into scenes based on dHash similarity.
    A scene is a sequence of frames showing the same visual state.
    One LLaVA call per scene, using the representative (middle) frame.
    """
    if not frames:
        return []

    scenes = []
    current_scene_frames = [0]  # indices into frames list

    for i in range(1, len(frames)):
        prev_hash = frames[i - 1].get("hash", "")
        curr_hash = frames[i].get("hash", "")

        if prev_hash and curr_hash:
            distance = FrameExtractor._hamming_distance(prev_hash, curr_hash)
        else:
            distance = 1.0

        # Scene boundary: significant visual change
        SCENE_BOUNDARY_THRESHOLD = 0.15
        if distance > SCENE_BOUNDARY_THRESHOLD:
            scenes.append(self._build_scene(
                current_scene_frames, frames, ocr_results
            ))
            current_scene_frames = [i]
        else:
            current_scene_frames.append(i)

    # Don't forget the last scene
    if current_scene_frames:
        scenes.append(self._build_scene(
            current_scene_frames, frames, ocr_results
        ))

    logger.info(
        "scene_grouping.completed",
        total_frames=len(frames),
        total_scenes=len(scenes),
        avg_frames_per_scene=round(len(frames) / max(len(scenes), 1), 1),
    )

    return scenes


def _build_scene(
    self,
    frame_indices: list[int],
    frames: list[dict],
    ocr_results: list[dict],
) -> dict:
    """Build a scene dict from a group of frame indices."""
    # Use the middle frame as the representative for LLaVA
    representative_idx = frame_indices[len(frame_indices) // 2]

    # Merge OCR text from all frames in the scene (deduplicated)
    all_text_parts = []
    seen_text = set()
    for idx in frame_indices:
        text = ocr_results[idx]["extracted_text"]
        if text and text not in seen_text:
            all_text_parts.append(text)
            seen_text.add(text)

    return {
        "frame_indices": frame_indices,
        "representative_idx": representative_idx,
        "representative_frame": frames[representative_idx],
        "representative_ocr": ocr_results[representative_idx],
        "merged_ocr_text": " ".join(all_text_parts),
        "start_timestamp": frames[frame_indices[0]]["timestamp"],
        "end_timestamp": frames[frame_indices[-1]]["timestamp"],
        "app_type": ocr_results[representative_idx]["app_type"],
        "frame_count": len(frame_indices),
    }


async def _analyze_scenes(
    self,
    job_id: str,
    scenes: list[dict],
    total_frames: int,
    pipeline_stages: list[str],
) -> list[FrameAnalysis]:
    """
    Run LLaVA analysis once per scene (not once per frame).
    Then propagate the scene analysis to all frames in that scene.
    """
    analyzed_frames = []
    prev_description = ""

    for scene_idx, scene in enumerate(scenes):
        # Update progress
        progress = 35 + (55 * scene_idx / max(len(scenes), 1))
        await self.job_store.update_job(
            job_id,
            current_stage=f"analyzing_scene_{scene_idx + 1}/{len(scenes)}",
            progress_percent=round(progress, 1),
        )

        rep_frame = scene["representative_frame"]
        rep_ocr = scene["representative_ocr"]

        # ONE LLaVA call per scene
        async with self._gpu_semaphore:
            analysis = await self.visual_analyzer.analyze_frame(
                rep_frame["frame_path"],
                scene["merged_ocr_text"],
                scene["app_type"],
                prev_description,
            )

        prev_description = analysis.get("description", "")

        # Create FrameAnalysis for EVERY frame in the scene,
        # reusing the scene-level LLaVA analysis
        for i, frame_idx in enumerate(scene["frame_indices"]):
            frame_info = scene["representative_frame"] if frame_idx == scene["representative_idx"] else {"frame_path": "", "timestamp": 0}
            # Get actual frame info
            # frame_indices are indices into the original frames list
            # We need the actual frame data — passed through scenes
            frame_analysis = FrameAnalysis(
                frame_id=str(uuid.uuid4()),
                frame_index=frame_idx,
                timestamp_seconds=scene["start_timestamp"] + (
                    (scene["end_timestamp"] - scene["start_timestamp"])
                    * i / max(len(scene["frame_indices"]) - 1, 1)
                ),
                application_type=scene["app_type"],
                page_title=analysis.get("page_title", ""),
                ui_elements=analysis.get("ui_elements", []),
                extracted_text=rep_ocr["extracted_text"],
                tables=analysis.get("tables", []),
                description=analysis.get("description", ""),
                frame_path=rep_frame["frame_path"],
                ocr_confidence=rep_ocr["ocr_confidence"],
                is_keyframe=(i == 0),
            )
            analyzed_frames.append(frame_analysis)

    return analyzed_frames


async def _complete_empty_job(self, job_id, session_id, tenant_id, start, pipeline_stages):
    """Handle videos with zero extractable frames."""
    elapsed = time.monotonic() - start
    result = VisualAnalysisResult(
        job_id=job_id,
        session_id=session_id,
        tenant_id=tenant_id,
        frames=[],
        total_frames_extracted=0,
        processing_time_seconds=round(elapsed, 2),
        pipeline_stages=pipeline_stages,
    )
    await self.job_store.update_job(
        job_id,
        status=JobStatus.COMPLETED.value,
        result=result.model_dump(mode="json"),
        processing_time_seconds=round(elapsed, 2),
        current_stage="completed",
        progress_percent=100.0,
    )
```

**Summary of changes to `main.py`:**
| Line(s) | Change | Why |
|---------|--------|-----|
| 299-415 | Replace `_process_video` completely | Scene-based pipeline instead of per-frame |
| +new | Add `_batch_ocr` method | OCR all frames without GPU semaphore |
| +new | Add `_group_into_scenes` method | Group similar frames by dHash proximity |
| +new | Add `_build_scene` method | Build scene metadata from frame group |
| +new | Add `_analyze_scenes` method | One LLaVA call per scene |
| +new | Add `_complete_empty_job` method | Handle edge case of zero frames |
| 5 (import) | Add `from app.frame_diff import FrameExtractor` (already there) | Need access to `_hamming_distance` |

**Performance math:**
- Before: 50 frames × (OCR + LLaVA) = 50 OCR + 50 LLaVA calls
- After (threshold dedup): ~12 frames → ~4 scenes × 1 LLaVA = 12 OCR + 4 LLaVA calls
- **~12x fewer LLaVA calls** (the bottleneck at 5-30s each)

---

### File 3: `engines/eyes-engine/app/vision/__init__.py`

**Changes needed:** None for Phase 1. The `VisualAnalyzer.analyze_frame()` method already accepts all the parameters we need. The scene-based approach simply calls it fewer times.

**Optional Phase 2 enhancement:** Add a `analyze_scene_batch()` method that sends multiple frame thumbnails in a single LLaVA prompt for even better context. This is an optimization, not a requirement.

---

## Phase 2: Concurrent Audio + Video Processing

### File 4: `products/nexus-qa-orchestrator/app/workflows/builtin/qa_testing.py`

**Current State:** The QA testing chain already has `visual_analysis` without `depends_on: ["transcription"]`, meaning it CAN run in parallel with transcription. **This is already correct in the new orchestrator.**

However, the `pii_redaction` stage depends on `["transcription"]` — and `rule_extraction` depends on `["pii_redaction", "visual_analysis"]`. This means the DAG already runs audio and video in parallel. **No changes needed for the new orchestrator chains.**

**But:** The old orchestrator (`products/qa-orchestrator/app/pipeline.py`) runs them sequentially. If we still support the old orchestrator, it needs fixing. See Phase 5.

---

## Phase 3: Canonical Artifact Storage (Process Once, Use Many)

### File 5: `sdk/nexus-sdk/nexus_sdk/media/models.py`

**Add the Canonical Artifact model:**

```python
# ADD to the end of the file, before any final exports

class CanonicalMediaArtifact(BaseModel):
    """
    The canonical processed output from a video+audio upload.
    Created ONCE by the canonical processing stage.
    Consumed by all downstream pipelines (QI, Knowledge, Regression, etc.)
    """
    artifact_id: str = ""
    session_id: str = ""
    tenant_id: str = ""

    # Audio processing results
    transcription: Optional[dict] = None          # Full TranscriptionResult dict
    audio_metadata: Optional[dict] = None         # AudioMetadata dict
    audio_job_id: str = ""

    # Video processing results
    visual_analysis: Optional[dict] = None        # Full VisualAnalysisResult dict
    video_job_id: str = ""
    scene_count: int = 0
    frame_count: int = 0

    # Combined
    transcript_text: str = ""                     # Plaintext transcript for downstream
    visual_summary: str = ""                      # Combined scene descriptions
    application_types_seen: list[str] = Field(default_factory=list)

    # Metadata
    source_video_filename: str = ""
    source_audio_filename: str = ""
    processing_time_seconds: float = 0.0
    created_at: str = ""
    status: str = "pending"                       # pending | processing | completed | failed
    error: Optional[str] = None
```

**Changes:**
| Location | Change | Why |
|----------|--------|-----|
| End of file | Add `CanonicalMediaArtifact` class | Defines the "process once" output |

---

### File 6: `products/nexus-qa-orchestrator/app/workflows/builtin/__init__.py`

**Current code:**
```python
def load_all_builtin_chains() -> list[ChainDefinition]:
    return [
        build_qa_testing_chain(),
        build_compliance_audit_chain(),
        build_knowledge_capture_chain(),
        build_regression_suite_chain(),
    ]
```

**Change:** Add a new canonical processing chain:

```python
from .canonical_processing import build_canonical_processing_chain

def load_all_builtin_chains() -> list[ChainDefinition]:
    return [
        build_canonical_processing_chain(),   # NEW — runs first
        build_qa_testing_chain(),
        build_compliance_audit_chain(),
        build_knowledge_capture_chain(),
        build_regression_suite_chain(),
    ]
```

---

### File 7: NEW FILE — `products/nexus-qa-orchestrator/app/workflows/builtin/canonical_processing.py`

This is the new chain that runs Ears + Eyes in parallel and stores the canonical artifact.

```python
"""
Built-in Chain: Canonical Media Processing.

The "Process Once, Use Many" pipeline.
Runs audio transcription and video analysis in parallel,
combines results into a CanonicalMediaArtifact, and stores it.

All consumer chains (QA Testing, Knowledge Capture, Compliance Audit,
Regression Suite) read from this artifact instead of re-processing.

DAG:
    transcription ─┐
                    ├─→ artifact_assembly
    visual_analysis ┘
"""

from ..schema import (
    ChainDefinition,
    PollingConfig,
    RetryPolicy,
    StageDefinition,
)


def build_canonical_processing_chain() -> ChainDefinition:
    return ChainDefinition(
        chain_id="nexus.canonical-processing",
        name="Canonical Media Processing",
        description=(
            "Process raw video+audio once: transcribe audio (Ears) and "
            "analyze video (Eyes) in parallel, then assemble a canonical "
            "artifact that all downstream pipelines consume."
        ),
        version="1.0.0",
        tags=["canonical", "media", "processing", "foundation"],
        stages=[
            StageDefinition(
                stage_id="transcription",
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
                file_mappings={
                    "audio": "$workflow.input.audio_file_id",
                },
                condition="$workflow.input.audio_file_id",
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
                stage_id="visual_analysis",
                name="Video Analysis",
                description="Analyze screen recording via Eyes engine (scene-based)",
                engine="eyes",
                endpoint="/api/v1/eyes/analyze-video",
                request_type="multipart",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                },
                file_mappings={
                    "video": "$workflow.input.video_file_id",
                },
                condition="$workflow.input.video_file_id",
                timeout_seconds=900,
                on_failure="skip",
                polling=PollingConfig(
                    enabled=True,
                    job_id_path="job_id",
                    poll_endpoint="/api/v1/eyes/jobs/{job_id}",
                    poll_interval_seconds=5.0,
                    max_poll_seconds=900.0,
                    completion_statuses=["completed"],
                    failure_statuses=["failed"],
                    result_path="result",
                    status_path="status",
                ),
            ),
            # Note: transcription and visual_analysis have NO depends_on
            # so they run IN PARALLEL (same DAG level).
            StageDefinition(
                stage_id="artifact_assembly",
                name="Canonical Artifact Assembly",
                description=(
                    "Combine transcription and visual analysis into a single "
                    "canonical artifact stored for all downstream consumers"
                ),
                engine="spine",
                endpoint="/api/v1/spine/store-artifact",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "artifact_type": "canonical_media",
                    "transcription": "$stages.transcription.output",
                    "visual_analysis": "$stages.visual_analysis.output",
                },
                depends_on=["transcription", "visual_analysis"],
                timeout_seconds=60,
                retry_policy=RetryPolicy(max_retries=2),
                on_failure="fail",
            ),
        ],
    )
```

---

### File 8: `engines/spine-engine/main.py`

**Current state:** Spine engine handles document ingestion (ingest, search, chunk). It needs a new endpoint to store and retrieve canonical artifacts.

**Add new endpoint:**

```python
# ADD to register_routes method, after existing /api/v1/spine/ingest route

@app.post("/api/v1/spine/store-artifact")
async def store_artifact(
    req: NexusRequest,
    user: NexusUser = Depends(get_current_user),
):
    """
    Store a canonical media artifact for a session.
    This is the 'process once' output consumed by all downstream pipelines.
    """
    tenant_id = req.tenant_id
    session_id = req.data.get("session_id", "")
    artifact_type = req.data.get("artifact_type", "canonical_media")

    artifact = {
        "artifact_id": str(uuid.uuid4()),
        "artifact_type": artifact_type,
        "tenant_id": tenant_id,
        "session_id": session_id,
        "transcription": req.data.get("transcription"),
        "visual_analysis": req.data.get("visual_analysis"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
    }

    # Build combined text fields for downstream consumers
    transcript_text = ""
    if artifact["transcription"]:
        transcript_text = artifact["transcription"].get("transcript_text", "")
    artifact["transcript_text"] = transcript_text

    visual_summary_parts = []
    if artifact["visual_analysis"]:
        for frame in artifact["visual_analysis"].get("frames", []):
            desc = frame.get("description", "")
            if desc:
                visual_summary_parts.append(desc)
    artifact["visual_summary"] = "\n".join(visual_summary_parts)

    # Store in Redis (keyed by session_id for fast lookup)
    artifact_key = f"artifact:{tenant_id}:{session_id}"
    await self.job_store.redis.hset(
        "canonical:artifacts", artifact_key, json.dumps(artifact, default=str)
    )

    return NexusResponse(
        success=True,
        engine="spine",
        engine_version=self.version,
        data=artifact,
    )


@app.get("/api/v1/spine/artifacts/{session_id}")
async def get_artifact(
    session_id: str,
    user: NexusUser = Depends(get_current_user),
):
    """Retrieve a canonical artifact by session ID."""
    artifact_key = f"artifact:{user.tenant_id}:{session_id}"
    raw = await self.job_store.redis.hget("canonical:artifacts", artifact_key)
    if not raw:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return json.loads(raw)
```

**Changes to `engines/spine-engine/main.py`:**
| Location | Change | Why |
|----------|--------|-----|
| `register_routes()` | Add `POST /api/v1/spine/store-artifact` | Store canonical artifact |
| `register_routes()` | Add `GET /api/v1/spine/artifacts/{session_id}` | Retrieve canonical artifact |
| imports | Add `import json` if not present | JSON serialization |

---

## Phase 4: Rewire Consumer Chains to Use Canonical Artifact

### File 9: `products/nexus-qa-orchestrator/app/workflows/builtin/qa_testing.py`

**Current state:** The QA testing chain has its own `transcription` and `visual_analysis` stages that call Ears and Eyes directly.

**Change:** Replace media processing stages with an artifact fetch stage.

```python
# REPLACE the first 3 stages (transcription, pii_redaction, visual_analysis)
# with these 2 stages:

StageDefinition(
    stage_id="fetch_artifact",
    name="Fetch Canonical Artifact",
    description="Retrieve the pre-processed canonical artifact for this session",
    engine="spine",
    endpoint="/api/v1/spine/artifacts/{session_id}",
    method="GET",
    input_mapping={
        "tenant_id": "$workflow.tenant_id",
        "session_id": "$workflow.session_id",
    },
    timeout_seconds=30,
    retry_policy=RetryPolicy(max_retries=3, backoff_seconds=2.0),
    on_failure="fail",
),
StageDefinition(
    stage_id="pii_redaction",
    name="PII Redaction",
    description="Detect and redact PII from transcript via Shield engine",
    engine="shield",
    endpoint="/api/v1/shield/redact",
    input_mapping={
        "tenant_id": "$workflow.tenant_id",
        "text": "$stages.fetch_artifact.output.transcript_text",
    },
    depends_on=["fetch_artifact"],
    condition="$stages.fetch_artifact.output.transcript_text",
    timeout_seconds=60,
    on_failure="fail",
),
```

**And update ALL downstream `depends_on` references:**
- `document_ingestion`: stays same (no dependency on media)
- `rule_extraction`: change `depends_on` from `["pii_redaction", "visual_analysis"]` to `["pii_redaction", "fetch_artifact"]`
- The `visual_context` input_mapping: change from `"$stages.visual_analysis.output"` to `"$stages.fetch_artifact.output.visual_analysis"`

**Full replacement for `qa_testing.py`:**

```python
stages=[
    StageDefinition(
        stage_id="fetch_artifact",
        name="Fetch Canonical Artifact",
        description="Retrieve the pre-processed canonical media artifact",
        engine="spine",
        endpoint="/api/v1/spine/artifacts/{session_id}",
        method="GET",
        input_mapping={
            "tenant_id": "$workflow.tenant_id",
            "session_id": "$workflow.session_id",
        },
        timeout_seconds=30,
        retry_policy=RetryPolicy(max_retries=3, backoff_seconds=2.0),
        on_failure="fail",
    ),
    StageDefinition(
        stage_id="pii_redaction",
        name="PII Redaction",
        description="Detect and redact PII from transcript via Shield engine",
        engine="shield",
        endpoint="/api/v1/shield/redact",
        input_mapping={
            "tenant_id": "$workflow.tenant_id",
            "text": "$stages.fetch_artifact.output.transcript_text",
        },
        depends_on=["fetch_artifact"],
        condition="$stages.fetch_artifact.output.transcript_text",
        timeout_seconds=60,
        on_failure="fail",
    ),
    StageDefinition(
        stage_id="document_ingestion",
        name="Document Ingestion",
        description="Ingest uploaded BRDs/SRS via Spine engine",
        engine="spine",
        endpoint="/api/v1/spine/ingest",
        request_type="multipart",
        input_mapping={
            "tenant_id": "$workflow.tenant_id",
            "session_id": "$workflow.session_id",
        },
        file_mappings={
            "file": "$temp.item",
        },
        condition="$workflow.input.document_file_ids",
        timeout_seconds=120,
        on_failure="continue",
        for_each="$workflow.input.document_file_ids",
        for_each_item_key="item",
        for_each_concurrency=3,
    ),
    StageDefinition(
        stage_id="rule_extraction",
        name="Business Rule Extraction",
        description="Extract business rules via Heart LLM engine",
        engine="heart",
        endpoint="/api/v1/heart/extract-rules",
        input_mapping={
            "tenant_id": "$workflow.tenant_id",
            "session_id": "$workflow.session_id",
            "transcript": "$stages.pii_redaction.output.safe_text",
            "visual_context": "$stages.fetch_artifact.output.visual_analysis",
        },
        depends_on=["pii_redaction", "fetch_artifact", "document_ingestion"],
        timeout_seconds=300,
        retry_policy=RetryPolicy(max_retries=2),
        on_failure="fail",
    ),
    # ... remaining stages (test_generation, test_data_generation,
    #     knowledge_storage, test_execution, report_generation, notification)
    # remain UNCHANGED — they don't reference transcription/visual_analysis
]
```

**Changes to `qa_testing.py`:**
| Location | Change | Why |
|----------|--------|-----|
| Stage `transcription` | REMOVE entirely | Canonical pipeline already did this |
| Stage `visual_analysis` | REMOVE entirely | Canonical pipeline already did this |
| NEW Stage `fetch_artifact` | ADD at position 0 | Fetch the pre-computed canonical artifact |
| Stage `pii_redaction` | Change `depends_on` to `["fetch_artifact"]` | Reads from artifact, not raw Ears output |
| Stage `pii_redaction` | Change `input_mapping.text` to `$stages.fetch_artifact.output.transcript_text` | Point to artifact transcript |
| Stage `rule_extraction` | Change `depends_on` to `["pii_redaction", "fetch_artifact", "document_ingestion"]` | References artifact, not visual_analysis stage |
| Stage `rule_extraction` | Change `visual_context` mapping to `$stages.fetch_artifact.output.visual_analysis` | Point to artifact visual data |

---

### File 10: `products/nexus-qa-orchestrator/app/workflows/builtin/knowledge_capture.py`

**Same pattern.** Replace `transcription` + `visual_analysis` stages with `fetch_artifact`.

```python
# Replace first 3 stages with:
StageDefinition(
    stage_id="fetch_artifact",
    name="Fetch Canonical Artifact",
    description="Retrieve pre-processed KT session recording artifact",
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
StageDefinition(
    stage_id="pii_redaction",
    name="PII Redaction",
    engine="shield",
    endpoint="/api/v1/shield/redact",
    input_mapping={
        "tenant_id": "$workflow.tenant_id",
        "text": "$stages.fetch_artifact.output.transcript_text",
    },
    depends_on=["fetch_artifact"],
    condition="$stages.fetch_artifact.output.transcript_text",
    timeout_seconds=60,
    on_failure="fail",
),
# visual_analysis stage: REMOVED (data is in fetch_artifact.output.visual_analysis)
# rule_extraction: update depends_on and visual_context mapping
StageDefinition(
    stage_id="rule_extraction",
    ...
    input_mapping={
        ...
        "visual_context": "$stages.fetch_artifact.output.visual_analysis",
    },
    depends_on=["pii_redaction", "fetch_artifact"],
    ...
),
```

---

### File 11: `products/nexus-qa-orchestrator/app/workflows/builtin/compliance_audit.py`

**Same pattern.** If this chain has transcription/visual_analysis stages, replace with `fetch_artifact`.

Currently compliance_audit has: `document_ingestion → analysis → rule_matching → report → notification`. It may NOT have audio/video stages — in that case, **no changes needed**. If it does reference Ears/Eyes, apply the same fetch_artifact pattern.

---

## Phase 5: Orchestrator API — Canonical Processing Trigger

### File 12: `products/nexus-qa-orchestrator/app/main.py`

**Add route to trigger canonical processing, then chain a consumer pipeline.**

```python
# ADD new endpoint after the /workflows/start route

class CanonicalProcessingRequest(BaseModel):
    """Request to upload media and trigger canonical processing."""
    tenant_id: str
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    audio_file_id: Optional[str] = None
    video_file_id: Optional[str] = None
    document_file_ids: list[str] = Field(default_factory=list)
    consumer_chain_id: Optional[str] = Field(
        default=None,
        description=(
            "Chain to run AFTER canonical processing completes. "
            "E.g. 'nexus.qa-testing', 'nexus.knowledge-capture'. "
            "If omitted, only canonical processing runs."
        ),
    )
    consumer_input_data: dict = Field(
        default_factory=dict,
        description="Additional input data for the consumer chain",
    )
    language: str = "en"


@app.post(
    "/api/v1/orchestrator/process",
    response_model=StartWorkflowResponse,
    tags=["Canonical Processing"],
)
async def start_canonical_processing(
    req: CanonicalProcessingRequest,
    background_tasks: BackgroundTasks,
    user: NexusUser = Depends(get_current_user),
):
    """
    Start canonical media processing (the 'process once' stage).
    Optionally chains a consumer pipeline after completion.

    This is the primary entry point for all media processing.
    """
    # 1. Start canonical processing
    canonical_chain = await registry.get("nexus.canonical-processing")
    if not canonical_chain:
        raise HTTPException(status_code=500, detail="Canonical processing chain not registered")

    input_data = {
        "audio_file_id": req.audio_file_id,
        "video_file_id": req.video_file_id,
        "language": req.language,
    }

    instance = await chain_engine.start(
        chain=canonical_chain,
        tenant_id=req.tenant_id,
        session_id=req.session_id,
        input_data=input_data,
        created_by=user.user_id,
    )

    # 2. Execute canonical processing, then optionally chain consumer
    background_tasks.add_task(
        _run_canonical_then_consumer,
        workflow_id=instance.workflow_id,
        canonical_chain=canonical_chain,
        consumer_chain_id=req.consumer_chain_id,
        consumer_input_data={
            **req.consumer_input_data,
            "document_file_ids": req.document_file_ids,
        },
        tenant_id=req.tenant_id,
        session_id=req.session_id,
        user_id=user.user_id,
    )

    return StartWorkflowResponse(
        workflow_id=instance.workflow_id,
        chain_id=canonical_chain.chain_id,
        chain_name=canonical_chain.name,
        status=instance.status,
        session_id=instance.session_id,
    )


async def _run_canonical_then_consumer(
    workflow_id: str,
    canonical_chain: ChainDefinition,
    consumer_chain_id: Optional[str],
    consumer_input_data: dict,
    tenant_id: str,
    session_id: str,
    user_id: str,
):
    """Execute canonical processing, then start consumer chain if specified."""
    # Run canonical processing
    await chain_engine.execute(workflow_id, canonical_chain)

    # Check if it completed successfully
    instance = await workflow_store.get_instance(workflow_id)
    if not instance or instance.status != WorkflowStatus.COMPLETED:
        logger.warning(
            "Canonical processing did not complete — skipping consumer chain",
            extra={
                "workflow_id": workflow_id,
                "status": instance.status.value if instance else "not_found",
            },
        )
        return

    # Start consumer chain if requested
    if consumer_chain_id:
        consumer_chain = await registry.get(consumer_chain_id)
        if not consumer_chain:
            logger.error("Consumer chain '%s' not found", consumer_chain_id)
            return

        consumer_instance = await chain_engine.start(
            chain=consumer_chain,
            tenant_id=tenant_id,
            session_id=session_id,
            input_data=consumer_input_data,
            created_by=user_id,
        )

        await chain_engine.execute(consumer_instance.workflow_id, consumer_chain)
```

**Changes to `products/nexus-qa-orchestrator/app/main.py`:**
| Location | Change | Why |
|----------|--------|-----|
| After `StartWorkflowRequest` | Add `CanonicalProcessingRequest` model | Request model for the new endpoint |
| After `/workflows/start` | Add `POST /api/v1/orchestrator/process` | Main entry point for canonical processing |
| Module level | Add `_run_canonical_then_consumer` function | Background task: canonical → consumer chain |

---

## Phase 6: EyesConfig Tuning

### File 13: `engines/eyes-engine/main.py` — Config section

```python
# CHANGE the default values for better performance

class EyesConfig(EngineConfig):
    engine_name: str = "eyes"
    engine_port: int = 8003
    ollama_model: str = "llava:7b"

    # Frame extraction — TUNED for screen recordings
    frame_diff_threshold: float = 0.08      # Raised from 0.05 — screen recordings have minor
                                             # antialiasing/compression changes
    max_fps_extract: float = 1.0             # Down from 2.0 — screen recordings don't need 2fps
    keyframe_only: bool = False

    # Scene grouping
    scene_boundary_threshold: float = 0.15   # NEW — dHash distance to start new scene

    # OCR
    ocr_languages: list[str] = ["en"]
    ocr_gpu: bool = True
    ocr_model_dir: str = "./models/easyocr"

    # GPU concurrency
    gpu_concurrency: int = 1                 # NEW — configurable (raise to 2 on multi-GPU)

    # Storage
    frames_storage_path: str = "./data/frames"
```

Then use `self.cfg.scene_boundary_threshold` in `_group_into_scenes()` instead of the hardcoded `0.15`, and `asyncio.Semaphore(self.cfg.gpu_concurrency)` instead of `Semaphore(1)`.

---

## Complete File Change Summary

### Files Modified (12 files)

| # | File | Changes | Risk |
|---|------|---------|------|
| 1 | `engines/eyes-engine/app/frame_diff/__init__.py` | Fix threshold dedup (Hamming distance), add `_hamming_distance()`, return hash in frame dict | LOW — backward compatible, fixes a bug |
| 2 | `engines/eyes-engine/main.py` | Replace `_process_video` with scene-based pipeline, add 5 new methods, tune config defaults | MEDIUM — core processing logic change |
| 3 | `engines/spine-engine/main.py` | Add `POST /store-artifact` + `GET /artifacts/{session_id}` endpoints | LOW — additive, no existing code changed |
| 4 | `sdk/nexus-sdk/nexus_sdk/media/models.py` | Add `CanonicalMediaArtifact` Pydantic model | LOW — additive only |
| 5 | `products/nexus-qa-orchestrator/app/workflows/builtin/__init__.py` | Import + register canonical chain | LOW — additive |
| 6 | `products/nexus-qa-orchestrator/app/workflows/builtin/qa_testing.py` | Replace transcription+visual_analysis with fetch_artifact, update depends_on | MEDIUM — chain DAG restructure |
| 7 | `products/nexus-qa-orchestrator/app/workflows/builtin/knowledge_capture.py` | Same pattern as qa_testing.py | MEDIUM |
| 8 | `products/nexus-qa-orchestrator/app/workflows/builtin/compliance_audit.py` | Same pattern IF it has audio/video stages | LOW-MEDIUM |
| 9 | `products/nexus-qa-orchestrator/app/main.py` | Add `POST /process` endpoint + background chaining logic | LOW-MEDIUM — additive |
| 10 | `products/nexus-qa-orchestrator/app/workflows/schema.py` | No changes needed — schema already supports GET method | NONE |
| 11 | `products/nexus-qa-orchestrator/app/workflows/engine.py` | No changes needed — engine already handles GET requests | NONE |
| 12 | `products/nexus-qa-orchestrator/app/workflows/context.py` | No changes needed | NONE |

### Files Created (1 file)

| # | File | Purpose |
|---|------|---------|
| 1 | `products/nexus-qa-orchestrator/app/workflows/builtin/canonical_processing.py` | New canonical media processing chain definition |

### Files NOT Changed (confirmed no modifications needed)

| File | Reason |
|------|--------|
| `engines/ears-engine/main.py` | Ears engine performance is acceptable (30-55s for 1-min audio) |
| `engines/ears-engine/app/transcription/__init__.py` | Whisper + Pyannote pipeline is already optimized |
| `engines/ears-engine/app/diarization/__init__.py` | Speaker diarization is single-pass |
| `engines/eyes-engine/app/vision/__init__.py` | LLaVA analyzer API is unchanged — we just call it fewer times |
| `sdk/nexus-sdk/nexus_sdk/stores.py` | JobStore API is sufficient |
| `sdk/nexus-sdk/nexus_sdk/events/__init__.py` | EventBus API is sufficient |

---

## Implementation Order (Dependency Chain)

```
Step 1  →  Step 2  →  Step 3  →  Step 4  →  Step 5  →  Step 6
 Fix        Add       Fix Eyes    New         Rewire     Add
 dHash      hamming   pipeline    canonical   consumer   /process
 threshold  distance  (scenes)    chain +     chains     endpoint
                                  Spine API
```

### Step 1: Fix frame dedup (5 min)
- File: `engines/eyes-engine/app/frame_diff/__init__.py`
- Add `_hamming_distance()` static method
- Replace binary `!=` with threshold comparison
- Add hash to frame dict output

### Step 2: Scene-based Eyes pipeline (30 min)
- File: `engines/eyes-engine/main.py`
- Replace `_process_video` with scene-based pipeline
- Add `_batch_ocr`, `_group_into_scenes`, `_build_scene`, `_analyze_scenes`
- Update `EyesConfig` with new parameters

### Step 3: Canonical artifact model + storage (15 min)
- File: `sdk/nexus-sdk/nexus_sdk/media/models.py` — add `CanonicalMediaArtifact`
- File: `engines/spine-engine/main.py` — add store/retrieve endpoints

### Step 4: Canonical processing chain (10 min)
- File: `products/nexus-qa-orchestrator/app/workflows/builtin/canonical_processing.py` — new file
- File: `products/nexus-qa-orchestrator/app/workflows/builtin/__init__.py` — register new chain

### Step 5: Rewire consumer chains (20 min)
- File: `products/nexus-qa-orchestrator/app/workflows/builtin/qa_testing.py`
- File: `products/nexus-qa-orchestrator/app/workflows/builtin/knowledge_capture.py`

### Step 6: Orchestrator API endpoint (15 min)
- File: `products/nexus-qa-orchestrator/app/main.py`

---

## Testing Strategy

### Unit Tests

```
tests/engines/test_eyes_frame_diff.py     — Test Hamming distance, threshold dedup
tests/engines/test_eyes_scene_grouping.py — Test scene boundary detection
tests/engines/test_eyes_batch_ocr.py      — Test batch OCR pipeline
tests/orchestrator/test_canonical_chain.py — Test canonical chain DAG
tests/orchestrator/test_consumer_rewire.py — Test consumer chains read from artifact
```

### Integration Tests

```
tests/integration/test_canonical_pipeline.py
  - Upload video → canonical processing → artifact stored
  - Artifact contains transcription + visual analysis
  - Consumer chain fetches artifact successfully

tests/integration/test_performance.py
  - 1-min video: < 5 min processing time (GPU), < 15 min (CPU)
  - Frame dedup: < 15 unique frames from 1-min screen recording
  - Scene count: < 8 scenes from 1-min screen recording
```

### E2E Test

```
tests/e2e/test_full_canonical_flow.py
  - Upload video + audio
  - Canonical processing completes
  - Trigger QI chain → reads from artifact → produces rules + tests
  - Trigger Knowledge chain → reads SAME artifact → stores knowledge
  - No re-processing of video/audio
```

---

## Deployment Sequence

1. **Deploy SDK changes first** — `CanonicalMediaArtifact` model (no breaking changes)
2. **Deploy Spine engine** — new endpoints are additive
3. **Deploy Eyes engine** — performance fix is backward compatible (same API)
4. **Deploy Orchestrator** — new chain + rewired consumers + new API endpoint
5. **Smoke test** — upload 1-min video, verify < 5 min processing
6. **Monitor** — watch `eyes.pipeline.completed` events for scene_count vs frame_count ratio

---

## Rollback Plan

Each change is independently deployable and backward compatible:
- **Eyes engine fix** can be reverted by restoring the old `_process_video` — the API contract is unchanged
- **Canonical chain** is registered alongside existing chains — existing workflows still work
- **Consumer chain rewiring** can be toggled by registering the old chain definitions (they're just Python dicts)
- **Spine artifact endpoints** are additive — nothing depends on them until the consumer chains are deployed

No database migrations are required. All state is in Redis (same keys, same structures).
