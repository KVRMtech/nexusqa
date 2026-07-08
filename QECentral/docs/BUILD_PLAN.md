<!-- Companion visual diagram published as a Claude Artifact (build-plan-v1):
     https://claude.ai/code/artifact/4fafb4c5-58f6-461d-94c2-3740a7c9105e
     Source of design: 8-agent workflow wf_8f51f011-bd2. Committed docs:
     PRODUCT_GOAL.md (north star), STARTING_POINT.md (first build), ONBOARDING_INPUT_CONTRACT.md (front door). -->

# QE-Central — The Definitive End-to-End Build Plan

## The whole build in a breath

We already own a proven, certified testing *factory* (VKPower): it turns evidence of what an app did into deterministic tests, runs them forever, self-heals under 1% false-heals, and refuses to green-wash. QE-Central does not rebuild any of that — it builds a **new way to feed it**. Instead of a human recording a video, a contained crawler explores the live app and writes the *exact same evidence rows* the video pipeline writes, at full confidence, with a screenshot at every step. Everything smart (reading the customer's code, deciding what's business-critical, approving scenarios, scheduling 10,000 apps, projecting compliance reports) hangs off the *sides* as plugins. The engine only ever drinks from one narrow, versioned contract in the middle — the substrate — so we can add a new UI technology, a new code stack, or a new regulator without ever touching the part of the system our trustworthiness depends on. We prove honesty *first* (the engine must refuse a wrong answer on data we control), then let each later phase become a better producer of the same trusted rows.

---

## 1. The end-to-end architecture, as clean layers

Read it top-to-bottom. Everything converges on **Layer 3 (the substrate)** and everything diverges from it. That single convergence is why the engine never has to change.

| Layer | Component | Status |
|---|---|---|
| **L0 — Intake & fences** | Signed job manifest (URL + code + answer-key + safety fences) | NEW |
| | Egress sandbox + fail-closed mutation guard | NEW |
| | Approval gate — *the 1%* | EXTEND |
| **L1 — Repo intelligence** *(seeding only, off critical path)* | Stack & platform detector with honest static-rule ceiling | NEW |
| | Per-stack atom extractors (routes / OpenAPI / validators) | NEW |
| | Provenance-tagged App Model (the coverage denominator) | EXTEND |
| | Crawler seed manifest (advisory, one-directional) | NEW |
| **L2 — Contained explorer** | a11y-first control inventory | NEW |
| | SPA state-graph explorer (state, not URL) | EXTEND |
| | Per-step screenshot + accessibility-tree capture | NEW |
| | Two-phase form controller (fill any env / submit disposable-only) | NEW |
| | Journey-graph state fingerprint | NEW |
| **L3 — THE SUBSTRATE (the narrow waist)** | `page_visits` + `page_actions` + `form_snapshot` at confidence 1.0 | REUSED |
| | Per-step screenshots + provenance tag (`live_crawl` vs video) | REUSED |
| | Substrate-writer harness + honesty guard | NEW |
| | Versioned contract + pinned golden CI test | NEW |
| **L4 — Scenario governance** | Deterministic criticality registry (P0…P3) | NEW |
| | Atoms-vs-invariants coverage model + named gaps | NEW |
| | Universe-shrinkage guard (deleted behavior = P0 gap) | NEW |
| | Behavioral-coverage tier label (RENDERS vs BEHAVES) | NEW |
| **L5 — The unchanged VKPower engine** | Suite generator → deterministic Playwright compiler | REUSED |
| | HONEST-10 certification gate | REUSED |
| | 15-rung self-healer + universal oracle + proven-control ledger | REUSED |
| | Triage → verdicts → hash-chained dossiers | REUSED |
| **L6 — Control plane** | Per-tenant fleet scheduler + concurrency caps | EXTEND |
| | Per-app politeness bucket (protects *customer's* app) | EXTEND |
| | Change detector + incremental suite selector | NEW / REUSED |
| | Cost-per-suite meter + CI budget gate | NEW |
| **L7 — Reporting & flywheel** | Compliance-framework adapters (projections, not captures) | REUSED |
| | Published KPIs (human-touches, false-heal, per band) | NEW |
| | Proven-control ledger + calibration loop | REUSED |
| **Cross-cutting** | Tenancy / RLS / KMS (encloses everything) | REUSED |

---

## 2. Phase-by-phase (0 → 5)

### Phase 0 — Measure-First Foundation + the Honesty/Refuse Harness
**Goal:** Prove, with a published number, that the unchanged engine — fed the new crawl-shaped evidence — *refuses* to green-wash a wrong answer, before any crawler, client, or secret exists.
**We build:** a ~200-line substrate-writer that hand-authored rows flow through; the answer-key fixture format; the extended REFUSE benchmark; the CI substrate contract test; a P0-invariant fixture run *to fail on purpose*; a real Postgres backup; a PII-redaction fix.
**How it works:** A human writes one answer key for an app we already own (Aegis/Skyward). The writer creates a `live_crawl` artifact, writes visits/actions/form rows at confidence 1.0 *with a real screenshot per step*, and runs them through the untouched generate → compile → certify → heal → verify chain. Then it deletes a required server-side rule and asserts the engine goes **red, not green**. Screenshots on the very first commit are non-negotiable — without them the oracle is inert and any green is *inherited, not earned*.
**Reuses:** the whole factory, the induced-drift benchmark, the semantic oracle, the M1 answer-key harness, RLS/KMS.
**Human vs autonomous:** Human *authors the fixtures* (the honest O(apps) cost, made visible on purpose) and authorizes backup. Everything else — write, certify, break, measure — is machine-run.
**Exit metric:** **Crawl-source false-heal rate = 0** across the MUST-REFUSE modes (correct-refuse = 100%), earned on the new source, plus a low honest P0-band autonomy number and a green contract test.
**Seams opened:** the `live_crawl` provenance discriminator; extractor-version "latest-wins"; break-mode→outcome map as data; the versioned substrate contract.
**Top risk:** a "safe" fill-only fixture never demonstrates a real transition → the engine scores a truthful-looking 10/10 that proves nothing. Mitigated by mandatory screenshots + deleting a *computed* server-side rule so refusal must go through the outcome oracle.

### Phase 1 — Contained Read-Only Explorer
**Goal:** A network-contained, fail-closed browser that explores *apps we own*, models SPA state as a graph, screenshots every step, and writes the substrate — proving containment and measuring discovery recall before a single client credential is touched.
**We build:** the egress sandbox; the fail-closed mutation guard; an irreversible-verb refuse pack (Bind/Surrender/Lapse…); a11y-first inventory; the SPA state-graph explorer; per-step capture; the recall benchmark extension.
**How it works:** Two independent safety layers — a default-deny network sandbox (the real blast-radius cap) and an in-browser guard that allows only GET/HEAD and aborts every write and mutation-signal GET. Inventory is built from the *accessibility tree* (role + name), the exact vocabulary the compiler and healer already rank on. State is fingerprinted from a normalized AX projection so wizard step-2 and step-3 on one URL are distinct. Danger controls are recorded as leaves, never clicked.
**Reuses:** the compiler's locator vocabulary + settle oracle, the substrate tables, the Pages&Forms recall harness, the KMS auth hook (present but unused).
**Human vs autonomous:** Fully autonomous crawl loop. Human only sets scope/allowlist and reviews the first-run audit log. No client creds yet.
**Exit metric:** **Discovery recall ≥ 80% on JS stacks, per stack, never blended**, AND hard co-gates: zero mutating requests escaped, cost within budget, two-tenant run with no SLA miss.
**Seams opened:** journey-graph fingerprint (reused for Phase-4 change detection); action-safety policy as data; pluggable frontier signals; the AX inventory as App-Model join key.
**Top risk:** a mutation escapes the guard. Mitigated by the network sandbox surviving guard bugs + apps-we-own-only so any miss can't touch real data.

### Phase 2 — Full Substrate + Behavioral Coverage on a Disposable Env
**Goal:** Where trust is *earned* on the crawl source — write the substrate at confidence 1.0, add a two-phase form flow that submits only on an attested disposable env, capture the confirmation as a demonstrated outcome, and label every suite RENDERS vs BEHAVES so a fill-only suite can never fake a 10/10.
**We build:** the confidence-1.0 writer + honesty guard; the two-phase form controller; the fail-closed submit gate; the confirmation-state capturer; the behavioral-coverage tier labeler; the extended MUST-REFUSE benchmark on the crawl source.
**How it works:** Because the crawler *drove* the browser, it truthfully knows three things video can only guess — the URL (from the browser's location), the locator (the element it actually resolved), and the value (the string it typed). Only those get stamped 1.0; anything inferred is sub-1.0 or omitted. Phase-A fills on any env; Phase-B submits *only* when a disposable-env attestation AND a per-flow approval AND a non-irreversible verb all line up. A successful submit writes the confirmation as a terminal visit + baseline screenshot — the single highest-value artifact in the product, because it simultaneously gives the generator a grounded outcome assertion, the oracle its baseline, and the dossier its proof image.
**Reuses:** the grounded-navigation gate, the compiler + HONEST-10 rubric, the semantic + universal oracle, the induced-drift benchmark, /verify + dossiers.
**Human vs autonomous:** Human *attests* the disposable env, supplies consumable data + reset-to-clean, and approves which flows may cross the submit boundary. Every cycle is otherwise autonomous.
**Exit metric:** **Crawl-source false-heal = 0 on the BEHAVES suite (incl. a deleted-validation P0 mode)**, gated on ≥1 P0 invariant certified end-to-end and 100% of suites carrying a tier label with zero RENDERS suites shown as outcome-proven.
**Seams opened:** the submit-gate as a provenance policy; the tier enum; modality-agnostic ground-truth rows; the confirmation baseline as a reusable anchor.
**Top risk:** a weak confirmation state re-opens outcome-blind green-wash. Mitigated by BEHAVES *requiring* a captured baseline — otherwise the case stays RENDERS, never fake-BEHAVES.

### Phase 3 — Repo-Intelligence as Seeding + Coverage Denominator
**Goal:** Turn the customer's code into a stack-aware, provenance-tagged App Model that *seeds* the explorer and supplies the coverage denominator — earning its place only through a measured recall lift, and honestly declaring a low ceiling on vendor-platform stacks.
**We build:** the repo connector (pinned to the *deployed* SHA); the stack/platform detector with a published static-rule ceiling; per-stack atom extractors; the App Model store; the seed manifest builder; the live↔repo drift reconciler; the directed-vs-blind A/B harness.
**How it works:** The detector runs *first* and decides how much the code can honestly tell us — JS/TS and Spring get a high band; Guidewire/Pega/Salesforce get an honest ~5–15% ceiling and a hard "route to crawl + human" label. It refuses to publish a confident coverage number where the crown-jewel rules aren't in clonable code. The seed manifest is *advisory and one-directional* — the explorer runs identically with or without it (pinned by a CI test), which is the structural guarantee repo-intel is off the critical path.
**Reuses:** the verbatim-quote demotion guard, the provenance tiers, the recall benchmark harness, the degraded-badge honesty pattern, RLS/KMS.
**Human vs autonomous:** Human confirms *which* repo/branch is deployed and the mono-repo folder scope (guessing pollutes the denominator). Per-stack extractor authoring is a one-time engineering cost, not a per-app cost. Below-ceiling stacks degrade to blind crawl with zero human.
**Exit metric:** **Per-stack recall lift (directed − blind) positive and significant (target +15 pts on JS/Spring)**, reported alongside the App Model's own universe-recall and the honest ceiling band. No global "percent of business rules covered" number ever exists.
**Seams opened:** per-stack extractor plugins; reserved DSL/DB-constraint provenance tiers; deployed-SHA stamp (for Phase-4 change detection); human-asserted atoms.
**Top risk:** the vanity-metric trap — JS toys read 70–90% then collapse on the first Guidewire client. Mitigated by detector-first ceilings + a beachhead fixture graded in Phase 0 + per-stack numbers only.

### Phase 4 — Fleet Operability + Change-Triggered Incremental Regression
**Goal:** Make 1000 clients / 10,000 apps economical and polite by re-testing only what changed, under per-tenant and per-app rate/concurrency/budget ceilings, with a cost meter that fails CI when the economics drift.
**We build:** the fleet cycle driver; the change detector (repo-SHA diff ∪ journey-graph fingerprint diff); the incremental suite selector; the per-tenant scheduler; the per-app politeness bucket; the cost-per-suite meter; the CI budget gate; the carved-out crawl-substrate DB with backup.
**How it works:** The economic thesis is a change of denominator — from "apps × routes × nights" to "changed-routes × change-events." Nothing runs unless triggered; when it is, a cheap probe recomputes each page's structural fingerprint and intersects the deployed SHA's changed files with the App Model — only the union is deep-crawled. Everything else carries its verdict forward as a *provenance-stamped, time-bounded* fact, never a silent green: an uncomputable page is treated as changed, a vanished page raises a possible-deletion gap, and a weekly full-crawl floor backstops. The genuinely new gate is *politeness toward the customer's own app* — a token bucket keyed on their host, so we never read as an attack.
**Reuses:** the tenant rate/concurrency limiters, the SLA budget scheduler, the structural graph diff + affected-scenarios mapper, the proven-control ledger, the metrics facade, the on-prem compose stack.
**Human vs autonomous:** Almost entirely autonomous (it's infrastructure). Human sets polite rate, blackout windows, and spend caps once at onboarding; paged only on a budget breach, SLA miss, or deletion alert.
**Exit metric:** **Cost-per-app-per-cycle ≥ 10× below full-re-crawl and flat (±15%) when the fleet scales 10×**, with 0 rate-limit SLA misses across ≥2 tenants sharing one carved-out DB.
**Seams opened:** source-agnostic change-source interface; cost-unit registry; criticality-aware scheduling priority; per-app politeness policy hook; regional worker pools.
**Top risk:** incremental miss = green-wash by omission. Mitigated by fail-safe-to-CHANGED, time-bounded verdicts, and the full-crawl floor.

### Phase 5 — Scenario-Synthesis Governance + the Approval Gate (the honest 1%)
**Goal:** Make the 1% real and bounded — a coverage model that names every gap, a guard that structurally cannot green-wash a deleted behavior, a deterministic criticality registry, and a gate where only NEW/CHANGED scenarios re-consume expert attention.
**We build:** the deterministic criticality registry (P0…P3); the atoms-vs-invariants coverage model; the universe-shrinkage guard; the scenario fingerprint + diff engine; the per-band approval gate; the approved-universe baseline store; the quote-validated explain lens; the universe-recall + planted-deletion red-team harness.
**How it works:** Coverage is split into two layers that are *never blended*: enumerable **atoms** (routes ∪ endpoints ∪ validators ∪ crawl edges, each with a band and provenance, the universe publishing its *own* recall vs the answer key) and human-authored **certified invariants** (executed end-to-end with a refuse-proof, never auto-discovered). The shrinkage guard remembers the denominator: any previously-approved, previously-covered behavior now GONE is raised as a P0 possible-deletion gap that a human must dispose as "deleted on purpose" vs "regression." Criticality is deterministic and *fails up* on ambiguity — the LLM may explain a band but can never set or cross one. Only NEW/CHANGED scenarios reach the queue; an unchanged app re-crawled consumes *zero* approvals.
**Reuses:** the triage classifier's marker table, the control-fingerprint hashing, the heal-policy AUTO/APPROVE/FAIL tiers, the verbatim-quote clamp, the HONEST-10 tier reader, /verify dossiers.
**Human vs autonomous:** The named domain expert — the departing employee whose tacit knowledge we capture — authors invariants once and blesses only NEW/CHANGED P0/mutating scenarios and deletion dispositions. Low-band render-only changes auto-approve under opt-in policy.
**Exit metric:** **Planted-deletion catch rate = 100%** (every deleted contracted behavior raises a P0 gap; 0 of N certify green), gated with criticality-band precision ≥ target and a re-approval ratio of *zero human touches* on an unchanged app.
**Seams opened:** criticality registry as a data-driven signal pack; typed atom-provider interface; per-band approval policy; versioned hash-chained baselines; source-agnostic invariant registry; typed human-touch meter.
**Top risk:** a P0 payment scenario misclassified as low would auto-approve without the expert. Mitigated by deterministic-only bands, fail-up, and red-teaming classifier precision before trusting any auto-approve.

---

## 3. The future-enhancement model — how it grows to 10,000 apps without a rebuild

Every seam is the *same shape*: a **many-to-one map into a canonical form**, and the engine binds only to the canonical form. "Add a thing" is always "register a plugin," never "modify the core."

| Seam | What plugs in | Why it needs no rework |
|---|---|---|
| **A — Capture adapters** | Web crawler now; later desktop-UIA, mobile/Appium, mainframe/3270, closed-shadow/canvas/WebGL | The engine's input is the substrate, not the capture mechanism — it can't tell whether a click came from a browser or a phone. A new modality is a new *producer* pouring into the same waist. |
| **B — Stack extractors** | Per-stack readers (React-Router, Spring, Rails, .NET, Guidewire/Pega/Salesforce detectors) | Extractors only *seed the crawl and widen the denominator* — never on the critical path. A missing extractor lowers a measured lift number; the blind crawl is always the fallback. Fail-open by design. |
| **C — Oracle / answer-key providers** | Spreadsheet, API-contract expectation, golden DB snapshot, human-attested confirmation | The oracle proves against a *normalized expectation*, not the source. New truth = a new mapping; the <1% false-heal proof step is untouched. |
| **D — Criticality-signal registry** | "Route touches payment," "form carries PII," "repo marks an invariant," "NAIC-material," "top-traffic band" | Bands are deterministic and frozen; a signal *informs* a score but can never cross a boundary. New signal = register a scorer; the governance guarantee holds automatically. |
| **E — Compliance-framework adapters** | NAIC, SOC2, EU AI-Act Annex-22, and future regimes | Evidence is captured *once* in a canonical dossier; each framework is a read-only *projection*. You never re-instrument to satisfy a regulator — you write one view over hash-chained evidence you already have. |

---

## 4. The one-page diagram spec

Render as **8 horizontal bands**, top to bottom, with one bold vertical spine of convergence at the substrate. Producers fan *in* from above; consumers fan *out* below; intelligence and control plane sit as *side-cars* connected by dashed (non-critical-path) lines.

```
┌────────────────────────────────────────────────────────────────────────┐
│ L0  INTAKE & GOVERNANCE                                                   │
│   [Job Manifest]   [Safety Fences / Egress Sandbox]   [Approval Gate ①%] │
└────────────────────────────────────────────────────────────────────────┘
        │ manifest + fences                          ▲ new/changed only
        ▼                                            │
┌────────────────────────────────────────────────────────────────────────┐
│ L1  EVIDENCE PRODUCERS  (Capture-Adapter Seam A)                          │
│   [Web Crawler = Recorder]  [Video Pipeline]                              │
│   :: future :: [Desktop-UIA] [Mobile] [Mainframe] [Closed-Shadow/Canvas] │
└────────────────────────────────────────────────────────────────────────┘
        │ writes rows + per-step screenshots + provenance (conf 1.0)
        ▼
╔════════════════════════════════════════════════════════════════════════╗
║ L2  THE SUBSTRATE  — versioned contract vN — THE NARROW WAIST            ║
║   [page_visits] [page_actions] [form_snapshot] [screenshots] [provenance]║
║   additive-only · fail-open · pinned golden contract test               ║
╚════════════════════════════════════════════════════════════════════════╝
        │ single source-agnostic read edge
        ▼
┌────────────────────────────────────────────────────────────────────────┐
│ L3  UNCHANGED VKPOWER FACTORY                                             │
│   [Scenario Synthesis] → [Suite Generator] → [Playwright Compiler]       │
│     → [HONEST-10 Gate] → [Executor ∞] → [15-Rung Self-Healer]            │
│     → [Universal Oracle] → [Triage] → [Verdicts + Hash-Chained Dossiers] │
└────────────────────────────────────────────────────────────────────────┘
   ▲ seeds/denominator   ▲ expected-truth   ▲ bands           │ verdicts
   │ (Seam B, dashed)    │ (Seam C)         │ (Seam D)        ▼
┌──────────────────────────────────────┐   ┌──────────────────────────────┐
│ L4  INTELLIGENCE & SEEDING (side-car)│   │ L5  CONTROL PLANE & REPORTING│
│  [Stack Detector + honest ceiling]   │   │  [Per-Tenant Scheduler]      │
│  [Per-Stack Extractors] [App Model]  │   │  [Per-App Politeness Bucket] │
│  [Oracle/Answer-Key Providers]       │   │  [Change Detector + Selector]│
│  [Criticality Registry]              │   │  [Cost-per-Suite Meter + CI] │
│  [Coverage Model + Shrinkage Guard]  │   │  [Compliance Adapters: Seam E]│
└──────────────────────────────────────┘   │  [Published KPIs]            │
                                            └──────────────────────────────┘

╔════════════════════════════════════════════════════════════════════════╗
║ CROSS-CUTTING (draw as an enclosing frame around everything)            ║
║  [Tenancy / RLS / KMS]   [FLYWHEEL: Proven-Control Ledger + Calibration]║
║  [Honesty / Refuse Harness → false-heal & correct-refuse rate]         ║
╚════════════════════════════════════════════════════════════════════════╝
```

**Arrows to draw (numbered):**
1. L0 Job Manifest → L1 producers (authorizes + scopes the crawl).
2. L0 Safety Fences → *wraps* the L1 crawler (egress sandbox + fail-closed guard).
3. **L1 every producer → L2 substrate** — all producers converge on this one box.
4. **L2 substrate → L3 factory** — single read edge, source-agnostic; all consumers diverge here.
5. L4 Stack Extractors / App Model → L1 crawler *and* L3 synthesis (dashed — seeds only, never critical path).
6. L4 Oracle providers → L3 Universal Oracle (normalized expected-truth).
7. L4 Criticality Registry → L3 Scenario Synthesis (deterministic bands).
8. L3 Synthesis → L0 Approval Gate → back to L3 (only new/changed round-trip the human).
9. L3 Verdicts → L5 Compliance Adapters → Published KPIs.
10. L3 Universal Oracle → Cross-cutting Proven-Control Ledger (memoize proven heals).
11. Ledger → L3 Self-Healer *and* L1 crawler (seed-before-run — the flywheel closing).
12. Honesty/Refuse Harness → measures L3 oracle/heal → feeds Calibration → adjusts thresholds back into L3 (second flywheel loop).
13. L5 Change Detector fingerprints → L5 Scheduler → L3 Executor ∞ (re-crawl only what changed).
14. Tenancy/RLS/KMS → drawn as the enclosing frame, not a single arrow.

**Read it to a founder in one sentence:** *Every way of watching an app pours into one contract in the middle; our proven engine drinks only from that contract; and everything smart or regulatory hangs off the sides as plugins — so we add a new UI technology, a new code stack, a new regulator, or a new source of truth without ever touching the engine our trustworthiness depends on.*

---

## 5. The single very-next action

**Build the Phase-0 substrate-writer harness against one owned app (Aegis) and publish the first crawl-source correct-refuse number** — hand-author one answer key, write the visits/actions/form rows *with a screenshot per step*, run them through the untouched generate → certify → heal → verify chain, then delete one required server-side rule and prove the engine goes red instead of green. That one result, produced with zero VKPower code edits and zero video, is the whole thesis made real: it proves the seam exists and that trust is *earned* on the new source before a crawler, a client, or a credential is ever built.
