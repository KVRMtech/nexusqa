# QE-Central — The Starting Point (Founding Architectural Direction)

**Status:** Chief-architect synthesis v1 (2026-07-07). Product of a 12-agent adversarial design workflow (6 investigation streams → 5 adversarial lenses → verified synthesis, `wf_0d48b9e6-3f2`). The single load-bearing technical claim was re-verified by hand against source before this doc was written.

**Relationship to VKPower:** shared trust machinery, new evidence source. Amended no-break guarantee — see §2.

**Supersedes** the starting-point section of `QE_CENTRAL_BLUEPRINT.md` §0/§6. The blueprint's product framing stands; its *ingestion seam and first build* are corrected here.

---

## 0. THE ONE VERIFIED FACT THAT REORDERS EVERYTHING

The blueprint (and three of six investigation streams) assumed: *"the crawler posts ground-truth events to `POST /api/v1/artifacts/{id}/ground-truth/events`, forces a regenerate, and the navigation overlay materializes `page_visits` at confidence 1.0 — even with zero video frames."*

**Verified in source — this is false:**

- `page_visit_extractor.py:364` — `_load_artifact_signals` does `if not frames: return None`.
- `page_visit_extractor.py:1884` — the extractor early-returns `visits_written=0` when `signals is None`.
- `page_visit_extractor.py:1974` — the ground-truth overlay runs *after* the frame loop, so on a frameless artifact it is **never reached**.
- The e2e test cited as "proof the events path works" (`scripts/test_ground_truth_e2e.py`) requires `--artifact-id <an artifact that has frames>` and posts to the **redacting** `/ground-truth` endpoint — a *different* endpoint from the non-redacting `/ground-truth/events` (`storyboard.py:749`, `value=e.value` raw at :810). It proves the overlay-on-a-video path; it says nothing about a frameless crawl.

**Consequence.** The blueprint's founding structural promise — *"emit events to an existing API, everything downstream is REUSED UNCHANGED, zero writes to VKPower internals"* — is **half true, and the false half sets the real build cost.** The downstream machinery (`/generate` → compile → auto-heal → verify → triage) is genuinely reusable and frame-free. But the **ingestion seam is not the events API.** The true seam is the **substrate** (`page_visits` / `page_actions`, with `form_snapshot` as JSON on `PageVisitRow`) — and it must be **written**, not event-fed. This is the spine of everything below.

---

## 1. THE PROBLEM, CORRECTLY FRAMED

### What is right
- **VKPower is the moat, not the pipeline.** The rare asset is *trust machinery*: provenance-tiered evidence, the HONEST-10 gate, the 15-rung healer with proven-control ledger + universal oracle, a published <1% false-heal rate, hash-chained dossiers, RLS/KMS. Crawling is commoditized (Crawl4AI ~58k stars, Firecrawl); *trustworthy refusal* is not.
- **"The crawler is a recorder"** is directionally right: a guided explorer that knows the exact locator + typed value at confidence 1.0 is a *strictly higher-fidelity* producer than video-OCR.
- **Deterministic-first, LLM-only-if-quote-validated** is the right doctrine and already exists (`qe_agents.py` verbatim-quote demotion).

### What is actually hard (in order)
1. **The oracle problem under a weaker evidence stack.** Video carries three things a *safe* crawl structurally lacks: demonstrated terminal outcomes, per-step baseline screenshots, and a benchmark defined over video-derived drift. **Reuse of downstream code ≠ reuse of downstream trust** (see R1).
2. **Coverage is undecidable, and its blind spot is anti-correlated with value.** A "business-critical scenario" is a multi-state invariant over ordered states/handlers/transactions — it lives in no single file:line and no single crawl edge. The atoms we *can* enumerate (routes, endpoints, declarative validators, observed edges) are exactly *not* the P0 money/PII/write invariants the product is sold on.
3. **Destructive action on real regulated data is existential**, and safety lives entirely in a component that does not exist yet, guarding a boundary an event sink cannot guard after the fact.
4. **Business rules in the beachhead don't live in code you can clone.** US life-insurance/financial rules live in Guidewire Gosu, Pega decision tables (in a DB), Salesforce Apex/Flows, Drools/DMN, PL-SQL, config rows — and departing employees' heads (the product's *own* vision). A JS/TS validator pass sees cosmetic front-end checks and is blind to the crown jewels.
5. **Fleet economics + durability, not accuracy, gate 1000 clients.** Full nightly re-crawl of 10,000 apps ≈ $900k/mo and ~415 concurrent Chromium contexts all writing one shared Postgres that **has no backup** and holds 1000 regulated tenants' evidence.

### Framings to correct before they become promises
- **"Identify ALL business-critical scenarios"** → *"X% of an enumerated atom-universe (which publishes its own recall) + N certified human-authored critical invariants."*
- **"~99% autonomous / ~1% human"** → **per-criticality-band** autonomy, never averaged (it collapses toward ~0% on the P0 mutating band).
- **"Understand ALL business rules, file:line grounded"** → demote from "understanding" to "deterministically-enumerable facts feeding a coverage denominator," with "rules live outside code" as an explicit, quantified tier.
- **"100% success"** → replaced by *published measured rates* (discovery recall per stack, false-heal per source, correct-refuse rate, triage accuracy, cost-per-suite) + a visible behavioral-coverage tier.

---

## 2. THE CENTRAL BET — VALIDATED, THEN AMENDED

**Bet (validated):** QE-Central is a new evidence source (guided live crawl + code facts) feeding VKPower's *unchanged* certification/heal/verify machinery. Downstream reuse from `/generate` onward is real and frame-free.

**Amendment (forced by §0):** the crawler is not an *event recorder*. **The crawler is a substrate writer + screenshot-evidence capturer.** It must (a) create the artifact container, (b) write `page_visits`/`page_actions`/`form_snapshot` at confidence 1.0, and (c) capture per-step baseline screenshots as evidence assets — because the events API no-ops on a frameless artifact *and* the screenshots are independently required by the semantic oracle, the dossier, and the false-heal benchmark.

### The TRUE reuse ledger (grounded)

**GENUINELY FREE — reuse unchanged** (verified frame-independent; consumes cases + `page_visits`/`page_actions` + runtime `base_url`, never frames):
- Suite generation — `generate_demonstrated_test_cases` / `POST /generate` (`service.py:263`, `generator.py:1162`).
- Deterministic Playwright compiler — `GET /playwright` (ZERO LLM, byte-identical out).
- Autopilot 15-rung healer + ledger + oracle — `POST /auto-heal/run-config` (`test_factory.py:2645`).
- `/verify` + hash-chained dossiers + risk model (`test_factory.py:4545`).
- Triage classifier (`qe_agents.py:173`).
- Tenancy/RLS/KMS (`database.py:111`); KMS-encrypted `storageState` for auth reuse (`auth_profiles.py:61/91`).

**MUST BUILD OR EXTEND (the real cost, previously mis-counted as "free"):**
- **Create-artifact-for-crawl seam** — no HTTP creation endpoint exists; mirror `CanonicalArtifactRow(...)` (`spine-engine/main.py:4698`) with `source_type='live_crawl'` + `SessionRow`. *(small, ~50 lines)*
- **Direct `page_visits` + `page_actions` + `form_snapshot` writes at conf 1.0** — the video path derives actions/forms from a vision-LLM over frames (`form_snapshot_extractor.py:206`); no ground-truth path exists. This is the highest-value, most detail-sensitive substrate (it *becomes* the steps + the data).
- **Per-step screenshot capture + attach** — required by `semantic_oracle` (returns an uncertain sentinel without `baseline_bytes`, `:166-167`), the dossier's proof images, and the benchmark. Near-free: the crawler already renders the DOM.
- **Guided explorer** (Playwright, a11y-first, SPA-aware, safe-verb) — the existing `engines/legs-engine/.../autonomous.py` (276 lines) has *zero* safety and auto-submits login forms; salvage the browser scaffold only.
- **Egress-sandbox + fail-closed mutation guard** — platform-enforced containment. *(week-1 critical path)*
- **Extend `induced_drift_benchmark` to crawl-sourced heals** — it enumerates video-derived drift only; the crawl-source false-heal number *does not exist yet*. **This extension IS the moat.**
- **Repo-intel** — stack/platform detector + OpenAPI/route seeding first; validator extraction is a bonus tier, never a coverage denominator. *(off critical path)*
- **Fix the PII leak** — route `/ground-truth/events` through `_redact_value` (present at `:508`, used by sibling `:542`, **absent** at `:810`). *(security fix, do regardless)*

**AVOID (named traps):** re-synthesizing screenshots as fake `visual_frames` fed to the vision extractors (re-imports OCR uncertainty + vision cost the conf-1.0 tier was built to eliminate — for `live_crawl` artifacts the vision extractors stay **OFF** via the surface-toggle at `storyboard.py:329`). A generic LLM web-agent as the explorer core. A generic LLM "scenario scorer."

### How this stays "without breaking VKPower"
Amend "APIs only, never internals" to: **a named, versioned internal substrate contract + additive, gated, fail-open VKPower extensions.** Two seams:
- **Learn-fast (week-1 harness):** direct substrate writes with a distinct `source_type='live_crawl'` + `extractor_version`, additive rows only, pinned by a CI contract test. Fastest thesis-falsification; measures the real RLS/FK/version coupling.
- **Production seam (converge here):** VKPower-owned additive extensions so QE-Central stays behind HTTP — a create-artifact endpoint, a screenshot-attach endpoint, a symmetric GT→`page_actions`/`form_snapshot` overlay mirroring `_apply_ground_truth_overlay`, and lifting the no-frames early-return when GT events exist. Every extension byte-identical/fail-open on the video path (the pattern already at `page_visit_extractor.py:1808-1829`), so the frozen pipeline is preserved.

---

## 3. THE STARTING POINT — the single most important decision

**Reframe the first deliverable from "a green certified suite from a URL" to "a measured trust number on the new evidence source." The first thing we build and measure is the crawl-source false-heal / correct-refuse rate, produced through a substrate-writer harness that captures per-step screenshots — proving the honesty machinery on the crawl source BEFORE generating a single customer-facing certified scenario.**

Why this is *the* decision:
- It **overturns** the events-only crawl→regenerate→certified-suite start that three streams converged on — a build that would harden the exact wrong contract (events-only, no screenshots, downstream-trusted-as-is) and produce a green demo whose greenness is *un-earned*: a due-diligence liability the moment a customer asks "did the green suite catch the deleted payment validation?"
- It **inverts** the measurement target from the design's strongest region (navigation, non-mutating, declaratively-validated — a vanity metric) to its weakest (outcome-proof under refusal) — exactly how VKPower's <1% number was earned.

### The concrete FIRST BUILD — substrate-writer + REFUSE harness (~200 lines, no crawler, no client app)
1. Create a `CanonicalArtifactRow` (`source_type='live_crawl'`) + `SessionRow`.
2. Write a **hand-authored fixture** of `page_visits` + `page_actions` + populated `form_snapshot` (source=`ground_truth`, conf 1.0) **and per-step baseline screenshots** as evidence assets — against a target we own (Aegis :8096 / Skyward :8095; parabank for the money-path shape).
3. Call the **unchanged** chain `/generate → /playwright → /auto-heal/run-config → /verify → triage`. Confirm a certified suite comes out with zero video and zero VKPower edits.
4. **Extend `induced_drift_benchmark`** to this crawl-sourced artifact and run a MUST-REFUSE mode: delete a required server-side validation, re-run, assert the suite does **not** stay green. **Publish the crawl-source false-heal + correct-refuse rate. This number, not the passing suite, is the exit artifact.**
5. Include a **P0-invariant falsification fixture**: one mutating, branch-conditional, computed-threshold invariant (a life-insurance auto-underwriting ceiling), attempt URL→certified end-to-end. It will fail to auto-discover (undecidable-static) and fail to safely-submit (no disposable env). **Measure that failure in week 1** — the honest input that forces the atoms-vs-invariants split and the submit-gating workflow to the front before we accrete a smoke-only demo.

This pins the two real contracts (exact `PageVisitRow`/`PageActionRow` columns + the `extractor_version` "latest-wins" selection in `service.py`; whether a new writer trips RLS INSERT on `010/029`) and proves the oracle/dossier/benchmark stay *live* because the screenshots exist. Every later phase becomes "a better producer of the same rows," written against a proven target.

### Repo-first vs crawl-first — resolved
**Neither is first.** The substrate-writer harness is first (no crawler, no repo-intel). Then **crawl-first**: the live explorer second; repo-intel demoted to *frontier-seeding + coverage-denominator*, never blocking first value. Building the AST/validator suite first front-loads investment into the capability that ceilings out (~5–15% static rule recall) on the exact market we sell to.

---

## 4. THE MEASURE-FIRST FOUNDATION — discovery + trust benchmark

Measure-first applies to **both** accuracy and operability. No feature work until the benchmark exists.

- **Apps:** parabank (money-path shape) + Aegis :8096 + Skyward :8095 for accuracy/reachability; **plus ≥1 beachhead-representative fixture** (Java-EE-+-stored-proc / Salesforce-Apex / Pega-shaped, or a vendor-platform-fronted-by-SPA case) so the repo-intel ceiling is learned in week one; **plus** one stateful, auth-walled, **rate-limited** target run as **two tenants against a shared DB** so the operability wall appears at commit one.
- **Answer keys (Phase-0, human-authored):** per app, U = routes ∪ API endpoints ∪ declarative validators (file:line) ∪ crawl-observed journey edges; **and** a small set of hand-authored P0 critical *invariants* (underwriting ceiling, insufficient-funds block, bind-eligibility). The universe object publishes **its own recall** so coverage reads "X% of enumerated routes (universe recall Y% vs key)."
- **Graded numbers (published from commit one):** (1) crawl-source false-heal + correct-refuse rate (cardinal); (2) autonomy % on the P0 mutating band (gate on this, never the aggregate); (3) discovery recall/precision **per stack**; (4) behavioral-coverage tier distribution; (5) cost-per-certified-suite with a CI budget assertion.

---

## 5. PHASED PLAN — each exit is a MEASURED number

**Phase 0 — Measure-first foundation + durability (week 1–2).**
Author answer keys (incl. beachhead-representative + P0 invariants). Turn on Postgres PITR/backup **or** carve out a crawl-substrate DB. **Exit:** substrate-writer harness certifies a suite from directly-written rows on parabank/Aegis, zero VKPower edits; a published crawl-source false-heal rate exists (target: on par with the video-source <1% under the extended MUST-REFUSE modes); the P0 underwriting-ceiling fixture's autonomy % is measured (expected: low — the honest baseline).

**Phase 1 — Contained read-only explorer (week 3–5).**
Egress-sandboxed container (outbound blocked except target host). Default tier = read-only discovery: navigate + DOM read + form *structure* + per-step screenshots; a network interceptor **blocks every non-GET and every mutation-signal GET** unless allowlisted. Only on apps we own; no client creds yet. **Exit:** discovery recall vs keys per app (target ≥80% JS stacks); cost-per-suite within CI budget; zero mutating requests escaped (asserted by interceptor log); the two-tenant/shared-DB/rate-limited run produces verdicts without SLA miss under a per-tenant token bucket.

**Phase 2 — Full substrate + behavioral coverage on a disposable env (week 6–9).**
Where a customer-attested disposable/seeded env exists, the two-phase form flow SUBMITs there and captures the confirmation state as demonstrated outcome + baseline. Where none exists, the suite stays at the lower behavioral tier **by construction**. **Exit:** behavioral-coverage tier visibly labeled on every certified suite (no silent 10/10 for fill-only); ≥1 P0 invariant certified end-to-end on the disposable env with a real MUST-REFUSE proof.

**Phase 3 — Repo-intel as seeding + denominator (parallel from week 3, lands week 8–10).**
Stack/platform **detector first** ("Guidewire/Pega/Salesforce detected → static rule ceiling ~5–15%, route rule-capture to crawl + human"). OpenAPI + route seeding only; validator extraction is a bonus tier. **Exit:** directed-crawl measurably beats blind-crawl (published recall delta); repo-intel recall reported **per stack**; "rules live outside code" is a quantified tier; no global "% of business rules covered" is ever published.

**Phase 4 — Fleet operability + incremental regression (week 8–12).**
Re-crawl only routes whose repo SHA or journey-graph fingerprint changed; per-client req/s cap + concurrency ceiling. **Exit:** steady-state fleet cost scales with *change*, not app count (published cost-per-app-per-cycle); no rate-limit-induced SLA miss across concurrent tenants.

**Phase 5 — Scenario-synthesis governance + approval gate (week 10–14).**
Coverage model (atoms vs invariants, named gaps; universe-shrinkage guard: a contracted atom absent from the new universe raises a **P0 possible-deletion gap**). Criticality registry cloned from `triage_classify` (deterministic, additive, stamped). Approval gate: only NEW/CHANGED scenarios re-consume human attention (fingerprint-diff). **Exit:** criticality precision vs hand labels; human-touches-per-cycle trending down; deletion of a required behavior raises a gap, never silently passes.

---

## 6. THE HONESTY CONTRACT

The public claim becomes one true, un-copyable sentence:

> **"From a URL we produce a certified regression suite; we publish our discovery recall *per stack* and our false-heal rate *per evidence source*; we label every suite by whether it proves the app RENDERS or proves it BEHAVED; and we REFUSE rather than silently pass a broken test — and we CONTAIN rather than corrupt your data."**

- **"~99% / ~1%"** → autonomy-per-criticality-band, never averaged. Publish the hidden per-app human sinks the "1%" omits: answer-key authorship (O(apps)), `storageState` re-auth per session-TTL, flaky-route adjudication, expert re-approval — which *requires exactly the departing domain expert the vision claims to replace* (name this; it is the approval-gate governance owner).
- **"All scenarios"** → two typed layers: ENUMERABLE ATOMS (auto-discovered, coverage-gradable, universe publishes its own recall) + CRITICAL INVARIANTS (human/expert-authored intents the product **executes and certifies**, not claims to auto-discover). Claim = "X% of enumerable atoms + N certified critical invariants."
- **"100% success"** → behavioral-coverage tier + published error rates. A fill-without-submit suite is certified at an explicitly-labeled *lower* tier ("form structure verified; business outcome UNPROVEN") than a submit-on-sandbox suite. Verified: `score_spec` correctly drops outcome assertions for lack of evidence but nothing docks a suite for asserting nothing about behavior — so the **tier label is mandatory** or the HONEST-10 score lies by omission.
- **Every criticality number + rule fact carries provenance** (`G_DETERMINISTIC` / `G_LIVE_CONFIRMED` / `G_INFERRED`; rules: `code-derived` / `config-derived` / `db-constraint-derived` / `dsl-derived` / `human-asserted`). The LLM may only explain within a deterministic band, quote-validated, never set or cross a P-class boundary.

---

## 7. TOP RISKS — rank-ordered

**R1 (FATAL) — Outcome-blind green-wash / inherited-not-earned trust.** The events-only path yields zero page_visits; even after direct writes, a *safe* crawl fills-without-submitting, so the terminal transition is never demonstrated, the auditor drops every outcome assertion and scores 10/10, the semantic oracle is inert without `baseline_bytes`, and the healer falls back to the URL-only oracle MEMORY flags as "the one real green-wash hole." The <1% would then be *inherited from the video stack, not earned on the crawl stack* — kills the trust pitch in due diligence. **Mitigation (Phase 0–2):** screenshots on the crawler's first commit; extend `induced_drift_benchmark` to the crawl source and publish that number *before* any external <1% claim; behavioral-coverage tier from day one; submit-on-sandbox to earn outcome assertions.

**R2 (SERIOUS) — Destructive crawl on real regulated data.** Safety is 100% new build; the ingest sink cannot guard an already-executed action. "Fill-without-submit" does not bound mutation (onBlur/onInput fire MIB/credit inquiries); even `navigate` mutates on some GET backends; an English danger lexicon misses "Bind/Surrender/Lapse/Escheat/Rescind/Disburse"; "customer declares non-prod" is an unverifiable checkbox; and `/ground-truth/events:810` **does not redact PII**. **Mitigation (Phase 1, platform-enforced):** egress-sandbox (the only real blast-radius cap); read-only default; fail-closed mutation guard; domain irreversible-verb REFUSE pack; kill "fill-without-submit" as a *safety* concept; fix the PII leak. Explorer forbidden from client `{URL, creds}` until containment exists.

**R3 (SERIOUS) — Coverage measures the wrong set; blind spot ∝ criticality.** P0 invariants are simultaneously undecidable-static, unreachable-by-safe-crawl, and absent-from-the-journey-graph. **Mitigation (Phase 0/5):** atoms-vs-invariants split; per-band autonomy metric; the P0 falsification fixture graded first; universe-shrinkage guard; the product *executes and certifies* human-authored invariants rather than pretending to discover them.

**R4 (SERIOUS) — Repo heterogeneity: rules live outside cloned code.** JS/TS validator extraction sees cosmetic front-end checks; the crown jewels are in Guidewire/Pega/Salesforce/DB/PL-SQL/config/human. JS-toy benchmarks read 70–90% recall then collapse to single digits on the first real client — silently. **Mitigation (Phase 3):** demote repo-intel to seeding + denominator; stack/platform detector first; per-stack recall; beachhead-representative fixture before extractors; the dual-grounded oracle degrades to crawl-single-source (live behavior) + human-tacit-knowledge, never code-single-source.

**R5 (SERIOUS) — Scale, operability, durability.** Full re-crawl ≈ $900k/mo; ~415 concurrent crawlers writing one shared Postgres with **no backup** holding 1000 tenants' hash-chained evidence = a single write bottleneck **and** a single existential data-loss point. **Mitigation:** durability-first (PITR/carve-out crawl-substrate DB **before client #2**); operability meter (cost-per-suite) from commit one; per-tenant token-bucket + concurrency cap; drift-triggered incremental regression.

**R6 (moderate) — Substrate schema coupling breaks VKPower silently.** Direct writes couple to internal tables + the `extractor_version` "latest-wins" convention — tighter than an HTTP contract. **Mitigation:** freeze a named, versioned internal substrate contract with a **CI contract test**; converge to VKPower-owned additive HTTP extensions; all changes additive/gated/fail-open/byte-identical on the video path; confirm RLS INSERT on `010/029`.

---

## 8. WEEK-1 ACTIONS + WHAT'S NEEDED FROM THE FOUNDER

### First moves (in order)
1. **Author Phase-0 answer keys** for parabank + Aegis :8096 + Skyward :8095 **and** the P0 underwriting-ceiling invariant fixture; stand up one beachhead-representative app. No feature code until keys exist.
2. **Build the substrate-writer harness** (~200 lines) and pin the column/`extractor_version`/RLS contract with a CI test.
3. **Extend `induced_drift_benchmark`** to the crawl-sourced artifact; produce the **first published number: crawl-source false-heal + correct-refuse rate.** (The week-1 exit artifact — not a green suite.)
4. **Run the P0-invariant fixture** URL→certified and **measure the failure** (autonomy % on the P0 band).
5. **Fix the PII leak** at `/ground-truth/events` and **turn on Postgres PITR/backup.**
6. **Stub the egress-sandbox** network policy so Phase 1's explorer has containment to build against.

### Decisions only the founder can make
1. **A disposable/seeded environment commitment + one beachhead-representative sandbox app.** Without a disposable env, *all* mutating/P0 coverage is permanently capped at outcome-blind smoke — an explicit product tier, not a surprise.
2. **Authorization to enable Postgres backup/PITR (or fund a carved-out crawl-substrate DB) before client #2.**
3. **Green-light to amend "APIs only, never internals, zero VKPower writes"** to "a versioned internal substrate contract + additive, gated, fail-open VKPower extensions, CI-contract-tested." (Permission for *small additive* VKPower changes — not "never touch.")
4. **Name the two humans the "1%" hides:** who authors the O(apps) answer keys, and who is the domain-expert approver empowered to bless a P0 payment/underwriting scenario.
5. **Decide the submit-gating policy source:** disposable-sandbox flag vs repo-inferred idempotency vs per-flow human pre-approval. Decided *before* the explorer, because it caps autonomous transactional coverage.
6. **Approve the metric contract:** autonomy per band (never averaged), coverage as "% of enumerated atoms + N certified invariants" (never "all"), recall per stack, a behavioral-coverage tier on every suite.
7. **Identify one regulated design partner** willing to accept the hash-chained dossier as their FDA-CSA / EU-Annex-22 evidence artifact.

---

### One-line synthesis
**QE-Central's starting point is not a crawler and not a repo-analyzer — it is a substrate-writer + REFUSE harness whose first shipped number is the crawl-source false-heal rate. We prove we can be trusted on the new evidence source before we generate a single certified scenario, we contain the blast radius before we touch a real credential, and we measure the product at its weakest point (P0 mutating invariants) — because that is the honesty VKPower's <1% false-heal rate was born from, and it is the only thing in this market that cannot be copied in 18–36 months.**
