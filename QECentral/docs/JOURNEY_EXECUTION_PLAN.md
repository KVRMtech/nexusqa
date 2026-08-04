# Runnable Journeys — Release D Production Implementation Plan

**Date:** 2026-08-04
**Status:** Direction stated by founder ("a journey must run like a test case, in the Playwright tab, end to end"). Awaiting go-ahead.
**Prime directive:** the Journey Graph's claims become RE-PROVABLE ON DEMAND — every completed journey has exactly one runnable end-to-end case, executed through the EXISTING factory/runner/heal/verdict machinery, and the run verdict folds back onto the journey. The frozen factory is never edited; qe-central links and orchestrates.

---

## 1. Position

Today a journey is proven only by a crawl. A Test Case is runnable but speaks page-language, not business-language. Release D closes the gap in both directions:

```
CRAWL (discovers + proves once)          RUN (re-proves forever)
journey walked → traversal      →  D1 →  Journey Case (one E2E script, named
   path_fps + evidence                    after the journey's business name)
        ↑                                        │ runs in Playwright tab,
        └──────────  D3 verdict fold-back  ◄─────┘ heals, certifies — UNCHANGED
                                                   machinery
```

Honesty law carried over: **only a COMPLETED traversal compiles into a Journey Case.** A truncated journey (`loop`, `budget_exhausted`, `oracle_unavailable`) surfaces "not runnable yet — re-crawl to prove the path first", never a script that pretends to cover a path the crawl never finished.

## 2. Verified ground truth this builds on

| Fact | Where |
|---|---|
| The factory already compiles crawl substrate → ProductionTestCase → real Playwright scripts; the crawl auto-generates on completion (3 cases exist for KVR Test) | `substrate.writer` → `factory.generate` (internal.py auto-generate) |
| The factory's synthesis already builds a **trunk journey** case per funnel (+ per-terminal-form flows) | `platform/api/.../synthesis.py` ("Build the trunk journey…") |
| Test Cases + Playwright tabs are artifact-keyed and already execute/heal/verdict any case via the Phase-1 bridge | `factory_proxy`, Studio panels |
| Journeys carry `path_fps` (ordered fingerprints) and traversals link `exploration_id` → the crawl artifact | `journey_models.py` (qec_005) |
| Case names follow the "Verify …" business-name law (F5); journey business names exist (C2) | `journeys.business_name` |
| Frozen factory: additive only — no VKPower factory edits | standing doctrine |

## 3. Phases

### D0 — Journey ⇄ Case linkage (the read-only bridge)

The cases for a journey's path already exist (auto-generate). D0 makes the relationship a stored fact instead of a coincidence.

- New qecentral table (alembic `qec_006_journey_cases`, RLS like qec_005): `journey_cases` — tenant_id, app_id, journey_id, artifact_id, test_case_id, kind (`linked | journey_e2e`), display_name, created_at; unique (tenant, journey_id, test_case_id).
- New `services/journey_case_linker.py`: after fold + auto-generate in the completion callback, read the artifact's cases through the EXISTING factory read seam, match each case's step states against the journey's traversal `path_fps` (fingerprint intersection scoring — deterministic, no LLM), upsert links. Best-effort, idempotent, never fails a callback.
- Journeys API detail gains `cases: [{test_case_id, name, kind, runnable}]`; the portal journey card lists its linked cases with a deep link into the Test Cases / Playwright tab.
- Exit gate: KVR Test's quote journey shows its linked case(s) in the Journeys tab, and the link opens the exact case in the studio.

### D1 — The Journey Case (one E2E script per completed journey)

- For every journey with ≥1 COMPLETED traversal, ensure exactly ONE end-to-end case covers the full walked path (entry → terminal). Mechanics, additive-only: qe-central invokes the existing generate seam scoped to the journey's flow (the factory's trunk-journey synthesis already produces funnel-length cases from the same substrate — D1's job is selection + guarantee, not a new compiler). Where a trunk case already spans the path, it is ADOPTED as the Journey Case (`kind='journey_e2e'`); only if none spans it is a scoped re-generate requested.
- The Journey Case carries the journey's BUSINESS name via the qe-central display overlay (`journey_cases.display_name` = "Verify <business_name> end to end" — F5 law; the frozen factory's own case name is untouched).
- Truncated journeys: `runnable=false` with the honest reason (which terminal blocked it, what to do — re-crawl / raise budget / restore service).
- Exit gate: "Get Life Insurance Quote" shows ONE `journey_e2e` case named after the journey; "Explore Life Insurance Options" shows the honest not-runnable reason.

### D2 — Run Journey (execution through the existing runner)

- Portal Journeys tab: **Run journey** button per runnable journey → POST `/api/v1/qec/apps/{app_id}/journeys/{journey_id}/run` → qe-central dispatches the Journey Case through the EXISTING test-run path (factory_proxy → test-runs → runner), returns run_id; the button shows live status exactly as the Playwright tab does (same run objects, no parallel machinery).
- Environment + member: the run uses the app's bound run environment and run identity exactly as a Playwright-tab run does today (Members×Env F1–F7 machinery unchanged). Nothing new to configure.
- A **Prove all journeys** button runs every runnable Journey Case as one batch (sequential dispatch through the same seam, honest per-journey statuses — no aggregate green).
- Exit gate: clicking Run on the quote journey executes the real script via the runner against VKPower Life and produces a verdict visible in both tabs.

### D3 — Verdict fold-back (the loop closes)

- New qecentral table `journey_runs` (same migration): tenant, app, journey_id, test_case_id, run_id, status, env_ref, identity_ref, started_at, finished_at, certificate_ref. Written when a journey-dispatched run completes (poll the existing run-status seam qe-central already proxies; no factory edits).
- Journeys API + portal show BOTH proofs side by side, never merged: *"Proven by crawl <time> · Last run: GREEN <time> (certificate)"*. A red run NEVER un-completes the crawl claim and vice versa — they are different facts (discovery-proof vs regression-proof) and the card states both.
- Attribution rides along unchanged: a failing journey run shows the Attribution Engine's verdict (product/script/env/data) exactly as the Playwright tab does — the journey never blames the app without it.
- Exit gate: run the quote Journey Case → green verdict + certificate link appear on the journey card; kill the env → run again → red with honest attribution, crawl-proof untouched.

### D4 — Journey suite semantics (the payoff)

- The journeys list gains the app-level run rollup (counts only): journeys runnable / run-green / run-red / never-run. `branch_coverage` stays crawl-derived and separate.
- Certificates: journey runs reference the existing Certificate-of-Execution chain; the journey card's certificate link is the SoR artifact a client signs off on.
- Horizon (explicitly out of D, needs its own plan): GitLab-diff → affected-journeys regression selection; journey runs scheduled per cycle; branch-walk results joining the same run ledger.

### D-P — Live proof gate (all must pass on VKPower Life)

1. Quote journey shows one `journey_e2e` case, named "Verify Get Life Insurance Quote end to end", visible in Test Cases AND Playwright tabs.
2. **Run journey** executes it through the real runner → green verdict + certificate, folded back onto the journey card.
3. The truncated journey is honestly not-runnable with its reason; no script exists that claims its path.
4. A forced failure (wrong env / broken selector) runs red with attribution; the crawl-proof line is unchanged.
5. Heal path: rename a control in the app → run heals through the existing auto-heal → journey run records the healed-green honestly.

## 4. Order, size, risk

| Phase | Depends on | Size | Risk |
|---|---|---|---|
| D0 linkage | C1 (graph live) | S | Low — read-only matching + one table |
| D1 journey case | D0 | M | Medium — adoption-vs-regenerate selection logic; factory untouched |
| D2 run button | D1 | S/M | Low — reuses the run path end to end |
| D3 fold-back | D2 | M | Medium — run-completion polling seam |
| D4 rollups | D3 | S | Low |
| D-P proofs | all | M | — |

Everything is additive: no factory edits, no runner edits, no changes to Releases A–C behavior; flags stay as they are.

## 5. Acceptance (founder sign-off checklist)

- [ ] Every COMPLETED journey has exactly one runnable, business-named E2E case, visible and executable in the Playwright tab like any test case.
- [ ] Truncated journeys are honestly not-runnable, with the reason and the remedy named.
- [ ] Run verdicts + certificates fold back onto the journey card; crawl-proof and run-proof are displayed as distinct facts.
- [ ] Failing runs carry attribution; heal works unchanged.
- [ ] Zero edits to the frozen factory and runner; all Release D state lives in qecentral tables under RLS.
