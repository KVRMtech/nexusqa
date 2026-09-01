# Canonical Pipeline — Quality Regression Suite

This suite is the Phase 1 deploy gate. It must pass before any tagged
build promotes to production. Failure modes:

1. **Semantic regression** — a step produces output that drifts from
   the golden by more than the configured tolerance.
2. **Throughput regression** — sustained 100 uploads/hour with p95
   end-to-end < 15 min is not maintained.
3. **Reliability regression** — any fixture's workflow lands in
   QUARANTINED or fails to recover from an injected worker restart.

## Layout

```
tests/regression/
├── README.md             — this file
├── conftest.py           — pytest fixtures (httpx client, manifest loader)
├── fixtures.yaml         — manifest of test media (name, sha256, kind)
├── golden/               — expected outputs (JSON, deterministic shape)
│   ├── ks-zoom-01.json
│   └── ...
├── runner.py             — end-to-end soak driver (CLI entry point)
├── test_audio_pipeline.py
├── test_video_pipeline.py
├── test_multimodal_pipeline.py
├── test_document_pipeline.py
└── test_recovery.py       — injects worker kills mid-step
```

## Fixtures are NOT committed

The media files are large; they live in the platform's regression
artifact bucket and are pulled by sha256 at test setup. The manifest
under `fixtures.yaml` is the single source of truth.

```bash
export NEXUS_REGRESSION_FIXTURE_URL=s3://nexus-regression-fixtures
pytest tests/regression -m "not soak"   # functional only, ~5 min
pytest tests/regression -m soak           # full 100 upload/hour run, ~60 min
```

## Tolerance model

Golden comparisons are field-aware:
- string fields use normalised Levenshtein distance (default ≤ 0.05)
- numeric fields use relative tolerance (default ≤ 5%)
- list cardinalities use absolute tolerance (default ±1)

Per-field overrides live in the golden file alongside the value:

```json
{
  "transcript": "Hello world",
  "_tolerances": {
    "transcript": {"kind": "string", "max_lev": 0.10}
  }
}
```

## Running locally

```
# Spin up dev env first (docker compose up)
make regression-bootstrap   # creates regression-tenant + uploads fixtures
make regression-run         # runs the suite
```

## CI gate

The GitHub Actions workflow at `.github/workflows/canonical-regression.yml`
runs the non-soak suite on every PR. The soak suite runs nightly on a
dedicated regression cluster and blocks promotion to production via the
Argo CD pre-sync hook described in [`infrastructure/gitops/CANARY_ROLLOUT.md`](../../infrastructure/gitops/CANARY_ROLLOUT.md).
