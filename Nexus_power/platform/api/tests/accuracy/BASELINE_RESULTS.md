# Canonical Trust Baseline — FIRST MEASURED RESULTS (2026-06-30)

The keystone of the 10/10 program: **measurement before improvement.** This is the first
real number, scored by the harness against hand-verified ground truth on **real
production extraction** for two recordings (saucedemo + the Aegis insurance flow).

Reproduce: `cd platform/api && python tests/accuracy/baseline.py`

## Aggregate baseline (3 scorecards)
| Metric | Value | What it means | 10/10 target |
|---|---|---|---|
| **fabrication_rate** | **0.36** | ~a third of confident rows don't match ground truth | ≈ 0 |
| **action F1** | **0.57** | ~57% precision/recall on actions | ≥ 0.95 |
| **ECE (calibration)** | **0.23** | confidence is NOT trustworthy (a 0.9 isn't right 90%) | ≤ 0.05 |
| **value_recall** | **0.76** | ~24% of typed values dropped | ≥ 0.98 (video-only ceiling-bound) |
| **total silent drops** | **18** | 18 misses with NO honest placeholder | 0 |

**Honest translation: the measured pipeline is ~4–5/10, exactly as the code review estimated — now with numbers to improve against.**

## Per-scorecard — the harness caught every known defect
**saucedemo · EXTRACTION:** fabrication 0.43, action F1 0.58, ECE 0.33, node F1 0.80, value_recall **1.0** (the login value `visual_user` IS captured at extraction). Fabricated edges = the recording-chrome phantom pages + the OCR `checkout-step-to`.

**saucedemo · TEST CASE:** fabrication 0.40 (**the duplicate "Sort order" steps**), action F1 0.52, **silent drops 8** (incl. `type Username`, `type Password`, `click Login` — **the dropped login**), value_recall **0.67** (**`visual_user` DROPPED** — the login value lost between extraction and the test). So the harness *proves* the bimodal finding: extraction captured the login; the test case dropped it.

**aegis insurance · EXTRACTION:** fabrication 0.25, action F1 0.62, **ECE 0.085** (much better calibrated than saucedemo), value_recall 0.60 (the raw extraction missed `First name`/`Last name`/`Country` — they only appear in the enriched/proposed test). The clean owned app scores higher — confirming quality is **bimodal**.

## What this baseline unlocks
1. Every later fix is now **provable**: `baseline X → fix → re-measure → Y`. No more opinions.
2. The next moves with the biggest measured leverage:
   - **Disambiguator** (parse `Add to cart - Sauce Labs Onesie` → name + anchor) → recovers the 3 add-to-cart misses → action-F1 up.
   - **Kill the `navigate@0.55` fabrication** → fabrication_rate down.
   - **Drop the recording-chrome phantom pages + OCR `checkout-step-to`** → node/edge F1 up, GED down.
   - **"Every typed value survives" invariant** → the dropped-login silent drops become 0.
   - **Calibration map** → ECE 0.23 → ≤ 0.05.
3. **M1B (next):** freeze this baseline + a CI regression gate so nothing regresses and every gain ratchets.

## Honest caveats on this first baseline
- Ground truth is hand-built from app knowledge + the layer-by-layer review (not frame-by-frame human labels) — a legitimate *seed* per the M1 plan; it will tighten as the corpus grows and the ground-truth overlay supplies gold labels.
- The matcher/normalizer is iteration 1; a few artifacts remain (e.g. a Chrome "No thanks" popup, page-placement of `Finish`). These are refinements, not blockers — the numbers are directionally honest and catch the real defects.
