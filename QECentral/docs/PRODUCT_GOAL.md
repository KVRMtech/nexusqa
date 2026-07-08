# QE-Central — Product Goal & End-to-End Picture (North Star)

**Status:** Finalized v1 (2026-07-07). The definitive "what we are building" for QE-Central. Companion docs: `ONBOARDING_INPUT_CONTRACT.md` (the front door), `STARTING_POINT.md` (what to build first), `QE_CENTRAL_BLUEPRINT.md` (architecture detail).

---

## 1. What QE-Central is, in one sentence

Point it at any company's web app, give it the code and the answer key, and it becomes an **autonomous QA team** that discovers the business-critical journeys, tests them forever across every browser, heals its own tests when the app changes, triages what actually broke, and reports honestly — **refusing to certify anything it cannot prove.**

It is a **separate product of the company** — not a feature of VKPower. It shares VKPower's *trust engine* as internal components but is a different product with a different customer, a different input, and its own roadmap.

---

## 2. Relationship to VKPower — share the engine, never break the car

| | **VKPower** (existing) | **QE-Central** (new) |
|---|---|---|
| Input | A **video** of a person using the app | A **URL + code + answer key** (no video) |
| Who records | A human demonstrates | The platform explores itself |
| Product | Video-grounded test authoring | Autonomous centralized regression |
| Shared | — | **Reuses VKPower's trust engine** |

**The shared engine** (built, certified, in production): the HONEST-10 certification gate, the 15-rung self-healer with its proven-control ledger and universal oracle (published <1% false-heal), the deterministic Playwright compiler, provenance-tiered evidence, hash-chained proof dossiers, defect/environment triage, tenancy/security.

**The no-break guarantee.** QE-Central is a **new evidence source feeding that unchanged engine.** VKPower's video pipeline is never modified. Where QE-Central needs the engine to accept a new kind of evidence, the changes are **small, additive, and fail-open** — byte-identical behavior on the video path, pinned by an automated contract test. VKPower keeps working exactly as today; QE-Central is a new road that reuses the same factory.

---

## 3. The end-to-end picture — 9 stages (followed through *Acme Life Insurance*)

The customer gives the 6-bucket inputs once. Everything after is a loop that runs forever.

**① ONBOARD — the front door (human, once per app).**
Acme provides the 6 buckets: how to get in (test site, VPN, MFA, role logins), the code, synthetic test data, **the answer key** (correct premiums, eligibility rules, must-refuse cases), the safety fences (never-click list, stubbed vendors), and ops (who to page, when to run). → *This is the 1%, and it is the only heavy human touch.*

**② UNDERSTAND — read the code (autonomous).**
Repo-intelligence clones Acme's code and builds an **App Model**: the routes, the API endpoints, the business rules it *can* see — every fact tagged with where it came from (file + line). It's honest about what it can't see (rules that live in Guidewire, not the front-end). → *Seeds the explorer so it doesn't wander blindly.*

**③ EXPLORE — the crawler is a recorder (autonomous).**
A guided, *contained* browser logs in as each role and walks Acme's app — visiting the quote page, the application form, the portal. As it goes it **captures evidence at confidence 1.0**: exactly what it clicked, what it typed, a screenshot of every step. It stays behind a hard safety fence (read-only by default; never presses Bind/Charge/E-Sign unless Acme gave a disposable environment). → *Produces the same evidence VKPower gets from video — but cleaner, because it knows the exact button, not a guess from pixels.*

**④ DECIDE WHAT MATTERS — scenario synthesis (autonomous propose → human approve).**
From the code rules + the explored journeys, it **ranks the business-critical scenarios**: quote-to-bind, beneficiary change, the over-60 coverage limit. It proposes them with evidence. → *A human blesses the list once (governance — this is the regression contract). Only NEW or CHANGED scenarios ever need human attention again. This is the second, tiny human touch.*

**⑤ TEST — the VKPower factory, reused unchanged (autonomous).**
Approved scenarios flow into the existing engine: generate the test cases → compile deterministic Playwright (zero LLM, the same script every time) → run it through the **HONEST-10 certification gate**, which blocks any test that can't prove what it claims.

**⑥ RUN FOREVER — regression (autonomous).**
Every time Acme ships a new build, the certified suite re-runs automatically, in every browser. Not a one-time test — a permanent guardian.

**⑦ SELF-HEAL — autopilot (autonomous).**
When Acme redesigns a page and a button moves, the 15-rung healer re-finds it and keeps the test alive — **but only when it can prove the new target is the right one.** If it can't prove it, it refuses to heal and flags a human. This is why the false-heal rate is <1%.

**⑧ TRIAGE — tell the real story (autonomous).**
When a test fails, it decides *why*: a real product defect, a flaky environment, a stale test, or bad data — automatically, so a human isn't paged for a network blip.

**⑨ REPORT HONESTLY — verdicts + dossiers (autonomous, human on real defects).**
Every result carries a **proof dossier** (screenshots, what was checked, provenance). When it proves a real break — the over-60 rule stopped working — it escalates to Acme's named contact. When it *can't* be sure, it says **"unproven,"** never a false green. → *A human only steps in to confirm a genuine defect: the third, tiny touch.*

---

## 4. The goal: 99% agentic / 1% human — defined so it's true, not just a slogan

The 99/1 is a **measured autonomy budget**, not a marketing number. Measured as **human touches per app onboarded** and **per regression cycle**, published and trending down.

**The 99% — fully autonomous, no human in the loop:**
understanding the code, exploring the app, deciding what matters, generating and compiling tests, certifying them, running them forever across browsers, healing them when the app changes, triaging failures, and writing the proof. Once an app is onboarded, cycle after cycle runs with **zero human hands** unless something genuinely new appears.

**The 1% — the irreducible human, and where it honestly sits:**
1. **Onboarding** the answer key + safety fences (once per app — the front door).
2. **Approving** the proposed scenarios (governance — a human owns the regression contract; only new/changed scenarios re-ask).
3. **Confirming** a low-confidence defect or an ambiguous rule.

**The honest caveat that keeps it credible:** autonomy is near-total on *read and navigation* flows. It is naturally *lower* on **P0 money-moving flows** (bind a policy, transfer funds), because safely completing a real transaction needs either a disposable environment or a human pre-approval — you cannot autonomously press "Bind" on real data. So we publish autonomy **per criticality band**, never as one averaged number. The flywheel raises it over time: every proven heal and confirmed scenario is remembered, so the same human is never asked twice.

**This is also the moat.** Anyone can build a clicking bot. The rare thing is an engine that **publishes its own error rates and refuses rather than green-washes.** The 1% human is not a weakness we hide — it is the honest governance layer that regulated buyers *require*.

---

## 5. What's reused vs. what's new

**Reused from VKPower (already built + certified):** certification gate, self-healer + ledger + oracle, deterministic compiler, triage, verdicts/dossiers, tenancy/security. *(This is why the build is faster — the hard, trust-critical half already exists and is proven.)*

**New to build for QE-Central:** repo-intelligence (read the code → App Model), the guided explorer (the contained crawler-recorder that writes evidence at confidence 1.0), scenario synthesis + the approval gate, the answer-key/oracle intake, the safety containment layer, and the fleet control plane (schedules, dashboards, 1000-client scale). *(Detail + build order in `STARTING_POINT.md`.)*

---

## 6. The path (measured, one number per phase)

Every phase exits on a **measured number**, never a self-report — the same discipline that produced VKPower's <1% false-heal rate.

- **First build (now):** the honesty harness — prove the engine *refuses to green-wash* a wrong answer on data we already have, before a single client secret changes hands. First shipped number: the crawl-source refuse rate.
- **Then:** the contained explorer (read-only, measured discovery recall) → full behavioral coverage on a disposable env → repo-intel as seeding → fleet scale + the approval/coverage governance.

---

## 7. North-star success metrics (all published, all measured)

- **Autonomy ratio** — human touches per app onboarded / per regression cycle (per criticality band), trending down.
- **False-heal rate** — per evidence source (inherit the <1% bar; earn it on crawl).
- **Discovery recall** — % of business-critical journeys found, per stack, vs. an answer key.
- **Behavioral-coverage tier** — every suite labeled: does it prove the app *renders*, or prove it *behaves*?
- **Correct-refuse rate** — how often it honestly says "unproven" instead of a false green.
- **Time-to-first-certified-suite** and **cost-per-suite** — the numbers that gate 1000 clients.

---

### One-line north star
**QE-Central is a separate product that turns any web app into a permanently-guarded, self-healing, honestly-reported regression suite — 99% autonomous by measured budget, 1% human by governance design — by feeding a brand-new evidence source (live crawl + code) into VKPower's proven, unbroken trust engine.**
