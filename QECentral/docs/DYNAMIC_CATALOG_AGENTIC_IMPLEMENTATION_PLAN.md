# Dynamic Catalog + Agentic Framework — Production Implementation Plan

**Status:** authored 2026-08-08 · grounded in a full code map of `qe-explorer`, `qe-central`, `platform-api`, the runner, and the deploy/guardrail plane (workflow `wf_0a18d950-f49`, 7 parallel readers, 190 file reads).
**Companion:** [`AUTONOMOUS_CRAWL_TO_CATALOG_MASTER_PLAN.md`](./AUTONOMOUS_CRAWL_TO_CATALOG_MASTER_PLAN.md) (the crawl→catalog pillar plan) and the strategic roadmap artifact.

Goal restated: **point CRAWL at a URL → autonomously discover and PROVE every business journey the app supports → produce a replayable, versioned catalog, scaling to 1000+ apps without an engineer per app.** For the life‑insurance beachhead this means: one **Master Catalog** (every page, every question, every branch rule) → **persona answers** → **N business journeys** → the full tail (underwriting → payment → eSignature → policy issue → sales packet), each with evidence.

---

## 0. How to read this plan

Three rules govern every phase. They are not aspirational — they are the guard against building the wrong thing at fleet scale.

1. **Additive, not a rewrite.** Every change below extends a named, existing function or adds a module beside one. The core walk (`crawler.py::_walk_wizard`), the journey graph (5 tables in `db/journey_models.py`), the branch planner, the persona store, the LLM router, and the deploy/guardrail plane are **reused as‑is**. Zero rewrites.
2. **Generic mechanism; discovered or declared content.** The engine's *mechanisms* are value‑free (labels, signatures, refuse‑pack verbs, commit/advance regex). All life‑insurance *content* — the 400 questions, the branch rules, the page sequence — is **discovered** by the crawl or **declared** by the tenant as inputs. We never write `if page == "lifestyle"` or a fixed question schema into the engine. The code map confirms the substrate is already domain‑neutral (life‑insurance appears only in docstrings, opt‑in `DOMAIN_BOOST_PACKS`, and additive refuse‑pack verbs). Anywhere a phase risks leaking domain content into a mechanism, it is called out.
3. **Honest coverage; no green‑wash.** Every new capability reports through the existing honesty substrate — `coverage.py` provenance (`G_DETERMINISTIC` / `G_LIVE_CONFIRMED` / `G_INFERRED`), `tier_label.py` (`renders` < `behaves`), `dispositions.py` (fail‑closed to `ASK`), `touch_meter.py` (per‑band, never an averaged number). A capability that cannot ground or verify **stops and flags**, and coverage reports which rung of the ladder produced each result.

---

## 1. Grounding — what is ALREADY built (do not rebuild)

| Pillar | Already built (reuse) | The real gap |
|---|---|---|
| **Advance / stuck‑decision** | 3‑tier engine `crawler.py::_pick_advance_e2e` (L2549): regex → destination → **agent oracle** (`main.py::_make_advance_oracle` → qe‑central `/internal/pick-advance`). Danger‑forward crossing at L2604‑2614. | Oracle contract is *pick‑one* (`{index,status,signature}`); no branch enumeration. |
| **Branch forcing** | `crawler.py::_choice_overrides` (L777) — "BRANCH WALK (Journey Graph C4)", threaded into every `fill_form_phase_a`. `branch_planner.plan_walks` / `plan_pairwise_walks` enumerate options as planned re‑crawls. | Plans only `discovered`; walks the branch but does **not** record which questions the branch *activates*. |
| **Questionnaire** | `crawler.py::_answer_questionnaire` (L2293), positional targeting (`match_index` → `.nth()`), hooked at top of `_walk_wizard` loop. | Answers ONE option, "prefers negative" to *skip* follow‑ups (L2352‑2357) — the opposite of mapping the branch. |
| **Journey graph** | 5 RLS‑scoped tables (`db/journey_models.py`): journeys, nodes (`controls_inventory` JSONB), edges (`from_fp→to_fp,trigger_label_norm`), traversals (`path_fps,path_hash,completed`), branches (`status` law: discovered→planned→walked\|blocked\|deferred\|equivalent). | No object linking a trigger *option* → the *set* of revealed questions. |
| **Catalog** | `catalog.py::extract_controls` → per‑node `{name,type,options,required,depends_on,signature,semantic_type}` + provenance badges (observed/confirmed/client_declared); served per‑journey at `journeys.py:718 get_catalog`. | Per‑node/per‑artifact only; no app‑wide aggregate, no **stable question id**, no `validation`/`business_rule`/`expected_next_page`, not versioned. |
| **Personas / answer keys** | `persona_store.py::TpPersonaRow` (traits, behavior_class) + `TpPersonaExpectedValueRow` (answer sheet) + encrypted cards; `answer_key.py` fill/outcome contracts; `combinations.py`/`pairwise.py` generate cases; run‑side `_persona_auth_resolve`/`build_persona_bundle`. | **No answer→journey projector** (predict pages visited/executed/activated/skipped from catalog+rules+answers). Persona↔journey seam **crosses an unbridged service boundary** (personas in `platform-api`, journeys in `qe-central`). |
| **Agentic / LLM / vision** | Tiered `llm/router.py::LLMRouter`; qe‑central relays via `clients/platform_api.py::complete_llm`/`complete_vision`. `advance_agent.pick_advance` (value‑free, commit‑veto) + learning (`advance_memory`, `mechanic_memory`). Vision `vision_medic.consult_vision` (R0‑gated, breaker). Grounded answers‑oracle stack (`value_oracle`/`rule_oracle`/`nl_case_builder`) with hard grounding gates. | Perceiver not wired (`consult_vision` `propose_fn` ↔ `complete_vision` not joined); no LLM cataloguer for novel widgets. |
| **Tail** | Danger‑forward already crosses underwriting *toward* e‑sign (`crawler.py:2604‑2614`, `_execute_approved_submit`). Commit vocab includes pay/checkout/sign. Card fields classified value‑free (`field_semantics CARD_*`). Recipe‑replay auth is **generic** (member_number/pin/OTP slots), evidence capture (screenshot/video/trace + encrypted `storageState`). Runner `server.js`: `/run`, `/run-live`, `/auth-capture`, `/ground-truth-capture`. | No payment module, no eSignature‑widget driver, no policy/sales‑packet **download** capture; boundary outcome filter drops policy‑number/confirmation. |
| **Deploy / flags / guardrails / coverage** | `scripts/deploy.ps1` BUILD+RECREATE; `config.py::Settings` env flags + per‑tenant column double‑gate (`branch_planner.autonomy_flags`); `prod_guard.assert_crawlable` triple gate; honest‑coverage substrate; escape→guard CI law. | **Deploy never rebuilds `nexus-base:dev`** (SDK changes go stale — HIGH). **qe‑central test suite is ungated in CI.** |

---

## 2. Architecture deltas (the four hard truths the code imposes)

These are the load‑bearing design decisions. Everything in §4 follows from them.

**Δ1 — Branch enumeration is multi‑crawl, not an in‑page fork.** `BrowserPort` (browser.py L305) exposes only `goto(url)` as a reset; there is no context fork or `back()`. A global fragmentation trap (`_visited_fingerprints` L1368, `_answered_questions` L871, `_wizard_states` L2732, `_oracle_memo` L772) will silently swallow a naive second pass. **Design:** we do NOT add a fork primitive. We (a) record, in one crawl, *which questions each taken answer activated* (`reveals`), and (b) walk the alternative answer as a **planned re‑crawl** via the existing `_choice_overrides`/`branch_planner` machinery, keyed by a branch id so the fragmentation guards don't eat it, then (c) reconcile both observations into a stored **trigger→child rule**. This respects the architecture instead of fighting it. (An in‑place both‑option prober for a single questionnaire group is a *later* optimization, gated behind the same reconciler.)

**Δ2 — The Master Catalog is an app‑scoped aggregation with a stable question id.** `catalog.py` is per‑node; re‑crawl mints a new `artifact_id`. We add a **stable `question_id` derived from `control_signature`** (value‑free, stable across crawls) and an app‑scoped catalog table so cross‑crawl regression and "one catalog, many journeys" both work.

**Δ3 — The persona→journey seam crosses a service boundary and needs a pure projector.** Personas/answer sheets live in `platform-api` (`TpPersonaRow`); journeys/branch‑rules live in `qe-central`. There is no code joining them, and no function that *predicts* a journey from answers (today every alternative is a real crawl). We add a **pure `project_traversal(catalog, rules, answers)`** in qe‑central and a **one‑way bridge** that projects a persona's declared answers into the qe‑central answer‑key contract at onboarding — no new plaintext‑credential egress.

**Δ4 — The agentic layer is orchestration over built parts.** The six roles map onto existing modules; only two need a new orchestrator (Perceiver, LLM Brancher). The deterministic engine stays the fast path; agents fire only on stuck/ambiguous decisions, behind the R0 grounding gate and the `crawl_vision_enabled` flag.

---

## 3. Cross‑cutting production rules (apply to every phase)

- **Deploy sequence.** Push to `mine`/`develop` → SSH `verdict-box` → `git pull` → per‑plane `docker compose -f docker-compose.qec.yml build {qe-central,qe-explorer}` → `up -d --force-recreate`; platform‑api via `docker-compose.yml`. **BUILD+RECREATE, never `docker cp`** for backends. **P0 fixes the base‑image gap:** any change under `sdk/nexus-sdk` (or a new shared contract) requires `docker build -f infrastructure/docker/Dockerfile.base -t nexus-base:dev .` **before** the qe‑central/repo‑intel build, or the change never reaches those services. `qe-explorer` has its own Playwright Dockerfile (no base dependency).
- **Feature‑flag discipline (fail‑closed, double‑gate).** Every new behavior: (1) an env flag on `config.py::Settings` defaulting `False`, wired `${QEC_*:-false}` in compose; (2) for tenant‑scoped autonomy, a `TenantProvisioningRow` column (default `False`) ANDed with the env flag in a helper mirroring `branch_planner.autonomy_flags` (L96‑100). New capabilities ship **OFF**, are proven on a disposable env, then enabled per‑tenant.
- **RLS on business text.** Any new table holding **question text, business rules, or persona answers** is tenant‑scoped and RLS‑FORCED (copy `_apply_rls()` from `qec_005`). Value‑free priors (signatures, label priors) may stay RLS‑free by the existing precedent — but nothing with business content does.
- **Guardrails preserved.** `prod_guard.assert_crawlable` triple gate, `resolve_effective_fences` (prod → `observe_only`), the submit triple‑gate (disposable attestation + per‑flow approval + refuse pack), and `guard.load_refuse_pack` fail‑closed loading are **invariants** — no phase weakens them. New irreversible verbs go in `refuse_pack.yaml::irreversible_verbs` (bump `version`).
- **Honest coverage.** New result kinds register as `coverage.py::ATOM_KINDS` and report through `compute_coverage` with a provenance tag; new human‑touch types go in `touch_meter.TOUCH_TYPES`. No averaged autonomy number.
- **CI.** P0 adds a CI job for `platform/qe-central/tests/{unit,contract,harness}` (currently ungated). Every new escaped‑defect class added to `ESCAPED_DEFECT_REGISTRY` must name ≥1 guard + ≥1 test or `test_escape_guard_registry.py` fails. Contract goldens (`fixtures/golden_3page_manifest.jsonl`) extended when the manifest/record schema changes.

---

## 4. The phases

Each phase: **Objective · Build on · Changes · Data model · Flag · Tests · Coverage · Definition of Done · Depends on.** File references are real (`file::function` L#) from the code map.

---

### P0 — Foundation hardening & CI/deploy safety  ·  size S

**Objective.** Take today's live wins to production‑grade and close the two infra gaps that would silently poison every later phase.

**Changes.**
- **Deploy base‑image fix.** Add a guarded step to `scripts/deploy.ps1`/`deploy.sh`: when `sdk/nexus-sdk` (or a declared shared‑contract path) changed since last deploy, rebuild `nexus-base:dev` before the qe‑central build. Print the base image sha so staleness is visible.
- **CI gate for qe‑central.** Add a `qe-central-tests` job to `.github/workflows/ci.yml` running `platform/qe-central/tests/{unit,contract,harness}` (blocking, per‑file isolation like `platform-api-tests`).
- **Questionnaire productionization.** `crawler.py::_answer_questionnaire` — replace the fragile DOM‑order group pairing with an observable, tested grouping; keep the `match_index`→`.nth()` positional handle; emit a structured `qec.questionnaire.answered` telemetry event (demote the current WARNING to INFO/metric) so fleet‑scale coverage of questionnaire pages is measurable. **No behavior change to "which option"** — that moves in P1.
- **Coverage telemetry per rung.** Register a `coverage.py` atom kind for "advance rung used" (regex / destination / oracle / questionnaire / danger‑forward) so per‑app reports show *how* each step advanced — the honest substrate for the fallback ladder.

**Flag.** No new autonomy; telemetry only. Deploy/CI changes are infra.
**Tests.** CI job self‑tests on a no‑op PR; questionnaire grouping unit tests (extend `test_crawler_submit_p3.py`); a deploy‑script dry‑run check that the base‑rebuild branch fires on an `sdk/` diff.
**Coverage.** New per‑rung atom kind visible in `compute_coverage`.
**Definition of Done.** A one‑line `sdk/` change provably reaches qe‑central after deploy; qe‑central suite runs in CI and blocks; questionnaire coverage appears per‑app in telemetry with no WARNING noise.
**Depends on.** Nothing — starts now.

---

### P1 — Branching Engine: trigger→child rules  ·  size L  ·  the core

**Objective.** Capture, value‑free, that *answering option X activates question set {Q…}* — the "Q331=Yes → show Q332‑350" rule — and store it as a first‑class, queryable object. This is the capability the whole vision pivots on.

**Build on.** `_choice_overrides` (L777) + `branch_planner.plan_walks`/`plan_pairwise_walks` (existing enumeration) + the DP record schema (`flow_ledger.py:112‑147`) + `JourneyBranchRow`.

**Changes.**
1. **Emit `reveals` per option (explorer).** Extend the decision‑point record in `flow_ledger.py:112‑147` (and its producers `crawler.py::_decision_points` L187 / `_answer_questionnaire`) to include, per option taken, the `control_signature` set that *appeared* on the resulting observation vs the pre‑answer inventory — a value‑free diff of question signatures. This is captured **within the normal walk** (no fork): the crawl already re‑observes after each questionnaire click (L2306‑2308); we diff those two inventories.
2. **Discovery answer policy (explorer).** In `_answer_questionnaire`, gate the current "prefer negative" pick behind a `discovery_mode` flag: when on, the answer chosen for a group is the one the **branch reconciler** asked for via `_choice_overrides` (so the planner drives *which* side to walk), defaulting to the negative pick only when unplanned. **The fragmentation‑trap fix:** key `_answered_questions` (L871) by `(question_sig, forced_option)` so the alternative answer is walkable in its own planned re‑crawl instead of being deduped away.
3. **Branch reconciler (qe‑central).** New `services/branch_reconciler.py` that: consumes the `reveals` from the base crawl and from each planned alternative re‑crawl (already produced by `branch_planner`), and folds them into a **trigger→child rule** — `{node_fp, control_signature, option_label_norm → reveals:[question_id…]}`. Store on a new `journey_branch_rules` table (or a `reveals` JSONB column on `JourneyBranchRow`). Write during `journey_fold.py:277‑348`.
4. **Planner honours rules.** `branch_planner.plan_walks` gains a mode that, given rules, plans the *minimum* set of alternative walks to cover each trigger→child edge (don't enumerate 2¹⁷ combinations — cover each rule once, plus `plan_pairwise_walks` for interactions), logging what was capped (`deferred` status already exists) so coverage stays honest.
5. **Fold surfaces branch coverage honestly.** `flow_ledger.summarize` currently hardcodes `"branch_coverage": False` (L266). Replace with a real computed value from the reconciled rules, provenance‑tagged.

**Data model.** Migration `qec_011_branch_rules.py` (`down_revision="qec_010"`): `journey_branch_rules(rule_id, tenant_id, app_id, node_fp, control_signature, option_label_norm, reveals_json, source_traversal_id, provenance)`, RLS‑forced. ORM in `db/journey_models.py` (bind `QecBase`; registered via `alembic_qec/env.py:26`).
**Flag.** `QEC_BRANCH_DISCOVERY_ENABLED` (env, default `False`) AND `TenantProvisioningRow.branch_discovery_enabled` (default `False`), ANDed in an `autonomy_flags`‑style helper. Reuses the disposable‑env gate — branch discovery does more submits, so it runs only where submits are allowed.
**Tests.** Explorer: `reveals` diff unit tests (two inventories → activated set); the `(sig, forced_option)` dedup key. qe‑central: reconciler folds base+alt into a rule; planner min‑cover + cap logging; CI‑gated fold test asserting a Q=Yes→children rule is produced from a fixture. Extend the golden manifest fixture with a `reveals` field.
**Coverage.** New atom kind `branch_rule` (provenance `G_LIVE_CONFIRMED` when both sides walked, `G_INFERRED` when one side capped/`deferred`).
**Definition of Done.** On the disposable VKPower env, the lifestyle questionnaire yields stored trigger→child rules (Yes activates the follow‑up set; No skips it), branch coverage on the journey reports a real number, and nothing is deduped into silence.
**Depends on.** P0.

---

### P2 — Master Catalog + rich metadata  ·  size L

**Objective.** One **app‑scoped**, versioned catalog: every question with a **stable id**, text, answer type, required/optional, options, validation, business rule, and expected‑next‑page — the "Q001…Qn" master, not duplicated per journey.

**Build on.** `catalog.py::extract_controls` (per‑node inventory + provenance badges), `journeys.py:718 get_catalog` (per‑journey serving), `answer_key`/`rule_oracle` (business rules already flow as `client_declared`).

**Changes.**
1. **Stable question id.** In `catalog.py::extract_controls` (L89‑112), derive `question_id` from `control_signature` (value‑free, stable across crawls/artifacts). This is the join key that makes "one catalog, many journeys" and cross‑crawl regression possible (Δ2).
2. **App‑scoped aggregator.** New `catalog.py::build_master_catalog(app_id)` aggregating every `JourneyNodeRow.controls_inventory` across the app's journeys into a deduped question set (by `question_id`), with page/section derived from node fingerprints and `funnel_classifier` buckets. New route `journeys.py GET /apps/{app_id}/catalog` mirroring `get_catalog:751‑799` but app‑wide.
3. **Metadata enrichment.** Add `validation` and `expected_next_page` to the control record in `extract_controls`: `validation` from observed HTML constraints (required/min/max/pattern — value‑free) plus, where opaque, an LLM cataloguer lane (P5) tagged `G_INFERRED`; `expected_next_page` from the journey edges (`from_fp→to_fp` under the taken option). `business_rule` already arrives via `answer_key` rules (`client_declared`) — surface it on the catalog row.
4. **Persistence + versioning.** Persist the master catalog to `catalog_questions` and snapshot it per crawl into `catalog_versions` for P6 diffing (a re‑crawl's new `artifact_id` becomes a new version row, diffed by stable `question_id`).

**Data model.** Migration `qec_012_master_catalog.py` (`down_revision="qec_011"`): `catalog_questions(question_id, tenant_id, app_id, page_key, text, answer_type, required, options_json, validation_json, business_rule, expected_next_page, provenance, first_seen_artifact, last_seen_artifact)` and `catalog_versions(version_id, tenant_id, app_id, artifact_id, snapshot_hash, created_at)`. **RLS‑forced** (holds question text — business content).
**Flag.** `QEC_MASTER_CATALOG_ENABLED` (default `False`). Read‑only aggregation → tenant gate optional; ships behind env flag first.
**Tests.** Stable‑id determinism across two artifacts of the same page; aggregator dedup by `question_id`; metadata extraction (required/pattern → validation); `expected_next_page` from edges; version snapshot + hash. Extend catalog contract tests.
**Coverage.** Each question row carries provenance; `catalog_summary` counts by provenance so "how much is observed vs inferred vs client‑declared" is visible.
**Definition of Done.** `GET /apps/{id}/catalog` returns a single deduped Q001…Qn master for VKPower with per‑question metadata and provenance, and a re‑crawl produces a new `catalog_versions` row.
**Depends on.** P0; runs in parallel with P1 (consumes P1 `reveals` for the branch‑rule column when available, but does not block on it).

---

### P3 — Persona‑driven journey generation (+ the projector)  ·  size M

**Objective.** From **one** Master Catalog + branch rules + a **persona's declared answers**, produce a distinct business journey showing pages visited · questions executed · **dynamically activated** · skipped — without duplicating the catalog and without a full crawl per persona.

**Build on.** `persona_store.TpPersonaRow` + `TpPersonaExpectedValueRow` (answer sheet), `answer_key` contracts, `branch_planner`/`pairwise` (must‑walk seeding), `_persona_auth_resolve`/`build_persona_bundle` (run‑side auth already works).

**Changes.**
1. **Pure projector (qe‑central).** New `services/journey_projector.py::project_traversal(catalog, rules, answers) -> {pages, executed, activated, skipped}` — a graph simulation over `JourneyNodeRow`/`JourneyEdgeRow` + P1 rules, applying a persona's answers to compute the path analytically (no crawl). This is the missing primitive (Δ3); `persona_diff.diff_structure` only diffs two *already‑crawled* journeys.
2. **Persona answer‑key bridge (one‑way, no secret egress).** At onboarding, project a persona's `TpPersonaExpectedValueRow` answer sheet into the qe‑central answer‑key contract (`answer_key.py::persona_answer_key(persona)` merging answer sheet + traits into `{fill,outcomes,rules}`). This crosses the service boundary **one way** with *answer values*, not credentials — the risky plaintext‑credential relay is explicitly **not** built (run‑side auth already handles member cards). Persona → `personas`/`persona_journeys` rows live in qe‑central referencing the platform‑api `persona_id`.
3. **Journey generation.** New `services/persona_journeys.py` that, per persona, runs the projector to produce a predicted journey, then (optionally, gated) dispatches a **single verifying crawl** with `_choice_overrides` seeded from the persona answers to *prove* the predicted path (predicted vs walked = an honesty check, provenance `G_LIVE_CONFIRMED` vs `G_INFERRED`).
4. **Reuse combination/pairwise for the "20 journeys."** `pairwise.factors_from_branches` + `branch_planner.plan_pairwise_walks` already build covering arrays over branch options — feed persona answers as `must_walk` so the generated journey set is persona‑meaningful, not just combinatorial.

**Data model.** Migration `qec_013_personas.py` (`down_revision="qec_012"`): `personas(persona_id, tenant_id, app_id, name, source_ref)` and `persona_journeys(persona_journey_id, tenant_id, app_id, persona_id, journey_id, path_hash, activated_json, skipped_json, provenance, verified_traversal_id)`. RLS‑forced.
**Flag.** `QEC_PERSONA_JOURNEYS_ENABLED` (default `False`) + tenant column. The projector (pure) can run unflagged for internal preview; **dispatch of verifying crawls** is flagged.
**Tests.** Projector correctness on a fixture graph (answers → activated/skipped set); bridge projects an answer sheet into a valid fill contract without leaking card slots; predicted‑vs‑walked agreement on a golden journey; `persona_journeys` rows carry the right provenance.
**Coverage.** Predicted journeys are `G_INFERRED`; verified ones `G_LIVE_CONFIRMED`. The report never claims a persona journey is proven unless a verifying traversal exists.
**Definition of Done.** For VKPower, ≥3 personas (e.g. Healthy / Tobacco / Diabetes) each yield a distinct journey object with correct activated/skipped question sets from **one** catalog, and at least one is verified by a real crawl.
**Depends on.** P1 (rules) + P2 (catalog). Full‑E2E journeys need P4 for the tail.

---

### P4 — Full tail: underwriting → payment → eSignature → policy → sales packet  ·  size L

**Objective.** Extend the walk past the underwriting decision through payment, eSignature, policy issue, and **sales‑packet capture**, with the Payment‑Failure and eSign‑Failure journeys proven.

**Build on.** The danger‑forward crossing **already reaches toward e‑sign** (`crawler.py:2604‑2614` → `_execute_approved_submit` L2826‑2847); commit vocab includes pay/checkout/sign; card fields are classified value‑free; recipe‑replay auth is generic (member/pin/OTP); evidence capture (screenshot/video/trace, encrypted `storageState`, `session_handoff.assess`) exists; runner has `/run-live` and record‑once sidecars.

**Changes.**
1. **Capture the tail's outcomes.** Relax the boundary `outcome_values` filter (`crawler.py:2838‑2841`, currently currency/decision/percent only) to also capture policy‑number/confirmation/reference outcomes (`value_infer.py:43‑46` already detects `policy|reference`) — value‑free detectors, not domain strings.
2. **Payment fill lane.** Add a **test‑card value provider** keyed off the existing `field_semantics CARD_*` classification — sandbox/disposable card values supplied as tenant inputs (never hardcoded, never real PANs), used only under the disposable‑env submit gate. Model retry/decline as branch options (a declined payment is a branch, captured by P1).
3. **eSignature widget driver.** Today "sign" is treated as a boundary to *cross*. Add an operate‑the‑widget lane: for canvas/"I agree"/attest widgets, drive via the interaction ladder + (for opaque ones) the P5 Perceiver; DocuSign/Adobe iframes handled via the record‑once choreography (`login_observer.js`/`ground_truth_recorder.js` cloned to a payment/esign recorder). eSign failure = a branch.
4. **Sales‑packet / policy‑document capture.** Add a Playwright `download`/PDF handler to the runner `server.js` (none exists today) and a document artifact type persisted via the `auth_profiles.save_profile` encrypted‑blob + `session_handoff.assess` pattern. This is the evidence that a policy actually issued.
5. **Recipe‑replay auth for the tail.** Drive tail auth via the generic recipe interpreter (`compiler.py::_AUTH_SETUP_TS` strategy `recipe`), **not** the live‑crawl `Authenticator` (`auth.py:183‑186` hard‑requires username+password and would block member/pin logins). Confirm and standardize on the recipe path for member‑scoped tail runs.

**Data model.** A `document`/`sales_packet` artifact kind + storage seam (reuse encrypted‑blob pattern); no new journey‑graph tables required (tail pages are ordinary nodes/edges).
**Flag.** `QEC_TAIL_CROSSING_ENABLED` + tenant column; strictly disposable‑env only (`resolve_effective_fences` already forces `allow_submit=False` off non‑prod). Payment/eSign verbs stay in the refuse pack for prod — crossed only under blanket disposable approval, exactly as underwriting is today.
**Tests.** Outcome filter captures a policy number; test‑card provider fills CARD_* without leaking; eSign widget operate‑vs‑cross decision; runner download handler persists a PDF; a decline/eSign‑failure produces a branch. Guardrail test: none of this fires on a non‑disposable env.
**Coverage.** Each tail stage is its own coverage atom with provenance; a journey is "reached sales packet" only when a document artifact exists (no green‑wash).
**Definition of Done.** On disposable VKPower, at least one persona journey runs quote → application → underwriting → payment → eSignature → policy issue → sales‑packet **with a captured policy document**, and the Payment‑Failure + eSign‑Failure journeys exist as branches.
**Depends on.** P3 (needs a walkable persona journey to reach the tail); uses P5 for opaque payment/esign widgets.

---

### P5 — Agentic hard‑widget coverage & the fallback ladder  ·  size XL

**Objective.** The 1000‑app long tail — shadow‑DOM, canvas, drag‑drop, custom date/upload, iframes, CAPTCHA — perceived and driven by agents, **proven** by the verifier, with unresolved cases descending the ladder to record‑once or a flagged human, reported honestly.

**Build on (orchestrate, don't rebuild — Δ4).** `llm/router.py` + qe‑central `clients/platform_api.py::complete_llm`/`complete_vision`; `advance_agent.pick_advance` (the tier‑3 seed); `vision_medic.consult_vision` (R0‑gated, breaker=3, max=10); `perceptual_diff` ($0 pixel evidence); the grounded answers‑oracle stack (`value_oracle`/`rule_oracle`/`nl_case_builder.verify_outcomes`/`qe_agents.validate_intent_quotes`).

**Changes (the six roles → existing modules).**
- **Perceiver** — new orchestrator wiring `vision_medic.consult_vision`'s injected `propose_fn` to `platform_api.complete_vision` (the one seam that exists but isn't joined), using `perceptual_diff` + `semantic_oracle` as evidence. Reads a rendered page → structured control understanding for widgets the DOM can't explain.
- **Cataloguer** — extend `options_extractor._extract_one` (forced‑tool vision pattern) + `catalog.extract_controls` with an opaque‑widget lane feeding P2's metadata; ground via `field_agent.classify_fields`.
- **Brancher** — add an LLM priority signal behind the deterministic `branch_planner` (rank which forks matter) + extend `advance_agent.pick_advance` to *enumerate* (the current contract is pick‑one; add a branch‑enumeration response so the oracle can propose both sides for P1).
- **Persona‑Answerer** — thread a persona seed into `data_agent.refine_with_llm`/`build_llm_prompt`, keeping `enforce_ask_hardline` (no coverage → ASK, never guess).
- **Executor** — reuse `crawl_medic.consult_medic` + `mechanic_memory.recall_all` (the caged action layer), extend interaction vocab, keep the R0 gate.
- **Verifier** — reuse the answers‑oracle stack as‑is; every agentic action must re‑observe and prove it registered (`nl_case_builder.verify_outcomes`), else it stops.
- **Fallback ladder** — a dispatcher that descends **Deterministic → Agentic → Record‑once → Human‑flagged** per stuck decision, recording which rung produced each result via a `touch_meter.TOUCH_TYPES` entry, so per‑app coverage reports honestly.

**Flag.** `crawl_vision_enabled` (already exists) + a per‑tenant vision‑autonomy column; the ladder's record‑once and human rungs are always available. Vision cost governed by `vision_medic` breaker + `llm/config` tiers.
**Tests.** Perceiver returns a grounded structure on a shadow‑DOM fixture; verifier rejects an unproven action; ladder descends and logs the rung; `advance_agent` enumeration contract; parity tests for the duplicated vocab (`advance_vocab` ↔ explorer `vocab`, `field_agent.VOCABULARY` ↔ `field_semantics`) so a partial rollout can't drift.
**Coverage.** Every widget resolved carries its rung + provenance; "covered" is never claimed for a silently‑skipped widget — the ladder logs the skip.
**Definition of Done.** A representative hard‑widget page (shadow‑DOM or canvas) is catalogued and driven by the Perceiver, proven by the Verifier, and an unresolvable widget is reported as "needs record‑once/human," not skipped.
**Depends on.** P0; hardens continuously against the fleet; feeds P2 (metadata) and P4 (payment/esign widgets).

---

### P6 — Fleet scale & catalog regression  ·  size M

**Objective.** Everything app‑agnostic and zero per‑app code; catalogs become versioned artifacts whose **diff** flags what changed between crawls — turning the engine into a change‑detection system for regulated apps.

**Build on.** `catalog_versions` (P2), `journey_baseline.detect_drift` (outcome drift exists at `journey_fold.py:350`), `controlplane/cycle/regression_diff.py`, the honest‑coverage substrate, the per‑tenant flag pattern.

**Changes.**
1. **Catalog diff.** New `services/catalog_diff.py` diffing two `catalog_versions` by stable `question_id`: added/removed questions, moved branch (a changed trigger→child rule), broken rule, changed validation/expected‑next‑page. Surfaced as a regression report alongside `journey_baseline` outcome drift.
2. **Fleet reliability.** Turn on the already‑wired multi‑replica hardening where needed (`QEC_ADMISSION_BACKEND=redis`, `QEC_DAEMON_LEADER_ELECTION=advisory_lock` — inert today), and per‑tenant cost/concurrency controls via `llm/config` tiers + `reuse_coverage`.
3. **Honest fleet coverage.** A per‑app dashboard sourced from `coverage.py`/`tier_label`/`touch_meter` showing, per app: catalog completeness by provenance, branch‑rule coverage, persona‑journey verification rate, tail reach, and ladder‑rung mix — no averaged autonomy number.

**Data model.** No new tables beyond P2's `catalog_versions`; diff is computed.
**Flag.** Diffing is read‑only (unflagged); replica hardening behind the existing env switches.
**Tests.** Diff detects an added question, a moved branch, a broken rule across two version fixtures; dashboard aggregation correctness.
**Coverage.** The diff itself is a coverage artifact; regressions are `G_LIVE_CONFIRMED` changes.
**Definition of Done.** Re‑crawling VKPower after a deliberate question change produces a catalog diff that names exactly what changed, and the fleet dashboard reports honest per‑app coverage.
**Depends on.** P2 (versions) + P1 (rules) + P3 (persona verification rate).

---

## 5. Sequencing, critical path & gates

```
P0 ─┬─► P1 ─┬─► P3 ─► P4 ─► (demo: quote → sales packet, persona-driven)
    └─► P2 ─┘         ▲
    P5 (continuous) ──┴─ feeds P2 metadata + P4 widgets
    P6 ◄── needs P1+P2+P3
```

- **Critical path to a founder demo** ("dynamic branching + a few personas → sales packet on VKPower"): **P0 → P1 + P2 (parallel) → P3 → P4.** P5 and P6 harden/scale alongside.
- **Gate between phases (no green‑wash):** a phase is "done" only when its Definition of Done is met **on a live disposable crawl**, its results report through `coverage.py` with correct provenance, and its flag can be turned on per‑tenant without weakening a guardrail. Passing tests alone does not close a phase — this matches the project's standing "verify live, not just green tests" discipline.
- **The full 20‑journey, 1000‑app posture is the whole programme**, not P4. Scope the initial release to the app classes the engine already handles (standard forms, wizards, button questionnaires) + the P1–P4 spine on the beachhead, and let the P5 ladder report honest coverage on everything else.

## 6. Risk register

| Risk | Phase | Mitigation |
|---|---|---|
| **`nexus-base:dev` not rebuilt by deploy** → SDK changes silently stale (HIGH). | P0 | Deploy‑script base‑rebuild step + printed sha; first thing built. |
| **qe‑central tests ungated in CI.** | P0 | Add blocking CI job. |
| **Fragmentation trap eats the branch re‑walk** (global `_visited_fingerprints`/`_answered_questions`). | P1 | Key dedup by `(question_sig, forced_option)`; enumerate via planned re‑crawls, not an in‑page fork. |
| **Oracle contract is pick‑one, not enumerate.** | P1/P5 | Extend `advance_agent.pick_advance` + `/internal/pick-advance` with a branch‑enumeration response (additive). |
| **Persona↔journey seam crosses services; no projector.** | P3 | Pure `project_traversal` in qe‑central + one‑way answer‑value bridge (no credential egress). |
| **Live‑crawl `Authenticator` hard‑requires username+password** → blocks member/pin logins for the tail. | P4 | Drive tail auth via the generic recipe interpreter, not `auth.py` `Credentials.from_payload`. |
| **No download/PDF capture in the runner** → can't prove policy issuance. | P4 | New Playwright download handler + document artifact (encrypted‑blob pattern). |
| **Vision cost / hallucination at fleet scale.** | P5 | Reuse `vision_medic` breaker + R0 grounding gate + Verifier; ladder falls back to record‑once/human, logged. |
| **Question text leaks business context if RLS missed.** | P2/P3 | All new business‑text tables RLS‑forced (copy `_apply_rls()` from `qec_005`). |
| **Duplicated vocab drift** (`advance_vocab`↔`vocab`, `field_agent.VOCABULARY`↔`field_semantics`). | P5 | Parity‑pinned tests; change both together. |
| **Domain leakage into a mechanism** (e.g. baking insurance questions into code). | all | Keep content discovered/declared; DOMAIN_BOOST_PACKS opt‑in; review each phase for `if page ==` smells. |

---

*Grounded in code map wf_0a18d950-f49. Sizes (S/M/L/XL) are directional engineering effort for one small squad, not commitments. Every `file::function` reference was read, not assumed; anything the map could not confirm is marked "confirm during implementation" in the phase text.*
