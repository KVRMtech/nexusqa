# Canonical Pipeline Load Tests (Phase 9)

Scripts that produce the measured numbers we need before claiming
"supports 100+ clients × 1000+ requests." Until these run against a
production-shaped pre-prod cluster and produce a `LOAD_TEST_REPORT.md`,
every capacity claim is an estimate.

The legacy [k6-load.js](k6-load.js) and [k6-stress.js](k6-stress.js)
scripts in this directory test HTTP-level performance against the
gateway. The canonical-pipeline scripts here test the
**workflow-plane end-to-end**: upload → orchestrator dispatch →
through every engine → backbone canonical artifact.

## Files

| File | Tests | When to run |
|---|---|---|
| `k6_smoke.js` | 1 tenant, 1 short workflow per modality | CI on every PR (smoke gate) |
| `k6_canonical_audio.js` | 50 concurrent audio tenants, mixed call lengths | Pre-prod calibration |
| `k6_canonical_video.js` | 50 concurrent video tenants, 1/5/10-min clips | Pre-prod calibration |
| `k6_canonical_multimodal.js` | 100 concurrent multimodal tenants | The 100-client claim gate |
| `generate_jwts.js` | Helper: mint N JWTs for N fake tenants | Used by the above |

## Required setup

1. **Test corpus.** Put fixture media files in `corpus/`:
   - `audio-5min.wav`  ~5 min, 16 kHz mono, 2 speakers
   - `audio-30min.wav` ~30 min, 16 kHz mono, 3 speakers
   - `audio-60min.wav` ~60 min, 16 kHz mono, 4 speakers
   - `video-1min.mp4`  ~1 min screen recording, 720p
   - `video-5min.mp4`  ~5 min screen recording, 720p
   - `video-15min.mp4` ~15 min screen recording, 720p
   - `multimodal-15min.mp4` ~15 min screen recording with audio
   
   These are NOT checked into git (large, sensitive). Use synthetic content
   the legal team has signed off on, OR get explicit customer consent.

2. **JWT secret.** Export `NEXUS_JWT_SECRET` matching the target
   environment's secret. The scripts mint per-tenant tokens at runtime.

3. **Target URL.** `BASE_URL=https://nexus-pre-prod.example.com` (gateway).

4. **k6 binary.** Install from https://k6.io/docs/get-started/installation/

## Running

```bash
# Pre-flight smoke (used by CI)
BASE_URL=http://localhost:8080 \
NEXUS_JWT_SECRET=$(grep NEXUS_JWT_SECRET ../../.env | cut -d= -f2) \
k6 run k6_smoke.js

# Single-modality calibration
BASE_URL=https://nexus-pre-prod.example.com \
NEXUS_JWT_SECRET=$PROD_JWT_SECRET \
k6 run --vus 50 --duration 10m k6_canonical_audio.js

# The 100-client gate
BASE_URL=https://nexus-pre-prod.example.com \
NEXUS_JWT_SECRET=$PROD_JWT_SECRET \
k6 run --vus 100 --duration 10m \
  --out json=results-multimodal.json \
  k6_canonical_multimodal.js
```

## Acceptance criteria (from Phase 9 of the plan)

- **Throughput:** Sustained 100 simulated tenants × 1 workflow/tenant/min × 10 min.
- **Latency:** p95 end-to-end < 5 min for 5-min videos.
- **Errors:** < 0.5% workflow failure rate (excluding bad-input rejections).
- **Queue stability:** Depth stays bounded — no unbounded growth.
- **Output:** All numbers in `LOAD_TEST_REPORT.md`, committed to repo.

If acceptance fails, the script's k6 thresholds will exit non-zero —
visible in CI logs as a red bar.

## Tuning loop

1. Run `k6_canonical_*` against pre-prod.
2. Read measured p95 + queue depths.
3. Tune `autoscaling.keda.components.*.maxReplicas` in
   [values-production.yaml](Nexus_power/infrastructure/helm/nexus-qa/values-production.yaml).
4. Tune per-step `deadline_seconds` in
   [plans.py](Nexus_power/sdk/nexus-sdk/nexus_sdk/workflows/plans.py).
5. Re-run. Document each iteration's numbers.
6. Land in `LOAD_TEST_REPORT.md`.

## What these scripts do NOT cover

- **GPU saturation curve.** Measure separately with a profiling job.
- **Failure-recovery latency.** Covered by Phase 10 DR drill, not load test.
- **Sustained 24h+ soak.** Use `--duration 24h` once the 10-min test
  is green. Watch for memory leaks in long-running engine processes.
