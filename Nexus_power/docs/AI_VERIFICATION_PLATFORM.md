# The AI Verification Platform — "Can we trust this automation in production?"

**Status:** Architectural blueprint v1 (2026-07-05) · **Owner:** Nexus QA
**Thesis:** Trust is a **measurement, not a promise.** Every AI assistant on the comparison list *asserts* its output is good; this platform **measures** it — against recorded evidence, against the live application, against runtime history — and refuses to certify what it cannot prove. The unit of value is not a script; it is a **certified script with a reproducible evidence dossier**.

**Legend:** `[LIVE]` deployed in this product today · `[EXTEND]` exists, needs the named upgrade · `[BUILD]` new.

**Prime rules (inherited from the product's trust track, all enforced in code today):**
1. Never green-wash — a verdict the evidence can't support is never emitted; gaps are declared, not hidden. `[LIVE]`
2. Deterministic first — the same input yields the same verdict; LLMs never score themselves (deterministic rubric is the source of truth; the LLM layer explains, proposes, and is verbatim-grounded). `[LIVE]`
3. Every decision explainable + reproducible — inputs, rules, evidence, alternatives, rationale recorded. `[LIVE partial → EXTEND]`
4. Runtime feedback compounds — verified outcomes feed the ledger/calibration so verification gets sharper with every run. `[LIVE]`

---

## 1. End-to-End System Architecture (Deliverable 1)

```
                        ┌───────────────────────────────────────────────┐
   Script + Case IR ───►│            VERIFICATION CORE                  │
   (any source: our     │  Dimension Registry (21 dims, §3)             │
   compiler, human,     │  ├─ Deterministic analyzers  ($0)   [LIVE/EXT]│
   Copilot import)      │  ├─ Evidence cross-checks (recording) [LIVE]  │
                        │  └─ LLM lenses (grounded, optional) [LIVE]    │
                        └──────────────┬────────────────────────────────┘
        ┌──────────────────────────────┼───────────────────────────────────┐
        ▼                              ▼                                   ▼
  LIVE PREFLIGHT [LIVE/EXT]     VERDICT ENGINE                     TEST DATA INTELLIGENCE
  resolve locators/auth/env     score+risk+confidence (§5-7)       tiers T0-T3, gaps,
  against the real app          decision: CERTIFIED/REPAIR/DEFECT  boundaries (§8) [LIVE/EXT]
        │                              │
        │                              ▼
        │                 AUDIT FIDELITY STORE (§10) — inputs, rules,
        │                 evidence, alternatives, rationale; hash-chained [EXT]
        │                              │
        ▼                              ▼
  REGENERATE v+1 (§11)          VERDICT HISTORY (§9) — per-version quality
  patch-channel repair          timeline, trends, regressions, approvals [EXT]
  compiler-emitted only [LIVE]         │
        │                              ▼
        └────────────► AGENTIC QE ORCHESTRATION (§12) — Intent/Context/
                       Sentinel/Verdict/Triage/Healer/Historian/Governor
                       over the runtime (runner + 15-rung healer + oracle
                       + ledger + calibration + false-heal benchmark) [LIVE]
                                       │
                                       ▼
                       CERTIFICATION + GOVERNANCE (§16) — block gate,
                       approvals, RBAC, PII, hash-chain, exports [LIVE/EXT]
```

**The closed loop is the platform:** verify → preflight → run → runtime verdicts → history → regenerate → re-verify. Prompt-only tools have exactly one arrow (generate) and zero loops.

## 2. Component Responsibilities (Deliverable 2)

| Component | Responsibility | Today |
|---|---|---|
| Verification Core | run the 21-dimension registry over (spec, case IR, evidence) | `playwright_auditor.score_spec` D1-D5 + `lint_spec` `[LIVE]` → registry `[EXTEND]` |
| Verdict Engine | fuse dimension scores → quality, risk, confidence, decision | min-gated overall + deterministic decision `[LIVE]` |
| Delivery Gate | block sub-threshold assets from reaching clients | `NEXUS_AUDITOR_GATE=block`, 409 with findings `[LIVE]` |
| Live Preflight | pre-execution validation vs the real app | preflight v2, 9-way locator classification `[LIVE]` → env/auth/data checks `[EXTEND]` |
| Audit Fidelity Store | reproducible decision records | audit endpoint + hash-chained heal_events `[LIVE]` → unified dossier `[EXTEND]` |
| Verdict History | per-version quality timeline | `script_versions` + fidelity scorecard `[LIVE]` → trend/regression engine `[EXTEND]` |
| Regenerate v+1 | patch-based repair through compiler channels | audit→repair endpoint; additive channels (reanchors, nav_overrides, interactions…) `[LIVE]` |
| Test Data Intelligence | validate/extend data tiers | T0 demonstrated / T1 variants / T2 synthetic+approval `[LIVE]` → T3 + gap analysis `[EXTEND]` |
| Agentic QE | specialized agents over the loop | heal analyst, auditor lens, triage design `[LIVE partial]` → full roster `[BUILD/EXTEND]` |
| Runtime substrate | execution + healing + oracles + memory | runner, 15 rungs, universal oracle, ledger, calibration, false-heal benchmark `[LIVE]` |

## 3. Verification Framework — the Dimension Registry (Deliverable 3, 5)

**Architecture rule:** every dimension = a registry entry `{id, axis, deterministic_checks[], evidence_sources[], llm_lens?, weight, hard_gate?}`. Deterministic checks are pure functions (spec text, case IR, evidence rows, preflight results, runtime history) → findings. The LLM lens is optional, evidence-quoted, and can only ADD explanations or PROPOSE — never change a score. `[EXTEND from D1-D5]`

**The 21 dimensions, grouped into 6 axes:**

| Axis | Dimensions | Primary signal | Today |
|---|---|---|---|
| **Correctness** | functional correctness, navigation correctness, assertion correctness | evidence cross-check: every action/assert traceable to recording; impossible-transition detection | `[LIVE]` D3-D5 |
| **Resilience** | locator resilience, synchronization, anti-flakiness, retry safety, parallel safety | rung-count per locator, uniqueness proofs (preflight), sleep/forbidden-API lint, shared-state scan, idempotence scan | `[LIVE]` lint + preflight; parallel/retry scans `[BUILD]` |
| **Intent** | requirement traceability, business intent alignment | case→evidence→(requirement id) chain; Intent Agent compares flow outcome to stated business goal | traceability chain `[LIVE partial]`; intent lens `[BUILD]` |
| **Data** | test data quality | tier coverage, boundary gaps, dependency detection (§8) | `[LIVE/EXTEND]` |
| **Operability** | diagnostics, logging, CI/CD readiness, performance, determinism | diagnostics schema presence, reporter wiring, config lint, run-time budget, byte-stable regen check | schema+reporter `[LIVE]`; determinism check = re-compile hash equality `[BUILD small]` |
| **Enterprise** | accessibility, security, maintainability, readability, governance | a11y lane, secret/PII scan, POM usage, naming/length lint, approval hooks | a11y+PII rules `[LIVE partial]` |

**Finding schema (every finding, machine-readable)** `[EXTEND — score_spec findings gain fields]`:
```json
{
  "dimension": "locator_resilience",
  "severity": "blocker|major|minor|advisory",
  "confidence": 0.92,                       // §5 model
  "evidence": [{"type": "spec_line|case_step|recording_ref|preflight|run_history", "ref": "..."}],
  "root_cause": "single-signal locator; label absent from a11y tree at preflight",
  "recommendation": "adopt anchor-bundle rung 2 (getByRole+name)",
  "estimated_impact": {"flake_risk": "high", "maintenance_cost": "medium", "blast_radius": "1 step"},
  "safe_remediation": {"channel": "reanchors", "patch_ref": "...", "auto_appliable": true, "requires_approval": false}
}
```
`safe_remediation` is always a **compiler-channel patch** (§11) — never freehand LLM code.

## 4. Core Capability 1 — Verify with AI

Pipeline per asset: **deterministic registry pass ($0) → evidence cross-check → optional LLM lenses (deep=1) → verdict fusion**. The LLM layer mirrors the existing auditor contract `[LIVE]`: every claim must carry a VERBATIM evidence quote or it is demoted to `mark_unproven`; on any LLM fault the deterministic verdict stands; the LLM can never certify. Third-party scripts (Copilot/human-written) verify through the same registry — evidence-dependent dimensions degrade honestly to "unverifiable: no recording evidence" instead of guessing `[BUILD: import adapter]`. That degradation IS a differentiator: we tell customers which trust claims are possible for unevidenced automation.

## 5. Confidence Scoring Model (Deliverable 4)

`confidence(finding) = w_sig·signal_count + w_ind·independence + w_hist·historical_precision − p_amb·ambiguity` `[EXTEND of locator-confidence + calibration]`
- signal_count: independent detectors agreeing (lint + preflight + runtime history)
- independence: signals from different sources (static/live/runtime) weigh more than three static rules
- historical_precision: per-check precision measured on the labeled benchmark corpus `[LIVE harness]` — checks that historically over-fire are down-weighted automatically (mirrors heal calibration `[LIVE]`)
- Calibration loop: every finding later confirmed/refuted by a run or a human updates the check's precision — ECE tracked like heal rungs `[EXTEND]`

## 6. Quality + Risk Scoring (Deliverables 5, 6)

- **Quality** = min-gated axis fusion `[LIVE pattern]`: `overall = min(round(mean(axes)), min(hard_gated_axes))` — one blocker sinks the score; cosmetic axes can't buy back a correctness failure.
- **Risk** (production-impact lens) `[BUILD]`: `risk = likelihood × blast_radius × detectability⁻¹` per script — likelihood from flake history + locator confidence + preflight ambiguity; blast_radius from journey-graph coverage (how many downstream pages a failure hides `[LIVE graph]`); detectability from assertion density on the affected pages. Output: LOW/MEDIUM/HIGH + top-3 drivers, each evidence-linked.
- **Certification levels:** `CERTIFIED-EVIDENCED` (recording-grounded, ≥9, preflight-clean) · `CERTIFIED-STATIC` (no recording; static+preflight only — honest ceiling) · `REPAIR` · `DEFECT` (app bug reproduced `[LIVE defect_report]`).

## 7. Explainability Framework (Deliverable 7)

Layered, per audience `[EXTEND]`: **one-line verdict** (exec) → **per-dimension scorecard with reasons** (QA lead — the UI panel that exists today `[LIVE]`) → **per-step verdict trail** (engineer, ✓/✕/ℹ with evidence refs `[LIVE]`) → **decision dossier** (auditor, §10). Vocabulary is fixed and versioned (grounded_ok, impossible_transition, missing_prerequisite, data_not_replayed, documented_exemption… `[LIVE]`) so explanations are stable across releases and machine-consumable.

## 8. Core Capability — Test Data Intelligence

- Tier model `[LIVE]`: T0 demonstrated (recorded values, per-test data files) · T1 demonstrated-alternates (variant cases) · T2 synthetic boundary/invalid (derived from observed formats, `requires_approval:true`, never auto-wired) · T3 dependency-aware `[BUILD]` (unique-per-run identities, duplicate probes, cross-field constraints learned from observed formats).
- **Gap analysis** `[BUILD]`: for each field: which equivalence classes are demonstrated / synthesized / missing → "data coverage %" per script + recommended business-valid sets, each labeled by tier and provenance.
- Validation: data files linted against observed masks (currency/date/locale) `[EXTEND]`; PII synthesis prohibited from real captured values `[LIVE rule]`.

## 9. Core Capability — Verdict History (Deliverable 9)

Versioning strategy `[EXTEND of script_versions]`:
- Every verify/run/regenerate appends an immutable `verdict_event` keyed to (script, version): scores per axis, decision, findings hash, evidence-dossier ref, actor, gate mode.
- **Timeline view**: quality trend per script/suite; regression detector = any axis drop > threshold between versions → auto-flag + block optional (uses the same benchmark-gate mechanics `[LIVE]`).
- **Approval workflow**: version transitions (draft → reviewed → approved → certified) with RBAC roles `[LIVE _rbac_gate]` + audit rows `[LIVE AuditLogRow]`; approved versions are pinned for runs (exists: "Save creates a new version; runs use the latest" → extend with pin-to-approved policy `[EXTEND]`).
- Retention: verdicts never deleted; superseded versions keep their dossiers (Part-11 posture `[LIVE hash-chain pattern]`).

## 10. Core Capability — Audit Fidelity (Deliverable 8)

**Decision dossier** per verdict `[EXTEND — unify existing pieces]`:
```
{inputs: {spec_hash, case_ir_hash, evidence_snapshot_ref, registry_version, env},
 rules_applied: [check ids + versions], evidence: [verbatim quotes/refs],
 confidence: per-finding, alternatives_considered: [e.g. locator rungs rejected + why],
 final_rationale: template-rendered from the above (never free prose),
 chain: sha256(prev_dossier + this) }
```
Reproducibility guarantee: registry is versioned + deterministic ⇒ replaying inputs at the same version reproduces the verdict byte-for-byte. LLM-lens outputs are stored but marked non-authoritative. Hash-chaining mirrors heal_events `[LIVE]`. This is the artifact a regulated buyer's auditor actually accepts — no prompt-tool can produce it.

## 11. Core Capability — Regenerate (Version +1) (Deliverable 9-adjacent)

Patch-based by construction `[LIVE — this is the existing repair architecture]`: findings map to **compiler channels** (reanchors, nav_overrides, interactions, stabilize, pre_advance, phantom_skips, force_open_shadow…) — the repaired spec is **always compiler-emitted**; no LLM string is ever spliced into a .spec.ts. Therefore: untouched sections are byte-identical (idempotent emission `[LIVE]`), diffs are minimal and reviewable, review history survives, and every patch links finding → channel → new version in the dossier. Human edits remain first-class: saved versions override compilation in delivery `[LIVE]` and are re-verified by the same gate `[LIVE]`.

## 12. Core Capability — Live Preflight

Existing engine `[LIVE]`: per-step locator resolution against the real app with 9-way classification (unique/ambiguous/absent/…) feeding the auditor + healing metadata. **Extend to readiness matrix** `[EXTEND]`: environment reachability, auth-profile validity (expired storage-state detection — relogin machinery exists `[LIVE]`), test-data file resolution (D-keys present), dependency/API health probes (network-oracle endpoints ping), browser/project matrix sanity, feature-flag divergence (preflight DOM vs recorded evidence delta), execution-budget estimate. Output: READY / DEGRADED(reasons) / BLOCKED(reasons) — wired as an optional pre-run gate in autopilot `[EXTEND]`.

## 13. Core Capability — Agentic QE Platform (Deliverable 10)

**Roster** (each agent = narrow mandate, structured I/O, confidence attached to every claim):

| Agent | Mandate | Grounding | Today |
|---|---|---|---|
| **Intent** | recover business intent from evidence (journey + values + outcome); flag intent-drift between script and demonstrated goal | recording + case IR | `[BUILD]` (data-carry + journey graph feed it `[LIVE]`) |
| **Context** | own app knowledge: journey graph, ledger priors, page/control inventory (POM), auth model | ledger + graph + POM `[LIVE]` | assembler `[EXTEND]` |
| **Sentinel** | watch runtime: flake trends, SLO breaches, drift between preflight and last-known DOM | trust-SLO + History&flake `[LIVE]` | correlator `[EXTEND]` |
| **Verdict** | fuse deterministic scores + lenses into decisions; owns certification | auditor + gate `[LIVE]` | |
| **Triage** | product-defect vs script vs environment vs data classification on failure | Agentic-QE reasoner design + defect_report `[LIVE partial]` | `[EXTEND]` |
| **Healer** | propose grounded repairs via rungs/channels; never ungrounded | 15 rungs + validate_fixes `[LIVE]` | |
| **Historian** | calibration: per-check/per-rung precision, ECE, threshold updates | heal_calibration `[LIVE]` → generalize `[EXTEND]` |
| **Governor** | approvals, PII/secret enforcement, RBAC, export policy | RBAC+audit `[LIVE]` → policy engine `[EXTEND]` |
| **Data** | tier coverage, gap analysis, dependency detection | §8 | `[EXTEND/BUILD]` |

**Collaboration protocol:** blackboard model — agents read/write typed claims `{claim, evidence_refs, confidence, agent, ttl}` on a shared per-asset board; no agent consumes another's claim without its evidence refs. **Conflict resolution:** deterministic precedence (Governor > Verdict > Triage > others) + rule "lower-confidence claim yields; ties escalate". **Escalation:** any blocker-severity claim below confidence 0.7, any Healer/Triage disagreement, and any Governor veto → human queue with the full dossier. **Confidence sharing:** all confidences pass through Historian calibration before fusion — an agent's raw self-confidence is never used directly (the anti-LLM-overconfidence rule).

## 14. Data Model (Deliverable 11) `[EXTEND — most tables exist]`

Existing: `factory_test_cases`, `script_versions`, `page_visits/page_actions/form_snapshots` (evidence), `heal_events` (hash-chained), `proven_control_ledger`, `audit_log`, run/job rows, benchmark keys.
New: `verdict_events(id, script_id, version, registry_version, axes jsonb, overall, risk, decision, dossier_ref, actor, created_at)` · `decision_dossiers(id, chain_hash, payload jsonb)` · `findings(id, verdict_id, dimension, severity, confidence, evidence jsonb, remediation jsonb, status: open|fixed|waived(approval_ref))` · `agent_claims(board_id, agent, claim jsonb, confidence, evidence jsonb)` · `data_coverage(script_id, field, tier_map jsonb, gaps jsonb)`.

## 15. APIs (Deliverable 12)

Existing `[LIVE]`: `POST /scripts/{id}/audit(?deep)` · `POST /scripts/{id}/audit/repair` · `POST /scripts/{id}/preflight` · `GET /playwright` (block-gated, report-in-zip) · `GET /playwright/manifest` (anchors, provenance, data_carry) · `GET /pages-forms/scorecard` · versions CRUD · heal-audit/trust-SLO/heal-benchmark.
New `[BUILD]`: `POST /verify` (any script + optional evidence → verdict+dossier) · `GET /scripts/{id}/verdicts?timeline=1` · `POST /scripts/{id}/certify` (approval transition) · `GET /scripts/{id}/risk` · `POST /preflight/readiness` (full matrix) · `GET /data-coverage/{id}` · `POST /waivers` (governed exception with expiry).

## 16. UI Concepts (Deliverable 13)

Extend the existing AI Audit panel `[LIVE]` into a **Trust Center** per script: verdict header (decision + level + risk with drivers), axis radar, per-step trail (✓/✕/ℹ already shipping), **verdict timeline** (versions × quality sparkline, regression markers, approval badges), dossier viewer (expandable rules/evidence/alternatives), remediation queue (one-click channel patches → v+1 diff view), preflight readiness board, data-coverage heat strip per field. Suite level: certification dashboard (percent certified-evidenced, risk distribution, trend), Sentinel feed.

## 17. Security Model (Deliverable 14) `[LIVE base → EXTEND]`

Tenant RLS everywhere; RBAC (`_PRIVILEGED`) on verify/certify/waive; KMS-envelope encryption for auth profiles; RUNNER_TOKEN service auth; secrets never in specs (env refs only); PII masking in dossiers/exports (password values never captured `[LIVE]`); hash-chained dossiers for tamper-evidence; export = dossier + report bundle with redaction profile; LLM lenses receive evidence excerpts only (no secrets/PII), logged.

## 18. Performance Model (Deliverable 15)

Deterministic registry: pure functions, ~ms/script, parallel per-asset; $0 LLM at steady state (lenses opt-in + cacheable by (spec_hash, registry_version) `[EXTEND]`). Preflight: one browser context per suite, locator probes batched `[LIVE engine]`. Verdict history: append-only, indexed by (script, version). Targets: verify ≤2s p50 static, ≤60s with preflight; suite of 100 scripts ≤5min full-trust pass; dossier write ≤50ms.

## 19. Enterprise Governance (Deliverable 16)

Certification levels + approval workflow (§6, §9); waivers with owner+expiry+reason (never silent) `[BUILD]`; block gate in delivery `[LIVE]` and CI (`--gate` benchmark pattern `[LIVE]`); Part-11-style immutable trails `[LIVE pattern]`; separation of duties (author ≠ certifier enforced by Governor); data residency: all verification on-prem/VPC (no external calls required — deterministic core) `[LIVE posture]`; regulated-buyer pack: dossier export + registry version manifest + benchmark accuracy statement `[EXTEND]`.

## 20. Competitive Analysis

| Capability | Copilot/ChatGPT/Claude/Gemini/Cursor/Windsurf/Qodo | Commercial platforms (Mabl/Testim/Functionize/testRigor…) | This platform |
|---|---|---|---|
| Independent verification layer | none (author = judge) | partial (run results only) | full registry + gate, author ≠ judge |
| Evidence grounding | none | DOM-recorder telemetry (requires instrumentation) | video-recording evidence, zero install `[LIVE]` |
| Deterministic, reproducible verdicts | no (sampling) | opaque proprietary scoring | versioned registry, byte-reproducible dossiers |
| Refuses to certify (honest ceiling) | never refuses | rarely | CERTIFIED-STATIC vs -EVIDENCED distinction; UNPROVEN declared |
| Live preflight vs real app | no | partial | 9-way locator classification + readiness matrix `[LIVE/EXT]` |
| Patch-based regen preserving history | regenerates wholesale | limited | compiler-channel patches, byte-stable untouched code `[LIVE]` |
| Runtime feedback loop | none | heal w/o false-heal control | ledger+calibration+false-heal benchmark <1% `[LIVE]` |
| Audit dossier for regulators | none | none public | hash-chained, replayable `[EXT]` |
**Hard to replicate with prompt-only AI:** everything above requires an evidence substrate, a runtime, versioned deterministic analyzers, and longitudinal memory — none of which a prompt has. Copying requires abandoning the prompt-tool architecture.

## 21. Roadmap (Deliverable 17) — leverage-ordered, deploy-first

1. **Unify what exists into `/verify` + verdict_events** (registry wrapper around auditor+lint+preflight; timeline API/UI) — mostly wiring.
2. **Decision dossiers** (schema + hash-chain + replay test) — extends audit endpoint output.
3. **Risk model + certification levels** (journey-graph blast radius; CERTIFIED-EVIDENCED/STATIC).
4. **Preflight readiness matrix** (auth/data/env probes on the live engine).
5. **Findings→channel remediation queue UI** (repair loop exists; expose per-finding one-click v+1).
6. **Historian generalization** (per-check calibration on the benchmark corpus; auto-down-weight noisy checks).
7. **Import adapter** for third-party scripts (honest degradation labeling).
8. **Data gap analysis (T3) + waiver governance + Sentinel correlator + Intent Agent.**

## 22. Success Metrics & KPIs (Deliverable 18) — all measured, benchmark-gated `[LIVE mechanics]`

Certification precision (certified scripts' first-run green rate — target ≥95%); false-certification rate (certified yet failing for script-fault reasons — target <1%, measured like false-heal `[LIVE]`); finding precision/recall per check (labeled corpus); mean time-to-trust (upload→certified); repair acceptance rate (channel patches merged without edit); verdict reproducibility (replay hash equality = 100%); % assets with dossiers; risk-model calibration (HIGH-risk scripts' incident rate vs LOW); LLM cost/verify (≈$0 default); trend: certified-evidenced share of estate.

## 23. Risks & Mitigations (Deliverable 19)

Registry rule drift vs Playwright versions → versioned registry + corpus gate on upgrade. Check over-firing erodes trust → Historian calibration + waiver analytics. Evidence-less imports disappoint → explicit STATIC ceiling messaging (sales asset, not weakness). Preflight needs env access → degrade to STATIC with named reason. Dossier PII → redaction profiles + Governor scan. Agent sprawl/conflict → blackboard + precedence + escalation (§13); agents never act outside channels. Performance at 10k assets → pure-function parallelism + verdict caching by hash.

## 24. Future Innovations (Deliverable 20)

Differential replay certification (video vs run-trace behavioral equivalence score); formal flow verification (journey-graph model checking: unreachable/contradictory steps proven, not sampled); counter-example-guided auto-repair (failing trace → minimal channel patch synthesis); federated verification priors (k-anon cross-tenant check-precision sharing `[LIVE substrate]`); insurance-grade certification (published accuracy + false-cert rates per release — the benchmark makes this signable); chaos preflight (throttled/latency-injected preflight predicting flake before CI); a11y certification lane (WCAG path proofs from the a11y-first locators); LLM-cost-zero guarantee tier (contractual deterministic-only mode for air-gapped buyers).

---
**Bottom line:** generation makes a script; **this platform makes it believable** — measured against evidence, proven against the live app, tracked across versions, explained to auditors, improved through governed patches, and certified only when the numbers allow. That sentence is the enterprise sale, and no prompt-tool can say it.
