# Execution Evidence Report — Canonical Requirements & Build Plan

**Status:** FOUNDER-APPROVED DRAFT v1.0 (2026-07-25)
**Category claim:** this report is the productization of the **Test Evidence
System-of-Record** — the audit-grade *Certificate of Execution*. It is not "a
nicer report"; it is the artifact that IS the product category.
**Supersedes:** the raw requirement draft of 2026-07-25 (all 17 sections are
incorporated here, amended by the doctrine fixes below).

---

## 0. Doctrine (non-negotiable, governs every section)

These four amendments correct the raw draft and are binding on any
implementation:

* **D1 — No fabricated precision.** A numeric confidence (e.g. "99.4%") may be
  displayed ONLY when it comes from a calibrated, measured source (e.g. the
  auto-heal calibration curves). Everywhere else the report shows the honest
  evidence class the platform already computes: **PROVEN / INFERRED /
  UNVERIFIED**, with the grounding evidence quoted. Grounded-or-UNVERIFIED
  governs the report exactly as it governs generation.
* **D2 — No unverifiable sentence.** Every claim in the report is a link to raw
  evidence (a step row, a screenshot, a network entry, a ledger event).
  Deterministic verdict text for PASSED steps (from the oracle itself); AI
  reasoning appears ONLY on failures / needs-review, always quoting evidence.
  No per-step LLM narration (cost, latency, and N chances to hallucinate in an
  audit document).
* **D3 — AI suggests, humans assert.** Severity, Priority, Component Owner and
  Fix Area on a defect are visually marked *suggested-by-AI* until a human
  confirms. The suggested→confirmed transition is a recorded audit event.
* **D4 — No lone green badge.** Any rollup (crawl, flow, case) always renders
  the full count triplet — passed / defect / execution-error / blocked /
  needs-review / skipped / cancelled — never a single "PASSED" badge. A
  skipped-counted-as-green display is a doctrine breach (this exact bug shipped
  twice; never again).

Standing platform doctrine also applies: never-green-wash; execution errors are
NEVER product defects (Attribution Engine, "never blame the app"); certification
runs never mix into client-facing stats.

---

## 1. Status State Machine (precise, deterministic)

### 1.1 Step-level display status

Derived deterministically from `test_run_step.status` + the Attribution Engine
class for that step. No LLM in the mapping.

| DB `status` | Attribution class            | Display status      |
|-------------|------------------------------|---------------------|
| `passed`    | —                            | **Passed**          |
| `failed`    | product-side (any rung)      | **Defect Found**    |
| `failed`    | script / locator / framework | **Execution Error** |
| `failed`    | environment / infra / auth   | **Execution Error** (sub-badge: Environment) |
| `failed`    | not-yet-attributed           | **Needs Review**    |
| `skipped`   | precondition step failed     | **Blocked**         |
| `skipped`   | user/config exclusion        | **Skipped**         |

Rules:
* `failed` NEVER displays as anything but Defect Found / Execution Error /
  Needs Review. There is no "soft fail".
* A step may carry at most ONE display status. Precedence within a step:
  attribution class wins; absent attribution ⇒ Needs Review (fail-closed
  toward human attention, never toward green).

### 1.2 Case-level display status

Rollup of its steps, first match wins (top-down precedence):

1. any step **Defect Found** → case = **Completed with Defects** (if the case
   ran to its final step) or **Defect Found — Halted** (if execution stopped at
   the defect).
2. any step **Execution Error** → case = **Execution Error**.
3. any step **Needs Review** → case = **Needs Review**.
4. any step **Blocked** → case = **Blocked**.
5. run interrupted before the case executed any step → **Cancelled**.
6. all steps skipped by config → **Skipped**.
7. all steps passed → **Passed**.

* "Completed with Defects" is a **success of the product** (the suite ran to
  completion and caught a real defect) and is styled as such — distinct color
  from Execution Error, never a red X.
* Quarantined / uncertified-exploratory cases excluded by the run-gate appear
  in the report as **Skipped** with the gate reason quoted (they were never
  executed; the gate is part of the trust story, §2.0).

### 1.3 Crawl-level

Always the count triplet per D4 + the derived rates (§2.14). No single badge.

---

## 2. Report Structure

Sections keep the raw draft's numbering; **§2.0 is new and mandatory**.

### 2.0 Trust Block (NEW — the differentiator; opens every report)

* Certification: link to the suite's certification run — *"this suite was
  proven on the baseline before judging your application"* — with its own
  passed/failed/skipped triplet.
* Quarantine: cases currently quarantined + the certified reason each is held.
* Exploratory gate: combinations not yet certified (and therefore not run).
* Oracle scorecard: % of assertions PROVEN vs INFERRED vs UNVERIFIED
  (`compute_artifact_scorecard` — exists today).
* Escape→guard registry status: escapes recorded, guards added.
* Product-fault metric (north star): client-visible product-fault count vs
  certification catches, trailing window.

No competitor can print this section. It is the moat, rendered.

### 2.1 Crawl Execution Summary
As drafted (name, IDs, app, URL, environment, browser, OS, device, times,
duration, executor, pages crawled, flows discovered, cases generated/executed)
**plus**: generator version, compiler version, run-gate decisions count.
Metrics rendered per D4 (full triplet, no lone badge).

### 2.2 User Flow Summary
As drafted. Each flow: case count, count triplet, duration, pass %, defect
count. Expandable to its cases.

### 2.3 Individual Test Case Report
As drafted (ID, name, functional area, business requirement, priority, risk,
generated-by, status, start/end, duration, retry count) **plus** the
**Reproducibility block** (NEW, per case):
* script version + generator/compiler versions,
* data values used (from `vkpower.data.json` two-tier resolution),
* environment profile + base URL, auth mode (form-login / imported session),
* one-click "download the exact bundle that produced this result".
An audit finding you can't reproduce is an anecdote. We own the whole chain;
this is cheap for us and impossible for screenshot-only tools.

### 2.4 Step-Level Execution Details
As drafted (step number, action, target, expected, actual, time, duration,
status per §1.1). Expected result shows its **oracle provenance**: the recorded
demonstration evidence it is grounded in (scene/control/edge ids — already on
`test_run_step`).

### 2.5 AI Analysis (amended by D1/D2)
* PASSED steps: deterministic oracle text only. No LLM.
* FAILED / Needs-Review steps: attribution ladder verdict with quoted evidence
  (rung, matched signal, excerpt), evidence class (PROVEN/INFERRED/UNVERIFIED),
  business/technical impact, recommendation.
* Numeric confidence only where a calibrated source exists (D1).

### 2.6 Defect Details (amended by D3 + dedup)
As drafted (title, description, expected, actual, rule violated, severity,
priority, root cause, first failed step, timestamp, owner, fix area) **plus**:
* **Defect identity & lifecycle (NEW):** a stable defect signature
  (scenario_id + step + oracle + failure fingerprint). The same signature
  across N runs is ONE defect with N occurrences, first-seen / last-seen /
  status (open, fixed-verified, regressed). Without dedup, defect counts
  inflate and credibility deflates.
* Severity/Priority/Owner marked *suggested* until confirmed (D3).
* Link to escape→guard registry entry when applicable.

### 2.7 Execution Error Details
As drafted (locator not found, browser crash, network, timeout, test data,
environment, auth expired, framework exception) — sourced from the Attribution
Engine's non-product rungs. NEVER counted as product defects (standing
doctrine, already enforced in `quarantine_decision`).

### 2.8–2.10 Evidence Capture (amended: tiering + trace-first)

**Evidence Tiering Policy (NEW, precise):**

| Tier | When | What is captured |
|------|------|------------------|
| **T0 — always** | every run | per-step status/selector/error rows; failure screenshot; final-page screenshot; run video config flag honored |
| **T1 — certification & on-demand** | cert runs; user re-run with "full evidence" | before/after screenshots per step, highlighted target element |
| **T2 — trace** | failures always; full runs opt-in | **Playwright `trace.zip`** — DOM snapshots, network, console, screencast, time-travel replay in one artifact |
| **T3 — deep diagnostics** | explicit opt-in | HAR, full HTML source per step, accessibility scan, performance metrics |

Rationale: before/after shots on every step ≈ 2× steps in screenshots per run
(a 47-case cert ≈ 1,700 images) — runner time + storage. `trace.zip` natively
covers §8+§9+§10 (DOM, network, console, video, step timestamps, replay-from-
step) at near-zero engineering cost; the report embeds the trace viewer. Video
replay-from-step (raw draft §9) is satisfied by the trace screencast.

Every artifact links to its step (already the schema: `e2e_run_screenshots`
keyed by run/step).

### 2.11 Execution Timeline
As drafted — chronological events; sourced from step timestamps + runner job
lifecycle + ledger events. No new capture needed.

### 2.12 Navigation Hierarchy
As drafted: Crawl → Flow → Case → Step → {Analysis, Evidence, Logs, Trace}.
Expand/collapse without leaving the report.

### 2.13 Search & Filters
As drafted, plus filter by: attribution class, evidence class
(PROVEN/INFERRED/UNVERIFIED), defect lifecycle state, certified vs
uncertified, environment profile.

### 2.14 Report Dashboard
As drafted (pass rate, defect rate, error rate, blocked, avg/longest times,
slowest step, most frequent defect, most common error, top impacted modules)
**minus** "AI Confidence Distribution" (D1 — replaced by *evidence-class
distribution*), **plus**:
* **Run-over-run diff (NEW):** what changed since the previous report — new
  defects, fixed defects, new flake, coverage delta. Executives and auditors
  read the delta, not 900 steps.
* Product-fault metric trend (exists today: `/quality/product-faults`).

### 2.15 Coverage Honesty (NEW)
What was crawled but NOT tested: pages with zero cases, flows not exercised,
combinations generated-but-not-run (gate reasons), fields never asserted.
Never-green-wash applied to scope — and disarming in a sales demo.

### 2.16 Export Options (amended: priority + governance)
* **Wave 1:** interactive HTML (self-contained), ZIP package, **signed verdict
  JSON** (machine-readable: statuses, attributions, evidence classes, defect
  signatures — so CI gates and dashboards consume the same truth humans read;
  JUnit XML is lossy and cannot carry attribution).
* **Wave 2:** PDF.
* **Wave 3:** Excel/CSV/JUnit-XML (commodity).
* **Export governance (NEW):** exports contain screenshots + network logs =
  PII egress. RBAC on export; redaction rules (configurable field/URL masks);
  watermark "exported by {user} at {time}"; export itself is an audit event.

### 2.17 Audit Trail (amended: mechanism, not adjective)
"Immutable" is implemented, not asserted:
* hash-chained event log (each event carries SHA-256 of predecessor) —
  extends the existing Part-11 heal ledger pattern (`heal_evidence`);
* signed export manifest: SHA-256 per artifact + chain root, verifiable
  offline;
* recorded events: execution start/end, every status change, every AI
  suggestion, every human confirmation (D3), evidence creation, report
  generation, every export (§2.16).
* map controls explicitly to 21 CFR Part 11 / SOC2 language (SOC2_CONTROLS.md).

### 2.18 Needs-Review Workflow (NEW — status ⇒ workflow)
A queue, not a label: assignee, disposition (confirm-defect / reclassify /
retest), e-signature for regulated tenants, recorded in the audit trail.
Closes the D3 loop; answers "who signed off?".

---

## 3. Acceptance Criteria (amended)

AC-1..AC-15 of the raw draft stand, with these edits:
* AC-6 adds: rollups always render full count triplets (D4).
* AC-7 adds: confidence shown only when calibrated; else evidence class (D1).
* AC-11 (video replay-from-step) is satisfied by embedded trace viewer.
* AC-15 adds: hash-chain + signed manifest verifiable offline (§2.17).
* **AC-16 (new):** report opens with the Trust Block (§2.0).
* **AC-17 (new):** every case carries a Reproducibility block (§2.3).
* **AC-18 (new):** identical defect signatures dedup across runs (§2.6).
* **AC-19 (new):** report generation is async and cached; run completion is
  never blocked on report rendering.
* **AC-20 (new):** exports enforce RBAC + redaction + watermark (§2.16).

---

## 4. Exists-Today Mapping (EXTEND, don't rebuild)

| Requirement | Existing component | Status |
|---|---|---|
| Crawl→flow→case→step data | `test_run` / `test_run_step` / `factory_test_cases` / substrate | LIVE |
| Per-step evidence keys | `test_run_step` (selector, error, evidence ids, screenshot) | LIVE |
| Screenshots at rest | `e2e_run_screenshots` (bytea, per run/step) | LIVE |
| Attribution (product vs script vs env) | `attribution_engine.py` (9-rung, evidence-quoted) | LIVE |
| Cert / quarantine / exploratory gate | `test_runs.py` decisions + run-gates | LIVE |
| Oracle provenance | `oracle_scorecard.compute_artifact_scorecard` | LIVE |
| Product-fault metric | `/quality/product-faults` endpoint | LIVE |
| Defect authoring | `defect_report.py` (repro + bug on real regression) | LIVE (needs dedup/lifecycle) |
| Part-11 ledger pattern | `heal_evidence.record_heal_event` | LIVE (needs hash-chain + scope widening) |
| Delivery integrations | qTest/TestRail/Zephyr/Xray/Excel factory delivery | LIVE |
| Timeline data | step timestamps + runner job lifecycle | LIVE (no UI) |
| Trace capture | Playwright supports natively | NOT WIRED |
| Unified report surface | — | NEW |
| Signed verdict JSON / manifest | — | NEW |
| Defect dedup + lifecycle | — | NEW |
| Review workflow + e-sign | — | NEW |
| Run-over-run diff | — | NEW |
| Coverage honesty view | — | NEW |
| Export governance (RBAC/redaction/watermark) | — | NEW |

≈60% of the data plumbing is live. The new work is the surface + signing +
lifecycle + workflow + trace.

---

## 5. Phased Build Plan

Ordering principle: ship the differentiators first (Trust Block, honest
statuses, evidence links), commodity exports last. Each phase is
demoable and never-green-wash-auditable on its own.

### Phase R1 — The Honest Report Core
* Report data assembler (async, cached; AC-19): one endpoint materializing
  Crawl→Flow→Case→Step with §1 state machine applied.
* §2.0 Trust Block + §2.1 summary + §2.2 flows + §2.3 cases + §2.4 steps.
* D4 triplet rendering everywhere; step evidence links (screenshots, errors).
* Interactive HTML export (self-contained).
**Exit proof:** report for artifact 574ce778 renders 47 cases / 855 steps with
zero unverifiable claims; every number click-through resolves to a DB row.

### Phase R2 — Trace + Defects with Identity
* Playwright trace capture per tiering policy (T2), trace viewer embedded;
  replay-from-step satisfied.
* Defect signature + dedup + lifecycle (§2.6); Needs-Review queue (§2.18,
  minimal: assign + disposition, e-sign deferred).
* Run-over-run diff (§2.14) + Coverage honesty (§2.15).
**Exit proof:** induce one defect twice → ONE defect, two occurrences; diff
view shows it as "new" then "recurring".

### Phase R3 — Audit-Grade
* Hash-chained event log + signed export manifest (§2.17); offline verifier
  script shipped with the export.
* Signed verdict JSON schema v1; ZIP package export.
* Export governance: RBAC, redaction rules, watermark, export-as-audit-event.
* E-signature on review dispositions (regulated tenants).
**Exit proof:** tamper 1 byte in an exported ZIP → verifier fails loudly.

### Phase R4 — Reach
* PDF export; Excel/CSV/JUnit-XML.
* Dashboard analytics (§2.14 full set), evidence-class distribution.
* Search/filter full matrix (§2.13); performance/a11y T3 capture opt-ins.

Deferred (explicitly): cross-tenant benchmarking, BI connectors, scheduled
report emailing — post-adoption features, not category proof.

---

## 6. Open Decisions (founder)

1. Trace retention window (traces are MBs; propose 30 days hot, then archive
   with hash retained in the chain).
2. Redaction defaults: mask all input values in exports, or opt-in per field?
   (Propose: mask credential-kind fields always; others configurable.)
3. e-sign mechanism: platform-native click-sign vs delegated (SSO assertion)?
