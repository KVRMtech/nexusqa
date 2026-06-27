# Pages & Forms → Trustworthy Accuracy — Phase-by-Phase Implementation Plan

> **Decision (final, 10-agent panel):** hybrid-tiered capture. Instrument for ground
> truth where we can (web), keep hardened video as the universal fallback, and make
> **PROVEN-vs-INFERRED provenance load-bearing** instead of marketing a "100%" number.
> Sequence Road-A hygiene FIRST (fixes the audited 2/10 on the existing corpus); treat
> Road B as a real (greenfield) recorder build, gated by a cost spike + PII + reachability.
>
> **Hard constraint threaded through every phase: GENERIC.** 100+ customers, 1000+ UI
> apps. Zero per-customer/per-app code. Everything keys off **protocol/signal**
> (CDP events, URL shape, OCR/a11y signals, extraction tier) — never an app/host denylist
> beyond config-driven, extensible signal lists. New apps work on day one with no change.

Owner: ___  ·  Status: PLANNED  ·  Relates to: `playwright_10of10_plan`, `pages_forms_capture_decision`.

---

## North-star + how we MEASURE it (build this first — it judges every phase)

**Goal:** every Pages & Forms row is either **ground-truth-correct** or **honestly labelled
best-effort** — never a confidently-wrong guess. "Accuracy" = (a) right SET of pages
(completeness), (b) right URL per page, (c) right actions, (d) right form field values.

**Measurement harness (Phase 0 deliverable, generic):**
`tests/test_pages_forms_accuracy_harness.py` — `score_pages_forms(extracted, gold)` over a
**gold set** of hand-labelled recordings spanning modalities (clean web, conferencing
screen-share, SPA, multi-app, mainframe/desktop). Metrics per recording: page-completeness
(missing/extra/merged), URL exactness (scheme/www/path/.html/query), action recall,
form-value recall, and **calibration** (does a row's `extraction_confidence` predict its
correctness?). The harness is the CI gate and the kill-criteria evidence — it is generic
(works on any recording, any app) and is the single source of the "did it improve" number.

---

## PHASE 0 — Road A hygiene + 4 cheap fixes  ·  Week 1  ·  fixes 3 of 4 audited failures

**Why:** these fix the OBSERVED 2/10 on the EXISTING corpus, on the frozen-adjacent
extractor, additive, zero new attack surface — and they help EVERY modality that will never
have an event channel (mainframe/Citrix/canvas), so they pay off forever.

| # | Task | File / function | Generic-by-design |
|---|---|---|---|
| 0.1 | **Admit `.` to the path regex** so `.html`/`.aspx`/`.jsp` survive; stop the trailing-punctuation strip eating `.html` | `page_visit_extractor.py` `_PATH_ONLY_PATTERN` (~L198), `_first_url_match` (~L204) | Pure URL-shape rule — works for any extension/TLD/path on any app |
| 0.2 | **Preserve `www`/scheme** through canonicalisation (store the real host as observed; canonical host stays a separate field) | `_canonicalise_host` (~L983), `_build_page_visit` (~L1211) | Generic host handling; no host allow/denylist |
| 0.3 | **Stop folding a homeless frame ACROSS a URL change** — when a no-location frame sits between two *different* URLs, emit a low-confidence **`possible_missing_page`** event-less visit instead of silently merging it into the previous page | grouping loop `if key == ("","",""):` (~L1497-1504) | Generic: any fast transition on any app surfaces instead of vanishing |
| 0.4 | **Recording-tool / conferencing-chrome quarantine** — a generic, config-driven **signal classifier** that flags a frame/scene as *capture-overlay noise* (so it can never become a page). Signals: OCR/title matches an **extensible regex set** of recording-overlay phrases ("screen share", "you are sharing", "stop sharing", "main view", meeting-toolbar tokens) + the frame lacks any address-bar/app-content signal | new `recording_chrome.py` guard called in `_resolve_frame_location` / scene layer; signal list in `config.py` (env-overridable) | **Generic + extensible:** signal-based, not an app denylist; a new conferencing tool is covered by adding one regex to config, and the "no app-content" co-signal generalises it |
| 0.5 | **Accuracy harness + gold set** (the north-star above) | `tests/test_pages_forms_accuracy_harness.py` | Generic scorer over any recording |

**Acceptance:** harness shows (a) `.html`/`www` survive on a web recording; (b) a fast
transition no longer disappears (emits `possible_missing_page`); (c) the conferencing
"Main View" frame is quarantined, not page 0; (d) **no regression** on the frozen
scene-URL canonicalisation (run the existing storyboard tests). Target: clean single-app
web recording 2-3/10 → **7-8/10**; conferencing screen-share materially improved.

**Freeze-safety:** all additive to the page_visits projection; the canonical
`visual_frames/scenes/flows` tables are NOT mutated.

**Exit gate → Phase 1:** harness green + the 4 fixes merged behind the existing
extractor version bump (idempotent re-derive).

---

## PHASE 1 — The honest-degradation contract  ·  Week 1-2  ·  makes every later claim safe

**Why:** this delivers the PROOF / never-green-wash thesis WITHOUT ground truth, and makes
a "100%" over-claim **structurally impossible** (every row already wears its provenance).
Ship this BEFORE any recorder.

| # | Task | File / function | Generic-by-design |
|---|---|---|---|
| 1.1 | **Provenance is already computed** (`PageVisitSource` + `_confidence_for_source` + `extraction_confidence` per row). Surface it **end-to-end**: add `source` + `extraction_confidence` to the `/visual-flow` + storyboard API payloads | `routers/artifacts.py`, `routers/storyboard.py`, `page_schemas.py` | Generic: every row, every app, stamped by tier |
| 1.2 | **UI: PROVEN vs INFERRED per row** — a provenance chip on each page in `PageVisitsPanel` (PROVEN=url_regex/instrumented, INFERRED=vision/title), + a `possible_missing_page` badge; extend the existing extraction-health degraded badge | `VisualFlowDiagramPage.tsx` `PageVisitsPanel` | Generic display driven by the row's source field |
| 1.3 | **Exporters carry provenance** — every exported test case / Excel / qTest / Playwright header states which rows are proven vs inferred | `services/test_exporters/*`, `script_factory/compiler.py` (header) | Generic per row |
| 1.4 | **GATE test-gen on confidence** — in the generator, any page/step sourced below a threshold (e.g. not url_regex/instrumented) is flagged `needs_confirmation` and its derived assertions are emitted as **INFERRED/UNPROVEN** (reuse the Track-1 honest-confidence path), never silent fact | `test_factory/generator.py`, `confidence.py` | Generic: keys off `extraction_confidence`, not app identity |
| 1.5 | **Calibration check** in the harness — assert `extraction_confidence` predicts correctness on the gold set | harness | Generic |

**Acceptance:** a recording with a vision-inferred page shows INFERRED in UI + export + the
generated test marks those steps UNPROVEN; a fully url_regex recording shows PROVEN
throughout. No row can present a guess as fact.

**Freeze-safety:** read-path + display + an additive gate in generation; no pipeline change.

**Exit gate → Phase 2:** provenance visible end-to-end; harness calibration passes.

> **At this point the audited failure is fixed and the product is HONEST without any new
> capture tech.** Everything below is the path to *ground truth* for web.

---

## PHASE 2 — Recorder feasibility spike (throwaway)  ·  1 week  ·  proves the cost

**Why:** the deciders assumed "we already own the recorder." We DON'T — `server.js`
`startCapture()` is `goto+login+storageState`, zero CDP event instrumentation. **Prove the
real cost with evidence before betting.**

| # | Task | Generic-by-design |
|---|---|---|
| 2.1 | On a THROWAWAY branch of the runner, add a **CDP session**: subscribe to `Page.frameNavigated` + hook `history.pushState/replaceState/popstate` + capture `locator.inputValue()` on input events; emit a timestamped **sidecar event log** | CDP is **protocol-generic** — works on ANY web app (1000+) with no per-app config |
| 2.2 | Run it on saucedemo (and 2-3 other public flows) and **measure against the gold set**: does it catch the missing `cart.html` + SPA sub-pages? Does it give exact URLs + form values? | Generic measurement |
| 2.3 | **Honestly count the code** — recorder + event schema + join-to-frames + redaction + UI mode. Kill the "mostly wiring" assumption with a real estimate | — |

**Exit gate → Phase 3 (PROCEED) only if:** the spike catches the structurally-missing page +
SPA sub-pages + exact URLs/values. **KILL/downgrade** Road B if it still drops SPA sub-pages
after the history hooks, OR if Phase 0 already moved the audit to ≥7-8/10 and the marginal
ROI doesn't justify a greenfield recorder.

---

## PHASE 3 — PII redaction-at-source (BLOCKING GATE)  ·  before Road B touches any customer

**Why:** capturing real DOM + real form **values** is a strictly larger PII surface than
lossy OCR guesses. For regulated on-prem, raw SSN/policy values flowing to a sidecar — or
to any external LLM — breaks the residency attestation. This is a **hard deal-gate, not a
follow-up.**

| # | Task | Generic-by-design |
|---|---|---|
| 3.1 | **Redact at source, in-perimeter**: the recorder redacts field values BEFORE persist (reuse `redaction.py` patterns: SSN/DOB/email/phone/policy/card), `password`/masked inputs forced empty | Generic PII detectors (pattern + field-type), not per-app rules |
| 3.2 | **No external-LLM egress of raw values** — the ground-truth channel never sends DOM/values to a cloud model; capture+merge run fully in the tenant boundary | Generic invariant |
| 3.3 | **Per-tenant redaction policy** (config), defaulting to strictest | Generic, tenant-scoped, no app coupling |

**Exit gate → Phase 5:** redaction provably airtight in-perimeter on the gold set. If it
can't be met, **Road B does not ship to that tenant** — the (improved) video path serves them.

---

## PHASE 4 — Network-reachability + workflow-fit check  ·  parallel with Phase 3

**Why:** guided capture requires the customer's app to be **reachable from the on-prem
runner**, and can't cover a flow that legitimately spans browser + thick-client + 3270 in
one session. Validate before generalizing.

| # | Task | Generic-by-design |
|---|---|---|
| 4.1 | With 2-3 ICP design partners, confirm target apps are reachable from the runner and guided capture fits their workflow | — |
| 4.2 | Classify the real flow corpus: % web-reachable vs % multi-modality vs % video-only | Generic corpus metric |

**Exit gate → Phase 5:** a meaningful share of flows are instrumentable. If most are
unreachable/multi-modality, **downgrade Road B to a niche accelerator** and double down on
Road A + honest gating.

---

## PHASE 5 — Build Road B: GROUND_TRUTH Tier-0 overlay  ·  the root fix for web

**Why:** real URL + DOM + form values + nav events kill all four failure families *by
construction* for web. Built additive so the frozen video pipeline is byte-identical when
no sidecar is present.

### 5a. The recorder (generic across all web apps)
- **Guided web capture** reusing the existing controlled on-prem browser (`server.js` +
  the `/auth-capture` posture) — **nothing installed on the customer endpoint** (no Chrome
  Web Store extension, no MITM proxy, no root-CA: those are change-management poison and are
  reserved as opt-in, IT-gated supplements only).
- CDP subscriptions: `Page.frameNavigated` + `history.pushState/replaceState/popstate` +
  per-action `inputValue()`/DOM snapshot. **Protocol-generic** — works on any of the 1000+
  web apps with zero per-app config; a new app needs no code.
- Emit a **timestamped sidecar event log** (real URL incl. scheme/www/.html/query, nav
  events, form `label→value`, key DOM actions), PII-redacted at source (Phase 3).

### 5b. Wire-in as an additive Tier-0 (frozen pipeline untouched)
| # | Task | File / function |
|---|---|---|
| 5.1 | Add `PageVisitSource.GROUND_TRUTH` (confidence 1.0) | `page_schemas.py`, `_confidence_for_source` (~L1181) |
| 5.2 | Load the sidecar once per artifact | `_load_artifact_signals` (~L290) |
| 5.3 | **Tier-0 branch** at the top of `_resolve_frame_location`: if a real nav event covers the frame's timestamp, populate `raw_location/url_host/url_path/url_query` from it and **return before Tier 1** | `_resolve_frame_location` (~L460) |
| 5.4 | **Inject a zero-frame visit** for a nav that fell BETWEEN sampled frames → recovers the structurally-missing `cart.html` | grouping pass |
| 5.5 | Real field values **supersede** the vision form-snapshot + DOM input events supersede the action extractor, per visit | `form_snapshot_extractor`, `page_action_extractor` |
| 5.6 | **Shadow cross-check:** keep OCR/vision tiers running; flag disagreements rather than disabling — a recorder miss surfaces honestly, never a confidently-wrong 100% | overlay layer |
| 5.7 | Guard tail-dedup so a GROUND_TRUTH visit is never overwritten by an adjacent low-confidence OCR visit | `_merge_same_page_tail` |

**Generic-by-design:** the overlay is one tier keyed on event-presence; **absent a sidecar,
behaviour is byte-identical to today** for every app. No app/host coupling anywhere.

**Acceptance:** instrumented saucedemo → exact `www.saucedemo.com/inventory.html`, `cart.html`
present, real First/Last/Zip values, the four failure families gone; harness ≥ 9-10 on
instrumented web; **video-only artifacts unchanged** (fail-open proven by re-running the
Phase-0 gold set with no sidecar → identical output).

**Exit gate:** harness proves ground truth on instrumented web AND byte-identical on
video-only; provenance is load-bearing end-to-end (no green-wash).

---

## PHASE 6 — Fund Road A forever + per-modality adapters  ·  ongoing  ·  protects the moat

**Why:** mainframe/3270, Citrix/VDI, canvas/WebGL, desktop are ~half the life-insurance
back-office and the actual "any-UI" differentiator. Once web ground truth exists, the org
will be tempted to let the video tail rot — **don't.**

| # | Task | Generic-by-design |
|---|---|---|
| 6.1 | **Explicit ongoing ownership** of the video / any-UI path with its own roadmap + harness metrics | — |
| 6.2 | **Per-modality instrumented adapters feeding the SAME schema** (each "best available", not 100%, an endpoint-agent IT lift): Windows UIA / macOS AX (desktop), HLLAPI/EHLLAPI (3270/5250 mainframe), Appium/XCUITest (mobile) — each emits the same sidecar event shape → reuses the Tier-0 overlay unchanged | **Generic within each modality**; one schema, one overlay; new modality = new adapter, no extractor change |
| 6.3 | Video remains the **unconditional floor** for Citrix/VDI, canvas/WebGL, third-party demo recordings — explicitly best-effort, proof-gated | Generic |

---

## Sequencing & dependency graph

```
Phase 0 (Road A + harness) ──► Phase 1 (provenance contract) ──► [audit fixed, product honest]
                                      │
                                      ▼
                         Phase 2 (recorder spike) ──► PROCEED? ──► Phase 3 (PII gate) ┐
                                                                   Phase 4 (reachability)┤
                                                                                        ▼
                                                                          Phase 5 (Road B overlay)
                                                                                        │
                                                                                        ▼
                                                                          Phase 6 (fund Road A + adapters)  [forever]
```

## Never-green-wash invariants (hold across ALL phases, ALL 1000+ apps)
1. No row presents a guess as fact — every row carries `source` + `extraction_confidence`.
2. A GROUND_TRUTH overlay never overwrites itself with a lower-confidence OCR row.
3. The recorder is shadow-cross-checked by OCR; disagreements surface, never silently resolve to green.
4. Absent a sidecar, the frozen video pipeline is byte-identical — provable by the gold-set harness.
5. Zero per-customer/per-app code: everything keys off protocol/signal/tier. A new app or customer works with no change.
6. "100%" is never marketed; the **PROVEN/INFERRED stamp** is the claim.
