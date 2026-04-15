# Canonical Latency Plan

## Goal

Cut short-video canonical completion from 45+ minutes to a practical interactive target while preserving the canonical artifact contract.

## Latency Targets

- P0 target: 60-second screen recording completes fast canonical in <= 8 minutes on CPU-only local development.
- P1 target: 60-second screen recording completes fast canonical in <= 3 minutes on a GPU-backed workstation.
- P2 target: deep enrichment runs asynchronously after canonical completion and does not block the first artifact.

## Current Bottlenecks

- Eyes visual extraction dominates wall-clock time.
- Ears transcription is also CPU-bound and slower than an interactive upload path should be.
- Downstream canonical steps are already sub-second and are not the latency problem.

## Implementation Plan

### Phase 1: Fast Canonical Path

Files:
- [docker-compose.canonical.yml](docker-compose.canonical.yml)
- [products/nexus-qa-orchestrator/app/workflows/builtin/canonical_processing.py](products/nexus-qa-orchestrator/app/workflows/builtin/canonical_processing.py)
- [engines/eyes-engine/main.py](engines/eyes-engine/main.py)

Changes:
- Keep canonical artifact persistence and quality gate in the synchronous path.
- Force the canonical workflow to submit Eyes jobs with `processing_profile=fast`.
- In Eyes fast mode:
  - cap analyzed frames aggressively
  - cap analyzed scenes aggressively
  - preserve extracted-frame counts for observability
  - keep output schema stable so Spine and Brain continue working

Expected outcome:
- same canonical artifact contract
- much less OCR and multimodal inference per upload

### Phase 2: Deep Enrichment Path

Files:
- [products/nexus-qa-orchestrator/app/workflows/builtin/canonical_processing.py](products/nexus-qa-orchestrator/app/workflows/builtin/canonical_processing.py)
- [engines/eyes-engine/main.py](engines/eyes-engine/main.py)
- [engines/spine-engine/main.py](engines/spine-engine/main.py)

Changes:
- add an async deep-enrichment trigger after artifact persistence
- write enrichment output back onto the canonical artifact as an upgrade, not a new source of truth
- let UI surface fast canonical completion separately from deep enrichment completion

Expected outcome:
- user-visible completion becomes fast
- rich semantics still arrive later

### Phase 3: Audio Speed Tier

Files:
- [engines/ears-engine/main.py](engines/ears-engine/main.py)
- [engines/ears-engine/app/transcription/__init__.py](engines/ears-engine/app/transcription/__init__.py)
- [docker-compose.canonical.yml](docker-compose.canonical.yml)

Changes:
- introduce quick vs deep transcription profiles
- default fast canonical uploads to a smaller Whisper model on CPU deployments
- keep large-v3 for deep or high-accuracy runs

Expected outcome:
- transcript latency drops substantially for short uploads

### Phase 4: Honest Runtime Modes

Files:
- [sdk/nexus-sdk/nexus_sdk/health/__init__.py](sdk/nexus-sdk/nexus_sdk/health/__init__.py)
- [scripts/validate_deployment.py](scripts/validate_deployment.py)

Changes:
- surface CPU-only inference explicitly in health and deployment validation
- treat missing acceleration as degraded for performance-sensitive profiles

Expected outcome:
- stack health reflects actual production capability, not just process liveness

## Benchmark Plan

Files:
- [scripts/benchmark_canonical_latency.py](scripts/benchmark_canonical_latency.py)
- optional future CI test under `tests/e2e/`

Measure:
- total workflow latency
- per-stage latency
- artifact status and quality outcome
- fast canonical completion rate across a small media matrix

Benchmark matrix:
- 60-second screen recording with audio
- 60-second screen recording without meaningful OCR density
- 5-minute screen recording
- CPU-only local profile
- GPU-backed profile

Success criteria:
- P0 target met on CPU-only local runs
- P1 target met on GPU-backed runs
- no regression in artifact persistence or quality-gate semantics