# "No.1" Requirements → Code Traceability (audited 2026-07-23)

**Question answered:** can Nexus QE-Central meet the founder's 7-requirement
"No.1 autonomous crawl & test platform" document — and what exactly is missing?

**Method:** a 10-agent codebase audit (one reader per requirement area, file:line
evidence required, docs distrusted) over branch `feat/qec-phases-0-6`, digested
into this matrix. 134 requirement items audited: **63 implemented · 63 partial ·
8 missing**. An adversarial spot-verification pass (14 claims) and a completeness
critic were rate-limited mid-run and are **pending** — statuses below are
reader-reported with code evidence, not yet independently re-verified.

**Verdict: the goal is reachable. ~70% is implemented and now live-proven; the
remaining 30% is (a) a ranked backlog of bounded engineering items and (b) ONE
genuinely new build — the R5 self-extending recovery agent — for which every
substrate piece already exists.**

The strongest proof is operational: on 2026-07-22/23 a live client session
exercised the entire chain on a real app — resilient crawl (3 crash classes
fixed) → honest substrate gate → generation (9 nav journeys + the P0 quote form
flow) → compiled Playwright → **verified green runs**, with an honest RED +
stored failure screenshot when the locator was genuinely ambiguous. Eight
root-cause fixes landed, each with an incident-replica regression test
(c6917e4, df0e4bc, f35c6fc, f84f2b3, b4a67fe, 9370e54).

---

## Scoreboard

| Req | Area | Readiness | One-line honest status |
|---|---|---|---|
| R1 | Universal crawl engine | **~75%** | 30 control types audited: core controls solid; sliders/custom-dropdown-fill/time-pickers/pagination/drag-drop are named gaps. "Never terminate on a control" — **done & live-proven.** |
| R2 | Environments + policy | **~80%** | Profiles, sealed creds, fail-closed submit gate all real. Gaps: no `uat` env_kind, prod read-only unenforced on the RUN path, env_kind unvalidated at write. |
| R3 | Explore vs Target modes | **~55%** | Explore: done. **Target mode (journey-confined crawl) does not exist** — the single biggest product gap. Combinations exist but 1-base-case only; ranking is structural, not business-impact. |
| R4 | Executable case anatomy | **~80%** | Steps/expected/validation/test-data/compiler/exports real. Missing: risk-level field, role permissions; exports drop most metadata. |
| R5 | Autonomous recovery agent | **~60% substrate, 0% loop** | Heal+diagnose machinery is deep (proof-gated re-anchor, refuse-to-heal taxonomy, honest stop_diag). **Self-extension is missing**; several key modules are built-but-unwired (defect_report, network_oracle, auto_diagnosis, agentic triage). |
| R6 | Continuous learning | **~70%** | Proven-control ledger live; regression-test-per-incident discipline demonstrated (5 paired commits this week). Gaps: ledger FIX_KINDS bug, no tests on the ledger itself, cross-tenant flywheel default-off, KB view unexposed. |
| R7 | Honest reporting | **~80%** | The doctrine is enforced at every layer. 5 of 8 outcome classes wired; Config/External-Dependency/unified-adjudicator missing. **Two ingest defaults can mint unverified green — P0 fix.** |
| R0 | Scale to 100 clients / 10k apps | **~65%** | RLS+admission+quotas+backups+observability implemented; **2 known-RED RLS findings**, qec-ci never green end-to-end, compose runs as superuser (bypasses RLS), crawl queue is dead code. |

---

## R1 — Universal Crawl Engine

**Proven core** (all with file:line evidence in the audit): buttons, links, text,
password, email, number (now min/max/step-aware — f84f2b3), native selects,
checkboxes, toggles, nav menus (aria-haspopup hover + aria-expanded click),
file uploads (`__nxSetFiles` fail-closed), open shadow DOM, same-origin iframes
(frame_selector recipe), SPA/pushState + hash routes, multi-step wizards
(danger-vetoed `_walk_wizard`), cross-origin iframes/canvas/closed-shadow
**positively detected and ledgered OPAQUE** (never silent).

**Never-terminate (the R1 core): implemented and live-proven.** Per-state
exception isolation (crawler.py:839-844), UNHANDLED/OPAQUE coverage ledger,
uncommitted-fill guard (auth.py — live incident), auth_incomplete public-page
resilience (live incident), honest stop_reason enum, clobber guard. The audit's
adversarial sweep found **no code path that kills a crawl on an unsupported
control**.

**Partial / missing (ranked backlog):**
1. Custom searchable dropdowns / autocomplete — open-probe reads options, but
   Phase-A fill uses `select_option` (native-only); no type-to-filter commit.
   *Quick win identified: route `kind=='select' && tag!='select'` through the
   existing open+click-option logic (`crawler._commit_choice`).*
2. Sliders — detected + honestly refused; `RANGE_SET` primitive unimplemented.
3. Time/month/week inputs — synthesized a DATE value (invalid → fill errors).
   *One-line branch: `'12:00'` for time.*
4. Pagination — actively defeated by url_template query-dropping.
5. Accordion/tree-revealed FORM content not re-inventoried (reveal drivers are
   select/radio/checkbox/toggle only).
6. Radio-GROUP assembly (`GROUP_ASSEMBLE`) — roadmap; unseeded groups left unset.
7. Drag-and-drop — absent, and not even ledgered as a blind spot (honesty leak).
8. `window.open` popups + native JS dialogs unobserved.

## R2 — Environment Selection

**Real:** per-app named environment profiles (base_url, cookies, headers,
basic-auth, data_overrides, fences, sealed KMS creds, AAD=env_id), run-time
binding (`schedule.run_environment` → env_context → runner), the two-layer
submit gate (fences.allow_submit + per-flow submit_approvals + disposable
attestation; explorer re-verifies per mutating request), fail-closed refusal
with auditable reasons when no authorization exists.

**Gaps (all bounded):** vocabulary is `{disposable, staging, prod}` — no `uat`
kind (UAT = profile name, inherits staging/prod posture); prod read-only is
enforced at crawl/attestation but **not on the ordinary run path** (runner never
consumes fences); `env_kind` stored unvalidated (`_ENV_KINDS` defined, unused);
`NEXUS_ENV` defaults to development (fail-open); sealed per-env login creds
never routed into runs; bound-profile delete/rename unguarded.

## R3 — Crawl Modes (the big one)

**Explore mode: done** (budgeted BFS, priority frontier, caged-LLM
priority_patterns + info-gain novelty, resume, honest stops).

**Target mode: MISSING — the largest single gap vs the requirement.** Scope is
host-level only (`_in_scope`); no path-prefix/journey confinement reaches the
crawler. *The audit found the exact seam: `_in_scope` (crawler.py:1894-1900) is
a single choke point every enqueue already consults — a path-prefix scope
parameter lands there.*

**Combinations:** grounded generation from captured option domains exists
(never guesses), but derives from ONE base case per artifact and ranks by a
structural heuristic (required+2/non-default+1), not business impact. The
criticality registry (money/PII/destructive) exists elsewhere and is never
applied to combinations. *Quick wins identified: loop combinations over each
grounded journey/form-flow base; band with criticality.evaluate; persist the
risk score into source_evidence.*

This is the client's literal ask (quote combinations: Term/10yr; Term/20yr/
Female/Tobacco…) — **P0 on the build plan.**

## R4 — Case Anatomy

**Real:** ordered steps with expected/expected_result per step + case-level
expected_outcome; assertion-grade validation (toHaveURL anchors, token-tolerant
value oracles, checked-state, selected-option text); per-step test-data
(data_ref → Excel column / Zephyr testData); deterministic compiler to runnable
Playwright (proven green live); Excel/CSV/JSON + qTest/TestRail/Zephyr/Xray
connectors (real REST, sealed creds); evidence grades (A/B/C per case).

**Gaps:** no `risk_level` field (additive add + deterministic seed from
criticality band); permissions = binary auth-required only (no role modeling);
priority archetype-hardcoded (P0/P1/P2 by case type); Excel/CSV export only 6
base columns (drops preconditions/priority/tags/grade); connectors map only
name/description/steps; screenshots are page-level frames, not per-step.

## R5 — Recovery Agent (the flagship build)

**Deep substrate, live:** headed re-run + ci_run_id-correlated observation;
similo re-anchor with floors/margins/role-gates + prove-green + 2×-confirm;
REAL_REGRESSION refuses to heal, re-confirms across 2 runs, persists full
stop_diag; auth-loss two-way detection + gated re-login; DATA_PRECONDITION
refuse-to-heal; proven-control ledger (heal once, seed later runs, quarantine
stale).

**Built but UNWIRED (the cheapest big wins in the codebase):**
- `defect_report.build_defect` — the filing-ready defect artifact has **zero
  call sites**; wire into the confirmed REAL_REGRESSION stop.
- `network_oracle` — classifier exists, never threaded into diagnose/triage.
- `agentic/auto_diagnosis.diagnose_failures` (always-on sentinel) + `agentic/
  triage` (PRODUCT/SCRIPT/ENVIRONMENT verdict) + semantic cross-field reasoner
  + Governor (budgets/provenance) — complete, tested, dormant.
- `heal_policy.evaluate_heal_tier`, hollow-green refusal, wait/scope recipe
  library — consulted nowhere.

**Missing:** the self-extending loop (diagnose product gap → generate
interaction strategy → validate → regression test → resume). The recipe
registry is static. **Design decision (aligned with the doctrine): the agent
PROPOSES a strategy + failing repro test behind a human gate; it never
self-modifies production silently.** Tonight's 8-fix session is the exact loop
to encode.

**Taxonomy:** ~8 of 9 classes exist in some form; add an explicit CONFIG cause
(env/profile vs recorded base_url mismatch — data already in hand).

## R6 — Continuous Learning

**Real:** proven-control ledger end-to-end for control_kind/reanchor/
interaction fixes; UACR 7-recipe library (code-level, benefits every tenant);
regression-test-per-incident discipline **demonstrated this week** (5 paired
fix+test commits); strict-xfail registry makes open regressions visible.

**Gaps:** `FIX_KINDS` gate silently drops nav/advance/nav_recover memos (the
write path passes them; the gate rejects them — one-line fix); **zero tests on
the ledger module itself**; heal calibration + false-heal benchmark are honest
"unavailable" stubs (originals lost to repo/VM divergence); cross-tenant
flywheel complete-but-default-OFF (needs consented tenants); `list_proven_
controls` KB view has no route/UI (learning invisible to users); qe-explorer +
qe-central + in-tree test suites not in any CI gate; 6 efd0269 regressions
open at HEAD awaiting founder sign-off.

## R7 — Honest Reporting

**Enforced everywhere:** 19-reason refusal taxonomy with client-friendly
translations (CI-lockstep test); FULLY/PARTIAL/UNHANDLED/OPAQUE coverage
ledger; conservative 5-way run reducer (dismissal only on positive evidence);
6-disposition cycle classifier (advisory signals can never downgrade a
regression); honest stop_diag surfaces; `__nxClick` refuses ambiguous binds;
AUTHENTICATED-AREAS-NOT-COVERED banners.

**Findings that violate the doctrine at the margin (cheap, P0):**
1. **Ingest green-wash holes:** a CI step with NO status field persists as
   `passed` (test_runs.py:158,215); a declared run-level `passed` can override.
   → treat absent status as broken/422.
2. **External-dependency misclassification:** network classifier takes the
   worst 5xx from ANY origin — a third-party outage would read as an app
   defect. → origin-gate against the recorded base host.
3. Missing enums: External Dependency Failure (absent), Configuration
   (diagnosed with remediation but no named class), capability_gap at RUN time
   (coverage knows it; runs don't). 4 disjoint verdict vocabularies need one
   canonical mapping.

## R0 — Scale (100 clients / 10k apps)

**Implemented:** qecentral RLS everywhere + CI coverage gate + behavioural
WITH-CHECK proof; redis admission mutex (atomic Lua, crash-healing leases,
fail-closed); advisory-lock leader election; Helm validateScaling render guard;
fail-closed quotas at 3 choke points; dual-DB backups + seeded restore drill;
ServiceMonitor/PrometheusRule over real hot-path metrics; artifact dedup with
app_id fingerprint.

**Honest reds/gaps:** page_visits RLS proof RED (grant/test mismatch — one
GRANT fixes CI); ground_truth_events RLS exists only in an operator script
(neither migrations nor CI apply it); **compose/VM runs platform-api as
superuser `nexus` → nexus-DB RLS dormant in that deployment**; qec-ci never
green end-to-end (so RLS/mutex/restore are wired-but-unproven in CI); crawl
queue is dead code (no production caller); quota plans read env var, not the
provisioning store; scale-safe settings are opt-in defaults.

---

## The build plan

### P0 — this/next week (pilot-hardening; every item bounded, most are wires not builds)
1. **Target mode v1** (R3): path-prefix scope at `_in_scope` + operator
   priority_patterns bypass; loop combinations over journey/form-flow base
   cases; criticality-banded combination ranking. *The client's quote-
   combinations ask.*
2. **Close the R7 green-wash holes**: absent step status ≠ passed; origin-gate
   the network classifier; add CONFIG + EXTERNAL_DEPENDENCY + capability_gap
   verdicts (data already in hand).
3. **Wire the dormant R5 modules** (each ~hours): defect_report into the
   REAL_REGRESSION stop; network_oracle into diagnose; auto_diagnosis into the
   run timeline; agentic triage into the analyze endpoint.
4. **R6 one-liners**: FIX_KINDS + ledger tests + expose the proven-controls KB
   route; put qe-explorer/qe-central suites into CI.
5. **Un-red the RLS proofs** (GRANT + apply ground_truth_events SQL in CI) and
   land the efd0269 sign-off (restores the Phase-0 auditor).
6. **UX debts from the live session**: seed-panel approvals visible when all
   fields auto-fill; old-app internal URL re-point; live-view end-of-run frame;
   RTM expected_result projection.
7. **Top-3 R1 controls**: custom-dropdown commit path, time-picker value,
   slider RANGE_SET.

### P1 — 2-3 weeks
- **Recovery Agent v1 (R5)**: encode the manual loop — watch runs → classify
  through the (now-complete) taxonomy → product-gap findings become a proposal
  bundle (diagnosis + failing repro test + suggested strategy) behind a human
  gate → on approval, deploy + auto-rerun + regression test. LLM parts run
  under the existing Governor.
- R1 second wave: pagination, accordion-reveal re-inventory, radio-group
  assembly, dialogs/popups observed.
- R4: risk_level field + full-metadata exports + connector field mapping.
- R2: env_kind write-validation, `uat` kind, prod read-only enforced at run,
  NEXUS_ENV fail-closed.

### P2 — month+
- **Self-extension (R5/R6 full)**: agent authors new interaction strategies +
  their regression tests from heal-ledger mining; promotion pipeline from
  proven data-plane heals to code-level UACR recipes.
- Cross-tenant flywheel activation (needs consented tenants) + calibration
  analytics rebuild.
- Scale proofs: qec-ci green end-to-end, non-superuser DSN on compose, crawl
  queue wired, k8s backup CronJob, load-test gate on staging.

---

## Caveats (what this document does NOT claim)
- The adversarial verify pass (14 spot-checks) and completeness critic did not
  run (session rate-limit); statuses are single-reader with code citations.
  Re-run is queued; any refuted row will be corrected here.
- "Live-proven" claims cover the pilot VM deployment of 2026-07-22/23, not CI:
  qec-ci has never completed green end-to-end.
- 6 efd0269 strict-xfail regressions remain open at HEAD pending founder
  sign-off (docs/FINDINGS_PLATFORM_API_REGRESSIONS_2026-07-21.md).
