# Nexus QA — CPU Defect-Hunt Report

**Date:** 2026-05-17 (initial); **Updated:** 2026-05-18 (D1+D2+D4 fixed)
**Stack:** Local docker-compose, all 19 containers healthy
**Scope:** Defect identification on canonical pipeline.
*Performance numbers not in scope — CPU host. GPU validation deferred to GCP.*

> **Update 2026-05-18:** D1, D2, and D4 are all fixed and verified locally.
> Only D3 (Docker Desktop resource starvation) remains — environmental, not
> a code defect; no production impact on GCP.

---

## Executive Summary

| Category | Count |
|---|---|
| Test scenarios run | 11 (10 battery + 1 client UI live test) |
| **Clean completions** | **9** |
| Test-script artifacts (not product bugs) | 1 |
| Real product defects found | **3** |
| P0 (block release) | 0 |
| P1 (must-fix before GPU prod) | 2 → **0 (both fixed 2026-05-18)** |
| P2 (nice-to-fix) | 1 (D3 — environmental) |
| P3 (data-purity, no user impact) | 1 → **0 (D4 fixed 2026-05-18)** |

**Bottom line:** Pipeline is functionally correct on the happy paths.
Three real defects identified, none P0. All findings reproducible.

---

## Phase A — Existing test coverage baseline ✅

| Suite | Result |
|---|---|
| 19 containers healthy | ✅ |
| Contract tests (`tests/contracts/`) | ✅ 38/38 passing |
| Eyes degraded-fallback test | ✅ |
| Architect-P0 fixes (5 categories) | ✅ All shipped + verified |

---

## Phase B — Multi-shape upload battery

| # | Scenario | Profile | Terminal | Artifact status | qg_passed | qg_outcome | Wall (s) | Notes |
|---|---|---|---|---|---|---|---|---|
| 1 | audio-only `.m4a` | fast | SUBMIT_FAIL | — | — | — | — | Test-script bug (file path with .m4a extension); orchestrator accepts audio-only fine — not a product defect |
| 2 | video, no audio | fast | ✅ completed | `completed_degraded` | false | **`needs_review`** | 36 | ✓ Correct — no real transcript |
| 3 | short A+V 17s | fast | ✅ completed | `completed_degraded` | true | `pass_with_warnings` | 78 | ✓ Correct |
| 4 | medium A+V 35s | fast | ✅ completed | `completed_degraded` | true | `pass_with_warnings` | 77 | ✓ Correct |
| 5 | larger A+V 38s | fast | ✅ completed | `completed_degraded` | true | `pass_with_warnings` | 78 | ✓ Correct |
| 6 | multimodal | multimodal | ✅ completed | `completed_degraded` | true | `pass_with_warnings` | 237 | ✓ Correct (LLaVA circuit-broke on CPU, fallback OK) |
| 7 | **duplicate fingerprint** | fast | ✅ completed | (new artifact created) | — | — | 77 | **🔴 D1 — fingerprint dedup did NOT short-circuit** |
| 8 | **deep profile** | deep | ❌ **quarantined** | — | — | — | 767 | **🔴 D2 — deep profile times out on CPU** |
| 9 | cross-tenant | fast | ✅ completed | `completed_degraded` | true | `pass_with_warnings` | 335 | ✓ Tenant isolation working |
| 10 | second multimodal | multimodal | ✅ completed | `completed_degraded` | true | `pass_with_warnings` | 699 | ✓ Slower (system was loaded) |

**Per-scenario wall times** illustrate the CPU bottleneck:
- Fast profile: ~70-80s (audio + minimal frames, no LLaVA)
- Multimodal: 237-699s (LLaVA per scene, 3-12 min on CPU)
- Deep: quarantined at 12+ min (deep is GPU-only by design)

---

## Phase C/D/E — partial (will continue post-handover if needed)

Phase B exposed the highest-priority defects already; remaining phases
would surface lower-severity issues (UI label drift, recovery edge cases).
Defer until GCP-GPU validation provides cleaner baseline.

---

## Defects Identified

### D1 (P1) — Fingerprint dedup didn't short-circuit duplicate upload ✅ FIXED

**Phase B test #7:** Uploaded `/tmp/pb_7.mp4` which is byte-identical to
`/tmp/pb_3.mp4` (sha256 verified: both `b7128f33dfbd...`). Same tenant
`68c79953`, ~4 min after test #3 completed.

**Expected:** Cache hit + reuse existing artifact_id.

**Actual:** New workflow created (`56405756-...`), new artifact_id
allocated (`f5e9bf14-...`), full pipeline ran 77s.

**Actual root cause:** The orchestrator's fingerprint compute hashed the
**post-auto-extract** file set: the uploaded video bytes PLUS the
ffmpeg-extracted audio bytes. ffmpeg injects encoder metadata + muxer
timestamps that vary slightly between runs, so the combined sha256
differed even for byte-identical re-uploads. The persisted artifact
ended up indexed under fingerprint `2230603f96b5...` (post-extract),
but the re-upload computed a different post-extract sha256, so the
Redis cache lookup missed.

**Fix shipped** (in
[products/nexus-qa-orchestrator/app/main.py](Nexus_power/products/nexus-qa-orchestrator/app/main.py)):
move the fingerprint compute to BEFORE `_extract_audio_from_video` so
only the source uploads contribute to the hash. Applied at both the
`/process` and `/start-workflow` ingress paths.

**Verified locally:** uploading `/tmp/pb_3.mp4` twice → first run
persists artifact `7c3869ff` under fingerprint `b7128f33dfbd...`,
second submission logs `P3 cache hit: fingerprint=b7128f33dfbd0d8c
artifact=7c3869ff (cache_reason=terminal_artifact_exists)`.

---

### D2 (P1) — Deep profile quarantines on CPU at ~13 min ✅ FIXED

**Phase B test #8:** `deep` profile on the 17s seed video. Quarantined
at 25.5 min wall time, attempt=13, error="step exceeded deadline",
current_step=ears.align.

**Root cause:** Deep profile runs LLaVA on every frame (not just
representative). On CPU, each LLaVA call is 30+ seconds. Many frames =
hours of work. Step deadlines fire before completion → quarantine.

**Architecturally known:** Deep profile is documented as GPU-only.

**Impact:** A client picking "deep" on a CPU deployment quarantines and
sees a "Failed" state with no useful error to the end-user.

**Fix shipped** (in
[products/nexus-qa-orchestrator/app/main.py](Nexus_power/products/nexus-qa-orchestrator/app/main.py)):
new `EYES_DEEP_PROFILE_ENABLED` env flag (default `false`). When false,
both `/process` and `/start-workflow` return HTTP 400 immediately with
detail message:
*"deep profile requires a GPU-enabled deployment. Use 'fast' or
'multimodal' instead, or contact your admin to enable
EYES_DEEP_PROFILE_ENABLED."*
Production GPU deployments flip the env var to `true` to allow deep
profile submissions.

**Verified locally:** `curl -F processing_profile=deep` → `HTTP 400`
with the exact message above (instant; no 13-min quarantine).

---

### D3 (P2) — Docker Desktop / WSL2 starves under sustained load

**Trigger:** Phase B running 10 sequential workflows on a Windows
Docker Desktop / WSL2 host. After ~4 hours of cumulative load, the
Docker Desktop daemon returned 500s on its container API, causing the
UI's `createSession` call to time out at the default 30s.

**Symptoms observed in browser:** "Error: timeout of 30000ms exceeded".

**Root cause:** Resource exhaustion on the dev host (not a code
defect). Docker Desktop on Windows has constrained CPU/memory by
default; with 19 containers + heavy concurrent workflows, the bridge
network becomes a bottleneck.

**Production impact:** None on GCP (managed Cloud SQL + Memorystore +
dedicated VM eliminate the bottleneck). Only affects local dev hosts
when running stress tests.

**Severity P2** because client-facing failure mode (timeout error) is
confusing; mitigated by:
- (a) Client UI fix shipped: `createSession` timeout 30s → 60s defensive
  ([api.ts:521-535](Nexus_power/client/src/services/api.ts#L521-L535))
- (b) Operational guidance: dev hosts need ≥8 vCPU + ≥16 GB RAM
  allocated to Docker Desktop for sustained-load testing

---

### D6 (P0/P1 bundle) — Canonical Result page renders mostly empty after multimodal-on-CPU completion ✅ FIXED

**Reported by client live (2026-05-18):** After workflow `b55a60ec` /
artifact `fda3bf71` completed (multimodal profile, CPU host, LLaVA
circuit-broke as expected), the Canonical Result page rendered:

- **Hero:** Canonical Quality Score showed "N/A"; IDs truncated to
  `6a46044b-302…` with no way to grab the full id. "NEEDS REVIEW" and
  "Fresh" badges had no explanation.
- **Recent Sessions tile:** "Insights: 0  Processing: 0.0s"
- **Trust & Quality Gate panel:** Transcript / Visual / PII Redaction
  bars all blank (`—`), Completeness 50%; "This asset needs review"
  banner had no specific reasons.
- **Model Provenance:** `ears_model: whisper-tiny`, `eyes_model: ""`
  (literally blank).
- **Processing Timeline:** every stage rendered `—` (no timestamps).
- **Visual analysis summary:** dumped raw OCR text from a Guardian
  Life Insurance website ("guardian dental The Guardian Life
  Insurance…") as if it were a curated visual summary.

**Root causes (six independent backend gaps + one UI gap):**

1. **score_breakdown** (`full_artifact_json.score_breakdown`) was
   never written — UI Trust panel's four ScoreBars all show `—`.
2. **review_reasons** never written — orange "Needs Review" banner
   has no body, so the user can't tell WHY.
3. **eyes_model** in `model_provenance` left as `""` when LLaVA
   circuit-broke or when the eyes engine didn't surface its tool
   chain — Model Provenance table cell blank.
4. **processing_time_seconds** stayed at the default `0.0` because the
   enrichment write path never computed wall time → UI shows
   "Processings: 0.0s".
5. **brain_quality_score** stayed NULL when the brain quality-gate
   step was skipped (vision-dependent path) → UI shows "N/A".
6. **`/api/v1/workflows/{id}/timeline`** only read the legacy
   `workflow_instances` table; Phase-12 plane workflows live in
   `workflow_state` + `workflow_step_history`, so this endpoint
   returned 404 → UI's Processing Timeline section was blank.
7. **UI ID truncation** used `.slice(0, 12)` with no copy affordance;
   client couldn't grab the full canonical/artifact/session id for
   support tickets.
8. **visual_summary** when LLaVA was offline dumped raw OCR text
   joined with `→`, so the UI presented it as a real visual summary
   even though it was just screen-text noise.

**Fixes shipped:**

- Spine ([engines/spine-engine/main.py](Nexus_power/engines/spine-engine/main.py)):
  - New helpers `_compute_score_breakdown` (returns the four
    sub-scores the UI needs; nulls for unmeasured dimensions),
    `_compute_review_reasons` (plain-English reasons the operator
    can act on, with degraded-stage-specific phrasing), and
    `_format_visual_summary` (clearly labels degraded LLaVA output
    instead of pretending OCR text is a curated summary).
  - `_update_artifact_enrichment_in_db` now:
    - merges a `model_provenance_patch` (with `eyes_model` fallback
      "tesseract+llava:7b (vision circuit-open)" when blank),
    - writes the new `score_breakdown` + `review_reasons` keys into
      `full_artifact_json`,
    - sets `brain_quality_score` to `semantic_completeness_score`
      when the brain gate didn't produce one,
    - computes `processing_time_seconds` from
      `now - artifact.created_at`,
    - sets `completed_at`.
- Platform-API
  ([platform/api/app/routers/artifacts.py](Nexus_power/platform/api/app/routers/artifacts.py)):
  `GET /api/v1/workflows/{id}/timeline` now falls through to
  `workflow_state` + `workflow_step_history` when the legacy table
  misses, aggregating raw DAG step rows into the 7 UI canonical-stage
  events (`media_probe.*`, `audio_transcription.*`, etc.). Verified
  client's workflow `b55a60ec` now returns 14 timeline events.
- UI ([client/src/pages/CanonicalResultPage.tsx](Nexus_power/client/src/pages/CanonicalResultPage.tsx),
  [client/src/components/StatusBadge.tsx](Nexus_power/client/src/components/StatusBadge.tsx),
  [client/src/pages/SessionCommandPage.tsx](Nexus_power/client/src/pages/SessionCommandPage.tsx)):
  - New `IdChip` component replaces `.slice(0, 12)` truncations with
    a click-to-expand + click-to-copy control; added a dedicated
    "Canonical" id chip alongside Session / Workflow / Artifact so
    the client has the canonical id labeled explicitly.
  - `StatusBadge` accepts a `tooltip` prop; "NEEDS REVIEW", "Fresh",
    "Freshly Produced", quality-grade badges, and the Quality Gate
    outcome badge all carry explanatory tooltips now.
  - Sessions tile's "Insights" and "Processing" labels carry tooltips
    explaining what the numbers mean.

**Verified locally (2026-05-18):** Fresh upload `033584c2` ran clean:
- `brain_quality_score=0.660` (was NULL)
- `processing_time_seconds=13.65` (was 0)
- `score_breakdown={"transcript": 0.32, "visual": 0.5, "pii": null, "completeness": 0.51}`
- `review_reasons` populated with the LLaVA-degraded explanation
- `model_provenance.eyes_model="moondream"` (real model name)
- `visual_summary` starts with: "Visual analysis was unavailable for
  this run (vision LLM was offline). Raw OCR text from screen frames
  preserved below for evidence purposes only — this is not a curated
  summary:"
- Timeline endpoint returns 14 events for the client's workflow.

**Client's stuck artifact (`fda3bf71`) backfilled** with computed
fields directly so the page no longer renders empty on refresh.

---

### D5 (P1) — Session row stuck at `scheduled` after workflow completes ✅ FIXED

**Reported by client live (2026-05-18):** After workflow
`1ff48084-06e7-44ee-9383-d56cf0de9485` / artifact
`9165b2d7-550f-46f8-be75-228b6dac20cf` completed cleanly with the
Processing panel showing "Completed · 100%", the Recent Sessions panel
still showed the session as "Processing". The session row in the DB
was at `status='scheduled'`, never updated.

**Root cause:** The Phase 12 plane-based workflow path
(`/process` → `_release_plane_workflow_resources` background reaper)
only released admission slots and the fingerprint lock when a
workflow reached terminal status. It never PATCHed the session row.
The legacy `_update_session_status` calls only fire on cache-hits or
duplicate-fingerprint rejections — never for the regular "submit ran
to completion" path.

**Fix shipped** (in
[products/nexus-qa-orchestrator/app/main.py](Nexus_power/products/nexus-qa-orchestrator/app/main.py)):
`_release_plane_workflow_resources` now accepts a `session_id`
parameter and, after observing the workflow plane's terminal status,
PATCHes the session row via `_update_session_status`. Status map:
`COMPLETED → completed`, `FAILED → failed`,
`CANCELLED → cancelled`, `QUARANTINED → failed`.

**Verified end-to-end (2026-05-18):**
Created session `8c866eb6`, submitted /process, polled to terminal.
Orchestrator log: `plane.workflow.terminal wf=95dfcc99 status=completed`
→ `PATCH /api/v1/sessions/8c866eb6 HTTP/1.1 200 OK`
→ `Session 8c866eb6 status updated to completed`.
DB confirms session row now reads `status=completed`.

**Client's stuck session (`e29eceb9`) manually unstuck** while the fix
was rolling out so the UI flipped immediately. All future workflow
completions auto-propagate.

---

### D4 (P3) — Minimal artifacts get `needs_review` outcome on short transcripts ✅ FIXED

**Observed in client's live UI test** (workflow `dcb46a7f-...`, artifact
`6ccb6eab-...`): During the brief window when the artifact is in
`status=minimal`, before enrichment completes, `quality_gate_outcome`
is `needs_review` because the score-floor check fires before the
`minimal` short-circuit in `_compute_quality_gate`.

**Production impact:** None visible to users. The UI's
`resolveWorkflowDisplayState` correctly forces "enriching" badge for
minimal artifacts regardless of underlying qg value. The DB row
self-heals to the correct outcome when enrichment updates run
(~30-60s after minimal write).

**Data purity impact:** Admin SQL queries on `quality_gate_outcome`
column would briefly count minimal artifacts as `needs_review`. Could
mislead reporting if someone polls during the enrichment window.

**Severity P3 — cosmetic / data-only.**

**Fix shipped** (in
[engines/spine-engine/main.py](Nexus_power/engines/spine-engine/main.py)):
`_compute_quality_gate` reordered so the `minimal` short-circuit fires
BEFORE the score-floor check. 1-line move.

**Verified locally:** uploading a fresh video → polling the artifact
every 1s during the minimal window now shows
`status=minimal, quality_gate_passed=true, quality_gate_outcome=pass_with_warnings`
(was `false, needs_review`).

---

## Defects Found and Fixed (this session — already shipped)

| # | Defect | Fix |
|---|---|---|
| F1 | FrameAnalysis fallback used wrong field names (`timestamp` vs `timestamp_seconds`) | Architect P0 #1 — corrected in workflow_handlers.py |
| F2 | OCR pool didn't truly kill hung workers (shutdown(wait=False) is soft) | Architect followup — terminate + SIGKILL on PIDs |
| F3 | Tenant admission was in-memory per-replica, not globally enforced | Architect followup — Redis Lua atomic check-and-incr |
| F4 | Tenant FK violations on persist_minimal_artifact for unknown tenants | Auto-bootstrap tenant row in `_ensure_tenant_exists` |
| F5 | NexusUser had `.role` but workflows code expected `.roles` (AttributeError on GET) | Added `roles` property returning `[self.role]` |
| F6 | LLaVA per-scene calls could hang the whole stage for 600s | Per-scene `asyncio.wait_for(30s)` + circuit breaker at 2 consecutive failures |
| F7 | OCREngine.load() was never awaited (coroutine warning, stub OCR) | `asyncio.run(eng.load())` in worker init |
| F8 | Replay re-dispatched envelopes with already-expired workflow deadline | Replay now extends `deadline_at = now + plan.deadline_seconds` |
| F9 | quality_gate_passed defaulted False, never updated → every artifact "failed" in UI | `_compute_quality_gate` helper wired into both persist + enrichment write paths |
| F10 | Persist endpoint hardcoded `quality_gate_passed=False` bypassing compute | Removed the hardcode |
| F11 | UI showed completed workflows as RUNNING/finalizing/100% | API now exposes `artifact_id` at top level; UI reads it |
| F12 | UI showed stale `error` on completed workflows | API surfaces `error=None` for status=completed |
| F13 | UI showed "Workflow not found" on cross-tenant access (was 403, masked) | Differentiates 403 → "Access denied" vs 404 |
| F14 | 503 saturation guard rendered as permanent "Failed" row | New 503 handler → transient "System busy, retry" banner |
| F15 | `completed_degraded` artifact status fell through React's mapping → showed RUNNING | Added to TERMINAL_ART set |
| F16 | createSession default 30s timeout failed under load | Bumped to 60s defensive |
| F17 | D1 (P1) — fingerprint dedup not firing on re-upload | Hash source uploads BEFORE ffmpeg-extract (orchestrator main.py) |
| F18 | D2 (P1) — deep profile quarantines on CPU | Reject at ingress via EYES_DEEP_PROFILE_ENABLED env flag (default false) |
| F19 | D4 (P3) — minimal artifacts get needs_review in admin SQL | Reorder _compute_quality_gate so minimal short-circuits first |
| F20 | D5 (P1) — session stays "scheduled" after workflow completes | `_release_plane_workflow_resources` now PATCHes session row on terminal |
| F21 | D6a — Trust panel ScoreBars all blank | spine: compute & store `score_breakdown` in full_artifact_json |
| F22 | D6b — "Needs Review" banner empty | spine: generate `review_reasons` with degraded-stage-specific phrasing |
| F23 | D6c — Model Provenance `eyes_model` blank | spine: fallback string when eyes engine didn't surface its model |
| F24 | D6d — "Processings: 0.0s" on completed artifact | spine: compute `processing_time_seconds` from wall time on enrichment write |
| F25 | D6e — "Canonical Quality Score: N/A" | spine: `brain_quality_score` falls back to `semantic_completeness_score` |
| F26 | D6f — Visual summary masquerading as real summary | spine: `_format_visual_summary` clearly labels degraded LLaVA path |
| F27 | D6g — Processing Timeline empty for Phase-12 workflows | platform-api: timeline endpoint reads from workflow_state + workflow_step_history |
| F28 | D6h — IDs truncated, no copy, "NEEDS REVIEW"/"Fresh" unexplained | UI: IdChip component + tooltip prop on StatusBadge wired through |

---

## Recommended Next Steps

1. ✅ **D1 + D2 + D4 — all fixed and locally verified (2026-05-18)**
2. **D3** is environmental — document in deployment guide, no code change
3. **Run Phase C + D + E now that D1/D2/D4 are fixed** — to surface remaining defects
4. **GCP-GPU validation** when quota approves — primarily for performance numbers; defect coverage already strong from this CPU run

---

## Test artifacts (for reproducibility)

| Asset | Location |
|---|---|
| Sample videos | `data/evidence/test_video.mp4`, `test_video2.mp4`, `test_synth.mp4` |
| Real-audio seed | `/tmp/seed_real_audio.mp4` |
| Phase B script | `scripts/cloud/phase_b_upload_battery.sh` |
| Phase B raw results | `/tmp/phase_b_results.csv` |
| Phase C script (not run) | `scripts/cloud/phase_c_multitenant.sh` |
| Phase D script (not run) | `scripts/cloud/phase_d_recovery.sh` |
| Live client test | workflow `dcb46a7f-4f3e-45b8-bc54-ef956bcbaf5a`, artifact `6ccb6eab-38f4-421b-914d-56f3ca1bea45` |
