# Nexus QA — Seed Strategy: The Test Evidence System-of-Record

> Generated 2026-06-26 from a 21-agent analysis (QE/SDET/architect/regulated-buyer/DevOps panels +
> 4-cluster competitor teardown + positioning judge-panel + roadmap + skeptical-seed-VC stress-test),
> grounded in the actual source (agentic_heal.py, confidence.py, the deterministic compiler,
> control_ledger.py, defect_report.py, network_oracle.py, oracle_scorecard.py).

---

## The one-liner

**Nexus QA is the Test Evidence System-of-Record: the proof-of-behavior layer above commoditized
codegen and self-heal that PROVES a green is real — with screenshot, selector, and value evidence a
regulated examiner accepts — and is structurally incapable of green-washing because it fails toward
red, refuses to heal real regressions, and auto-authors a filing-ready defect instead.**

Tagline for the title slide: **"Autonomy you can put in front of an auditor."**

---

## Where the product is best placed (positioning)

**Category to define and own: The Test Evidence System-of-Record** — the proof-of-behavior layer
*above* codegen and self-heal (both collapsed to ~$0: free Microsoft Playwright Healer v1.56,
Tricentis Agentic). We do **not** compete in "AI test generation" or "self-healing." We own the
unsolved layer that adjudicates **whether a green is real**, proves the app reached the **same
outcome a recorded human SME did**, and holds the **auditable evidence chain** a regulated examiner
accepts.

**Positioning statement:** For regulated financial-services and life-insurance quality/engineering
leaders whose suites go green while real defects ship — and who cannot send their app, data, or
change-control to a black-box cloud — Nexus QA proves a pass is real instead of asserting one.
Record an SME doing the real workflow once; Nexus authors, runs, and self-heals **real Playwright the
customer owns and runs in their own perimeter**, and emits a never-green-wash grounded verdict.
Unlike codegen-and-heal tools that prove a test *ran* or an element *resolves*, Nexus proves the app
**behaved** — and signs the evidence.

**Beachhead ICP:** Dir/VP Quality Engineering + Head of Test Automation (champion, owns the release
attestation), co-signed by CISO / regulatory-affairs (economic buyer, compliance budget), at US
FS/life-insurance enterprises with in-perimeter / data-residency mandates (policy-admin, claims,
underwriting, online-banking, customer portals under SOX, SR 11-7, NYDFS 500, Part-11-style
validation). The wedge buyer has been **burned twice**: a green suite that shipped a bug they
couldn't explain to an auditor, AND every cloud QE tool that fails their RFP on data-residency.

**Disqualify (for now):** cloud-native startups with no residency constraint, unregulated SMB, teams
content with hollow green — they buy cheaper SaaS.

---

## Why now (the tailwind is the thing that looks like a threat)

1. Codegen + basic self-heal collapsed to **free/commodity** (MS Playwright Healer v1.56, Tricentis
   Agentic) — re-pricing the market onto the unsolved question we own.
2. AI coding agents (Cursor/Copilot) + computer-use agents generate/run tests cheaply but emit
   **hollow green** (computer-use agents score <30% F1 as testers) — making an independent PROOF
   layer newly necessary.
3. Regulators tighten on AI in the FS/insurance SDLC (NYDFS, SR 11-7 model-risk, state insurance-AI
   bulletins, Part-11-style per-model-version validation) — **compliance PULL** for an auditable
   evidence chain + customer-controlled model versioning.
4. A **credibility vacuum**: every incumbent hides its false-heal rate. A trust-first entrant can
   take the honesty high ground first.
5. On-prem/data-residency hardened from a checkbox into a hard **RFP deal-gate** — disqualifying the
   cloud-only set before features are compared; Playwright as the de-facto open standard makes "real
   tests you own and run in-perimeter" credible and low-lock-in.

---

## The differentiated wedge — the VERDICT, enforced in source (not on a slide)

"Expected" is anchored to an **independent ground truth — the human SME in the recorded video** —
never the app's own behavior and never an LLM guess-oracle. Never-green-wash is enforced across five
independent, source-verified layers:

1. **confidence.py** scores each step PROVEN/REVIEW/CONFIRM from captured signals only, and keeps
   value-conflict steps RUNNING rather than skipping-to-green.
2. The **deterministic compiler** emits load-bearing `test.skip(true,'UNPROVEN')` (propagated into
   the Cypress + Playwright exporters) so no downstream assertion can false-green across an unproven gap.
3. **agentic_heal.validate_fixes()** DROPS any LLM rebind whose target name is not **verbatim** in
   the live a11y snapshot — the agent literally cannot fabricate a selector — gated at confidence ≥ 0.7.
4. **self_heal.assert_assertions_unchanged()** makes weaken-to-go-green impossible; heal can NEVER
   override a REAL_REGRESSION.
5. **oracle_scorecard.py** counts only positively-PROVEN outcomes as verified, reporting a bare green
   separately as "nothing threw, not proven."

**The novel output no rival ships:** on a real regression / 5xx, `defect_report.build_defect()` +
`network_oracle.py` auto-author a replayable, Part-11-ready repro+bug instead of healing —
**refuse-and-prove, not heal-and-hide.** A 4-cluster teardown found no competitor (Mabl, Testim,
Functionize, testRigor, Tricentis, Katalon, Sauce, BrowserStack, Applitools, MS Healer, QA Wolf,
Rainforest, Reflect, Autify, Momentic) produces this.

---

## Competition — one sentence

**Everyone proves the test RAN (or an element RESOLVES / RESEMBLES). We prove the app BEHAVED — and
sign the evidence.** The bug-vs-test verdict is deferred to a human everywhere (Mabl concedes it
"unsolved"); BrowserStack literally markets "keep builds green" (the exact failure mode we refuse).
Cloud rivals are architecturally locked out of the regulated RFP (send-app-to-cloud +
defend-black-box-heal both fail). The closest philosophical competitor to watch: **testRigor**
("heal on real recorded data, validated"). The name-collision to note: **Autify** also ships an
owned-code Playwright product.

**Uncontested whitespace (held by no competitor):** grounded same-outcome oracle · never-green-wash
refuse-and-file-defect · on-prem/owned-code + compliance evidence chain · video-grounded SME capture ·
proven-control ledger ("heal once, prove everywhere") · consented federated failure→fix flywheel.

---

## The 12-slide seed deck

| # | Slide | Headline |
|---|-------|----------|
| 1 | Title / Positioning | **Nexus QA: The Test Evidence System-of-Record.** Autonomy you can put in front of an auditor. We cede codegen/self-heal (now ~$0); we own the layer that proves a green is real. |
| 2 | Problem | **The green suite that shipped the defect — and the auditor you can't answer.** Two pains, one buyer: tools prove a test ran, none proves it behaved; "all tests passed" is no longer a defensible release attestation. |
| 3 | Why Now | **The market just re-priced from "can you write the test" to "can I trust the green."** Five converging forces (commoditization, hollow-green AI agents, regulatory pull, credibility vacuum, on-prem deal-gate). |
| 4 | Solution | **Record an SME once. Get real Playwright you own — and a verdict you can prove.** Ground truth = the human in the video, not the app, not an LLM. Fails toward red; auto-files a defect. |
| 5 | How It Works / Proof | **Five independent code layers — verified in source — that cannot fake a green.** (The technical-DD-earning slide; every claim maps to a file:line.) |
| 6 | Differentiation / Moat | **A trust posture incumbents are structurally disincentivized to ship.** Ordered by how real it is *today*: trust posture → sovereign evidence stack → proven-control ledger → federated flywheel (future) → published false-heal rate. |
| 7 | Market & TAM | **Don't sell into the deflating number — attach to the compliance budget that already exists.** Anchor price to the cost of one shipped regulated defect, not per-seat QE SaaS. |
| 8 | Competition | **Everyone proves the test RAN. We prove the app BEHAVED — and sign the evidence.** No rival auto-authors a repro+bug from a bug-vs-test verdict. |
| 9 | Go-To-Market | **Land bottom-up in CI, expand into the release gate, paid design partners first.** Grounded verdict as a CI gate (Action/CLI/PR check) + paid co-development LOIs. |
| 10 | Traction / Milestones | **First-ever clean run, never-green-wash verified in source — and the honest gaps.** Clean Run V1 (run c22dcde7, 24/24 on Aegis after ~80 failures; correctly refused a material consent change). Gaps stated plainly. |
| 11 | Roadmap to #1 | **#1 = the only credible answer to "can you PROVE the green and hand my auditor the evidence."** Now → 3-6 → 6-12 → 12-24mo. |
| 12 | Team & Ask | **Raise scoped to fund down three named risks — milestone-tranched.** Reproducible build → first measured number → named regulated design partner + a security/platform hire #3. |

---

## Roadmap to #1

**"#1 QE product" is redefined off generation/healing throughput (ceded) and onto VERDICT + EVIDENCE.**
Measured on 5 axes: (1) **proof accuracy** — a published, third-party-app-measured false-heal +
missed-regression rate; (2) **oracle breadth** — fraction of steps with a positively-PROVEN outcome
(today largely one `toHaveURL` regex); (3) **reproducibility/auditability** — whole system builds
from one versioned source with green CI + a TPRM-reviewable on-prem installer; (4) **design-partner
proof** — N named regulated partners running the verdict as a release gate on their apps; (5)
**compliance posture** — SOC 2 Type II, pentest, 21 CFR Part 11-grade e-record/e-sign/immutable audit.

**Win condition:** when a regulated buyer's RFP shifts from *"can you write and heal the test"* to
*"can you PROVE the green and hand my auditor the evidence"* — and Nexus is the only credible answer.

| Horizon | Theme | Key milestones |
|---|---|---|
| **NOW (0-3mo) — Seed Proof** | Make the differentiator REAL in source and MEASURED on data | (1) Single reproducible build: commit agentic_heal/control_ledger/defect_report/network_oracle to a coherent branch off develop, green CI building the actual differentiators, triage ~249 untracked files. (2) Apply ledger + run_screenshots migrations; demo Proven Control Ledger seed-before-run end-to-end with a before/after maintenance-hours figure. (3) **First MEASURED false-heal + missed-regression rate on a NON-self-owned app** (N≥20 human-confirmed). (4) Default-on visual + in-page post-step gate. (5) Runner emits per-step structured network/console/HAR so defects auto-file on silent 5xx. |
| **3-6mo — Land the wedge in CI + a real perimeter** | Become the release GATE where engineers work; prove on someone else's app in their boundary | (1) GitHub Action / GitLab component / Jenkins step + CLI + service-token endpoint → JUnit + exit code + PR status check (PROVEN/REVIEW/CONFIRM). (2) **First named FS/insurance design partner LIVE fully in-perimeter with a bring-your-own model endpoint** (resolves the air-gap-vs-external-LLM contradiction). (3) RBAC + segregation-of-duties (author can't approve own heal) + tamper-evident audit write. (4) One-click defect into Jira/Linear/Xray. (5) Real flake quarantine (never resolves a gate by retry-to-green). |
| **6-12mo — Auditable, scalable, externally proven** | Deep demo → hardened attestable multi-tenant platform | (1) Deep business-outcome oracles (computed premium, disclosure text, persisted record, downstream API effect) as reviewable contracts. (2) 21 CFR Part 11 kit + SOC 2 Type II underway + pentest + zero-egress DPA. (3) Parallel runner → real suite in single-digit minutes. (4) **Published, methodology-disclosed accuracy across ≥3 real apps (N≥200).** (5) 3-5 named partners with documented hours-saved + ≥1 real regression caught + auto-filed. |
| **12-24mo — Compounding moat + category ownership** | Light up the flywheel for real; own the category at fleet scale | (1) Federated failure→fix flywheel LIVE with N consented tenants (build the stubbed DP/secure-agg; k=20 signed priors that can NEVER downgrade a real_regression; leakage-guard green on real data). (2) Fleet control plane. (3) Legacy-decommission/parity attestation product (top FS budget driver). (4) SOC 2 Type II complete + reference logos. (5) Third-party-audited, continuously-published proof-accuracy benchmark — the artifact incumbents are commercially unwilling to match. |

---

## What will kill the raise — the diligence-killers (pre-empt, don't hide)

The VCs verified the code and the thesis. **They liked the honesty (it lowered the fraud/delusion
read) and rated it CONDITIONALLY FUNDABLE / QUALIFIED PASS — milestone-tranched, not a term sheet on
today's artifact.** The blockers, in priority order:

1. **EXISTENTIAL — reproducibility-from-source.** The four flagship modules (agentic_heal,
   control_ledger, defect_report, network_oracle) are **untracked docker-cp working copies**; `develop`
   is a single "Initial commit"; no tracked router imports them. A regulated CISO's TPRM fails on
   rebuild-from-source alone and concludes the headline features "do not exist in the product." It is
   also an execution-discipline *tell* for a security-branded founder. **~1-2 weeks; first use of
   funds; closing condition. Fix this before anything else.**
2. **The proof claim outruns the implementation.** The dispositive real-regression signal reduces
   largely to one regex (`toHaveURL`, test_runs.py:637), and the value oracle is wrapped in
   `.catch(()=>{})` (compiler ~line 566) — a **wrong value can silently pass**. Narrow the claim to
   "proves navigation + refuses regressions" until the semantic/visual/value oracle ships and is measured.
3. **Efficacy is one hand-tuned demo, not a rate.** One 24/24 run on our own app (Aegis, deliberate
   `?break=` modes) after ~80 failures; no third-party SSO/iframe/messy-DOM app touched. Publishing a
   false-heal rate cuts both ways — measure on a controlled third-party pilot **before** any external number.
4. **Air-gap contradiction.** Agentic heal + vision call external LLMs today — the data-residency
   attestation breaks the moment app state/PII leaves the perimeter. The bring-your-own on-prem model
   endpoint (3-6mo) is the unresolved dependency the value-prop rests on; until then, ship with agentic
   default-off (core grounded verdict still works).
5. **Moat is commercial, not yet structural.** The defensible delta is a self-admitted 2-3 quarter
   technical lead; the only compounding moat (flywheel) is **inert** (DP/secure-agg stubbed, zero data).
   For 2-3 quarters you're defended by a behavioral prediction (incumbents are disincentivized to ship
   "evidence mode") + the RFP deal-gate — never pitch the flywheel as a present advantage.
6. **GTM velocity vs burn + single-operator fragility.** Longest-cycle, highest-proof segment;
   2-3 concentrated slow deals; thin 2-person commit history. A slice of the raise is a hiring bet on a
   security/platform #3. Tenant RLS is query-layer only (app role bypasses RLS) — fine for single-tenant
   on-prem, must be closed before any multi-tenant claim.

**The single most important de-risking artifact is not a logo or a feature — it is a
third-party-app-measured false-heal/missed-regression number PLUS a paid design partner who pays from a
compliance budget.** Until both exist, the thesis is a compelling hypothesis on one demo.

---

## First 30 days (the falsifiable, cheap milestones the investor verifies execution on)

- [ ] Commit the four flagship modules to one coherent branch off `develop` with green CI that builds
      the actual differentiators; triage the ~249 untracked root files. *(Closes blocker #1.)*
- [ ] Apply the `proven_control_ledger` + `run_screenshots` migrations; demo seed-before-run
      end-to-end ("one renamed control, dozens of tests inherit the proven fix") with a before/after figure.
- [ ] Widen the oracle past `toHaveURL`: default-on in-page semantic + visual post-state gate; remove
      the silent `.catch(()=>{})` value-oracle hole.
- [ ] Stand up a controlled pilot on a NON-self-owned app; produce the first N≥20 human-confirmed
      false-heal/missed-regression number (not for publication yet).
