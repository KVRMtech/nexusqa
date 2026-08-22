# Phase 4 — Entry-Gate Report

**Verdict: PHASE 4 MAY NOT OPEN. P1 = RED · P2 = RED · P3 = RED.**

No Phase 4 implementation has been written. Per §2 and §18 of the brief, the gate
is reported, not worked around. What follows is measured on this checkout and on
the branch's own CI runs; every claim names what proves it, and every gap says so
in those words rather than rounding up.

Audited 2026-08-21 04:20–04:42 UTC · branch `feat/qec-dynamic-catalog-p0-p6`.

---

## §0 · The finding that governs the whole gate

**HEAD moved four times during a read-only audit, and ten other sessions are live
in this same checkout.**

```
23:25:08 -0500  c5127d8   <- HEAD when the audit started
23:36:31 -0500  84e445e   <- the commit CI run 32447553377 measured
23:38:44 -0500  cb07497
        later   79808cb   <- HEAD when this report was finished
```

`ListAgents` reports **10 peer interactive sessions** on `c:\Users\srika\nexusqa`.
An eleventh working tree, `.claude/worktrees/gate4`, holds a *different branch*.

This is not context. It is Stop Condition material, and it is the same condition
Gate 0 §0 escalated and Gate 5 §0 refused certification over. It has three direct
consequences for Phase 4:

1. **No Phase-4 proof taken here can be independently reproducible** (§17), because
   the tree it was taken against does not exist twice.
2. **The entry gate cannot be *closed* here either** — a remediation landed on this
   branch is invalidated by the next session's commit before it can be verified.
3. Phase 4 work, when it opens, **must run in its own git worktree** on its own
   branch. The gate4 squad already does this, and it is the only reason its
   evidence is attributable.

---

## §1 · Repository state (audit item 1)

| | |
|---|---|
| Branch | `feat/qec-dynamic-catalog-p0-p6` |
| HEAD at report time | `79808cb` (moved 4x during the audit — §0) |
| Pushed | yes; `origin/…` tracks the branch |
| Working tree | **50 dirty entries** (32 modified, 18 untracked) |
| Second worktree | `.claude/worktrees/gate4` on `gate4/phase3-proofs` |
| Merge base with gate4 | `3778c1a` — **4 commits each side, neither merged** |
| Merged to `develop` | **nothing**; `origin/develop` shares no history with this branch (`ci.yml` L6-20) |
| Deployed build | `verdict-box` serves `ede6bf2` — predates every Gate 0–4 commit (Gate 5 §0) |

### Untracked implementation — the A11 attestation issuer is not in git

```
?? app/services/attestation_issuer.py        ?? app/services/attestation_keys.py
?? app/services/attestation_revocation.py    ?? app/db/attestation_models.py
?? app/routers/attestation.py                ?? alembic_qec/versions/qec_023_attestation_issuer.py
?? tests/security/test_a11_api_authorization.py    ?? tests/security/test_a11_attestation_redteam.py
?? tests/security/test_a11_dispatch_integration.py ?? tests/security/_a11_kit.py
```

`qec_022` is tracked; **`qec_023` is not**. So a clean clone's alembic head is
`qec_022`, this working tree's is `qec_023`, and every green CI run cited by any
gate was measured on a tree that does **not contain the issuer at all**. This is
Gate 5 stop condition #2, still live, verified by `git ls-files --error-unmatch`.

---

## §2 · CI on the branch head (independently observable)

Run [`32447553377`](https://github.com/KVRMtech/nexusqa/actions/runs/32447553377),
commit `84e445e`, read live during the audit:

| job | verdict |
|---|---|
| frontend · lint · security · compile · integrity-proof · platform-api-tests · crawl-smoke · qe-explorer-tests · harness-jsdom | ✅ success |
| **qe-central-tests** | ❌ **failure** |
| **QE-Central database & tenant-isolation contract** | ❌ **failure** |
| **qe-explorer-characterization** | ❌ **failure** |
| test · qe-explorer-browser | did not report before the run concluded |

**Run conclusion: `failure`.** CI is red on the branch head.

**`qe-central-tests`** — `1 failed, 2298 passed, 150 skipped`:

```
FAILED tests/fleet/test_t_fl_03_object_storage_handoff.py::test_producer_key_layout_matches_the_sdk_build_key
       PermissionError: [Errno 13] Permission denied: '/app'
```

**`QE-Central database & tenant-isolation contract`** — the named milestone steps
all pass (contract suite `222 passed, 2 skipped`; A20 `5 passed`; M2.3 `3 passed`;
A21 `3 passed`; A24 `2 passed`), and then the final whole-suite step fails:

```
Phase 6 — qe-central suite against the live infrastructure
2350 passed, 10 skipped, 89 errors in 66.70s
ERROR:  permission denied for table tenants
```

All 89 errors are the **M3.3 fleet suite** (`test_t_fl_01_durable_queue`,
`test_t_fl_02_worker_registry`, …). Phase 3's concurrency proof therefore does not
execute in CI at all — it is a local-only green.

**`qe-explorer-characterization`** — `28 failed, 35 passed, 1 skipped, 795 deselected`
in 15m26s. Every failure is the same shape:

```
FAILED tests/browser/test_browser_characterization.py::test_manifest_golden[23-canvas-app]
       AssertionError: CHARACTERIZATION DIFF for 'manifest_23-canvas-app' — captured behaviour changed.
```

These are the 28 stale goldens Gate 3 recorded as *"known red, and NOT mine"* — they
have been red since `3420d88` and are still red. Note that one of the failures is
`test_a_behavioural_change_breaks_the_golden`, the harness's own self-check: **the
instrument that proves the golden gate can fail is itself failing**, so the lane
currently cannot distinguish a real behavioural regression from the stale baseline.
Fixture `23-canvas-app` is the same fixture A29's vision proof uses.

**Neither red lane is a Phase-4 artefact and neither is mine.** Both are recorded
because §16 forbids declaring anything complete off a red CI.

---

## §3 · Local measurement, and why this machine cannot reproduce CI

With the environment defect below neutralised (`-p no:randomly`), this working
tree measures:

| lane | result |
|---|---|
| `pytest tests --ignore=tests/browser` (qe-explorer) | **2025 passed, 0 failed** — 60.9 s |
| `pytest platform/qe-central/tests` (no live infra) | **2424 passed, 150 skipped, 0 failed** — 91.3 s |

### The environment defect — recorded because it is a reproducibility hazard

A *plain* `pytest` on this box produces **2531 errors / 728 passed** (explorer) and
**1542 errors / 1644 passed** (qe-central). Every one is:

```
pytest_randomly/__init__.py:177  _reseed(...)
thinc/util.py:96                 numpy.random.seed(seed)
E  ValueError: Seed must be between 0 and 2**32 - 1
```

`pytest-randomly` and `thinc` (a spaCy dependency) are installed in this machine's
**global** Python 3.10 site-packages; `thinc` monkeypatches `numpy.random.seed`,
which rejects the seeds `pytest-randomly` derives from certain node ids. CI runs
Python 3.11 with a clean `pip install pytest pytest-asyncio`, so it never happens
there.

It is a workstation defect, not a repository defect — but it means **the documented
command does not reproduce the documented result on the author's own machine**,
which is exactly what §17 requires of a milestone proof. Phase 4 must run in a
project virtualenv, not the global interpreter.

---

## §4 · P1 = RED

**Claim under test: Phase 1 (M1.x — journey engine, page lifecycle, walk
persistence, green-wash closure) is green and independently reproduced.**

| evidence | reading |
|---|---|
| Tag `gate-1` → `3420d88`, an ancestor of HEAD | the engine landed |
| `QECentral/docs/GATE_2_THREE_APPLICATIONS.md` | **"NOT MET. One of three applications completes a journey."** |
| `evidence/gate2/summit-life-carrier/journey.json` | `boundaries_crossed: 0`, `journeys_completed: 0`, `oracle_decisions: []` |
| `evidence/gate2/acme-life/journey.json` | `boundaries_crossed: 2`, one confirmation — the one that works |
| Gate 2 §1 | `proving-grounds/vkpower-life/` holds **two different applications**; the local lane and the CI lane have never tested the same software |
| `git ls-files` on the A11 issuer (§1) | Gate 1's attestation half is **not in any commit** |

**Why RED, precisely.** Phase 4 M4.2 / T-BX-01 asks a *single crawl* to reach a
decision point, walk branch A, replay, walk branch B, and fold both. That is a
strictly harder demand than "complete one journey", and one journey on one of three
applications is the current ceiling. Building branch coverage on top of a 1-of-3
journey engine would produce a branch proof that exists only on `acme-life` — a
single-application result presented as a capability.

The untracked A11 issuer is independently disqualifying: T-NAV-03's promotion gate
and T-BX-04's tenant isolation both sit downstream of walk attestation, and that
code cannot be reviewed, CI-tested, or reproduced from a clean clone.

---

## §5 · P2 = RED

**Claim under test: Phase 2 (M2.x — the catalogue as a reviewable evidence
artifact) is green.**

Genuinely proven, verified in this audit's own CI read (§2):

* **A20** `qec_019` round-trips in the CI database — 5 passed.
* **A21** three real app changes → three correct classifications — producer 8/8 x2, consumer 3/3 against real Postgres.
* **A23** live network trace, 10/10 on 68 real events.
* **A24** live-tenant capture → persisted catalogue, 9/9 + 2/2.

Not proven:

* **A22 — BLOCKED.** ⛔ The chain
  `real crawl → coverage → fold → catalog → compiler → verified regression`
  cannot be completed by **any application in this repository**, measured rather
  than asserted. Two independent layers:

  1. **The bare-button wizard gate.** `discovery.py` requires
     `fill.filled or fill.has_unanswered_decisions`; a step whose only control is a
     button commits nothing, so `_answer_questionnaire` never runs. Measured on a
     real crawl: the app's own server log shows `POST /api/quote` was actuated,
     while the coverage account records `forms_found: 0`, `flows: 0`,
     `journeys_completed: 0`.
  2. **The outcome page is discarded.** `state_identity.note_state_signals` opens
     with `if not signals and not controls: return`. A funnel's result page is by
     construction a page with neither — so the one page whose value a generated
     spec must assert on is the page the account drops. The manifest holds the
     evidence (`"label": "Your monthly premium", "text": "42.50"`); coverage never
     sees it.

  The inventory table in Gate 3 is the load-bearing part: `m24_generation` calls a
  backend but is not walkable; `acme-life` is walkable but has `fetch(` count
  **0**; `vkpower-life` is a static export (68 requests, all GET/200);
  `summit-life-carrier` does not start in CI. A22 needs both properties in one
  crawl, and no application has both.

* **A25 — NOT ATTEMPTED**, blocked by more than scheduling: M2.1's E2E drives an
  **in-process** `Crawler` and imports qe-central's pure functions directly.
  Nothing in it reaches a deployed service. "M2.1 executes against deployed
  services" needs a variant that does not exist.

**Why this is fatal to Phase 4 specifically.** M4.1's final demonstration is

> `Navigator action → Observation → Evidence → R0 verification → Catalog row →
> Compiler → Verified regression artifact`

That is the A22 chain with a navigator substituted for the walker at the front.
Swapping the front of a chain does not repair a break in its middle. If Phase 4
opened today, M4.1 would either fail at the same two lines — or be "closed" against
`m24_generation`'s hand-written `crawl_evidence.py` fixture, which is precisely the
fixture A22 exists to replace. **That is the single most likely way Phase 4
green-washes, and it is now on the record before it can happen.**

---

## §6 · P3 = RED

**Claim under test: Phase 3 (M3.x — vision, frames/shadow, fleet concurrency) is
green.** Source: `GATE_4_PHASE3_PROOFS.md` on `gate4/phase3-proofs`, plus this
audit's own checks.

| # | milestone | verdict | verified how |
|---|---|---|---|
| A28 | vision-operate caller | code complete **on another branch** | `grep -c A28` on **this** branch's `app/main.py` = **0**; matches only under `.claude/worktrees/gate4/` |
| A29 | real multimodal prediction | ⛔ **BLOCKED** | `evidence/gate4/a29_real_vision.json`: `finish_reason: "error"`, `raw_reply: ""`, `error: openai_compat_http_401` |
| A30 | signed vision attestation rung | ⛔ **NOT STARTED** | blocked by A29 plus a design conflict; per prior audit it would **de-certify A11** |
| A31 | KEDA on a real cluster | ✅ PASS, reproduced twice | real kind cluster, repo's own ScaledObject unedited |
| A32 | T-FL-08 vs real Chromium | ✅ PASS | 36 cross-fence attempts, 0 violations |
| A33 | live Squid fence reload | ✅ PASS | flipped live, no restart |
| A34 | `_scan_fleet` scalability | ✅ DECIDED | bound measured, formally accepted |
| A35 | crossing journal recovery | ✅ PASS | both crash shapes, 0 double-submits |
| M3.3 | fleet concurrency suite | ⛔ **does not run in CI** | 89 errors, `permission denied for table tenants` (§2) |

**Why RED.** Phase 4 is *"vision-first navigation"*. Its foundation is the one
Phase-3 milestone that has never produced a real prediction:

```json
"model_call": { "provider": "openai_compat", "model": "gpt-4o",
                "finish_reason": "error", "raw_reply": "",
                "error": "openai_compat_http_401: Incorrect API key provided…" }
"verdict": "FAIL - the provider returned no text; no prediction was produced"
```

Every part of the vision chain except the credential is proven real. But a
vision-first navigator built on a vision layer that has **never seen a model reply**
would be validated entirely against stubs — and T-NAV-04 needs that same provider
credential to produce a benchmark number at all. One expired key blocks A29, A30,
T-NAV-04, and the M4.1 demonstration simultaneously.

Note also that A31–A35's real-infrastructure passes live on an **unmerged branch
that has diverged from this one**. Phase 4 cannot inherit them without a merge that
has not happened.

---

## §7 · Existing navigator / evidence / catalog architecture (audit items 6–15)

**This is the good news, and it is substantial: the seams Phase 4 needs already
exist. The gate is red on proof, not on architecture.**

### The evidence plane, end to end

```
BrowserPort (app/browser.py:306)         — the async port; Playwright adapter + scripted fake
  → app/emit.py                          — append-only fsync'd manifest.jsonl; record types
                                           crawl_meta | page_state | action | screenshot |
                                           guard_event | edge | checkpoint
  → qe-central app/substrate/writer.py   — a dumb 1:1 mapper onto ExplorationBundle
  → services/journey_fold.py             — flows → Journey Graph nodes/edges/branches (idempotent)
  → services/catalog.py                  — extract_controls → build_master_catalog → catalog_questions
  → services/journey_spec.py             — M2.4: a journey compiles on its OWN evidence
  → clients/factory.py                   — ranked journeys → Playwright specs + lint verdict
```

* **R0 is a pure function**: `app/browser.verify_intent(...) -> bool | None`, where
  `True` = positively verified, `False` = provably unmet, `None` = *unverifiable*.
  It takes no browser and no model — a navigator adapter can call it unchanged.
* **The wire shape is frozen as data**: `contracts/m22_catalog_question_v1.json`
  ("CHANGING THIS FILE IS A PROTOCOL CHANGE"), plus `gate1_walk_attestation_v1.json`
  and `m17_business_rule_v1.json`.
* **The anti-green-wash law T-NAV-03 asks for is already implemented once**, for
  vision, in `app/vision_loop.py`: *"A vision prediction is never catalog truth."*
  Every perceived control leaves on exactly one of two paths — `VERIFIED` (promoted)
  or `REFUSED` (recorded in the vision ledger, returned in nothing the catalogue
  reads) — and `None` from R0 is a **refusal**, not a pass. A second navigator
  should extend this module's contract, not clone it.
* **The LLM seam is already singular**: `app/oracle_gateway.py` ("the single seam
  through which crawler internals consult an LLM"), with per-crawl call caps,
  circuit breakers, and `unavailable` as a first-class non-raising outcome.
* **The vision gate is already fail-closed on the target, not just the operator**:
  `app/vision_gate.py` — `attested AND tenant_enabled`, exhaustive truth table.

### What exists for M4.2

| requirement | status |
|---|---|
| T-BX-01 branch machinery | **exists** — `services/branch_planner.py` (single-variable enumeration, `discovered → planned → walked`, honest `deferred` at the cap) plus `journey_fold` branch rows. The brief's "do not build a second framework" is the right call. |
| T-BX-02 eSignature | **recognizer only** — `app/esign.py` names canvas / attest / vendor-iframe. No driver, no live signer. |
| T-BX-02 payment | **test cards only** — `app/identity_pack.py` generates Luhn-valid numbers from published test BINs. **No processor sandbox integration, and no proving-ground app has a payment step.** |
| T-BX-02 policy PDF | not found as a capability |
| T-BX-03 HITL | **halves exist, unjoined** — `services/assist_classifier.py` names the five genuine `HUMAN_REQUIRED` reasons (`second_factor`, `captcha`, `enterprise_account`, `hardware_token`, `legal_approval`) and separates them from `agent_gap`; `app/resume_state.py` implements durable frontier checkpointing; `routers/explorations.py` reuses **both** crawl id and exploration id on resume and carries a `resumes` counter. There is **no `paused` status** in the exploration lifecycle (`pending \| dispatched \| writing \| running \| completed \| failed`) and no human-intervention record. |
| T-BX-04 flywheel | **exists and is consent-gated** — `services/advance_memory.py` + `mechanic_memory.py`, `_tenant_consented()`, contribution opt-in and OFF by default; `tests/unit/test_flywheel.py` exists. No A/B measurement protocol. |

### T-NAV-04 — the benchmark gap is total

* Repository-wide grep for `webarena|webvoyager|mind2web|browsergym|GAIA` returns
  **nothing** outside one archived prose paragraph. There is no public
  web-navigation benchmark harness.
* The only benchmark that exists, `benchmarks/pages_forms/run_benchmark.py`, is a
  *different product surface* (video → Pages & Forms accuracy) and needs the
  platform-api Postgres.
* All five `proving-grounds/` applications (`acme-life`, `catalog-evidence`,
  `questionnaire-life`, `summit-life-carrier`, `vkpower-life`) are **seen**
  applications this engine was tuned against. **None can serve as a "previously
  unseen application"**, and reusing them would be the benchmark equivalent of
  training on the test set.

  Worth stating plainly: `run_benchmark.py`'s own docstring already contains the
  discipline T-NAV-04 needs — draft keys are scored but the headline number counts
  **verified** keys only, and a regression gate exits non-zero on a drop. The
  doctrine is in the house; the corpus is not.

---

## §8 · T-NAV-01 decision inputs (gathered; decision NOT taken)

Per §3 of the brief, the decision is taken *after* the gate is green. These are the
inputs it will consume, recorded now so the work is not repeated.

**Inputs that favour hybrid/adapter over both pure build and pure buy:**

1. The port already exists (`BrowserPort`), is already dual-implemented (Playwright
   plus a scripted fake), and is already documented as never-raising.
2. The promotion law already exists once (`vision_loop`) and is the strongest asset
   in the repository. A vendor navigator that returns "I clicked it and it worked"
   is *exactly* the input that law was written to refuse.
3. `oracle_gateway` already isolates provider calls behind telemetry, caps and
   breakers — most of the observability §13 asks for is already wired.
4. `vision_gate`'s attestation double-gate means a vendor navigator inherits the
   egress control rather than needing a new one.
5. **Counter-input, and a real one:** the evidence plane's value is that it is
   *ours*; A29 proves the model layer is *rented*, and a rented layer that is 401
   for a day takes the whole navigator offline. Failure behaviour (item 9 of the
   T-NAV-01 template) must be "degrade to the DOM walker", not "fail the crawl".
6. **Counter-input:** M3.1's vision integration found 10 defects, including
   `click_at` page-vs-viewport coordinate confusion. Coordinate-space bugs at the
   adapter boundary are the empirically demonstrated failure mode here, so the
   normalized event contract must carry the coordinate space explicitly.

**Still missing before the decision can be responsibly made:** a working provider
credential (A29), a measured baseline on *any* unseen application, and a
cost-per-task figure — none obtainable while the gate is red.

---

## §9 · Blockers — `BLOCKER → WHY → EVIDENCE → IMPACT → OWNER → FIX → RE-TEST`

### B1 · The tree does not hold still
* **WHY** 10 concurrent sessions plus a second worktree write to one checkout.
* **EVIDENCE** HEAD `c5127d8 → 84e445e → cb07497 → 79808cb` during one audit; `ListAgents` = 10 peers; Gate 0 §0 and Gate 5 §0 escalated the same condition previously.
* **IMPACT** No Phase-4 proof taken here is reproducible (§17). Blocks *every* milestone.
* **OWNER** Repository owner / release manager — not in an implementer's gift.
* **FIX** Phase 4 gets its own branch in its own `git worktree`. Gate closure gets an announced freeze window on `feat/qec-dynamic-catalog-p0-p6`.
* **RE-TEST** `git rev-parse HEAD` identical before and after a full gate run.

### B2 · A11 attestation issuer is untracked
* **WHY** 5 app modules, `qec_023`, and 4 security test files were never committed.
* **EVIDENCE** `git ls-files --error-unmatch app/services/attestation_issuer.py` → *did not match any file(s) known to git*; `qec_022` tracked, `qec_023` not.
* **IMPACT** Clean clone has a different alembic head. CI has never executed A11. T-NAV-03 promotion and T-BX-04 isolation both sit downstream.
* **OWNER** Gate 1 / A11 squad.
* **FIX** Commit the set together (§16.3), push, confirm the DB contract job applies `qec_023`.
* **RE-TEST** Clean clone → `alembic upgrade head` → A11 security suite green in CI.

### B3 · A22 — no application is both walkable and backend-calling
* **WHY** wizard gate plus outcome-page discard, on an inventory where no app has both properties.
* **EVIDENCE** Gate 3 §A22, with the app's own server log versus `forms_found: 0`; `state_identity.note_state_signals` line `if not signals and not controls: return`; strict-xfail producer committed.
* **IMPACT** **M4.1's final demonstration is unreachable.** Highest-severity Phase-4 blocker.
* **OWNER** Crawl-engine squad (M2.1 declined the gate change as out of scope).
* **FIX** Preferred: relax the bare-button wizard gate in `discovery.py` — benefits every SPA. Plus admit outcome-only pages to `coverage["states"]` when they carry `displayed_values`. Alternative (cheaper, weaker): give `m24_generation`'s quote funnel one real input.
* **RE-TEST** The strict xfail **XPASSes**; then a real crawl → catalog row → compiled spec → green on healthy, red on seeded regression.

### B4 · A29 — the vision provider credential is invalid
* **WHY** `openai_compat` returns HTTP 401.
* **EVIDENCE** `evidence/gate4/a29_real_vision.json` — `finish_reason: "error"`, `raw_reply: ""`.
* **IMPACT** Blocks A29, A30, T-NAV-04, and the M4.1 demonstration. Phase 4 is *vision-first*; its foundation has never produced a prediction.
* **OWNER** Whoever holds the provider account. Not solvable in code.
* **FIX** Provision a valid key through the existing KMS envelope (A37.1 proved 9/9 production credentials decrypt through Cloud KMS). **Do not hard-code it** (§14).
* **RE-TEST** Re-run `gate4_a29_real_vision.py`; require a non-empty `raw_reply` and a bbox the model actually produced.

### B5 · Three CI lanes are red on the branch head; the run concludes `failure`
* **WHY** `T-FL-03` `PermissionError: '/app'`; 89 fleet errors `permission denied for table tenants`; 28 stale characterization goldens.
* **EVIDENCE** Run `32447553377`, conclusion `failure` (§2).
* **IMPACT** §16 forbids milestone closure off a red CI. M3.3 has **never run in CI**. And because `test_a_behavioural_change_breaks_the_golden` is itself failing, the characterization lane cannot currently tell a real regression from the stale baseline — so it would not catch a Phase-4 change that altered crawl behaviour.
* **OWNER** A26/A27 (object storage), M3.3 (fleet), and whoever owns the goldens stale since `3420d88`.
* **FIX** T-FL-03: stop writing to `/app` in the CI runner. Fleet: grant the CI test role on `tenants` in the RLS bootstrap. Goldens: re-record as a *reviewed* diff in the same commit as their producer (the Gate 0 A2 rule).
* **RE-TEST** All three jobs green on one commit, with the golden self-check passing.

### B6 · No unseen-application corpus and no public benchmark harness
* **WHY** never built; all 5 proving grounds are seen apps.
* **EVIDENCE** repo-wide grep for the public benchmarks returns nothing; `benchmarks/` holds only `pages_forms` and `redteam`.
* **IMPACT** T-NAV-04 cannot be started, let alone published.
* **OWNER** Phase 4 — this is legitimately new Phase-4 *scope*, not a gate blocker, and it is listed here so it is not discovered late.
* **FIX** Adopt a public suite with a pinned version and a recorded task set; forbid post-hoc task exclusion in the harness itself.
* **RE-TEST** Two runs of the pinned task set on two machines agree within a stated tolerance, with failures preserved.

### B7 · This workstation cannot reproduce CI
* **WHY** global Python 3.10 carries `pytest-randomly` plus `thinc`; `thinc` breaks `numpy.random.seed`.
* **EVIDENCE** §3 — 2531 errors by default versus 0 with `-p no:randomly`.
* **IMPACT** The documented command does not produce the documented result on the author's machine.
* **OWNER** Phase 4 implementer.
* **FIX** A project virtualenv pinned to CI's Python 3.11 and CI's dependency set.
* **RE-TEST** Bare `pytest` in the venv reproduces the CI counts.

---

## §10 · Remediation list — the exact work that makes the gate green

Ordered by dependency. Nothing in Phase 4 starts until **P1a–P3b** are closed.

| # | item | closes | gate |
|---|---|---|---|
| **P0a** | Give Phase 4 its own branch plus worktree; announce a freeze window for gate closure | B1 | — |
| **P0b** | Project virtualenv on Python 3.11 with CI's dependency set | B7 | — |
| **P1a** | Commit the A11 issuer set (5 modules + `qec_023` + 4 test files) together; push; confirm `qec_023` applies in the DB contract job | B2 | P1 |
| **P1b** | Gate 2 to 3-of-3: fix `summit-life-carrier`'s CI start and `auth_failed`; resolve the two-applications-one-directory ambiguity in `vkpower-life` so the local and CI lanes test the same software | — | P1 |
| **P2a** | Close the bare-button wizard gate in `discovery.py` | B3 | P2 |
| **P2b** | Admit outcome-only pages carrying `displayed_values` into `coverage["states"]` | B3 | P2 |
| **P2c** | Land A22: real crawl → catalog row → compiled spec → green on healthy, red on seeded regression. The strict xfail must XPASS. | B3 | P2 |
| **P2d** | Build the deployed-services variant of M2.1, then attempt A25 | — | P2 |
| **P3a** | Provision a valid vision credential via KMS; re-run A29 to a real prediction | B4 | P3 |
| **P3b** | Merge `gate4/phase3-proofs` into the Phase-3 line so A28 and A31–A35 are inherited, not orphaned | — | P3 |
| **P3c** | Fix the two red CI lanes; get M3.3's fleet suite executing in CI | B5 | P3 |
| **P3d** | A30 signed vision attestation rung — including its conflict with A11 | — | P3 |

**Only then** does §20's execution order begin: T-NAV-01 → T-NAV-02 → T-NAV-03 →
T-NAV-04 → M4.1 proof gate → T-BX-01 → T-BX-02 → T-BX-03 → T-BX-04 → M4.2 proof gate.

---

## §11 · What Phase 4 may legitimately do while the gate is red

Nothing that touches the evidence plane, the catalog, or the compiler. Two items are
genuinely independent of P1–P3 and carry no risk of manufacturing evidence:

1. **B6 — the unseen-application corpus and the benchmark *definition***. Fixing the
   benchmark definition *before* any number exists is precisely what §6 demands
   ("Do not publish a number until the benchmark definition is fixed"). Writing it
   while no navigator exists is the strongest possible guarantee that the definition
   was not tuned to a result.
2. **The T-NAV-01 decision document's *evidence-gathering* half** — §8 above.

Both are documentation and fixtures. Neither can be mistaken for a Phase-4 proof.
They should be started only on the explicit instruction of the repository owner, on a
Phase-4 branch in its own worktree (P0a).

---

## §12 · Statement

> Phase 4 does not open. P1, P2 and P3 are each RED on measured evidence, not on
> missing paperwork: one of three applications completes a journey, no application in
> the repository can carry a discovered journey through to a verified regression
> artifact, and the vision layer Phase 4 is named after has never received a model
> reply. The architecture Phase 4 needs is largely in place and is better than the
> brief assumes; the proof beneath it is not. Converting any of the three to GREEN
> without the remediation in §10 would be manufacturing evidence, which is the one
> thing this programme's gates exist to prevent.

---

## §13 · Addendum — remediation executed on `phase4/entry-gate-remediation`

This section is appended, not merged into the sections above: §0–§12 are the audit
as it stood, and rewriting them to match a later state would destroy the record of
what was true when Phase 4 was refused.

Worked in an isolated worktree pinned to `cfab4ed`, so the base holds still while
the shared branch moves (B1). Nothing below is pushed.

### What closed

| # | item | state |
|---|---|---|
| **B7** | this workstation cannot reproduce CI | **CLOSED** — clean venv with CI's dependency set; bare `pytest` gives `2025 passed, 0 skipped`. Correction to §3: every explorer/browser lane in CI pins **3.10**, not 3.11, so the venv is exact parity |
| **B2** | A11 issuer untracked | **CLOSED BY A PEER** — committed with `qec_023` in `1065083` |
| **P2a** | bare-button wizard gate | **CLOSED** — `discovery.py`; the walk was always able to handle it, the entry condition refused to ask |
| **P2b** | outcome page discarded | **CLOSED** — `state_identity.py`; state admitted AND its selector carried |
| **B3 / A22** | producer half | **CLOSED** — the strict xfail XPASSed and was retired; 5 passed |
| **B3 / A22** | consumer half | **CLOSED** — compiles, executes GREEN on the healthy application, and goes RED under a seeded silent-API regression |

### The chain, on evidence a crawl actually produced

```
real crawl -> coverage -> journey_fold (Postgres 16 @ qec_023) -> nodes=2 edges=1
           -> build_journey_case -> COMPILES
              steps           ['Open Quote Start', 'Click get quote']
              network         [('POST', '/api/quote', 'recorded')]
              outcome         'Your monthly premium' -> '#premium-value'
              outcome_oracle  soft
              provenance      journey_direct
```

...and then EXECUTES:

```
compile payload -> Playwright spec -> GREEN on the healthy application
                -> seeded silent-API regression -> RED, at the network assertion
```

Reproducible from the repository, in three stages because no one process can hold
all three services (M1.7):

| stage | test | produces |
|---|---|---|
| crawl | `engines/qe-explorer/tests/browser/test_a22_generation_crawl.py` | `coverage.json` |
| fold + compile | `platform/qe-central/tests/contract/test_a22_generation_from_real_crawl.py` | `compile_payload.json` |
| execute | `tests/m24_generation/test_a22_real_journey_executes.py` | the verdict |

The middle stage is wired into CI's `qec-database` job as its own named step
beside A20/A21/A24.

### A sixth defect, found by executing rather than by compiling

The first execution went **RED against a healthy application**: step 1 waited 30
seconds for a `POST /api/quote` that only happens on step 2's click. A false
regression is the mirror of a green-wash and worse in one respect — a green-wash
lies once, a suite that reds on a working system teaches an operator to ignore it.

Same drain-timing shape as defect 5, in the network channel: a state's endpoint
map is everything DRAINED during the visit, so the entry state inherited a POST
fired by a discovery click that then navigated away. The entry step's own comment
stated the premise it relied on — *"nothing precedes it, so no earlier state's
boot traffic can be confused with the calls this navigation made"* — which is true
about what precedes and false about what follows.

The M2.5 inventory had the correct answer all along (`/api/config` joined to
`navigate`, `/api/quote` to the `Get Quote` click) and nothing read it, because
`inventory_by_action` indexes on the clicked LABEL and a page load has a verb and
no label. `endpoint_map.navigate_caused()` reads that verb join:
`endpoints_asserted 3 -> 2`, `recorded_cause 1 -> 2`. Both steps are now RECORDED.

**This is why "it compiles" is not the acceptance criterion.** Every assertion in
that first spec was grounded in real recorded evidence, the lint executed with
zero errors, and the HONEST-10 audit scored it 10/certified — and it was still
wrong in the one way that matters to whoever has to read the result.

### Five defects, and why none of them had ever been caught

Each was unreachable rather than unnoticed, which is the finding that matters more
than any individual fix.

1. **Pillow undeclared.** Four module-level skips concealed **69 tests**, including
   the whole of `test_vision_loop.py` — the unit suite for the law that a vision
   prediction is never catalog truth until R0 verifies it. Never executed in CI;
   the lane read green throughout. Not a leak (`redact_screenshot` fails closed).
   Directly relevant to Phase 4: T-NAV-03 must extend that contract.
2. **The bare-button wizard gate.** `is_form` requires a fillable control, so a page
   that asks its question with buttons alone was never walked.
3. **`number` outcomes.** `journey_spec._NUMERIC_VALUE_TYPES` has always been
   willing to assert a number; `_BOUNDARY_OUTCOME_TYPES` never let one cross. P4
   had excluded it deliberately as noise, and P4 was right — the resolution was the
   candidacy signal `value_infer` already computes and nobody used.
4. **Persist-and-advance.** "Get Quote" satisfies every lexical veto in
   `_pick_persistence_control`, and on this funnel it POSTs and NAVIGATES. The walk
   crossed a navigation while believing it stood still, collapsing two states onto
   one fingerprint. Fixed structurally: a step's only actionable control is not
   persistence.
5. **Discovery-navigation attribution.** `_expand` reads displayed values last, by
   design, so an in-place reveal is captured — but a discovery click can navigate,
   and then another page's values are credited to the state we left.

**Every walked fixture in the suite is a same-URL SPA wizard.** That is why 4 and 5
could not be caught, and why `f2_auth_wizard`'s golden is clean: a URL refresh
cannot corrupt a wizard that never changes URL. The inventory gap Gate 3 named for
A22 — no application both walkable and backend-calling — was also a *coverage* gap.

### Two corrections to my own work, recorded because they were nearly shipped

* The first cut of P2b admitted the outcome page but carried none of its values, on
  the reading that `note_state_signals` is value-free. The account then looks
  correct from the outside while the compiler still cannot ground an assertion:
  `outcome_selectors` needs the **selector**, and only the state carries it. The
  blocker would have MOVED, not closed.
* I called the `number` exclusion an oversight and added it to the type tuple. A
  test caught me: P4 excluded it deliberately. Neither position was right.

### What Phase 4 still may not claim

P1 and P3 remain **RED** and the §10 remediation for them is untouched: Gate 2 is
still 1-of-3 (P1b), A29's credential is still invalid (P3a, B4), `gate4/phase3-proofs`
is still unmerged (P3b), and the fleet suite still does not execute in CI (P3c, B5).
**The entry gate is not open.** What changed is that P2's blocking milestone is no
longer blocked, and the reason A22 was blocked is now understood rather than
attributed to the application inventory alone.
