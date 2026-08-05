# Phase 5-A — Eyes Per-Step Pipeline (Architecture)

**Status.** Foundation landed (this session): manifest schemas + parity-test scaffold + design contract. Full implementation is 3 engineer-weeks of focused refactor and is queued, not committed.

**Why this doc exists.** I undersized Phase 5 once before by writing code before surveying the call graph. This doc is the survey + contract. The next engineer to pick this up has the design pinned so they can cut code without rediscovering the same blockers.

---

## Today's eyes pipeline (the monolith)

```
JobEnvelope(step=eyes.extract_frames)
  └─ handlers._handle_extract_frames
       └─ engine._process_video                 ← does EVERYTHING
            ├─ probe_video → duration
            ├─ branch: _process_video_single OR _process_video_chunked
            │     └─ frame_extractor.extract_frames     ffmpeg
            │     └─ _group_into_scenes                 dHash
            │     └─ _batch_ocr / _batch_ocr_representative_frames
            │     └─ _analyze_scenes                    LLaVA per rep frame
            │     └─ _analyze_scene_transitions_llm     LLaVA pass #2
            │     └─ ElementTracker.assign_entities     post-processing
            │     └─ _upload_frames_to_artifact_store
            │     └─ build VisualAnalysisResult
            └─ job_store.update_job(status=completed, result=...)
JobEnvelope(step=eyes.analyze_scenes) → PASSTHROUGH (no-op)
JobEnvelope(step=eyes.build_evidence) → PASSTHROUGH (no-op)
```

A failure inside any of the 7 internal stages retries the **whole** workflow, including the ffmpeg pass that already succeeded.

---

## Target: 6 real steps

```
JobEnvelope(step=eyes.extract_frames)        CPU  ~30s
  └─ handler._handle_extract_frames
       └─ engine._stage_extract_frames(video) → FramesManifest
       └─ upload FramesManifest + each frame PNG to artifact store
       └─ checkpoint.frames_manifest_key

JobEnvelope(step=eyes.detect_scenes)         CPU  ~20s
  └─ handler._handle_detect_scenes
       └─ download FramesManifest
       └─ engine._stage_detect_scenes(frames) → ScenesManifest
       └─ checkpoint.scenes_manifest_key

JobEnvelope(step=eyes.ocr_frames)            CPU  ~60-90s
  └─ handler._handle_ocr_frames
       └─ download Frames + Scenes manifests
       └─ engine._stage_ocr(scenes, frames, profile) → OCRManifest
       └─ checkpoint.ocr_manifest_key

JobEnvelope(step=eyes.analyze_scenes)        GPU  ~60-180s
  └─ handler._handle_analyze_scenes
       └─ download Scenes + OCR manifests
       └─ engine._stage_analyze(scenes, ocr) → EnrichedScenesManifest
       └─ checkpoint.enriched_scenes_key

JobEnvelope(step=eyes.analyze_transitions)   GPU  ~30-90s
  └─ handler._handle_analyze_transitions
       └─ download EnrichedScenes manifest
       └─ engine._stage_transitions(enriched) → TransitionsManifest
       └─ checkpoint.transitions_manifest_key

JobEnvelope(step=eyes.build_evidence)        CPU  ~30s
  └─ handler._handle_build_evidence
       └─ download all manifests
       └─ engine._stage_build_evidence(frames, scenes, ocr, enriched, transitions)
              → VisualAnalysisResult
       └─ ElementTracker post-processing applied here
       └─ event emit + job_store.update_job
       └─ checkpoint.eyes_result_key
```

---

## The mutable-shared-state problem (and how the refactor untangles it)

Today, [_process_video_single](Nexus_power/engines/eyes-engine/main.py#L1279) builds a `scenes` list early with **placeholder** OCR, then mutates the same dicts in place after the real OCR runs:

```python
placeholder_ocr_results = [("", [], 0.0) for _ in raw_frames]
all_scenes = self._group_into_scenes(raw_frames, placeholder_ocr_results)
...
# Later, after OCR runs:
for scene in scenes:
    rep_idx = scene["representative_idx"]
    scene["representative_ocr"] = ocr_results[rep_idx]       # mutation
    scene["merged_ocr_text"] = " ".join(parts)               # mutation
```

A workflow-step split forces this dataflow to become explicit:

- `detect_scenes` produces a `ScenesManifest` **without** OCR fields populated.
- `ocr_frames` reads the scenes manifest and produces an `OCRManifest` keyed by `frame_idx`.
- `analyze_scenes` reads both — at this point the join between scenes and OCR happens **at read time**, in memory inside a single pod, with no shared mutable state crossing the step boundary.

This is why the manifests are **structurally separate**, not just an inline copy of the in-memory shape.

---

## Element tracking + transition LLM placement

Today both happen inline inside `_process_video_single` after scene analysis. In the new shape:

- **`ElementTracker.assign_entities`** runs inside `build_evidence`. It needs the full enriched-scene list to do persistence-count adjacency matching; that data is already in the `EnrichedScenesManifest` plus the `FramesManifest` order. Pure CPU work, fits naturally with the terminal step.
- **`_analyze_scene_transitions_llm`** is a *second GPU pass* — explicit as its own step `eyes.analyze_transitions`. Same lane (`eyes.gpu`) as `analyze_scenes`. Splitting it out means a transition-LLM hang retries only that step.

---

## Chunked-video path

Today's [_process_video_chunked](Nexus_power/engines/eyes-engine/main.py#L2317) splits long videos via ffmpeg, processes each chunk through `_process_video_single`, concatenates results.

**Phase 5-A decision: drop chunking from the workflow path.** With per-step splitting, memory pressure between steps disappears (state lives in artifact store, not in-process). Long videos go through the same 6-step path; `extract_frames` produces one big `FramesManifest`, downstream steps work on it.

If memory pressure surfaces in `analyze_scenes` for 60-min videos, the right answer is **scene-level fan-out** (multiple `analyze_scenes` invocations per workflow, one per scene cluster), not in-process chunking. That's a Phase 5-B optimization, not 5-A.

The legacy REST endpoint keeps its chunked code path so the existing `/eyes/process` HTTP route doesn't regress.

---

## Step-by-step contract (input → output)

| Step | Reads | Writes | Failure semantics |
|---|---|---|---|
| `extract_frames` | `input_artifact_key` | `frames_manifest_key` + N frame PNGs | Retry: re-runs ffmpeg, idempotent (same input → same hashes). Replaces stale frames. |
| `detect_scenes` | `frames_manifest_key` | `scenes_manifest_key` | Retry: pure CPU on the manifest, deterministic. |
| `ocr_frames` | `frames_manifest_key`, `scenes_manifest_key` | `ocr_manifest_key` | Retry: re-runs EasyOCR on the same frames. CPU. |
| `analyze_scenes` | `scenes_manifest_key`, `ocr_manifest_key` | `enriched_scenes_key` | Retry: re-runs LLaVA. NOT idempotent (LLM output varies). Treat retry as "best of N." |
| `analyze_transitions` | `enriched_scenes_key` | `transitions_manifest_key` | Retry: re-runs second LLM pass. Same non-idempotency. |
| `build_evidence` | all of the above | `eyes_result_key` (legacy `VisualAnalysisResult` shape) | Retry: pure CPU synthesis + event emit. Event is at-least-once. |

---

## Acceptance criteria (when to call Phase 5-A done)

Functional:
- A 10-min video produces 6 distinct `workflow_step_history` rows.
- Failing OCR retries `ocr_frames`, not the whole video. Verified by an integration test that injects a one-shot OCR exception.
- Failing transition LLM retries `analyze_transitions`, not `analyze_scenes`.

Behavioral parity:
- For a known fixture (`tests/load/corpus/video-5min.mp4`), the new path's `VisualAnalysisResult` is **structurally identical** to the monolith's: same frame count, same scene count, same `ui_elements` shape (allowing for LLM output drift in description strings — assert scene IDs and frame counts, not LLM text).
- The legacy `/eyes/process` REST endpoint continues to work and produces the same shape.

Performance:
- Wall-time p95 of `eyes.analyze_scenes` step ≤ 180s under 4 concurrent videos per pod (the eyes.gpu lane's existing concurrency cap).
- Workflow-level p95 for a 15-min video improves materially vs the monolith — measured against [k6_canonical_video.js](Nexus_power/tests/load/k6_canonical_video.js).

Hygiene:
- `_process_video_single` either deleted or marked deprecated with a removal date and a passing parity test.
- No remaining `current_eyes_step` passthroughs in [workflow_handlers.py](Nexus_power/engines/eyes-engine/app/workflow_handlers.py).

---

## Risk register

| Risk | Mitigation |
|---|---|
| Element-tracker post-processing reaches across scenes; splitting may regress adjacency-based entity_id resolution. | Run element tracker inside `build_evidence` after the full enriched-scenes manifest is reconstituted. Parity test must assert `entity_id` stability across scenes. |
| Manifests can grow large (1000+ frames for 60-min screen recording). | Per-frame data is ≤500B; 1000 frames = ~500 KB. Well below artifact store sane limits. If we hit it, split FramesManifest into chunks (`frames_manifest_part_0.json`, …). |
| Cross-pod frame downloads dominate wall time for long videos. | Each step downloads only what it needs. Frames go up once (in `extract_frames`) and are referenced by key thereafter. The next pod only needs the manifest, not the PNGs, until `analyze_scenes` actually opens individual frames. |
| LLM non-determinism breaks parity tests. | Parity tests assert structural invariants (counts, IDs, types) not LLM output text. LLM text drift is normal. |
| Chunked-path users on `/eyes/process` regress. | Leave chunked path on the legacy REST endpoint. Workflow path doesn't use it. |

---

## Commit sequence (when this work starts)

1. **Schemas + parity scaffold.** Land [manifests.py](Nexus_power/engines/eyes-engine/app/manifests.py) + [test_eyes_phase5a_parity.py](Nexus_power/tests/workflows/test_eyes_phase5a_parity.py). No behavior change. (✅ this session)
2. **Stage methods.** Extract 6 `_stage_*` methods from `_process_video_single`. Legacy method calls them in sequence. Parity test verifies behavior identical.
3. **New handlers.** Rewrite [workflow_handlers.py](Nexus_power/engines/eyes-engine/app/workflow_handlers.py) — 6 real handlers + manifest upload/download helpers (reuse the pattern from [ears workflow_handlers.py](Nexus_power/engines/ears-engine/app/workflow_handlers.py)).
4. **Plan flip.** Update `video_plan` in [plans.py](Nexus_power/sdk/nexus-sdk/nexus_sdk/workflows/plans.py) to the 6-step shape. Drain workflow queue, deploy. Add `WorkflowPlan.version: 2` field with dispatcher refusing v1.
5. **Cleanup.** Delete `_process_video_chunked` from the workflow path (keep on legacy REST). Delete `current_eyes_step` passthroughs.

Effort: ~3 engineer-weeks total. Commits 1 (done) and 2 are the gating risk; 3-5 are mechanical once 2 lands.
