# The Playwright Generation Engine — Enterprise 10/10 Architecture

**Status:** Architectural blueprint (v1, 2026-07-04) · **Owner:** Nexus QA
**Prime rule:** the engine is a **COMPILER with a runtime feedback loop**, not a chatbot. Same input ⇒ same script, every emission evidence-bound, LLM usage measured in *cents per thousand scripts*, honesty gates that refuse to fabricate. That combination is the moat: every competitor on the comparison list is a text predictor with no evidence, no runtime, and no memory.

**Legend used throughout:** `[LIVE]` exists and is deployed in this product today · `[BUILT-NOT-DEPLOYED]` exists in the repo, needs wiring/deploy · `[EXTEND]` exists, needs the specified upgrade · `[BUILD]` new.

---

## 0. System Architecture (Deliverable 1 & 2)

```
                     ┌────────────────────────────────────────────────┐
                     │  EVIDENCE SUBSTRATE (video-only ingestion)     │
                     │  page_visits / page_actions / form_snapshots   │
                     │  4-tier provenance (ground_truth·bar-OCR·      │
                     │  vision·inferred) + evidence grades   [LIVE]   │
                     └───────────────┬────────────────────────────────┘
                                     ▼
  TEST CASE IR  ──  ProductionTestCase: steps with provenance, data_ref,
  (frontend)        selector hints, evidence tags, suite metadata [LIVE]
                                     ▼
  ┌──────────────────── COMPILATION PIPELINE (middle-end) ────────────────────┐
  │ P0 Case Linter        — schema, provenance sanity, PII scan     [EXTEND]  │
  │ P1 Locator Planner    — a11y-first synthesis + confidence       [EXTEND]  │
  │ P2 Wait Planner       — condition synthesis, zero-sleep         [EXTEND]  │
  │ P3 Assertion Planner  — business assertions by provenance       [EXTEND]  │
  │ P4 Data Planner       — demonstrated/variant/synthetic tiers    [EXTEND]  │
  │ P5 Navigation Planner — SPA/MPA/popup/frame strategy            [EXTEND]  │
  │ P6 Anti-Flake Linter  — static flake rules, strict-mode proof   [BUILD]   │
  │ P7 Healing Metadata   — anchor bundles per step for runtime     [EXTEND]  │
  └──────────────────────────────┬────────────────────────────────────────────┘
                                 ▼
  CODE EMITTER (backend) — deterministic templates: spec + synthesized POM +
  fixtures + data files + manifest + README                        [EXTEND]
                                 ▼
  STATIC AUDITOR GATE — "HONEST 10" rubric, score < threshold blocks
  delivery (7/7 checks built)                        [BUILT-NOT-DEPLOYED]
                                 ▼
  LIVE PREFLIGHT — resolves every locator on the real app pre-delivery,
  9-way classification                               [BUILT-NOT-DEPLOYED]
                                 ▼
  RUNTIME — runner + 15-rung self-heal + proven-control ledger + oracle +
  heal calibration + false-heal benchmark + defect reports          [LIVE]
                                 ▼
  FEEDBACK — ledger writes compile-time-usable fixes; benchmark + scorecard
  gate every release                                                [LIVE]
```

The pipeline is a **compiler**: frontend (case IR), middle-end (semantic passes P0–P7, each pure and unit-testable), backend (emission), then two **delivery gates** (static auditor, live preflight) and a **runtime** that feeds knowledge back into compilation via the ledger. No pass calls an LLM; LLM exists only as a narrow escalation tier (§18).

---

## 1. Playwright API Intelligence

**Why it matters:** API choice is where flakiness is born (raw `click()` vs web-first assertions) and where future-proofing dies (deprecated selectors).
**Why AI tools fail:** Copilot/ChatGPT emit whatever pattern dominated training data — mixed `waitForTimeout`, `$eval`, deprecated `page.type`, no strict-mode awareness, different APIs per regeneration.
**Architecture rule — the API Selection Table (deterministic, versioned):** every step verb maps to exactly one blessed API per situation class:

| Intent | Blessed API | Forbidden (linter enforced) |
|---|---|---|
| click | `locator.click()` (auto-wait) | `page.click(sel)`, `dispatchEvent`, `elementHandle.click` |
| fill | `locator.fill()`; `pressSequentially` only when demonstrated masked/IME | `page.type`, `keyboard.type` for fields |
| select | `selectOption({label})`; UACR recipe for custom widgets `[LIVE]` | value-index selection |
| assert value | `expect(locator).toHaveValue(/tolerant/)` `[LIVE]` | `inputValue()` + manual compare |
| assert nav | `expect(page).toHaveURL(path-regex)` only when provenance ≥ demonstrated `[LIVE]` | hard URL equality on inferred navs |
| wait | condition-based (§6) | `waitForTimeout` (zero-sleep policy `[LIVE]` in heal loop; extend to emitted code) |
| frames | `frameLocator()` chain `[LIVE]` | `page.frames()` index access |
| shadow | pierce by default; `force_open_shadow` preamble for closed roots `[LIVE]` | JS-path drilling |
| network | `page.waitForResponse(predicate)` from network oracle patterns `[LIVE]` | sleep-after-click |

The table is data (`api_policy.json`), versioned with the Playwright version; upgrading Playwright = updating one table + rerunning the corpus benchmark. **Metric:** 0 forbidden-API occurrences in emitted code (linter-verified, CI-gated).

---

## 2. Locator Intelligence

**Why it matters:** locators are ~70% of maintenance cost and the #1 flake source.
**Why AI tools fail:** they guess selectors from a text description with *no DOM access at generation time* and no uniqueness proof; brittle `nth-child` and text-matches are endemic.
**What we uniquely have:** the recording tells us what the control *looked like and said*; the Live Preflight resolves candidates against the *real* app; the ledger remembers what *provably worked*; Similo-style multi-signal ranking + 15 heal rungs recover drift. `[LIVE/BUILT]`

**Synthesis chain (compile-time, per control):**
1. `getByRole(role, {name})` from demonstrated label + control kind (a11y-first)
2. `getByLabel` / `getByPlaceholder` (form semantics from form_snapshots)
3. `getByText` scoped to a stable ancestor (block-scope rule `[LIVE]`)
4. `data-testid` when present in preflight DOM
5. Scoped structural CSS (parent-child, table row/column math for grids)
6. Token-scoped/unique-match fallbacks (consent/checkbox recipes `[LIVE]`)

**Confidence score (0–1, emitted as step metadata):**
`conf = w₁·signal_count + w₂·uniqueness_proof + w₃·stability_class + w₄·ledger_history − penalty(ambiguity)`
- *signal_count*: independent signals agreeing (label, role, vicinity text, snapshot key)
- *uniqueness_proof*: preflight resolved exactly 1 node (else ambiguity_scoped rung plans scoping `[LIVE]`)
- *stability_class*: static heuristics (generated-id detection, index-dependence, text-volatility)
- *ledger_history*: proven-control ledger hits, calibration-weighted `[LIVE]`

**Hard-UI coverage:** shadow (open pierce/closed shim `[LIVE]`), frames (`frameLocator` + frame healing `[LIVE]`), canvas/WebGL (propose-from-candidates + orthogonal oracle + human gate — REFUSE without oracle `[LIVE research policy]`), virtualized grids (`[BUILD]`: scroll-into-view protocol + row-key locators from demonstrated cell text), SVG (role/aria-first, else title/desc anchors `[BUILD]`).
**Fallback chain:** every step emits its ranked alternates into **healing metadata** (§10) — runtime healing starts from compile-time knowledge instead of rediscovering.
**Metrics:** preflight unique-resolution rate; locator survival rate across app versions (ledger measures this for free); heal-invocation rate per 100 steps.

---

## 3. Assertion Intelligence

**Why it matters:** a test that clicks but never *proves behavior* is theater. **Why AI tools fail:** they assert what's plausible, not what was observed — the classic impossible `toHaveURL` (our own 2/10 audit finding, fixed `[LIVE]`).
**The provenance rule (non-negotiable):** assertion strength is *bounded by evidence tier*:

| Evidence | Permitted assertion |
|---|---|
| demonstrated (video/action stream) | hard: `toHaveValue(/tolerant/)`, URL-path, visible-state `[LIVE]` |
| corroborated (snapshot cross-verify `[LIVE]`) | hard value assertions |
| inferred | soft: presence/non-empty + honest UNPROVEN comment `[LIVE]` |
| unproven (never demonstrated) | **no assertion emitted** — recorded as a gap in the manifest |

**Assertion classes:** functional (field/values `[LIVE]`), URL (`[LIVE]` provenance-gated), network/API (network oracle patterns → `waitForResponse` + status/shape checks `[LIVE→EXTEND]` to emitted code), storage/cookie (auth-token presence pattern e.g. `nx_auth` `[LIVE in proving ground]`→ generalize `[EXTEND]`), **cross-page data-carry** (`[BUILD]`, high WOW: value demonstrated on page k asserted re-appearing on page n — coverage `$150,000` re-checked on the estimate page; pure compile-time dataflow analysis over the case IR), visual (screenshot proof on green `[LIVE]`; opt-in region snapshots `[EXTEND]`), accessibility (§15 `[BUILD]`), business rules (derived only from demonstrated invariants; otherwise UNPROVEN-gap).
**Confidence:** each assertion carries `{evidence_tier, corroborations, oracle_class}` — the run report aggregates into the case's evidence grade `[LIVE]`.

---

## 4. Navigation Intelligence

**Scenarios:** MPA (URL milestones `[LIVE]`), SPA (no-URL-change sub-pages — vision-split visits `[LIVE]`; assert on landmark/heading instead of URL `[EXTEND]`), redirects/entry normalization (`entry_url_normalized` rung `[LIVE]`), auth (auth-profile preconditions + relogin_reinject rung `[LIVE]`), popups/tabs (`context.waitForEvent('page')` pattern `[BUILD]`, demonstrated-only), dialogs (`page.on('dialog')` accept/dismiss from demonstrated behavior `[BUILD]`), wizard advance + revert (`[LIVE]`), back/refresh (only when demonstrated; nav_recover rung heals unexpected landings `[LIVE]`), session timeout (relogin rung `[LIVE]`).
**Verification algorithm `[LIVE]`:** each boundary is *demonstrated* (previous group grounded-navigated ⇒ hard URL-path assert) or *inferred* (honest UNPROVEN comment; proven-nav oracle + nav-recovery own it at runtime). This is exactly the never-green-wash rule applied to navigation.

---

## 5. Grounded Replay (the foundation)

**Guarantee:** every emitted action traces to evidence: `page_actions` (vision+OCR corroborated), `form_snapshots` (independent read), `ground_truth_events` (conf 1.0 when present), network oracle, journey graph. Each emitted step carries `evidence_ref` (artifact_id, visit, action ids) in an annotation — **click-through from script line to the video moment** `[EXTEND: emit refs; data already linked]`.
**Anti-hallucination:** the generator refuses cases below 2 milestones `[LIVE]`; fabrication-kill + consensus demotion in extraction `[LIVE]`; validate_fixes drops ungrounded heal proposals `[LIVE]`; the auditor gate re-checks emitted scripts against the case IR `[BUILT-NOT-DEPLOYED]`. **No competitor can make this guarantee — they have no evidence substrate to trace to.**

---

## 6. Wait Intelligence

**Policy:** zero `waitForTimeout` in emitted code (already true in the heal loop; enforce via P6 linter).
**Condition synthesis sources:** Playwright auto-wait (default); URL/network conditions from the network oracle `[LIVE]`; SPA gate (hydration/route settle `[LIVE]` as heal layer → promote to emitted preamble `[EXTEND]`); **video-taught loading indicators** (`[BUILD]`, unique to us): spinners/skeletons *seen in the recording frames* become grounded wait targets — `await expect(spinner).toBeHidden()` — no competitor can learn an app's loading grammar from evidence; animation settle via two-frame stability rule (rAF/bounding-box check helper); mutation-quiet fallback for stubborn SPAs (bounded, never unbounded networkidle).
**Ordering:** deterministic per-step wait plan: `[precondition waits] → action (auto-wait) → [postcondition waits] → assertions`.

---

## 7. Test Data Intelligence

**Tiers (governance-integrated):** T0 demonstrated values (`[LIVE]`, incl. data_ref files); T1 demonstrated alternates → variants (`[LIVE]` suite-gen); T2 synthetic (boundary/invalid/locale/currency/date/timezone) — generated from field kind + observed format masks, **always tagged `unproven-data` and gated behind the approval workflow** `[EXTEND: synthesis library; governance hooks exist]`; T3 dependency-aware (unique-per-run emails/usernames with run-scoped suffixes; duplicate-detection cases) `[BUILD]`.
**Rules:** PII never synthesized from real captured values (masking §17); currency/locale formats derived from *observed* formatting (the video showed `$150,000` — the mask is evidence).

---

## 8. Anti-Flakiness Architecture

**Compile-time (P6 linter `[BUILD]`, rules from measured failures):** forbidden APIs (§1), unscoped text locators, `nth(` without table-math justification, missing wait plan on SPA boundaries, strict-mode ambiguity (must carry preflight uniqueness proof or scoping), animation-sensitive actions without settle rule, overlay-prone patterns (modal recipes exist `[LIVE]`).
**Pre-run:** Live Preflight resolves every locator, 9-way classification `[BUILT-NOT-DEPLOYED]` — flake candidates surface *before* the first run.
**Runtime:** auto-wait + retries with **evidence** (retry proves same failure twice before verdict `[LIVE]` 2x gate), overlay/precondition rungs `[LIVE]`, History & flake tracking `[LIVE]`.
**Feedback:** every heal event is a flake-cause datapoint; calibration ranks rung reliability `[LIVE]`; benchmark gate blocks releases that raise flake rate (§16 metrics).

---

## 9. Runtime Validation

Before-action gates (emitted helper + engine): page-state (URL/landmark match to plan), locator uniqueness (strict mode ON always), visibility/enabled/receives-events (Playwright actionability), overlay detection rung `[LIVE]`, stale-element re-resolution (locators are lazy — enforced by never caching handles; linter forbids ElementHandle), correct frame/window (frameLocator chain + page-count guard `[EXTEND]`), focus for keyboard flows `[BUILD with a11y §15]`.
Failures at these gates route to diagnosis (§11) with the gate name as the taxonomy head — never a blind retry.

---

## 10. Self-Healing (deterministic, explainable, honest)

Already the product's crown jewel — the engine's job is to make healing *informed*:
- **15 rungs** (reanchor multi-signal, block-scope, entry-url, wizard advance/revert, UACR recipes, opener-then-act, token-scoped, consent unique-match, nav_recover, force_open_shadow, ambiguity_scoped, relogin, agentic verbatim-grounded analyst, phantom_skip, defect-reproduces) `[LIVE]`
- **Proven-control ledger** (memoize/seed/invalidate/quarantine, fix_kinds, nav fingerprints) `[LIVE]`
- **Universal oracle** (<1% false-heal target; contradiction-defined benchmark `[LIVE]`), **calibration** (per-rung reliability + thresholds `[LIVE]`), **federated priors** (k-anon, dormant pending tenants `[LIVE]`)
- **Never silently wrong:** oracle + 2x prove-green gate `[LIVE]`; REFUSE policy without orthogonal oracle on non-DOM UIs `[LIVE policy]`
**Engine addition `[EXTEND]`:** P7 emits an **anchor bundle** per step (all signals + ranked alternates + ledger keys) into the manifest so runtime healing starts warm; healing decisions already log explanation + confidence — surface them in the script's run annotations. Healing classes: locator/frame/navigation/popup/sync/data (data healing = only swap to *demonstrated* alternates; synthetic swaps require approval — honesty rule).

---

## 11. Failure Diagnostics

**Bundle schema (one JSON per failure) `[EXTEND — most parts LIVE]`:** verdict taxonomy (product-defect vs script vs environment triage — Agentic-QE reasoner `[LIVE plan/partial]`), expected vs actual (from assertion metadata), locator ranking table at failure time, screenshot + trace + DOM + a11y snapshot, console + network tail, heal attempts with per-rung reasoning `[LIVE]`, recommended fix (rung proposal or defect report with repro `[LIVE]`), confidence. **stop_diag honesty rule `[LIVE]`:** the UI must never editorialize a diagnosis.

---

## 12. Code Architecture (what a senior architect would write)

**Emission targets `[EXTEND]`:**
- **Synthesized POM-lite:** one page object per page_visit group — *the video already discovered the pages*; objects expose demonstrated controls with blessed locators. POM-from-video is a headline differentiator.
- **Spec files:** thin, readable scenario scripts consuming the POM; `test.step` per case step with evidence_ref annotation `[LIVE steps; EXTEND refs]`.
- **Fixtures:** auth profile (storage-state `[LIVE]`), data loader (T0–T2 files), engine helpers (waits, consent, shadow shim `[LIVE templates]`).
- **Manifest:** machine-readable map case→steps→locator confidences→anchor bundles→evidence refs `[EXTEND from existing manifest endpoint]`.
- Deterministic naming from case names; idempotent regeneration (same IR hash ⇒ byte-identical output — enables diff-based review and version pinning) `[EXTEND: hash-stamp exists in spirit via uuid5 test_ids]`.

---

## 13. Enterprise Governance

RBAC + audit log on generation/heal/approve actions `[LIVE]`; Part-11-style hash-chained heal_events `[LIVE]`; approval workflow for: unproven-data cases, healing beyond ledger-proven fixes on protected suites, regenerate-overwrite (destructive-regenerate governance `[LIVE gaps list]`); PII masking (§17); versioning: script artifacts stamped with IR hash + engine version + policy table version; traceability: requirement→case→script→run→evidence chain (system-of-record thesis `[LIVE strategy]`); risk & coverage scoring: evidence grade per case `[LIVE]` + journey-graph coverage % (pages/edges exercised vs discovered `[LIVE graph]`).

---

## 14. Performance

Compile: pure passes, no LLM ⇒ ~ms per case, embarrassingly parallel. Run: storage-state reuse (skip login `[LIVE]`), per-case isolation for parallel workers, trace/video on-retry-only, project matrix for cross-browser, shard by case with journey-graph-aware ordering (fail-fast on trunk before variants), CI: preflight as a cheap smoke lane before full runs. Target: p50 case runtime dominated by app, not framework; generation throughput >100 scripts/min/node.

---

## 15. Accessibility `[BUILD — high differentiation, low cost]`

Because locators are a11y-first (§2), every generated suite doubles as an accessibility probe: emit optional a11y lane per page object — landmark/role assertions from the synthesized POM, keyboard-path variant of the trunk (tab-order walk of demonstrated fields; focus assertions §9), axe-core scan step per page (tagged `advisory`, never green-wash: violations reported, not failed, unless policy says fail). Roles/names come from the same evidence substrate; screen-reader-name assertions for demonstrated controls.

---

## 16. Observability

Per-run telemetry (JSON + UI): step timeline with waits/retries/heals, per-step confidence & evidence tier, healing attempts + rung + explanation `[LIVE]`, oracle verdicts V/D/O/N/R `[LIVE design]`, trust SLO endpoints `[LIVE]`, execution graph (journey overlay: planned vs actual path `[EXTEND on journey graph]`). Release-level: accuracy benchmark + scorecards + false-heal benchmark + flake trend — all gates, not dashboards `[LIVE]`.

---

## 17. Security

Secrets: never in scripts — env/keystore references only; auth via captured storage-state encrypted at rest (KMS envelope `[LIVE]`), RUNNER_TOKEN service auth `[LIVE]`; password fields: value never extracted (`value=""` rule `[LIVE]`) and never asserted; PII masking on export + logs `[EXTEND per governance gaps]`; token lifecycle: relogin rung + TTL awareness `[LIVE]`; secure logging: evidence refs instead of raw values in annotations.

---

## 18. AI Architecture — and the LLM-cost innovation

**Placement table (the answer to "where AI, where deterministic"):**

| Stage | Mode | Rationale / cost |
|---|---|---|
| Case IR → compile passes P0–P7 | **Deterministic only** | correctness must be reproducible; 0 tokens |
| Code emission | **Deterministic only** (templates) | byte-stable output; 0 tokens |
| Locator synthesis | Deterministic; **LLM tier-2** only when signals conflict (agentic analyst, verbatim-grounded, schema-validated, ledger-cached) `[LIVE]` | tokens only on the ambiguous tail (~2-5% of controls, once — then ledger-memoized) |
| Wait/assertion planning | Deterministic (evidence-derived) | 0 tokens |
| Healing | Deterministic rungs first; gated LLM analyst as rung ~13 `[LIVE]` | tokens only after cheap rungs fail; oracle-verified |
| Diagnosis narrative | Template-first; optional LLM summarizer over structured bundle | cache by failure signature |
| Extraction (upstream) | Hybrid (vision where OCR fails) `[LIVE + skip-proven economics]` | already cost-tiered |

**Cost story:** competitors spend LLM tokens on *every line of every script, every regeneration*. We spend ~0 at steady state: evidence + compiler + ledger convert one-time ambiguity spend into permanent deterministic knowledge. At 10,000 apps this is the difference between a COGS problem and a rounding error — and it's simultaneously the determinism story auditors require.

---

## 19. Competitive Analysis

| Capability | Copilot/ChatGPT/Claude/Gemini/Cursor/Windsurf/Qodo/Cody | This engine |
|---|---|---|
| Evidence of real app behavior | none (text prompt) | video-grounded substrate, 4-tier provenance |
| Uniqueness proof before delivery | none | Live Preflight resolution `[BUILT]` |
| Same input ⇒ same script | no (sampling) | yes (compiler, hashable) |
| Honesty about the unproven | asserts plausibly | provenance-bounded assertions, UNPROVEN gaps, auditor gate |
| Self-healing with false-heal control | none / rename-level | 15 rungs + oracle + calibration + <1% false-heal benchmark |
| Cross-run memory | none | proven-control ledger + federated priors |
| Release accuracy measurement | none | benchmark + scorecard gates |
| LLM cost per script | full generation, every time | ≈0 steady-state |
| Traceability to evidence | none | step→video-moment refs; hash-chained audit |
Commercial test-gen platforms (Mabl/Testim/Functionize/testRigor class) have runtimes but **require their recorder/DOM instrumentation and cannot ingest plain video**, don't publish false-heal controls, and none grade their own extraction honestly. **Un-replicable core:** the evidence substrate + honesty gates + ledger flywheel — copying it requires abandoning the "just ask an LLM" architecture entirely.

## 20. Future Innovations

Journey-graph **model checking** (flows as FSM: reachability/dead-page proofs before generation); **program-synthesis repair** (counter-example-guided step repair from failure bundles); **differential replay** (frame-by-frame video vs run-trace comparison — behavioral equivalence scoring); **simulation flake oracle** (network/CPU-throttled preflight to predict flake before CI); **federated selector genome** (k-anon cross-tenant priors `[LIVE substrate]`); **a11y-first generation mode** (suites that certify WCAG paths); **formal data-flow contracts** (cross-page carry assertions as verified invariants); **compiler-style optimization levels** (-O0 verbatim replay → -O2 parallel-safe, data-parameterized suites).

---

## Deliverables 15–17: Risks · Roadmap · Metrics

**Risks/mitigations:** preflight needs app access at compile time (mitigate: degrade to static confidence + first-run learn); POM emission churn on re-derivation (mitigate: IR-hash idempotence + stable uuid5 naming); LLM tier-2 drift (mitigate: schema validation + ledger cache + verbatim-grounding `[LIVE pattern]`); governance friction (mitigate: approvals only on unproven tiers).

**Prioritized roadmap (leverage-ordered — deploy what's built first):**
1. **Deploy the two gates**: auditor "HONEST 10" gate + Live Preflight v2 into the delivery path `[BUILT-NOT-DEPLOYED → LIVE]`.
2. P6 anti-flake linter + API policy table (pure static, fast win).
3. Locator confidence emission + P7 anchor bundles in manifest (healing starts warm).
4. Synthesized POM-lite + evidence_ref annotations (the reviewer-visible WOW).
5. Wait planner promotion (SPA gate + video-taught spinners into emitted code).
6. Cross-page data-carry assertions + storage/network assertion emission.
7. Data tiers T2/T3 + approval workflow wiring.
8. A11y lane; observability graph overlay; diagnostics bundle schema v1.

**Success metrics (all measured, never estimated — benchmark-gated):** first-run green rate on the labeled corpus; auditor score distribution (target: 100% ≥ 9/10); preflight unique-resolution %; flake rate per 100 runs; heal precision/recall + false-heal % (<1%); locator survival across app versions; LLM tokens per script (target ≈0 steady-state); mean time-to-diagnosis; % steps with evidence refs (target 100%).
