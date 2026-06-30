# Milestone 1 — Canonical Trust Baseline (Design / Planning)

**Status:** PLAN — no implementation yet. **Date:** 2026-06-29.
**Scope:** Canonical processing only (Video → frames+OCR → SCENES → URL-keyed PAGE_VISITS + ACTIONS).
**Mode:** Additive; behavioral changes default-off / lifecycle-only. Video-only stays the byte-identical floor.

## Why this milestone (and why first)
We cannot currently **measure** canonical accuracy — there is no end-to-end harness, only OCR-coverage "quality" scores. So every rating ("4/10", "7/10") is an *opinion*. M1 changes the **epistemics, not the extraction**: it makes canonical processing measurable, honestly-stated, and gated **before any smarter AI**. Its deliverable is not "a higher score" — it is **the first number we are allowed to say out loud**, plus the gates that make over-claiming impossible to merge.

**Standing guardrails (every section honors these):**
- **Generic / no hardcoding** — no per-app/host/client branches; domain knowledge is a confidence *boost* only.
- **Never-green-wash** — the system may be uncertain but never confidently wrong; a silent drop is a failure.
- **Video-only is the floor** — the ground-truth overlay stays additive and *late* (not M1).

## The split (your adjustment — do not contaminate the baseline)

| | **M1A — Measure Only** | **M1B — Trust Gates** |
|---|---|---|
| Changes extractor behavior? | **No.** Pure measurement + read-through. | **No extraction-logic change** — lifecycle/status + version-selection fix only. |
| Produces | The first honest baseline number (frozen). | Status contract, enforced provenance, live regression gate, version-freshness fixes, per-artifact quality report. |
| Deliverables | harness, seed corpus, label-sidecar schema, baseline metrics, provenance fields *designed*, silent-drop metrics *measured*, version-selection *audit*. | completion states, provenance/fact-status *enforced* on outputs, silent-drops → *gate failures*, version-freshness *fixes*, quality report attached to every artifact. **Then rerun the harness and compare to M1A.** |
| Exit | A committed per-video + aggregate scorecard; baseline frozen. | Every artifact carries status + quality report + provenance; regression gate green; version selection freshness-safe **everywhere**. |

The baseline must be captured in **M1A before** any gate or fix lands, so M1B's improvements are measured against a pristine reference.

---

## Section 1 — Label-sidecar schema + seed corpus

**The label sidecar (the contract).** One versioned JSON file per recording, the human-verified truth the harness scores against.

| Field group | Fields | Notes |
|---|---|---|
| `page_nodes[]` | `page_key` (stable *semantic* key, NOT a brittle full URL), `canonical_location`, `visible_in_video` (bool) | `visible_in_video=false` separates "video physically could never show this" (needs overlay) from "video showed it, we missed it" (a real defect). |
| `actions[]` | `verb` (navigate/type/select/click/submit), `target_label`, `value` (or `MASKED`), `page_key`, `required` vs `optional`, `visible_in_video` | The disambiguator (e.g. *"Sauce Labs Onesie"*) lives in `target_label` so the matcher can test whether the pipeline preserved it. |
| `edges[]` | ordered `(from_page_key → to_page_key)` transitions | Lets us score the **page-graph** and catch fabricated edges (the `Add-to-cart → /cart` invention). |
| `form_fields[]` | `label`, `value` (or `MASKED`), `page_key` | Feeds the value-survival invariant. |

- **`page_key`** is a *semantic* identity (normalized path / screen-name), never a raw URL — so it's stable across benign OCR variance and works for no-URL desktop/mainframe apps.
- **PII:** values are **masked at label time** (store a shape/hash or `MASKED`, never raw PII in the repo).

**Seed corpus (15–30 recordings, stratified, cross-domain):**
- saucedemo (e-commerce; we own the expected behavior) · a USAA/insurance flow · an internal CRUD tool · a **desktop / no-URL** app · an **SPA with a static shell URL** · a **mainframe-style** app · **Skyward + Aegis** proving grounds (we own the DOM → *gold* labels for free).
- **Held-out whole-app slice:** ≥2 apps that appear in **no** tuning — the genericity proof is "scores hold on an app the system never saw."
- **Labeling at scale (the 10k strategy, stated honestly):** gold from owned-DOM/overlay apps (free); silver from cross-model high-agreement; **humans adjudicate only disagreements.** The corpus certifies the generic *method*; per-tenant proof comes later from the live monitor + overlay — it is **not** a per-app proof, and we say so.

---

## Section 2 — Metrics and pass/fail gates

**The matcher (domain-blind).** Aligns artifact rows to labels by reusing the pipeline's **own** canonicalizers (`_normalized_url_path`, `_same_page_tail`, `_path_segments`, `_screen_name_from_url`), then aligns actions within a matched page by `verb + fuzzy target_label + value` (Hungarian/greedy on similarity, never exact-string). Metrics reported at **multiple thresholds** so leniency can't hide fabrication and strictness can't inflate it.

**Five metric families:**
1. **Action precision / recall / F1** — overall **and per-verb** (navigate/type/select/click/submit).
2. **Page-graph** — node P/R + edge P/R + **graph-edit-distance** (catches over-fragmentation *and* fabricated edges).
3. **Fabrication rate** — fraction of rows at `confidence ≥ 0.65` (the automation threshold) or `automation_ready=True` with **no matching label**. Measured *at* the threshold so the `verb=none→NAVIGATE@0.55` rule's leakage is exposed. **Target ≈ 0.**
4. **Calibration** — ECE + Brier + reliability diagram + AUROC + **overconfident-wrong mass** (conf ≥ 0.8 yet wrong).
5. **Value-survival** — every label value with `visible_in_video=true` appears in some `action.value`/`form_snapshot`, **plus** zero-silent-drop: `flagged_misses == total_misses`.

**M1A:** *measure* all five → commit per-video + aggregate **baseline scorecards** to the repo. No gate yet (reporting only).
**M1B:** turn the baselines into a **CI regression gate** — the build fails if fabrication > ε, ECE > threshold, action-F1 / graph-edit-distance / value-recall regress vs baseline, or any invariant breaks. Baselines committed per-corpus-version so improvements ratchet and regressions block. *(ECE is sample-size sensitive on a small corpus → report confidence intervals; keep the gate loose until the corpus grows.)*

---

## Section 3 — Canonical status / state contract

An artifact may be `completed` **only if** all required canonical outputs exist and pass validation. Otherwise it is honestly downgraded — never silently clean.

**States:** `completed` · `completed_with_warnings` · `needs_review` · `failed_retriable` · `failed_terminal`.

**Required-output checklist for clean `completed`:**
- source media stored **and hash-verified**
- frames extracted
- OCR/video evidence available **or explicitly skipped (reason recorded)**
- Pages & Forms derived **or explicitly marked unavailable**
- **no stale extractor versions** (→ Section 4)
- **no silent drops** (every drop logged with a reason)
- quality score computed

**Downgrade rules:**
- A required output weak/degraded → `completed_with_warnings`.
- A required output missing or a fact-status conflict that needs a human → `needs_review` (and **not** auto-exported / not auto-promoted to a buyer bundle).
- Transient failure (rate-limit, timeout, vision provider down) → `failed_retriable` (retry/backpressure policy; DLQ is later, P7).
- Unrecoverable (corrupt/unsupported media) → `failed_terminal`.

**Consumer rule:** downstream stages (test-case generation, export, Autopilot) read the status and **refuse to treat `needs_review`/`failed_*` as a trustworthy source.** This is the artifact-lifecycle expression of never-green-wash.

---

## Section 4 — Provenance / fact-status model + version-freshness audit

**Provenance on every canonical row:**

| Field | Values | Today |
|---|---|---|
| `source_artifact` | artifact id | ✅ exists |
| `source_frame` / `timestamp` | frame index + ts | ⚠️ partial |
| `extractor_version` | e.g. `v10` | ✅ stamped |
| `signal_source` | OCR · URL-regex · vision · overlay · LLM · user-edit | ⚠️ partial (`PageVisitSource`) |
| `confidence` | calibrated 0–1 | ⚠️ static today (calibration is M2/P3) |
| `fact_status` | **proven · inferred · conflict · missing** | ❌ introduce the field now |

- **M1A** *designs* the complete field set; **M1B** *enforces* it on outputs. The `fact_status` field is **introduced** in M1 (populated from existing strong-tier logic); full `proven = ≥2 independent signals agree` and `conflict` emission arrive with **consensus fusion in M2** — M1 just makes the field exist and never lie.

**Version-freshness audit (CONFIRMED bug, grounded):**
- **The hazard:** `composer.py:162 _max_version` uses `select(func.max(version_column))` — **lexical** max — and its docstring wrongly claims *"lexical max is sufficient."* It **breaks at v9 → v10** (`'v9' > 'v10'` lexically). Used in **8 derivation gates** (`composer.py:193, 210, 227, 244, 261, 278, 295, 312` — `_needs_scene_grouper`, `_needs_app_deduper`, …). Currently **masked** only because versions are still single-digit; **it will pick stale output at scale.**
- **The safe pattern already exists in the same layer:** `page_action_extractor.py:170-182` and `form_snapshot_extractor.py:177-189` order by `created_at.desc()` — and form_snapshot's comment *explicitly* documents the `'v9' > 'v10'` trap. The lesson was learned in the extractors, not propagated to the composer.
- **M1A (audit):** enumerate **every** version-selection site across the canonical layer; classify each as lexical-max vs `created_at`-ordered vs numeric-parse; flag the unsafe ones. Read-through only.
- **M1B (fix):** unify all sites on a freshness-safe selection (`created_at.desc()` or a numeric version parse), add a regression test that `v2 < v9 < v10 < v11` order correctly, and wire the **"no stale extractor versions"** check into the `completed` gate (Section 3).

---

## What M1 deliberately does NOT do (deferred, and why)
- **No multi-signal consensus fusion** → M2 (the structural engine; needs the harness as its judge).
- **No killing the `0.55` fabrication rule** → M2 — it changes extraction behavior, so it must be the **first *measured* improvement** against the M1A baseline.
- **No calibration refit, self-verification oracle, audio revival, vision-default-on** → M2–M3.
- **No ground-truth recorder, queues/DLQ/backpressure** → P6–P7.

M1 is **epistemics + lifecycle only.** Afterward, every later change is provable instead of asserted.

## Definition of done
1. Harness runs over the committed corpus and emits per-video + aggregate scorecards (the first honest number). *(M1A)*
2. Baseline frozen and committed. *(M1A)*
3. Every version-selection site audited; unsafe ones listed. *(M1A)*
4. Regression gate live and green against the baseline. *(M1B)*
5. Every artifact carries a status (5-state), a quality report, and complete provenance incl. `fact_status`. *(M1B)*
6. Silent drops are gate failures; version selection is freshness-safe everywhere. *(M1B)*
7. Harness re-run post-M1B; the delta vs M1A is documented (no regression; gates added).
