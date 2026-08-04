# Journey Graph — Release C Production Implementation Plan

**Date:** 2026-08-03
**Status:** Direction approved by founder (2026-08-03). Execution order is fixed: Release A → Release B (`E2E_ADVANCE_HARDENING_PLAN.md`) → **Release C (this document)**. No Release C phase starts before the Release A and B proof gates pass.
**Scope:** qe-explorer (decision-point capture, choice overrides), qe-central (graph store, fold, naming agent, branch planner, APIs), qecentral DB (Alembic chain `alembic_qec`), verdict-portal (journey surface)
**Product claim this release earns:** the system stops reporting *states visited* and starts reporting *named business journeys proven, path by path* — with unexplored branches as first-class visible objects, never as a disclaimer sentence.

---

## 1. Position: evolution, not greenfield

The Journey Graph is the evolution of the flow ledger, not a parallel model.

Today ([qe-explorer/app/flow_ledger.py](../../Nexus_power/engines/qe-explorer/app/flow_ledger.py)) a journey is a **linear path**: entry, ordered steps, terminal, derived `completed`. The ledger itself declares the limitation — `branch_coverage: False`, *"One path per journey. At each decision point a single option was taken, so business paths behind the other options were not visited."* Release C closes that confession:

```
TODAY                                RELEASE C
flow  = one walked path              journey    = named subgraph: entry → all discovered terminals
                                     node       = state (fingerprint), decision points marked
                                     edge       = observed transition, evidence-linked
                                     traversal  = one walked path (today's flow, unchanged)
                                     branch     = an option NOT taken — first-class, visible, plannable
```

Every flow ever recorded is day-one graph data: `flow_id = flow_id_for(entry_fingerprint)` already keys journeys by their stable entry, and each flow's step sequence contributes nodes and edges on first fold. Nothing is migrated destructively; the manifest stays the canonical evidence store and the graph is the queryable index over it.

**Naming collision guard:** qe-central already has `services/flow_grouping.py` — the heuristic Pages&Forms field grouping for the seed panel. That is a different artifact for a different purpose and is not touched, not reused, and not to be confused with the Journey Graph. All Release C code uses the `journey_` prefix.

## 2. Verified ground truth this plan builds on

| Fact | Where (verified 2026-08-03) |
|------|------|
| Flows ship **only** in the crawl manifest, capped at 200, with `flow_summary` | `crawler.py:735-736` |
| qe-central never reads, stores, or displays ledger flows today | no `flow_summary` reference outside the explorer |
| Per-field fill ledger already exists — `{name, signature, semantic_type, basis, provenance, filled, sensitive}` per control | `qe-explorer/app/forms.py:139-148` |
| qe-central reads crawl manifests server-side already (seed panel grouping) | `qe-central/app/routers/apps.py:864-873` |
| qecentral DB migrates via Alembic chain `alembic_qec`; latest revision `qec_003_app_environments` | `qe-central/alembic_qec/versions/` |
| Crawl scheduling queue exists (per-domain serialization) | `qe-central/app/controlplane/scheduling/crawl_queue.py` |
| Governance coverage machinery exists (`qec_scenarios`, `qec_coverage_atoms`, `qec_coverage_gaps`) | `qe-central/app/db/gov_models.py` |
| Portal source | `Nexus_power/verdict-portal/src/features/` |
| Release B P3 gives per-step `advance: {tier, control_name, oracle}` | `E2E_ADVANCE_HARDENING_PLAN.md` Phase 3 |
| Release B P4 gives `advance_memory` keyed by decision-point signature | `E2E_ADVANCE_HARDENING_PLAN.md` Phase 4 |

## 3. Doctrine for a graph (the honesty rules get sharper, not looser)

1. **Per-path claims only.** "Journey proven" is never a journey-level boolean. Every proven claim names the path (ordered fingerprints), the identity that walked it, the environment, and the time. The journey-level view is an aggregation the reader can expand, never a green light that hides paths.
2. **Unwalked is a record, not an absence.** Every enumerated option not taken at a decision point exists as a `journey_branches` row with a status. The portal renders it. Silence is forbidden.
3. **No combinatorial lies.** Full path counting is computed only while the enumerable path product at decision nodes is ≤ 64 per journey. Above that, the product is reported as "> 64 (not enumerated)" and coverage is stated per-decision-point-option — always truthful, never extrapolated to a percentage of an uncounted space.
4. **Values stay in the tenant.** Node/edge/branch rows carry labels, kinds, signatures, titles — UI shape. Chosen option labels are product UI text and stay tenant-scoped under RLS. Nothing in Release C contributes to cross-tenant priors (that door stays closed until a future, separately-consented release).
5. **Names are business prose.** Journey names come from titles and outcome shapes, never URL text (F1/F2 doctrine; the V_URL_TEXT auditor pattern applies to generated names). Operator renames always beat agent proposals and are never overwritten.
6. **The graph amplifies its foundation.** Fold only ingests flows produced by post-Release-A crawlers (manifest carries the crawler build; older manifests fold with `pre_hardening: true` marked on their traversals) — a green-washed path must not silently become a proven edge.

## 4. Data model (Alembic revision `qec_005_journey_graph` — `qec_004` was taken by Release B's advance memory)

All tables: `tenant_id` + `app_id` scoped, RLS enabled in the same migration (standing rule: no new table without RLS), FKs to existing `client_apps`. Timestamps are server-side UTC.

**`journey_nodes`** — one row per state the graph knows.
- `id` PK; `tenant_id`; `app_id`; `fingerprint` (unique with tenant+app); `url`; `title`
- `is_decision` bool — the step's control inventory contains at least one enumerable path-selecting control (radio / select / toggle / checkbox)
- `is_boundary` bool — a commit-labeled or danger control was present (the submit boundary lives here)
- `has_outcome` bool — outcome values (currency / decision / percent) were displayed here
- `first_seen_at`; `last_seen_at`; `stale` bool — not observed in the app's latest completed crawl; stale nodes are kept (history), marked, and excluded from active planning

**`journey_edges`** — one row per observed distinct transition.
- `id` PK; `tenant_id`; `app_id`; `from_fp`; `to_fp`; `trigger_label_norm` (unique with tenant+app+from+to)
- `advance_tier` smallint nullable (from Release B P3 evidence; null for pre-P3 traversals)
- `walk_count`; `first_walked_at`; `last_walked_at`

**`journeys`** — one row per entry point.
- `id` PK; `tenant_id`; `app_id`; `entry_fingerprint` (unique with tenant+app); `flow_id` (= `flow_id_for(entry_fingerprint)`, kept equal to the ledger's id so history joins for free)
- `business_name`; `name_source` enum `agent | operator | fallback`; `named_by` nullable; `name_proposed_at`
- `deepest_steps`; `last_proven_at` nullable; `created_at`

**`journey_traversals`** — one row per walked path (the graph's index into manifest evidence).
- `id` PK; `tenant_id`; `app_id`; `journey_id` FK; `exploration_id` (the crawl); `terminal`; `completed` (copied from ledger, still derived-only at source); `fully_answered`
- `path_fps` JSONB ordered fingerprint list; `identity_ref` nullable (member / synthetic identity used); `env_ref` nullable
- `pre_hardening` bool (doctrine rule 6); `created_at`

**`journey_branches`** — one row per enumerated option at a decision node.
- `id` PK; `tenant_id`; `app_id`; `node_fp`; `control_signature`; `control_label_norm`; `option_label_norm` (unique with tenant+app+node+signature+option)
- `status` enum `walked | discovered | planned | blocked` (`blocked` = a planned walk failed for an attributed reason and will not be silently retried)
- `walked_in_traversal` nullable FK; `last_status_at`

## 5. Phases

### Phase C0 — Decision-point capture (qe-explorer, additive)

The explorer starts telling the truth it already knows: *which choices were available and which one the fill made*.

**Changes**
- `forms.py`: the per-field ledger entry for enumerable controls (radio / select / toggle / checkbox) gains `options` (enumerated option labels, normalized, capped at 24) and `choice` (the option the Phase-A fill actually selected). Provenance already answers *why* — no new provenance values needed in C0.
- `crawler.py` `_walk_wizard` / `_expand`: each flow step gains `decision_points` — the subset of that step's field-ledger entries that are enumerable, carrying `{control_signature, control_label_norm, options, choice, provenance}`. Today's placeholder `fields_filled: 0 / fields_unfilled: 0` in walk steps is replaced with real counts from the step's fill result — the plumbing this phase exists to add.
- `flow_ledger.build_flow`: step schema accepts and passes through `decision_points` (additive; absent key remains valid so explore/target manifests are unchanged in shape).
- Manifest size guard: `decision_points` respects the existing flows cap (200) and per-step option cap (24); measured manifest growth is part of the exit gate.

**Exit gate** — unit tests: enumerable kinds captured with options + choice; non-enumerable kinds absent; caps enforced; explore/target manifests byte-compatible when no wizard ran; VKPower Life local crawl manifest shows the smoker/coverage-amount decision points with the chosen option.

### Phase C1 — Graph store + fold (qe-central)

**Changes**
- New `app/db/journey_models.py` with the five tables of §4; Alembic revision `qec_005_journey_graph` (upgrade + downgrade + RLS policies).
- New `app/services/journey_fold.py`: pure fold procedure `fold_crawl(tenant_id, app_id, exploration_id, manifest) -> fold_report`. Reads `manifest["flows"]` (same manifest-access pattern `apps.py` already uses), then per flow: upsert nodes by fingerprint (set `is_decision` / `is_boundary` / `has_outcome` flags from step data), upsert edges from consecutive step pairs with the advance trigger label, insert one traversal, upsert branches from each step's `decision_points` (chosen option → `walked`, others → `discovered` unless already `walked`).
- **Idempotency:** the fold is upsert-only on natural keys; re-folding the same crawl is a no-op except traversal insert, which dedups on (exploration_id, journey_id, path hash). Re-crawls that mint new artifacts (standing gotcha) fold cleanly because keys are fingerprints, not artifact ids.
- **Staleness:** after folding a *completed* crawl, nodes of that app not seen in it are marked `stale = true` (never deleted); branches on stale nodes leave active planning.
- Fold is invoked from the completion-callback path in `routers/internal.py` after existing completion work, wrapped so a fold failure logs WARNING and never breaks crawl completion (the manifest remains the source of truth; a fold can be replayed).
- New internal replay entry point (admin-token guarded, same auth family as existing internal routes): fold an existing exploration's manifest — this is how history (pre-C0 flows, `pre_hardening` traversals) enters the graph without re-crawling.

**Exit gate** — fold unit tests over synthetic manifests (idempotency, branch status transitions `discovered → walked`, staleness, pre-C0 flows fold without `decision_points`); migration up/down; RLS policy tests (cross-tenant read/write denied); replay of a real VKPower manifest produces expected node/edge/branch counts.

### Phase C2 — Journey naming agent (qe-central)

**Changes**
- New `app/services/journey_naming.py`, same caged-agent pattern as `advance_agent`: prompt carries entry title, ordered step titles, outcome value labels + types, terminal kind — **no URLs, no values**. Task `name_journey` through the existing `platform_api.complete_llm` seam. Output is a short business name + one-sentence description, validated: length caps, business-prose check reusing the F2/V_URL_TEXT guard (a name containing URL-ish text is rejected and falls back).
- Deterministic fallback when the LLM is unavailable or validation rejects: `business_name = entry_title`, `name_source = fallback`. Naming never blocks the fold; it runs after fold and updates the `journeys` row only when `name_source != operator`.
- Rename API: `PATCH /api/v1/apps/{app_id}/journeys/{journey_id}` (operator auth, existing app-scoped auth dependency) sets `business_name`, `name_source = operator`, `named_by`. Agent proposals never overwrite operator names (enforced in the update statement's WHERE, not in application hope).

**Exit gate** — naming prompt contains no URL material (asserted on the builder); rejection + fallback paths; operator-wins pinned by test (agent re-run cannot flip an operator name); VKPower journeys named in business language on the live fold.

### Phase C3 — Journey surface (qe-central API + verdict-portal)

**Changes**
- New read endpoints under the existing app router auth:
  - `GET /api/v1/apps/{app_id}/journeys` — journeys with per-journey rollup: paths walked / completed, branches walked / discovered / blocked, deepest steps, `last_proven_at`, staleness.
  - `GET /api/v1/apps/{app_id}/journeys/{journey_id}` — nodes, edges, traversals (with terminal + identity/env refs), branches, and the enumeration honesty block: path product if ≤ 64, else `"> 64 (not enumerated)"` with per-decision option coverage.
- Portal (`verdict-portal/src/features/`): a Journeys panel on the app studio — journey list led by business names; drill-down renders the graph with walked edges (evidence-linked to the crawl manifest views that already exist), **unwalked branches visually distinct and labeled "discovered, not walked"**, boundary nodes marked, truncated traversals showing their terminal reason verbatim (including `oracle_unavailable` from Release A). No aggregate percentage anywhere; rollups are counts with expandable path lists (doctrine rules 1-3).
- Portal deploy follows the standing portal convention (client build on VM/PowerShell — never git-bash — then the established portal deploy path).

**Exit gate** — API contract tests (rollups match table state; honesty block switches at the 64 threshold); portal renders walked vs discovered distinctly; a truncated traversal shows its terminal; screenshot set recorded for the register.

### Phase C4 — Member-driven branch walking (the coverage engine)

The branch backlog becomes work the system can execute: *walk the path behind the option nobody chose, as a coherent identity*.

**Changes**
- **Explorer — `choice_overrides`:** `ExploreRequest` and the `Crawler` constructor gain `choice_overrides: {control_signature → option_label_norm}` (additive, default empty, same DI pattern as `advance_oracle`). Consumed inside the Phase-A fill for enumerable controls only: when the control's signature matches, the override option is selected **if and only if** it is among the control's enumerated options. Overrides never inject free text, never touch password/sensitive fields, never alter any safety gate, and are recorded in the field ledger with a new provenance value `planned` so evidence says why that choice was made.
- **qe-central — branch walk planner:** new `app/services/branch_planner.py`. Input: a journey's `discovered` branches. Output: walk plans, each `{entry_url, choice_overrides, journey_id, branch_ids}` — one plan per branch set that a single traversal can satisfy (branches on the same path prefix combine; conflicting options on the same control never combine). Prioritization: decision nodes closest to the entry first, journeys with `has_outcome` nodes first (different premium = different business path = highest value).
- **Identity coherence:** each plan resolves an identity whose data honestly matches the forced choices — existing members whose resolved values already select the target option are preferred; otherwise the agent-mode coherent synthetic identity (field-learning machinery) is constrained to be consistent with the override (a forced "smoker: yes" produces a coherent smoker, not a contradiction). The identity used lands in `journey_traversals.identity_ref`.
- **Dispatch:** plans dispatch as E2E crawls scoped to the journey entry through the existing exploration dispatch + `crawl_queue` (per-domain serialization and the admission mutex hold — branch walks are ordinary crawls to the control plane). Branch status: `discovered → planned` at dispatch, `→ walked` when the fold sees the option chosen in a completed traversal, `→ blocked` (with the attributed reason) when the walk completes without reaching the option or the crawl fails attributed — `blocked` is surfaced, never silently retried.
- **Budgets and flags:** per-tenant flag `branch_walks_enabled` default **OFF** (surface-toggles cost doctrine); `QEC_BRANCH_WALKS_PER_CYCLE` cap (production default 4); every dispatched plan logs tenant, journey, branch ids, and identity ref at WARNING-visible level.

**Exit gate** — override unit tests (only enumerated options; sensitive fields untouched; `planned` provenance in ledger); planner tests (prefix combination, conflict separation, priority order); status lifecycle tests including `blocked`; live proof deferred to C-P.

### Phase C5 — Autonomy loop + journey claims

**Changes**
- After each fold, when `branch_walks_enabled` and unwalked branches remain and the per-cycle cap allows: plan and enqueue automatically (flag `journey_autowalk`, default **OFF**, requires `branch_walks_enabled`). The loop's stop conditions are explicit: no `discovered` branches, cap reached, or every remaining branch `blocked` — and the journey rollup then states which of the three it is.
- Journey claims join the reporting chain: the crawl coverage summary gains a `journeys` block (named journeys with per-path proven counts and branch backlog), and `flow_summary.branch_coverage` finally becomes conditional — `true` **only** for a journey whose enumerable options are all `walked | blocked`, with the note replaced by per-journey facts. The global claim stays `false` until every journey of the app earns it (the flag remains derived, never asserted — the same law as `completed`).
- Horizon (explicitly out of Release C, recorded so nobody scope-creeps it in): run/certification linkage (journey claims inside the Certificate-of-Execution), GitLab-diff → affected-journey regression selection, and cross-tenant journey-shape priors. Each needs its own plan and consent design.

**Exit gate** — autonomy loop unit tests (stop conditions, caps, flag gating); `branch_coverage` flip pinned by test (one `discovered` branch anywhere keeps it `false`); coverage summary contract test.

## Phase C-P — Release C live proof gate (VKPower Life, all six must pass)

1. **Graph proof:** one E2E crawl → fold → portal shows named journeys (quote, apply) with nodes, walked edges, and the smoker / coverage-amount decision points listed as branches `discovered, not walked`.
2. **Business-branch proof (the one that matters):** dispatch a branch walk forcing the alternative smoker option → second traversal recorded → **both branches walked, with different premium outcome values in evidence** — proof the graph captures genuinely different business paths, not page permutations.
3. **Identity coherence proof:** the branch traversal's identity is coherent with the forced choice (smoker identity answered smoker-consistent fields), visible in the field ledger's `planned` provenance plus coherent neighbors.
4. **Honesty proof:** a journey with remaining `discovered` branches shows `branch_coverage: false` at every level; the walked-path list names identity, env, and time per path; no percentage appears anywhere in the portal.
5. **Safety proof:** branch-walk crawls hit the same submit boundary with zero submissions recorded in the app (Release A gates hold under overrides).
6. **Blocked proof:** a branch made unreachable (option removed from the app build) ends `blocked` with an attributed reason surfaced in the portal — not retried, not silent.

Deploy per release: qe-explorer + qe-central BUILD + force-recreate (never docker cp); `qec_004` (advance memory) + `qec_005` (journey graph) migrations run in the qe-central deploy; portal via the standing portal build/deploy convention. Rollback: images tagged pre-deploy; migration has a real downgrade; all C4/C5 behavior is flag-OFF by default so rollback of *behavior* is a flag, not a deploy.

## 6. Order, size, risk

| Phase | Depends on | Size | Risk |
|-------|-----------|------|------|
| C0 | Release A (gates), B-P3 (advance evidence in steps) | S | Low — additive manifest fields |
| C1 | C0 | M | Medium — new tables + fold correctness; mitigated by idempotent upserts + replay |
| C2 | C1 | S/M | Low — caged agent, fallback never blocks |
| C3 | C1 (C2 for names) | M | Low-Medium — read-only surface |
| C4 | C1, B-P4 (signatures), members machinery | L | **Highest** — overrides touch the fill path; mitigated by enumerated-options-only rule, `planned` provenance, flag OFF |
| C5 | C4 | M | Medium — autonomy loop; mitigated by caps + explicit stop conditions |
| C-P | all | M | — |

Explore/target crawls remain bit-for-bit unchanged (no wizard → no `decision_points`; overrides default empty; oracle stays `None` there). The frozen factory is untouched. All Release C behavior that *does* anything (C4/C5) ships flag-OFF and turns on per tenant after C-P.

## 7. Acceptance criteria (founder sign-off checklist)

- [ ] Every historical and new crawl folds into a tenant-scoped, RLS-protected journey graph keyed by the ledger's existing `flow_id`s; re-folding is idempotent.
- [ ] Journeys carry agent-proposed business names, validated URL-free, with operator renames permanently winning.
- [ ] The portal answers "did you get all the way through Apply?" per path, per identity, per env, per time — and shows every branch nobody walked yet as a visible object.
- [ ] A branch walk driven by a coherent identity proves a second business path with a different outcome value, end to end, on VKPower Life.
- [ ] `branch_coverage` becomes earnable but only by exhausting enumerable options (`walked | blocked`), and remains derived — no caller can assert it.
- [ ] No Release C table or prompt carries user values, URLs in prose, or anything cross-tenant; C4/C5 are per-tenant flags, OFF by default.
- [ ] Path counting never extrapolates: products ≤ 64 are enumerated, larger spaces are stated as not enumerated with per-option coverage only.
