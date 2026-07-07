# QE-Central — Centralized Agentic Regression QE Platform

> ⚠️ **CORRECTED BY `STARTING_POINT.md` (2026-07-07).** A 12-agent adversarial design pass verified against source that the "emit ground-truth events → page_visits materialize at conf 1.0 → everything reused unchanged" seam **does not hold for a frameless crawl** (`page_visit_extractor.py:364/1884` early-returns `visits_written=0` with no video frames; the overlay at :1974 is never reached). The true ingestion seam is the **substrate tables** (`page_visits`/`page_actions`), written not event-fed; the first build is a **substrate-writer + REFUSE harness**, not an events-crawler. **Read `STARTING_POINT.md` first** — it supersedes §0/§6 below. The product framing (§1–§5, §7–§8) still stands.

**Status:** Founding blueprint v1 (2026-07-07) · **Relationship to VKPower:** shared chassis, new vehicle — zero changes to VKPower code; integration via its existing APIs only.

**The product in one sentence:** the customer gives a URL, test data, and a GitLab repo; the platform *reads the code as evidence*, *explores the live app as a recorder*, synthesizes business-critical regression scenarios with provable provenance, and runs them continuously with autonomous healing, environment/defect triage, and honest reporting — human involvement measured and minimized, never hidden.

---

## 0. The founding insight that cuts the build by ~60%

**The crawler is a recorder.** VKPower's entire factory — Pages & Forms, suite generation, the deterministic Playwright compiler, the HONEST-10 certification gate, autopilot with the 15-rung healer, verdicts/dossiers/history — consumes an *evidence substrate* (page visits, actions, form snapshots, ground-truth events). Today that substrate comes from video. **QE-Central's explorer emits the same substrate at the HIGHEST provenance tier** (the `ground_truth_events` ingest API already exists and is proven: events → visits at confidence 1.0). Downstream of the substrate, *everything already works and is already certified*.

So QE-Central is not a second engine. It is a **second evidence source** feeding the same trust machinery:

```
VKPower ingestion:   video ──► evidence substrate ─┐
                                                    ├─► SAME factory: cases → certified
QE-Central ingestion: repo analysis + live crawl ──┘    Playwright → autopilot heal/triage
                                                        → verdicts/dossiers → regression history
```

This is also the moat statement: competitors bolt an LLM onto a crawler. You have a *certification pipeline with measured error rates* that any evidence source can feed.

## 1. Corrections to the product framing (requested: "correct me if I am wrong")

1. **"1000% success rate" → published, measured rates.** No system can promise perfection, and regulated buyers distrust anyone who does. The credibility asset — proven with VKPower — is *published measured numbers*: false-heal <1%, discovery recall vs answer keys, certification precision. Sell "we publish our error rates; nobody else does," not a number that collapses in due diligence.
2. **"Identify ALL business-critical scenarios" → measurable coverage against an enumerable model.** Completeness over an arbitrary app is undecidable. What IS measurable: the repo yields an enumerable model (routes, API endpoints, business rules, validators); the crawl yields the reachable journey graph. Report **coverage %** against that model, ranked by criticality, with every uncovered item listed. "All" is a hope; "94% of enumerated routes, 100% of payment paths, 3 named gaps" is a contract.
3. **"99% autonomous / 1% human" → a defined, measured autonomy budget.** The honest 1% is: scenario approval (governance — a human blesses what the machine proposes before it becomes the regression contract), credentials/test-data provisioning, and defect confirmation on低-confidence triage. Metric: **human touches per app onboarded** and **per regression cycle** — measured, trending down.

## 2. Direction decision: separate project or same workspace?

**Recommendation: same monorepo, new bounded-context service — integrate with VKPower over its APIs, never its internals.**

- New folders only: `QECentral/` (this folder) for product docs/plans; implementation as `Nexus_power/platform/qe-central/` (new FastAPI service, own container) + `Nexus_power/engines/repo-intel/` (GitLab analysis engine, own container). **Zero edits to existing VKPower services** — the no-break guarantee is structural: QE-Central calls `POST /ground-truth/events`, `POST /generate`, `GET /playwright`, `POST /auto-heal/run-config`, `POST /verify`, `POST /triage` — all existing, all versioned, all already gate-protected.
- Why monorepo: shared SDK/tenancy/RLS, one deploy story, the ledger/calibration flywheel stays unified (heals learned under QE-Central benefit VKPower and vice versa), and extraction to a separate repo later is cheap *because* the boundary is API-shaped from day one.
- Why not a separate repo now: two repos = duplicated SDK, drift, split flywheel, double release engineering — pure cost at this team size, no benefit until org structure demands it.

## 3. System architecture

```
 INPUT: app URL + credentials/test data + GitLab repo (token)
   │
   ▼
 ┌─ REPO INTELLIGENCE (engines/repo-intel — NEW) ────────────────────────────┐
 │ clone → detect stack → extract: routes, API surface (OpenAPI/controllers),│
 │ business rules (validators/schemas/constraints), auth flow, feature flags,│
 │ existing tests (mined as intent hints), data models                       │
 │ OUTPUT: App Model — every fact tagged file:line (code IS the recording)   │
 └───────────────┬────────────────────────────────────────────────────────---┘
                 ▼
 ┌─ GUIDED EXPLORER (qe-central — NEW; Playwright via existing runner) ──────┐
 │ App-Model-seeded crawl: login with provided creds, visit routes, inventory│
 │ controls/forms (a11y-first), exercise safe interactions, budgeted+polite; │
 │ EMITS ground-truth events + visits/actions/snapshots (conf 1.0 tier)      │
 │ + LIVE↔REPO DRIFT REPORT (route in code but unreachable, etc.)            │
 └───────────────┬────────────────────────────────────────────────────────---┘
                 ▼
 ┌─ SCENARIO SYNTHESIS (NEW agent, deterministic-first) ─────────────────────┐
 │ journeys ranked by criticality signals: repo (payment/auth/txn handlers,  │
 │ validation density), app (forms, money/PII fields), test-mining hints;    │
 │ LLM lens allowed ONLY with file:line/DOM quotes (Intent-agent contract)   │
 │ OUTPUT: proposed scenarios + criticality + evidence → HUMAN APPROVAL gate │
 └───────────────┬────────────────────────────────────────────────────────---┘
                 ▼
 ┌─ DATA INTELLIGENCE v2 ────────────────────────────────────────────────────┐
 │ map user data → discovered forms; synthesize boundaries FROM CODE         │
 │ VALIDATORS (upgrades T2 from UNPROVEN to code-grounded); dependency-aware │
 └───────────────┬────────────────────────────────────────────────────────---┘
                 ▼
 EXISTING VKPOWER MACHINERY (unchanged): suite generation → deterministic
 compiler (+__nxClick/__nxSettle/POM/attempt-mode) → HONEST-10 gate (BLOCK) →
 autopilot (15 rungs, ledger, oracle, <1% false-heal) → env/defect triage →
 verdicts + dossiers + regression history + sentinel + escalations
                 ▼
 ┌─ REGRESSION CONTROL PLANE (NEW, thin) ────────────────────────────────────┐
 │ app registry (1000 clients × N apps), schedules, baseline diffing on      │
 │ verdict history, coverage + autonomy dashboards, cross-browser matrix     │
 └────────────────────────────────────────────────────────────────────────---┘
```

## 4. Provenance model (the honesty architecture, ported)

| Tier | Source | Example |
|---|---|---|
| `code-derived` | repo-intel, file:line tagged | "amount must be ≤ 50,000 — src/validators/transfer.ts:41" |
| `crawl-observed` | explorer DOM/AX state (conf 1.0 — instrumented) | "transfer form has fields X,Y,Z on /transfer" |
| `user-provided` | customer test data/creds | account fixtures |
| `llm-inferred` | quote-validated lens only | "this journey is checkout-critical *because* [quoted handler]" |

Rules carried over verbatim: assertions bounded by tier; the gate blocks unproven claims; every scenario/verdict carries a dossier; unknown = honest verdict; LLM never scores, never acts ungrounded.

## 5. What's reused vs built (the honest bill of materials)

**REUSED unchanged (already certified in production):** runner + healer + ledger + calibration + false-heal benchmark, compiler + gate + linter, suite generation, verify/verdicts/dossiers/waivers/risk, triage classifier, sentinel + escalations, tenancy/RLS/KMS, benchmark + red-team harness patterns.
**NEW build:** repo-intel engine (largest new piece), guided explorer (medium — Playwright crawl + event emission), scenario synthesizer + criticality ranking + approval UX (medium), data intelligence v2 (small-medium), control plane registry/scheduling/dashboards (medium), discovery benchmark answer keys (small, day one).
**Explicitly NOT needed:** the video pipeline (eyes/spine) — untouched, unaffected.

## 6. Phased implementation plan

- **Phase 0 — Foundations (week 1):** scaffold `platform/qe-central` + `engines/repo-intel` (empty services, compose entries, contracts doc); **seed the discovery benchmark first** (measure-first, the VKPower lesson): 4-6 target apps (parabank, the two proving grounds, 2-3 OSS apps like a shop/admin template) with hand-authored answer keys: expected scenarios, routes, business rules. Every later phase is graded against these keys in CI.
- **Phase 1 — MVP: "URL + creds → certified smoke suite" (weeks 2-4):** explorer v1 (route-seeded crawl, form inventory, safe interactions) → emits ground-truth events → existing factory generates, certifies, and autopilot-runs the suite. *No repo intelligence yet.* This is deliberately the fastest path to the full end-to-end demo because everything downstream exists. Exit criterion: parabank from URL to certified passing suite, zero human steps except credentials.
- **Phase 2 — Repo Intelligence v1 (weeks 3-8, parallel):** GitLab connector; stack detection; extractors for the top web stacks first (JS/TS: React/Angular/Vue routes, Express/Nest controllers, OpenAPI, zod/yup/joi validators; then Java Spring). App Model store with file:line provenance; live↔repo drift report. LLM summarization lens with verbatim-quote validation.
- **Phase 3 — Scenario Synthesis + the 1% (weeks 6-10):** criticality ranking (deterministic signals first), scenario proposals with evidence, approval workflow (reuse waiver/approval patterns), coverage scorecard vs the enumerable model. Exit: discovery recall ≥ target on answer keys; every proposal quote-grounded.
- **Phase 4 — Data Intelligence v2 (weeks 8-12):** provided-data → form mapping; validator-derived boundary/negative data (code-grounded T2); dependency-aware sequencing (T3).
- **Phase 5 — Regression Control Plane + scale (weeks 10-16):** registry, schedules, baseline diffs on verdict history, dashboards; then the known Tier-2 infra list (replica pools, CI/CD, observability) shared with VKPower.

## 7. KPIs (all measured, benchmark-gated — never self-reported)

Discovery recall & precision vs answer keys · coverage % of enumerated routes/APIs (and 100%-of-payment-paths style pledges per criticality class) · scenario approval rate (proposed→approved) · first-run green rate · false-heal <1% (inherited benchmark) · triage accuracy vs labeled outcomes · **autonomy ratio: human touches per app onboarded / per regression cycle** · time-to-first-certified-suite from URL submission.

## 8. Top risks & mitigations

Destructive actions during crawl (real data!) → safe-verb allowlist, non-prod URLs required, form submission gated by data policy, dry-run mode. Auth walls/MFA → provided-cred flows first, SSO profiles later (storage-state machinery exists). Repo heterogeneity → stack-by-stack extractors graded on the benchmark, honest "unsupported stack" degradation (STATIC-ceiling pattern). Crawl explosion on big apps → budgets + criticality-first frontier. Over-promising autonomy → the §1 corrections are the sales language.
