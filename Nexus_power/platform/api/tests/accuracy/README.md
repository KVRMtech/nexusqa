# Canonical Trust Baseline — Accuracy Harness (Milestone 1A)

The **measuring stick** for canonical processing. It turns *"it produces rows"* into
*"the rows are TRUE and the number is trustworthy."* Pure stdlib, offline — no app/DB/LLM
imports, so it runs in CI and standalone.

> **Status:** MEASURE-ONLY (M1A). This subsystem **changes no extractor behavior.** It
> only scores an extracted artifact against a hand-verified label and reports the five
> trust metrics. The gates (M1B) come next.

## What it measures (the five trust metrics)

| Metric | Question | Never-green-wash rule |
|---|---|---|
| **faithfulness** (`fabrication_rate`) | Of rows emitted at/above the automation threshold, how many have no matching label? | Target ≈ 0. Measured *at* the threshold so a just-under-threshold fabrication rule is exposed. |
| **completeness** (action P/R/F1 + `silent_drops`) | Is every visible item represented — as a confident row OR an honest placeholder? | A miss is honest only if a **same-page placeholder** covers it; an unrelated `MISSING_PAGE` can't launder a dropped action. |
| **calibration** (ECE / reliability / overconfident-wrong) | Does a 0.9 mean ~90% correct? | Reported, not assumed. |
| **page_graph** (node/edge P/R + graph-edit-distance) | Is the page graph right — and free of fabricated edges? | Fabricated edges surface explicitly. |
| **value_survival** | Does every typed value the label marks `visible_in_video` appear in the extraction? | Survival needs exact / full-**token** match — never a raw substring (`admin` ≠ `administrator`). |

`visible_in_video` on every label item separates *"video physically could never show
this"* (needs the ground-truth overlay) from *"video showed it, we missed it"* (a real
defect). Only the latter counts against confident recall.

## Run it

```bash
cd Nexus_power/platform/api
PYTHONPATH=".:../../sdk/nexus-sdk" python -m pytest tests/accuracy/ -q   # self-tests
python tests/accuracy/test_canonical_harness.py                          # standalone + prints a scorecard
```

## Add a labeled recording to the corpus

1. **Extract** a real recording through canonical processing and export its page_visits /
   page_actions / edges into the harness shape (`CanonicalDoc` — see `harness.py`).
2. **Hand-label** the truth as a sidecar JSON (`page_nodes`, `actions`, `edges`), each item
   carrying `visible_in_video: true|false`. Mask PII values (`"MASKED"` or a shape/hash —
   never raw PII in the repo).
3. **Score**: `from harness import score, CanonicalDoc; score(extracted, label)`.
4. **Aggregate** across the corpus with `aggregate([...scorecards])` and commit the
   per-video + aggregate scorecards as the frozen baseline.

### Seed corpus (target: 15–30, stratified, cross-domain)
saucedemo (e-commerce) · a USAA/insurance flow · an internal CRUD tool · a desktop/no-URL
app · an SPA with a static shell URL · a mainframe-style app · Skyward + Aegis (we own the
DOM → *gold* labels for free). Plus a **held-out whole-app slice** (apps used in no tuning)
— the genericity proof. Labeling at scale: gold from owned-DOM/overlay apps; silver from
cross-model high agreement; humans adjudicate only disagreements.

## Genericity (no hardcoding)
The matcher reuses generic normalizers (`norm`, `norm_page_key`) — path + screen-name only,
no host lists, no per-app constants. Works for URL apps and no-URL desktop/mainframe apps
alike. Adding a domain term may only *boost* a match, never gate one.

## Next (M1B — trust gates)
Commit the baseline → add `test_corpus_regression.py` that FAILS the build if
`fabrication_rate > ε`, `ece > threshold`, action-F1 / graph-edit-distance / value-recall
regress, or any invariant breaks. See `docs/CANONICAL_TRUST_BASELINE_M1.md`.
