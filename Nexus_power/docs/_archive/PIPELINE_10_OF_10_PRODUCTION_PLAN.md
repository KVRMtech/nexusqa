# Nexus QA — Whole-Pipeline 10/10 + Production-Ready Plan

**Date:** 2026-06-29. **Status:** PLAN (planning only — no implementation in this doc).
**Goal:** take all six pipeline stages from a verified ~4/10 to a defensible **10/10**, **production-ready for deployment at 1,000+ apps and 100+ clients**, pure generic / no hardcoding, never-green-wash.
**Companion docs:** [CANONICAL_TRUST_BASELINE_M1.md](CANONICAL_TRUST_BASELINE_M1.md) (the canonical-stage Milestone 1 detail).

---

## The one diagnosis that explains all six 4/10s
Every stage has the **same shape**: genuinely good engineering, **no correctness oracle at the handoff**, and **the validators that exist aren't wired into the default path**. So the master fix is the same pattern applied at every handoff:

> **MEASURE → GATE → GROUND every fact (provenance) → CONSENSUS where one signal isn't enough → SCALE/OPS for production.**

This is a **wiring + hardening job, not a rewrite.** The hard engineering is largely done; it's ungated and unmeasured.

## What "10/10" means (honest + achievable, same as canonical)
Not "perfect pixels" (a video-only asymptote). **10/10 = verified trustworthiness on every handoff:**
1. **Faithfulness** — never fabricate; fabrication-rate ≈ 0 at the automation threshold.
2. **Completeness-honest** — every visible fact is a confident row OR an explicit flagged placeholder; **zero silent drops.**
3. **Calibration** — a 0.9 is right ~90% of the time (measured, not asserted).
4. **Genericity** — same scores on a held-out whole-app slice; CI fails on any literal app host/name.
5. **Closed-loop** — the autonomous path produces a **real green pass** when the app behaves, or an **honest stop / grounded defect** when it doesn't. Never a fake green.
Plus the dual-regime accuracy claim: 10/10 trustworthiness on video-only for **all** apps; 10/10 raw accuracy on the optional, additive ground-truth-overlay subset; every row labels its regime.

## What "production-ready for 1,000+ apps / 100+ clients" means
- **Multi-tenant isolation** (RLS, per-tenant queues, no cross-tenant leakage)
- **Throughput**: per-tenant concurrency limits, backpressure, retries, **dead-letter queue**, fair scheduling
- **SLOs**: bounded p95 latency, bounded cost (LLM calls/artifact ceiling), every pass fail-open to deterministic
- **Observability**: per-tenant dashboards, failure-rate + calibration-drift alerting, run/heal/defect ledgers
- **Governance**: RBAC, immutable audit, approval lifecycle, version-promotion + rollback, Part-11-grade evidence
- **Deploy**: on-prem packaging, secrets/KMS, security review, reproducible builds, no deploy divergence

---

# The phased roadmap (each phase has a MEASURABLE exit gate)

## Phase 0 — Unblock + stop the worst green-wash (days, ~$0, default-off)
*Prove the autonomous loop can close NOW, and plug the holes that can silently lie.*
- **Stage 4:** carry the product disambiguator into the locator (generic anchor/scope derivation, not a saucedemo special-case) → kills the 6-way ambiguity. *(Proven: 1 match vs 6.)*
- **Stage 4:** wire the existing `playwright_auditor` as a **gate** on the default generate/compile path.
- **Stage 4:** add a **locator-uniqueness dimension** to the auditor (its current blind spot); audit the `.first()` paths that could silently click the wrong element.
- **Stage 6:** wire `is_base_host_connection_failure` so an env outage stops filing a defect against the customer's app.
- **Ops:** resolve the **deploy divergence** (the advanced heal loop may not be live on the VM) — one source of truth.
- **Exit gate:** Autopilot produces its **first real green pass** on a clean recording (or an honest stop); auditor blocks an impossible-transition/ambiguous script in CI; no env-outage defect mis-files.

## Phase 1 — Measurement & gates everywhere (the trust baseline)
*Make every handoff measurable and gated. Change epistemics, not extraction.*
- **Stage 1–2 (canonical/extraction):** build the labeled **accuracy harness** + seed corpus + baseline scorecards (**Milestone 1A/1B** — see companion doc). Artifact **status contract** (completed / completed_with_warnings / needs_review / failed_retriable / failed_terminal). **Version-freshness fix** (`composer._max_version` lexical → `created_at`/numeric, everywhere).
- **Stage 3 (test-cases):** wire the **validator + auditor as a gate at test-case write time** (the nav-grounding gate and login-precondition already exist — just gate them).
- **Stage 4 (playwright):** the Phase-0 gate becomes a **hard CI gate** with committed baselines.
- **All stages:** **provenance + fact-status** field on every row (`proven / inferred / conflict / missing`); **no silent drops** become gate failures.
- **Exit gate:** the first honest per-stage numbers exist and are committed; every handoff has a gate; every artifact carries an honest status + quality report; green-washing is impossible to merge.

## Phase 2 — Ground every fact (consensus + invariants + calibration)
*Raise real accuracy; make confidence mean something. This is where faithfulness/calibration hit 10.*
- **Stage 1–2:** **multi-signal consensus** extraction — a fact is PROVEN only when ≥2 **independent signal types** agree (weight by independence; OCR + URL-regex = one axis, not two); disagreement → **conflict**, never a fabricated 0.55 navigate. **Kill the `verb=none→NAVIGATE@0.55` fabrication** (first *measured* improvement vs the Phase-1 baseline).
- **Stage 1–2:** the **self-verification oracle** at the canonical handoff (demote-only: flags impossible facts + injects MISSING for omitted values). **Extraction invariants:** "every typed value survives," "no proven navigation → no navigation assertion."
- **All stages:** replace static confidence with **measured calibration** (ECE ≤ 0.05, every PROVEN ≥ 0.98).
- **Exit gate:** fabrication-rate ≈ 0, ECE ≤ 0.05, value-survival recall up, zero silent drops — all on the harness, held-out slice within band.

## Phase 3 — Close the autonomous loop generically (B & C to real green)
*Make Autopilot/Batch close the loop on real apps, not just refuse honestly.*
- **Stage 5:** feed the agentic analyst the **full context** it was starved of (anchor/product/disambiguator); make **ambiguity a first-class diagnosis class** (not NEEDS_REVIEW); add **re-login-in-flow** for auth expiry; generalize the locator-disambiguation so heal can rescue ambiguous controls.
- **Stage 6:** build the **env-log analyzer** (the real agentic white space) — read runner/browser/console logs and synthesize a grounded "what to fix"; validate that an auto-authored defect actually **reproduces**.
- **Stage 5–6:** the vision-grounded cross-check oracle (from Phase 2) feeds heal so it can disambiguate by perception when text isn't enough.
- **Exit gate:** on the stratified corpus, B produces **real green passes** where the app behaves and honest stops/defects otherwise; C batches it unattended with honest reports.

## Phase 4 — Scale & production hardening (1,000+ apps / 100+ clients)
*Operationally production-ready and safe at fleet scale.*
- **Throughput/ops:** per-tenant queues, concurrency limits, backpressure, retry policy, **dead-letter queue**, fair scheduling; per-artifact **cost ceiling**; every pass fail-open.
- **SLOs/observability:** p95 latency + failure-rate tracking; **per-tenant calibration-drift monitor**; queue dashboards; run/heal/defect ledgers.
- **Governance:** RBAC, immutable audit, approval lifecycle, **version-promotion + rollback** (reprocess → compare on harness → promote only if better), Part-11-grade evidence chain.
- **Accuracy ceiling-breaker:** ship the optional **ground-truth overlay recorder** (additive; video-only stays byte-identical floor; extend overlay to values to kill dropped-login structurally).
- **Deploy:** on-prem packaging, KMS/secrets, **security review**, reproducible builds, no divergence.
- **Exit gate:** load test sustains the fleet within SLO; per-tenant isolation proven; drift alerting live; rollback rehearsed; security review passed.

## Phase 5 — Continuous trust & moat (the flywheel)
*Turn the proven trust into a durable, compounding advantage.*
- Live per-tenant calibration as a standing SLO; the **consented failure→fix flywheel**; SOC2 / compliance evidence chain; the **Test-Evidence System-of-Record**.
- **Exit gate:** the trust numbers are continuously monitored in production, not just in CI; the flywheel compounds across consented tenants.

---

## Per-stage 4→10 trajectory (where each stage gets fixed)
| Stage | Now | Reaches 10 via |
|---|---|---|
| 1. Canonical | 4 | P1 harness/status/version → P2 consensus+oracle+calibration → P4 overlay |
| 2. Extraction | 4 | P1 harness → P2 consensus + invariants + self-verification oracle |
| 3. Pages&Forms→Test cases | 4 | P1 wire validator-as-gate (+ inherits P2 extraction fixes) |
| 4. Test cases→Playwright | 4 | P0 disambiguator + auditor-gate + uniqueness dimension → P1 hard CI gate |
| 5. Self-heal + Autopilot | 4 | P0 disambiguator → P3 full-context analyst + re-login + ambiguity-class |
| 6. Defect + Env triage | 6 | P0 wire is_base_host → P3 env-log analyzer + defect-reproduces check |

## Sequencing logic (why this order)
- **P0 first** because it's ~$0, proves the loop today, and stops the worst green-wash holes.
- **P1 before P2** because you cannot honestly claim any accuracy improvement until you can measure it — and the gates make regressions un-mergeable.
- **P2 before P3** because closing the autonomous loop on a fabricated foundation just automates the lie; ground the facts first.
- **P4 before scale GA** because 1,000 apps × 100 clients is an ops/isolation/cost problem, not an accuracy problem — and it needs the harness (P1) to certify the generic method floor.
- **P5** is the compounding moat once trust is real and monitored.

## What this plan deliberately is NOT
- Not "make the AI smarter first." The headline bugs are deterministic; smarter AI on an ungated, unmeasured pipeline just scales the errors.
- Not a rewrite. It is wiring (gates), grounding (consensus/provenance), and hardening (ops/scale).
- Not a "100% video accuracy" claim. Video-only = trustworthy floor; overlay = optional accuracy ceiling; every row says which.
