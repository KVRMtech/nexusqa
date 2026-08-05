# Autonomous Crawl → Catalog → Test Master Plan

**Date:** 2026-08-05
**Authors' lens:** Product architect · LLM architect · Crawl architect
**Status:** Direction proposal — thorough evaluation + phased, production-ready build. Awaiting founder go-ahead per phase.
**Prime directive:** the crawl must *operate* an arbitrary application, *know when its own action did not land*, *enumerate every decision it meets*, and *name every gap it could not cross*. Intelligence escalates; determinism leads; the app is never graded by itself.

---

## 0. The honest North Star: what "100% success crawl" means

Literal 100% — every arbitrary UI operated perfectly with zero human input — is not achievable, and any vendor who claims it is lying or defining "success" as "did not crash". We reject both. Our attainable, defensible, *stronger* target is **HONEST-100%**, four guarantees:

1. **Operate-or-name.** Every reachable control is either successfully operated (intent verified) or named as blocked, with the reason, the locator, and what was tried. No silent failure. (Tonight's client failure was a *silent* failure: three errored clicks reported as "3 fields filled".)
2. **Enumerate-every-fork.** Every decision point (radio group, product cards, HLQ options, select) is discovered and its options enumerated. Every option is exercised at least once across crawls; unwalked options are first-class visible records.
3. **Reach-or-explain.** Every journey is walked to a real terminal, or the walk stops with a named, attributed reason a client can act on.
4. **Never-twice.** A control operated successfully once is remembered (per-tenant, value-free); the next crawl never re-fights it.

The measurable proxy: **interaction success rate** (controls operated ÷ controls met) trending to 99%+, with the residual fully attributed — and **agent-escalation rate trending DOWN** over time as the deterministic ladder and mechanic memory absorb each new pattern. If the LLM fires *more* over time, the deterministic layer is under-built. That discipline is what keeps this economical across 10,000 apps.

This reframe is not a climb-down — it is the exact promise a regulated-insurance buyer will pay for: *"we show you what we operated, what we couldn't, and what we never tried"* survives an architect's cross-examination; *"we tested everything"* does not.

---

## 1. Current state — verified ground truth (2026-08-05)

What already exists (EXTEND, do not rebuild):

| Capability | Where | State |
|---|---|---|
| Central actuator that observes url/read-back/errors/dialogs/dom-diff | `qe-explorer/app/main.py` `_act` (976) | Live. Observes, but **does not assert intent** — `committed_value` is recorded, never compared to the intended value. "Did it land" is derived downstream by `classify_after` precedence. |
| One mechanism fallback (set_checked→click) | `main.py` `_act` (1002-1007) | Live (shipped tonight). The ONLY action-mechanism fallback. No per-archetype ladder. |
| Locator builder chain (role→label→text) | `main.py` `_locator` (1044-1054) | Build-time fallback only; **no action-time retry**, no css/xpath (though `qec.css_hint` exists in inventory, unused). |
| Rich UI primitives | `main.py` / `crawler.py` | hover, menu_reveal (1467), materialize/scroll (1112), probe_select_options (1594), probe_dependencies (1710), press_key, upload_seed, drain_network, `_interactive_signature` before/after. A strong toolbox already. |
| Discovery + frontier | `crawler.py` `_expand`(1017)/`_discover`(1208) | Clicks nav/links, enqueues hrefs, menu-reveal; fingerprint + url_template dedup; max_depth. |
| Multi-step wizard walk | `crawler.py` `_walk_wizard` (2061) | Fill→advance→record→repeat; honest terminals incl. oracle_unavailable. |
| 3-tier advance detection + crawl-time agent | `crawler.py` `_pick_advance_e2e` (1954) + `qe-central advance_agent` | Live. Agent = perception ("which button advances?") only. |
| Decision-point recording | `crawler.py` `_decision_points` (145) | Records fork + options + chosen option; does NOT act on the others. |
| Branch walking (take-every-option) | `qe-central branch_planner` + `journeys.py` dispatch | Live but **planner-driven**: one option per control per plan, dispatched as separate crawls; explorer has NO internal enumeration loop. |
| Journey catalog (graph) | qec_005 tables + journeys API | Live: nodes/edges/traversals/branches; business-named; per-path honesty. |
| Runnable journeys | qec_006/007 + linker + runner | Live: each completed journey → one E2E test case, Run + verdict fold-back. |
| Deterministic Playwright generation + owned code + edit/regenerate | frozen factory + script_versions | Live. |
| Value/rule oracle (grounded-or-UNVERIFIED) | answer_key stack + PROVEN-vs-INFERRED scorecard | Live — the honest-oracle substrate the approval lifecycle builds on. |
| Post-crawl agents (field classify, journey naming) | qe-central services | Live. |

**The three structural gaps this plan closes:**
- **G-ACT — actuation has no self-verification and no per-archetype ladder.** The crawl cannot reliably tell "I gestured" from "it worked", and has one ad-hoc fallback instead of a designed ladder. *This is what killed the client demo.*
- **G-ENUM — option enumeration is planner-driven and one-at-a-time.** The HLQ "take all nine, then every downstream branch" story is not systematically driven to completion; it's a loop of separately-dispatched crawls with no explosion control or risk prioritization.
- **G-ORACLE — "expected results" have no approval lifecycle.** Captured outcomes are observations; nothing turns them into confirmed expectations except ad-hoc, so validation cannot be claimed honestly.

---

## 2. Architecture: three pillars

```
PILLAR A — INTERACTION AUTONOMY            PILLAR B — COVERAGE ENGINE           PILLAR C — ORACLE LIFECYCLE
(operate any UI; know when you didn't)     (every option, every journey)         (make "expected" honest)

 R0 intent contracts (sensor)              E1 systematic enumeration            O0 Capture→Approve→Validate→Drift
 R1 deterministic ladder (per-archetype)   E2 combination strategy (risk)       O1 client-rule oracle (first-crawl)
 R2 crawl diagnostician (named stops)      E3 catalog as source-of-truth        O2 NL generation vs confirmed rules
 R3 Crawl Medic agent (caged escalation)                                        O3 Jira ingestion (connector, last)
 R4 mechanic memory (compounding)
 R5 vision escalation (flag-gated)

CROSS-CUTTING: environment postures (Dev/Test/UAT full+submit · Prod observe-only) — extend prod_guard
```

Governing laws (carried from existing doctrine, non-negotiable):
- **Determinism leads, intelligence escalates.** L0-L1 handle the overwhelming majority; the LLM is for genuine novelty, bounded per crawl, circuit-broken, honest-unavailable — identical governance to `pick_advance`.
- **The agent never asserts success.** It proposes a choice from a closed, reversible action vocabulary; deterministic verification (R0) decides. Green-wash authority is never handed to a model.
- **Every escalation is evidence.** Which layer operated each control rides into the coverage report, exactly as advance-tier evidence does now — the audit story AND the telemetry for growing the ladder.
- **Safety gates are depth-invariant and agent-invariant.** Submit boundary, danger gate, egress fence, commit veto are untouched by anything an agent decides or any depth reached.
- **The app is never its own oracle.** Correctness enters only via client-declared rules or human approval.

---

## PILLAR A — Interaction Autonomy

### Phase R0 — Intent contracts (the sensor layer) · DETERMINISTIC · EXTEND

**The fix that had to come first.** Every actuation declares an **intent** and verifies it, turning "I performed the gesture" and "the intent was achieved" into two separate, recorded facts. Intent-unmet becomes the trigger event for every layer above.

- EXTEND `_act` (main.py:976): after acting, compute an explicit `intent_met: bool` per intent kind — `selected` (read-back is_checked true / aria-checked / the card gained selected state via `_interactive_signature` delta on the control subtree), `value_committed` (read-back equals the intended value, not merely non-null — today it is never compared), `navigated` (url delta), `state_changed` (dom_changed). Return it on `RawObservation` (extend browser.py:89 dataclass, additive).
- EXTEND `forms.py` `_fill_one` (619) and `resolve_field` ledger: a fill whose intent is unmet is **honest residue**, never counted filled (tonight's toggle-error fix generalized to every kind). The per-field ledger gains `intent_met` + `attempts` so the coverage report can say *which* control failed and how.
- EXTEND `_walk_wizard` / `_discover`: consume `intent_met` — an advance whose click did not change state is already handled (dom_changed), but a *fill* that silently failed must now block the "form is answered" assumption.
- **Verifier:** the read-back + subtree-diff itself (your ACT-THEN-DIFF keystone, applied per-control). **Cost:** ~zero (already observing; now comparing).
- **Tests:** errored/no-op fills of every kind → `intent_met=False`, residue not counted; a genuine commit → `intent_met=True`. Golden-corpus scenario: the product-card page.
- **Proof gate:** re-crawl the client funnel; the coverage report names "product cards: selection intent unmet" instead of "3 filled".

### Phase R1 — Deterministic interaction ladder (per-archetype) · DETERMINISTIC · EXTEND

Make the one-off set_checked→click fallback a first-class, per-archetype ladder tried until R0 verifies intent. Covers the ~90% of custom controls that are not exotic — with zero AI.

- BUILD `app/interaction_ladder.py`: per control archetype, an ordered list of mechanic thunks. Reuse the existing primitives (they already exist — this *sequences* them):
  - **radio / choice-card:** native set_checked → click label → click wrapping card (anchor-scope locator) → focus + Space → focus + Arrow.
  - **custom select / listbox:** native select_option → `_probe_select_options` open + click matching `[role=option]` → type-ahead.
  - **custom slider:** native fill → focus + Arrow keys → drag (bounded).
  - **date:** native fill → open picker + click day cell.
  - **text with masks:** fill → type char-by-char (`press_key`) → paste.
- EXTEND `_act` to run the ladder for the control's archetype, stopping at first R0-verified rung; record `mechanic_used` (which rung won) on the observation.
- EXTEND `_locator` (1044): add css/xpath rungs from `qec.css_hint` (present in inventory, currently unused) as later builder fallbacks — a named-locator miss is not a dead end.
- **Verifier:** R0 intent-met. **Cost:** deterministic, bounded rungs, fast. **Governance:** ladder never includes a commit/danger control — it operates form controls only; the submit boundary is upstream and unchanged.
- **Tests:** each archetype's ladder selects/commits via a non-native rung when the native one is stubbed to fail; ladder stops at first success; danger control never laddered.
- **Proof gate:** the client product-cards select via the click/label rung, the funnel opens, the walk reaches HLQ.

### Phase R2 — Crawl diagnostician (named stops) · DETERMINISTIC-FIRST, thin agent · EXTEND

The fastest way a failed crawl reads as competence not breakage: it *tells the client exactly what blocked it*. Extend the existing typed crawl-diagnosis surface, do not build a new panel.

- EXTEND `qe-central crawl_diagnosis`: new codes derived deterministically from the R0/R1 evidence in the coverage report — `INTERACTION_BLOCKED` (a control's intent never met after the full ladder, with control name + locator + attempts), `WALK_BLOCKED_VALIDATION` (advance rejected while required fields unmet), `DECISION_UNRESOLVED`. Reads existing `flow_summary` + field ledger; no LLM required for the common cases.
- OPTIONAL thin agent (one call per genuinely-stuck walk): reads validation error texts + field ledger + last-action diff (caged, value-free) and writes a business-language remediation sentence: *"Blocked at product selection: the choice cards did not register a selection; Continue remains rejected by form validation."* Same governance as advance_agent (bounded, circuit-broken, honest-unavailable, never asserts).
- **Verifier:** the diagnosis only ever restates evidence present on the row (the standing never-invent law). **Cost:** deterministic path free; agent path one call per stuck walk.
- **Tests:** each new code from a representative coverage snapshot; agent-off path produces the deterministic sentence; agent never fabricates a reason absent from evidence.
- **Proof gate:** a deliberately-broken control yields a named, remediable diagnosis in the portal — the demo-saving behavior.

### Phase R3 — Crawl Medic agent (caged escalation) · AGENTIC · BUILD

For the genuinely novel widget the deterministic ladder cannot operate. Fires ONLY after R1 exhausts and R0 still reports intent-unmet.

- BUILD `qe-central/app/services/crawl_medic.py` + `POST /internal/operate-control` (HMAC, mirrors advance_agent exactly).
  - **Input (caged, value-free):** control shape (role/tag/name/attributes/css_hint), declared intent, the ladder rungs tried and what each observed, visible page error texts, sibling controls. Never values, never raw HTML.
  - **Output:** a choice from an **enumerated action vocabulary** — `click_candidate:<n>`, `press:<key>`, `open_then_pick`, `field_is_blocking`, `control_is_display_only_skip`. The explorer executes the choice via existing primitives; **R0 verifies**; an unverified proposal is refused and recorded.
- EXTEND explorer: an `operate_oracle` callable (mirrors `advance_oracle`), injected only in e2e, bounded by `QEC_MEDIC_MAX_CALLS`, circuit-broken, honest-unavailable → the control becomes named residue (R2).
- **Verifier:** R0. **Governance:** enumerated vocabulary only → auditable, reversible, safety-gate-invariant. **Cost:** one call per truly-novel stuck control, memoized by R4.
- **Tests:** medic proposal executed + verified path; unverified proposal refused; vocabulary is closed (an out-of-vocab reply → unavailable); danger/commit control never offered to the medic.
- **Proof gate:** a synthetic exotic widget (canvas-free custom control) operated via a medic pick, verified, recorded with `mechanic_used=medic`.

### Phase R4 — Mechanic memory (compounding) · DETERMINISTIC · EXTEND

Turns the per-crawl operate cost into a compounding asset — identical shape to advance memory / field learning.

- New qecentral table `control_mechanics` (RLS): tenant + control signature (value-free: role+tag+name-shape+attrs) → the mechanic that verified (`click_label`, `open_then_pick`, `space_key`, …), proof_count, last_proven_at. Consent-gated value-free cross-tenant priors, OFF by default.
- EXTEND `_act`/ladder: before the ladder, recall the proven mechanic for this signature and try it first; write-back only on R0-verified success (proof, not guess).
- **Verifier:** R0. **Tests:** recall hit skips the ladder; only verified mechanics written; signature carries no values/urls; RLS.
- **Proof gate:** second crawl of the client app operates the product cards with zero ladder iterations and zero medic calls.

### Phase R5 — Vision escalation (frontier) · MULTIMODAL · BUILD, flag-gated

For DOM-opaque surfaces (canvas, unlabeled custom widgets, iframes without accessible names) — the last rung, rare, expensive, optional.

- EXTEND the medic to a multimodal mode: screenshot + the opaque-surface bbox → propose a click region. Governed by your existing `hard_ui_healing_research` law: **refuse without an orthogonal oracle** — a vision pick is accepted only when R0 verifies the resulting intent.
- Flag `QEC_CRAWL_VISION_ENABLED` OFF by default; per-tenant. Bounded calls.
- **Proof gate:** a canvas control operated via a verified vision pick on a deliberate test surface.

---

## PILLAR B — Coverage Engine

### Phase E1 — Systematic option enumeration (the HLQ pattern) · DETERMINISTIC orchestration · EXTEND

Today branch walking is one-option-per-plan, separately dispatched, with no drive-to-completion. Make it systematically exhaust every option of every discovered decision point.

- EXTEND `branch_planner.plan_walks`: from a journey's discovered branches, generate the full enumeration set — every not-yet-walked option of every decision control on the proven path — not one per call. Each option → one walk plan (choice_overrides). The autowalk loop drives until the branch ledger has no `discovered` option left (walked or attributably blocked).
- EXTEND the ledger reconcile: an option walked deeper that itself reveals NEW decision points enqueues those as new discovered branches — the recursive HLQ explosion, captured breadth-first, each option's downstream journey fully walked (Pillar A makes each walk actually reach the end).
- **Explosion control (the honest-math guard):** per §0, enumerate exhaustively only while the per-journey option product ≤ the configured cap; beyond it, prioritize by risk (see E2) and mark the rest `deferred` with the count — never silently truncated, never claimed as "all combinations".
- **Verifier:** branch status lifecycle (discovered→planned→walked|blocked|deferred), all counts honest. **Cost:** one crawl per option; bounded per cycle; memoized advances/mechanics keep each cheap.
- **Tests:** a 9-option page yields 9 plans; a walked option revealing 2 new forks enqueues them; the cap defers with an honest count; nothing truncated silently.
- **Proof gate:** the client HLQ page — all options enumerated, each downstream journey captured, the branch ledger complete-or-honestly-deferred.

### Phase E2 — Combination strategy (risk-prioritized) · DETERMINISTIC + optional agent · BUILD

Full Cartesian combinations are exponential and dishonest to promise. Prioritize which multi-fork combinations to walk.

- BUILD a combination selector: pairwise (all-pairs) coverage across decision controls as the default (the industry-standard honest coverage model), plus any client-declared scenarios (from O1) as must-walk combinations, plus risk weight (outcome-bearing forks — a different premium — first).
- OPTIONAL agent: propose high-value combinations from the fork semantics (value-free labels) — a suggestion the deterministic selector schedules, never an autonomous action.
- **The claim this earns:** "every option exercised; pairwise combination coverage; your named scenarios guaranteed; the rest visible and deferred" — survivable, and stronger than "all combinations".
- **Proof gate:** pairwise plan generated for a 3-fork journey; client scenario forced as must-walk.

### Phase E3 — The catalog as source of truth · EXTEND

The vision's "single source of truth": pages, fields, allowed values, mandatory flags, validations-observed, locators (xpath/css/id/name), buttons, links, navigation, business-rules-observed.

- EXTEND the journey/evidence surface to expose, per node: the full control inventory already captured (name, role, locators incl. css_hint, options/allowed-values, required/mandatory as observed, validation messages seen, displayed outcome values). Most of this is *already captured in the manifest* — E3 is largely a **surfacing** task, not new capture.
- Mark every datum with provenance: **observed** vs **confirmed** (post-O0 approval) vs **client-declared** (O1). A locator is fact; an "allowed value" is observed-candidate until confirmed; a "business rule" is *observed behavior* until a rule confirms it (see Pillar C — never label observed behavior as a rule).
- **Proof gate:** the catalog view renders a journey's pages/fields/locators/allowed-values with honest provenance badges.

---

## PILLAR C — Oracle / Approval lifecycle (make "expected results" honest)

### Phase O0 — Capture → Approve → Validate → Drift · BUILD

The missing hinge. A captured outcome is an **observation**; it becomes an **expectation** only when a human who knows the business approves it.

- BUILD a journey **baseline lifecycle** state on the journey/traversal: `captured` → `approved` (SME clicked Approve on the captured steps + outcome values) → `validated` (a later run/crawl matched the approved baseline) → `drifted` (a later run/crawl differs — awaiting adjudication).
- BUILD the **drift adjudication** loop: a re-crawl/run whose outcome differs from the approved baseline presents the diff; a human rules **defect** (raise it) or **intended change** (baseline moves, with who/when audit). Never auto-absorb drift; never auto-fail on it.
- Everything unapproved wears **UNVERIFIED** openly (extend the existing PROVEN-vs-INFERRED scorecard to journey level).
- **This is the system-of-record** your seed strategy names — the product, not the crawl.
- **Proof gate:** approve the client quote journey's baseline; a re-crawl with an unchanged premium → `validated`; a changed premium → `drifted`, adjudicable.

### Phase O1 — Client-rule oracle (first-crawl validation) · EXTEND

The only oracle that validates on the *first* crawl. Client rate tables / eligibility rules → machine-checkable expectations, via the existing answer_key value/rule oracle stack.

- EXTEND answer_key ingestion: a rules artifact ("25yo non-smoker $250k term = $X") the crawl validates the captured outcome against immediately, grounded-or-UNVERIFIED (the FROZEN reducer law — no fabricated oracle, ever; this is the F-series incident's guardrail).
- **Proof gate:** a client rule confirms a captured premium on the first crawl, no human approval needed for that value.

### Phase O2 — NL generation against confirmed rules · EXTEND

The vision's "Create a test case for a 35-year-old female where premium = $40". The *user supplies the oracle* ($40) — that is legitimate and already the right shape.

- EXTEND the Co-Architect: NL request → find the journey → construct the constrained data (member/identity resolver + choice_overrides for the forks that select the profile) → generate case + Playwright asserting the user's expected value → if no confirmed rule supports the value, generate it but mark the assertion **UNVERIFIED until approved**.
- **Proof gate:** the 35yo-female-$40 request produces a runnable case whose premium assertion is honestly labeled.

### Phase O3 — Jira / manual-test ingestion · BUILD connector · LAST

A connector, not a differentiator — build once the catalog is trustworthy.

- BUILD Jira ingestion: import manual test cases → map to captured journeys by page/field/step similarity → generate executable case + Playwright grounded in the real captured flow → flag steps the manual test asserts that the crawl never observed (a gap either in the app or the manual test — surfaced, never silently dropped).
- **Proof gate:** a Jira manual case becomes a runnable grounded script; unobserved asserted steps flagged.

---

## Cross-cutting — Environment postures · EXTEND prod_guard

The vision's "works in Production" is a security-review disqualifier unless postured. Sell posture as the trust differentiator.

- EXTEND `prod_guard`: per-environment posture — **Dev/Test/UAT** = full-depth exploration + attested submit (existing disposable-env gate); **Production Test** = full-depth but submit only on explicit disposable attestation; **Production** = **observe-only** — capture pages/fields/locators/navigation, never fill a mutating field, never submit, never advance a commit. The crawl still catalogs prod; it never mutates it.
- **Proof gate:** a prod-posture crawl captures the catalog with zero mutating requests (guard-verified).

---

## 3. Sequence, extend-vs-build, effort, risk

| # | Phase | Build/Extend | AI? | Effort | Why here |
|---|---|---|---|---|---|
| 1 | R0 intent contracts | EXTEND | No | S | Everything stands on honest sensors; fixes the class of bug that hid the client failure |
| 2 | R1 deterministic ladder | BUILD+EXTEND | No | M | Fixes most real custom controls with zero AI — the demo-fixer |
| 3 | R2 diagnostician | EXTEND | thin | S | Turns a failed crawl into a named, client-facing remedy — fastest trust win |
| 4 | E1 systematic enumeration | EXTEND | No | M | The HLQ "every option" story, driven to completion honestly |
| 5 | O0 approve/validate/drift | BUILD | No | M | The hinge that makes "expected results" real — the system-of-record |
| 6 | R4 mechanic memory | EXTEND | No | M | Compounding moat; escalation-rate trends down |
| 7 | R3 Crawl Medic | BUILD | Yes | M | Genuinely novel widgets; caged, after the ladder |
| 8 | O1 client-rule oracle | EXTEND | No | S | First-crawl validation from declared rules |
| 9 | E2 combination strategy | BUILD | opt | M | Pairwise + scenarios — honest coverage model |
| 10 | E3 catalog surfacing | EXTEND | No | M | Single-source-of-truth view (mostly surfacing existing capture) |
| 11 | O2 NL depth | EXTEND | Yes | M | NL → case vs confirmed rules |
| 12 | Postures | EXTEND | No | S | Production trust; unblocks the security review |
| 13 | R5 vision | BUILD | Yes | L | Canvas/iframe frontier; flag-gated |
| 14 | O3 Jira | BUILD | Yes | M | Connector; last |

**Two releases to a defensible product:** *Release E (Interaction Reliability)* = R0+R1+R2+E1 — the crawl operates real UIs and names what it can't; this is what stops another demo failure. *Release F (System of Record)* = O0+R4+R3+O1 — approved baselines, compounding memory, novel-widget handling, first-crawl rules. Everything after is expansion.

The frozen factory, the runner, and Releases A–D behavior are untouched throughout; every phase is additive and flag-guarded where behavior could shift.

---

## 4. Acceptance — the founder sign-off checklist

- [ ] The crawl never reports an errored action as a success (R0); every unoperated control is named with locator + attempts.
- [ ] Custom radio/card/select/slider/date controls are operated by a deterministic ladder without AI (R1); the client funnel opens and reaches HLQ.
- [ ] A blocked crawl produces a named, business-language, remediable diagnosis in the existing portal surface (R2).
- [ ] Every discovered decision-point option is exercised or attributably deferred with an honest count; nothing is silently truncated (E1/E2).
- [ ] A captured journey outcome becomes an expectation only via SME approval; drift is adjudicated, never auto-absorbed (O0).
- [ ] A control operated once is remembered and not re-fought next crawl; agent-escalation rate trends down (R4).
- [ ] The novel widget that beats the ladder is operated via a caged, enumerated-vocabulary agent whose pick is deterministically verified (R3).
- [ ] Production crawls are observe-only, guard-verified zero-mutation (postures).
- [ ] No claim of "every combination" or "captures your business rules" survives in product copy — replaced by "every option exercised, every gap visible" and "observed behavior your SME confirms once, then validated forever."

---

## 5. The two lines to retire from the pitch, permanently

- ❌ "captures every possible combination of user journeys" → ✅ "enumerates every decision point, exercises every option, covers combinations by risk and your named scenarios, and shows every path not yet walked."
- ❌ "captures validations, business rules, expected outcomes" → ✅ "captures observed behavior and candidate rules; your SME confirms them once; from then on everything is validated automatically, with a signed certificate."

You lose nothing but exposure. You gain the only claim worth making to a regulated buyer: one that is true, checkable, and certifiable.
