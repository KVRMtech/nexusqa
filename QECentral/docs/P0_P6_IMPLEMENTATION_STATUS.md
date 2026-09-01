# P0–P6 Implementation — Honest Status Report

**Run:** autonomous overnight implementation of the [Dynamic Catalog + Agentic Framework plan](./DYNAMIC_CATALOG_AGENTIC_IMPLEMENTATION_PLAN.md).
**Branch:** `feat/qec-dynamic-catalog-p0-p6` (8 commits, local only — **not pushed, not deployed**).
**Verification:** explorer suite **598 passed**; qe-central suite **1432 passed, 68 skipped** (DB-gated). All new code is additive and behind existing gates.

> **What "done" means here.** Every phase below is real, committed, unit-tested code. It is **not** live-proven — the plan's "done" gates require a watched crawl on the disposable VKPower env, which needs the running environment and a person watching (and deploy touches the KEK / force-recreate data-loss path). So each phase is marked **CODE-COMPLETE + TESTED** or, where a part needs the live env / runner / vision, **DEFERRED (live-dependent)** — never "done" when it isn't.

---

## What was deliberately NOT done (and why)

- **Not pushed, not deployed.** Deploy = force-recreate on the shared VM (documented KEK data-loss trap), and every "done" gate needs a watched live run. Left for a watched session.
- **No live proof.** Cannot be produced autonomously — needs the running app + someone watching the crawl.
- **Live/runner/vision-dependent parts deferred, not shipped blind** — implementing payment/eSign/PDF-capture/vision blind would be exactly the green-wash this product exists to prevent.

---

## Per-phase status

### P0 — Foundation hardening · ✅ CODE-COMPLETE + TESTED
- `deploy.ps1`/`deploy.sh`: rebuild `nexus-base:dev` before qe-central when `sdk/nexus-sdk` or the base Dockerfile changed in the pulled range (or `--rebuild-base`/`-RebuildBase`). Closes the silent-staleness gap. PowerShell parse-checked; bash `-n` clean. **Not run against the VM.**
- `ci.yml`: added a **blocking** `qe-central-tests` job (1400+ tests were ungated). Verified locally green with the CI env vars.
- `crawler._answer_questionnaire`: demoted the per-question `WARNING` to a structured `INFO` event (fleet-noise fix); behavior unchanged. Added lock-in tests (17-question scale, 3-option questions, auth-chrome exclusion).
- **Honest correction:** dropped the planned "advance-rung coverage atom" — `coverage.py` models a universe of things-to-cover with shrinkage/verdict math; advance-rung is step metadata (already on `JourneyEdgeRow.advance_tier`). Forcing it into `ATOM_KINDS` would corrupt that math.

### P1 — Branching engine (trigger→child) · ✅ CODE-COMPLETE + TESTED · ⏳ end-to-end PENDING LIVE PROOF
- `flow_ledger.activated_signatures(before, after)`: value-free, **count-aware** diff (a revealed same-label Yes/No question raises the count, so it's caught).
- Walk loop emits `reveals` on the answered question by diffing the inventory it already re-observes; DP sanitizer carries `reveals` through.
- `_answer_questionnaire` honors a forced option from `choice_overrides` keyed by the question signature → a planned re-crawl can walk the "Yes" side. Naturally gated (overrides only exist for planned walks).
- `qec_011`: additive `reveals` JSONB on `journey_branches` (RLS inherited). `journey_fold.merge_reveals` unions across crawls; stored on the **walked** branch only.
- **No new flag needed:** questionnaire branches are `discovered`, so the existing `branch_walks_enabled` gate + `branch_planner` already dispatch the Yes-side re-crawl with a question-sig override that `_answer_questionnaire` now consumes.
- **PENDING LIVE:** the full discover → plan → re-crawl → fold-both-sides loop on a real questionnaire app.

### P2 — Master Catalog + metadata · ✅ CODE-COMPLETE + TESTED · ⏳ route + persistence PENDING
- `catalog.question_id_for`: stable, value-free id from the control signature (falls back to normalized name) — survives a re-crawl's new `artifact_id` (Δ2).
- `extract_controls` now stamps `question_id` + `validation` shape (HTML constraint attrs only).
- `build_master_catalog(nodes, edges)`: dedups questions by `question_id` across every journey/node into ONE app-scoped catalog (400 questions live once); records pages, sticky-required, `expected_next_page` from edges.
- `snapshot_catalog`: deterministic shape hash for versioning (P2) + regression (P6).
- `qec_012`: `catalog_questions` + `catalog_versions`, **RLS-forced** (hold question text). ORM `CatalogQuestionRow` + `CatalogVersionRow`.
- **PENDING:** the `GET /apps/{id}/catalog` route + fold-time persistence to the new tables + live proof.

### P3 — Persona journey projector · ✅ CODE-COMPLETE + TESTED · ⏳ bridge + generation PENDING
- `journey_projector.project_traversal(questions, rules, answers)`: the missing primitive (Δ3). Pure graph simulation → `{visible, executed, activated, skipped}`, fixpoint for transitive activation. **Never invents an answer.**
- `persona_answers_for`: maps a persona's answer sheet onto catalog `question_id`s by name/semantic; unmatched ignored.
- `qec_013`: `personas` + `persona_journeys`, RLS-forced; provenance `inferred` until a verifying traversal makes it `live_confirmed`. ORM `PersonaRow` + `PersonaJourneyRow`.
- **PENDING:** the platform-api→qe-central persona-answer bridge; the reveals→child-question-id reconciler that feeds the rules; journey generation + verifying-crawl dispatch; live proof.

### P4 — Full tail to sales packet · 🟡 PARTIAL (outcome capture done) · ⛔ payment/eSign/PDF DEFERRED
- **Done + tested:** relaxed the boundary outcome filter (all 4 sites → shared `_BOUNDARY_OUTCOME_TYPES`) to keep a **policy-number/confirmation `reference`** as evidence of issuance. `value_infer` already classifies these; noise types stay excluded.
- **DEFERRED (live/runner-dependent, not shipped unverified):** payment fill lane (test-card provider), eSignature widget driver, sales-packet **PDF download** capture in the runner `server.js`, and relaxing the live-crawl `Authenticator` to accept `member_number`/`pin`. These need the runner (JS), sandbox gateways, and live proof.

### P5 — Fallback ladder · ✅ CODE-COMPLETE + TESTED (policy) · ⛔ Perceiver/vision DEFERRED
- `fallback_ladder.resolve_rung`: descends **deterministic → agentic(VERIFIED) → record_once → human**. The anti-green-wash rule is enforced in code: an agent action that resolved but couldn't be **verified** does not count — it descends, reason recorded. Maps each rung to a coverage provenance.
- `coverage_by_rung`: per-app roll-up by rung + how many need a human — no averaged autonomy number.
- `touch_meter`: `TOUCH_WIDGET_RECORD` / `TOUCH_WIDGET_RESOLVE`.
- **DEFERRED (live/vision):** the Perceiver orchestrator (`consult_vision` ↔ `complete_vision`), Cataloguer/Brancher/Persona-Answerer LLM lanes, wiring the ladder into the crawler's stuck-decision path, live proof.

### P6 — Catalog regression · ✅ CODE-COMPLETE + TESTED · ⏳ route + dashboard PENDING
- `catalog_diff.diff_catalogs(old, new)`: diffs two snapshots by stable `question_id` → `{added, removed, changed, unchanged}` with field-level kinds (`renamed`, `options_changed`, `validation_changed`, `required_changed`, `rule_changed`, `moved_next_page`). Turns a re-crawl into change-detection for a regulated app.
- **PENDING:** wiring to a route + reading/persisting `catalog_versions`; the multi-replica hardening toggles (inert today); the fleet dashboard; live proof.

---

## Migrations added (linear chain, compile-clean)

`qec_010` → **`qec_011`** (branch reveals) → **`qec_012`** (master catalog + versions) → **`qec_013`** (personas + persona journeys). All additive; new tables holding business text are RLS-forced. **Not yet applied to any database.**

## Guardrails honored throughout
Every new capability is additive and behind existing gates; no guardrail weakened (submit triple-gate, `prod_guard`, refuse pack, RLS on business-text tables, honest-coverage provenance). Value-free mechanisms; no life-insurance content hardcoded.

## Two findings worth your attention
1. **CI wiring is worth confirming.** `.github/workflows/ci.yml` lives under `Nexus_power/`, but the git root is `nexusqa/`. If the pushed GitHub repo is rooted at `nexusqa`, GitHub Actions won't trigger this workflow at all (it looks for `.github` at the repo root). This affects the **entire** pre-existing CI, not just the `qe-central-tests` job I added — worth a look.
2. **Repo-wide lint/format debt (pre-existing).** There is no repo-wide ruff config (only one scoped to `sdk/nexus-sdk`), and existing untouched files don't satisfy bare `ruff format`/`ruff check` defaults. My new code matches the repo's hand-formatted style and introduces **zero** pyflakes (`F`) issues; the format/import-order flags are a pre-existing whole-repo condition, not a regression.

## Wiring session addendum (routes + the P1→P2→P3 join)

After the initial P0–P6 cores, the **catalog pillar was wired end-to-end** and the
three pillars were **joined**. qe-central now **1439 passed / 69 skipped**.

- **Catalog pillar live** (`catalog_store.py`): `persist_catalog_version` runs at
  the end of every fold (best-effort) → durable `catalog_questions` + a
  `catalog_versions` snapshot. Routes: **`GET /apps/{id}/catalog`** (the app-scoped
  Master Catalog), **`GET /apps/{id}/catalog/diff`** (P6 regression between the two
  latest crawls). DB-gated round-trip test.
- **P1→P2→P3 join** (the flagged modeling gap, now decided + resolved):
  questionnaire questions are folded into the Master Catalog with a `question_id`
  derived from their control signature — the **same id space** the projector's
  rules use. `journey_projector.rules_from_branches` reconciles branch reveals
  (`kind:name`) to child question ids by name; unresolvable bare-button reveals are
  dropped honestly, not faked.
- **Journey generation** (`persona_journeys.py`): **`POST /apps/{id}/catalog/project`**
  — supply answers (by question_id or name), get the concrete journey (executed /
  activated / skipped). Proven in tests: tobacco=Yes activates the cigarettes
  question, healthy skips it — analytically, no crawl.

**Still pending (unchanged):** the platform-api→qe-central persona answer-sheet
bridge (to auto-drive the projector from stored personas), persisting projected
journeys to `persona_journeys`, the P4 tail (payment/eSign/PDF), the P5 vision
Perceiver, and **all live proof**. Still **not pushed / not deployed**; migrations
`qec_011–013` **not applied to any DB**.

## Recommended next steps (watched session)
1. Review the branch, then a **watched deploy** (`.\scripts\deploy.ps1` — it will rebuild the base if needed) to a disposable env.
2. Apply `qec_011`–`qec_013` to the qe-central DB.
3. **P1 live proof:** crawl a questionnaire app; confirm the discover→plan→re-crawl→fold-both loop produces trigger→child rules.
4. Then wire the pending routes/persistence (P2 catalog route + fold persistence; P3 bridge/generation; P6 route/dashboard) — each now has its tested pure core to build on.
