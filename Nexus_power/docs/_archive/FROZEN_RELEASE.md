# Nexus QA — v1.0-gpu-validated (2026-05-23)

Frozen state of the canonical pipeline running on GCP L4 GPU with all bugs from the May 21-23 debug session fixed.

## What's validated

| Metric | Value |
|---|---|
| GPU | NVIDIA L4 (asia-southeast1-a) |
| Total wall time (multi-scene video) | 34 seconds |
| LLaVA inference per scene | 5-8 seconds (on GPU) |
| OCR per frame | 0.7-1.5 seconds (on GPU) |
| Real LLaVA visual_summary | populated |
| quality_gate_outcome | pass for normal videos, needs_review for short/silent ones |
| All 20 DAG steps | green end-to-end |

## Bugs fixed in this release

1. EYES_OLLAMA_KEEP_ALIVE=-1 (string) caused HTTP 400 on every LLaVA call. Go time parser needs a unit. Fix: 24h.
2. Ollama runner silently reverting to CPU on respawn. Fix: OLLAMA_KEEP_ALIVE=-1 server-side + warmup sidecar locks model in GPU memory.
3. Backbone canonicalize orphan loop forever. Root cause: backbone was profile-gated to [full]. Fix: deploy unconditionally + NEXUS_ALLOW_DEGRADED_MODE=true.
4. Eyes container had no GPU access (OCR forced to CPU). Fix: GPU device reservation + NVIDIA_VISIBLE_DEVICES=all. OCR dropped from 13s/frame to 0.7s/frame.
5. LLaVA called with 1080p images. Fix: new _llm_image_b64 helper downscales to 512px JPEG quality 85 before encoding.
6. nexus_sdk.models module removed but engines still import it. Fix: compatibility shim at sdk/nexus-sdk/nexus_sdk/models.py with permissive Pydantic stubs.

## Critical config (.env)

EYES_OLLAMA_KEEP_ALIVE=24h
EYES_OLLAMA_NUM_PREDICT=300
EYES_OLLAMA_TRANSITION_NUM_PREDICT=150
EYES_LLAVA_PER_SCENE_TIMEOUT_S=120
EYES_LLAVA_CIRCUIT_THRESHOLD=3
EYES_PER_FRAME_LLAVA=false
EYES_MULTIMODAL_MAX_SCENES=12

## Critical env on services (docker-compose.yml)

- ollama: OLLAMA_KEEP_ALIVE=-1, OLLAMA_LLM_LIBRARY=cuda_v13, GPU device reservation
- ollama-warmup (new sidecar): triggers model load with keep_alive=-1 and num_gpu=999 on every compose-up
- eyes: GPU device reservation, NVIDIA_VISIBLE_DEVICES=all
- backbone: NEXUS_ALLOW_DEGRADED_MODE=true, runs by default (was profile-gated)

## Known limitations

- Milvus (vector store) runs in in-memory fallback. Knowledge graph still on Neo4j.
- For production-grade LLaVA quality, consider GPT-4-vision or Claude 3.5 Sonnet (Tier1) instead of LLaVA-7B.
