# Autonomous Crawl → Catalog → Test Master Plan

**Date:** 2026-08-05
**Authors' lens:** Product architect · LLM architect · Crawl architect
**Status:** Active build — Release E COMPLETE. **Release F COMPLETE.** Expansion: E2 SHIPPED, E3 SHIPPED, Postures SHIPPED, O2 SHIPPED, R5 SHIPPED 2026-08-05. O3 Jira next (last).
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
 R2 crawl diagnostician (named stops)      E3 catalog as source-of-truth        ~~O2 NL case builder~~ ✓
 R3 Crawl Medic agent (caged escalation)                                        O3 Jira ingestion (connector, last)
 R4 mechanic memory (compounding)
 ~~R5 vision escalation (flag-gated)~~ ✓

~~CROSS-CUTTING: environment postures (Dev/Test/UAT full+submit · Prod observe-only) — extend prod_guard~~ **SHIPPED 2026-08-05**
```

Governing laws (carried from existing doctrine, non-negotiable):
- **Determinism leads, intelligence escalates.** L0-L1 handle the overwhelming majority; the LLM is for genuine novelty, bounded per crawl, circuit-broken, honest-unavailable — identical governance to `pick_advance`.
- **The agent never asserts success.** It proposes a choice from a closed, reversible action vocabulary; deterministic verification (R0) decides. Green-wash authority is never handed to a model.
- **Every escalation is evidence.** Which layer operated each control rides into the coverage report, exactly as advance-tier evidence does now — the audit story AND the telemetry for growing the ladder.
- **Safety gates are depth-invariant and agent-invariant.** Submit boundary, danger gate, egress fence, commit veto are untouched by anything an agent decides or any depth reached.
- **The app is never its own oracle.** Correctness enters only via client-declared rules or human approval.

---

## PILLAR A — Interaction Autonomy

### Phase R0 — Intent contracts (the sensor layer) · DETERMINISTIC · EXTEND · **SHIPPED 2026-08-05**

**The fix that had to come first.** Every actuation declares an **intent** and verifies it, turning "I performed the gesture" and "the intent was achieved" into two separate, recorded facts. Intent-unmet becomes the trigger event for every layer above.

**As-built (committed, 507 explorer tests green, 1105 qe-central tests green):**

- **browser.py `RawObservation`** — two new fields: `intended_value: str` (what the action tried to achieve — fill value, select label, "true"/"false" for checked state) and `intent_met: bool | None` (True=verified, False=definitively unmet, None=unverifiable). Additive, defaults preserve all existing callers.
- **browser.py `verify_intent()`** — pure, per-kind verification function. Intent kinds:
  - **fill / select:** error → False; committed empty but intended non-empty → False; case-insensitive exact match → True; reformatted (non-matching but non-empty) → None (honest "can't tell").
  - **checked:** error → False; committed truthy/falsy matches intended_checked → True/False.
  - **click:** error → False; url_changed / dom_changed / dialog_opened → True; no visible effect → None (ambiguous, not definitively failed).
  - **hover:** always None (unverifiable). No kind ever guesses.
- **main.py `_act`** — all three return paths now carry intent: locator_unresolved → `intent_met=False`; action_error → `intent_met=False`; normal path → `verify_intent()` computed from the observation.
- **forms.py `_fill_one`** — universal intent gate: `observation.intent_met is False` → return None (honest residue) for ALL kinds — text, select, radio, checkbox, toggle, slider, date. Replaces the toggle-only `error_detail` check. `intent_met=None` (unverifiable) preserves current behaviour — no false negatives.
- **forms.py `PROV_INTENT_UNMET`** — new provenance for fills that attempted but failed intent verification. Distinct from `needs_input` (never tried) and `planned` (branch override).
- **forms.py `FormFillResult.intent_unmet`** — counter surfaced in logs (`qec.forms.phase_a intent_unmet=N`).
- **crawler.py `_step_record`** — `intent_unmet` count per step flows into the flow ledger.
- **flow_ledger.py `summarize()`** — `intent_unmet` total across all flows in the coverage summary.
- **Tests (35):** `verify_intent` per kind (fill/select/checked/click/hover × match/mismatch/error/none/reformatted); `_fill_one` integration (intent_met True/False/None → action/residue/preserve); `RawObservation` field defaults; flow_ledger summary propagation. All pinned.
- **Verifier:** the read-back + committed_value comparison itself. **Cost:** zero — already observing; now comparing.
- **Proof gate:** re-crawl the client funnel; the coverage report names "product cards: selection intent unmet" instead of "3 filled".

### Phase R1 — Deterministic interaction ladder (per-archetype) · DETERMINISTIC · EXTEND · **SHIPPED 2026-08-05**

Make the one-off set_checked→click fallback a first-class, per-archetype ladder tried until R0 verifies intent. Covers the ~90% of custom controls that are not exotic — with zero AI.

**As-built (committed, 525 explorer tests green, 1105 qe-central tests green):**

- **`app/interaction_ladder.py`** — NEW pure-data module. Per-archetype ordered `Rung(kind, variant)` tuples:
  - **radio / checkbox / toggle:** `native_set_checked` → `click_element` → `focus_space` (3 rungs)
  - **select:** `native_select_option` → `open_click_option` (open the custom listbox, click matching `[role=option]` by label, Escape on failure) (2 rungs)
  - **slider / color:** `native_fill` → `focus_arrow` (focus + ArrowRight) (2 rungs)
  - **date:** `native_fill` → `type_chars` (focus + `press_sequentially` char-by-char) (2 rungs)
  - **text:** `native_fill` → `type_chars` (2 rungs)
  - **button / link:** NO ladder (governance — submit boundary is upstream)
  - `ladder_for(kind)` / `archetype_has_ladder(kind)` — case-insensitive lookup.
- **`main.py` `_act_with_ladder()`** — wraps `_act()`. Walks the archetype ladder rung-by-rung; stops at the first rung where R0 `intent_met` is not False. Records `mechanic_used` (the winning rung's variant) on the returned `RawObservation`. If no rung verifies, returns the last failed observation.
- **`main.py` `_run_rung()`** — executes a single rung through the appropriate low-level primitive:
  - `kind=click` → `_act(control, "click")` then re-verifies intent against the original kind
  - `kind=press` → `locator.focus()` then keyboard gesture (`Space`, `ArrowRight`, or `press_sequentially`) then full observation + R0 verify
  - `kind=click_option` → `_select_via_open_click()` — click the select to open, click `[role=option]` by label, Escape-dismiss on failure
- **Port methods** — `fill()`, `select_option()`, `set_checked()` now call `_act_with_ladder()` instead of `_act()` directly. `click()` and `hover()` remain on raw `_act()` (no ladder for non-form gestures).
- **`main.py` `_locator()`** — added `css_hint` as a fourth fallback rung: `get_by_role` → `get_by_label` → `get_by_text` → `locator(css_hint)`. Reads from `qec.css_hint` or top-level `css_hint` (both present in the inventory, previously unused).
- **`browser.py` `RawObservation.mechanic_used`** — new field recording which rung won (empty when native succeeded on first try).
- **Tests (18):** ladder definitions (all archetypes have ladders, native is rung 0, button/link have none, case-insensitive, descriptive variants); integration (native success = no fallback, native fail = intent_unmet residue); mechanic_used field; governance (button/link never laddered).
- **Verifier:** R0 intent-met. **Cost:** deterministic, bounded rungs (2-3 per archetype), fast. **Governance:** ladder NEVER includes commit/danger controls.
- **Proof gate:** the client product-cards will select via the `click_element` rung when `set_checked` times out, the funnel opens, the walk reaches HLQ.

### Phase R2 — Crawl diagnostician (named stops) · SHIPPED 2026-08-05

The fastest way a failed crawl reads as competence not breakage: it *tells the client exactly what blocked it*. Extended the existing typed crawl-diagnosis surface with three new deterministic codes derived from R0/R1 evidence.

**As-built:**

- **`crawl_diagnosis.py`** — three new diagnosis codes, all pure/deterministic, checked BEFORE the generic `COMPLETED_OK`/`SEEDS_NEEDED`/`NO_CASES` codes within the `completed` branch:
  - **`INTERACTION_BLOCKED`** (severity: ACTION) — fires when `flow_summary.intent_unmet > 0` AND `generated <= 0`. Reads `coverage.field_ledger` for entries with `provenance == "intent_unmet"`, extracts the blocked control names (up to 8), and names them in the human-readable message: *"3 controls did not accept their intended value despite trying all available mechanics: Coverage Type, Rider Option, Term Length."* Remediation points to custom widget investigation + re-crawl. Evidence carries `intent_unmet` count + `blocked_controls` list.
  - **`WALK_BLOCKED_VALIDATION`** (severity: ACTION) — fires when there are truncated flows (`completed == False`) with `fields_unanswered > 0` AND `generated <= 0`. Reads `coverage.flows` and filters for the validation-blocked subset. Message: *"2 journeys could not advance past a step where 4 required fields went unanswered — the form's validation prevented progress."* Carries the `seed_fields` in the `fields` slot so the portal can render the residue ask. Evidence: `validation_blocked_flows` count + `total_unanswered`.
  - **`DECISION_UNRESOLVED`** (severity: ACTION) — fires when field_ledger has entries with `provenance == "needs_input"` AND non-empty `options` list AND `generated <= 0`. More specific than `SEEDS_NEEDED`: it names the enumerable decision forks and frames them as business-path branches that were never explored. Message: *"2 decision points were discovered but no value was available: Coverage Level, Rider."* Evidence: `unresolved_decisions` count + `decision_names`.
- **Precedence within the completed branch** (most-specific first):
  1. `EMPTY_SUBSTRATE` (visits ≤ 0)
  2. `ADVANCE_ORACLE_UNAVAILABLE` (platform fault)
  3. **`INTERACTION_BLOCKED`** (R0/R1 evidence — controls refused to commit) — NEW
  4. **`WALK_BLOCKED_VALIDATION`** (truncated flows with unanswered fields) — NEW
  5. `COMPLETED_OK` (generated > 0 — productive crawl always reads OK)
  6. **`DECISION_UNRESOLVED`** (enumerable forks with no value) — NEW
  7. `SEEDS_NEEDED` (generic unfilled fields)
  8. `NO_CASES` (fallback)
- **Key invariant:** a productive crawl (generated > 0) is ALWAYS `COMPLETED_OK`, even with intent_unmet or unresolved decisions deeper in the funnel. The R2 codes fire only on non-productive crawls where they explain WHY no cases were generated.
- **All three codes added to `TERMINAL_ATTENTION_CODES`** so the portal UI renders them as actionable.
- **Tests (23 new, 50 total in `test_crawl_diagnosis.py`):** INTERACTION_BLOCKED (6 — representative snapshot, singular grammar, productive crawl doesn't fire, wins over seeds_needed, tolerates empty field_ledger, attention code); WALK_BLOCKED_VALIDATION (6 — truncated+unanswered snapshot, singular journey, productive crawl doesn't fire, completed flows don't trigger, interaction_blocked wins precedence, attention code); DECISION_UNRESOLVED (7 — enumerable needs_input snapshot, singular, non-enumerable falls to seeds_needed, empty options ignored, productive crawl doesn't fire, wins over seeds_needed, attention code); `test_all_codes_are_reachable` updated to include all three new codes.
- **Verifier:** the diagnosis only ever restates evidence present on the row (the standing never-invent law). **Cost:** fully deterministic, zero LLM calls.
- **Thin-agent path (DEFERRED):** the optional one-call business-language remediation sentence is deferred to R3's medic agent, which will have the caged context to write grounded sentences. R2 is complete as a deterministic layer.

### Phase R3 — Crawl Medic agent (caged escalation) · AGENTIC · BUILD · **SHIPPED 2026-08-05**

For the genuinely novel widget the deterministic ladder cannot operate. Fires ONLY after R1 exhausts and R0 still reports intent-unmet.

**As-built:**

- **Service:** `app/services/crawl_medic.py` — `consult_medic(tenant_id, control, intent, ladder_results, page_context)` → `MedicDecision(status, action)`. Input is caged: control shape only (name, kind, role, tag, css_hint, attributes — never values, never raw HTML). Ladder results summarize what each rung tried and observed. Page context carries title + URL path + sibling controls + visible errors. Disabled → `display_only`. Danger → `unavailable`. Nameless → `unavailable`.
- **Enumerated vocabulary (the safety contract):** `click`, `press:Space`, `press:Enter`, `press:ArrowDown`, `open_then_pick`, `display_only`, `unavailable`. No other action may leave the module. Each term maps 1:1 to an existing crawler primitive. The reply parser normalizes case and punctuation, matches exact or substring, rejects unrecognized replies as `unavailable`.
- **Endpoint:** `POST /internal/operate-control` — HMAC-authenticated (same shared secret as pick-advance). Request: `{tenant_id, control, intent, ladder_results, page_context}`. Response: `{action, status}`. Every outcome is HTTP 200 (best-effort); only a bad signature is 401.
- **Explorer oracle:** `_make_medic_oracle(http_client, tenant_id, crawl_id)` returns an async callable, same resilience pattern as `_make_advance_oracle` — per-crawl call cap (`QEC_MEDIC_MAX_CALLS`, default 50), circuit breaker (`QEC_MEDIC_BREAKER_THRESHOLD`, default 3 consecutive), `QEC_MEDIC_ORACLE_TIMEOUT_S` (default 10s). Injected only in e2e crawl mode.
- **Integration in `_act_with_ladder`:** after the deterministic ladder exhausts, the medic is called with the control shape + ladder results + page context. The proposed action is executed through `_execute_medic_action` (maps vocabulary terms to `Rung` objects or `_act` calls). R0 verifies the result. An unverified proposal → the control is named residue (R2 diagnostician). `mechanic_used` is stamped `medic:<action>` (e.g. `medic:click`, `medic:press:Space`).
- **R4 compounding:** a medic-proven mechanic is persisted by mechanic memory at completion time, so the NEXT crawl tries it deterministically — zero medic calls for any previously-healed control.
- **Tests (23 new):** vocabulary completeness, reply parser (exact match, case insensitive, whitespace, surrounding text, substring extraction, unrecognized, empty), prompt builder (control name, ladder results cap at 8, empty page context, css_hint, attributes), input validation (empty/nameless/disabled/danger controls).
- **Verifier:** R0. **Governance:** enumerated vocabulary only → auditable, reversible, safety-gate-invariant. **Proof gate:** a stuck control operated via a medic pick, verified by R0, recorded with `mechanic_used=medic:<action>`.

### Phase R4 — Mechanic memory (compounding) · DETERMINISTIC · EXTEND · **SHIPPED 2026-08-05**

Turns the per-crawl interaction cost into a compounding asset. When the explorer R0-verifies that a specific ladder rung operates a control, the proven mechanic is persisted. Next crawl tries the proven mechanic FIRST — zero ladder walk, zero medic calls for every previously-seen control.

**As-built:**

- **Tables:** `control_mechanics` (RLS-forced, tenant-private, PK `(tenant_id, control_sig)` → `mechanic` variant name + `proof_count` + `last_proven_at`). `mechanic_priors` (cross-tenant, value-free, PK `(control_sig, mechanic)` → proof count + distinct tenants + pseudonymous contributor hashes). Migration `qec_009`. ORM in `advance_models.py` (`MechanicMemoryRow`, `MechanicPriorRow`).
- **Service:** `app/services/mechanic_memory.py` — `recall_all(tenant_id, app_id)` returns `{control_sig: mechanic}` for the entire app (bulk recall at dispatch time, not N+1 during crawl). `recall_priors(control_sigs)` returns cross-tenant best-proof mechanics (threshold-gated, value-free). `harvest_mechanics(tenant_id, app_id, coverage)` extracts R0-proven mechanics from field-ledger entries, upserts into `control_mechanics` (same mechanic reinforces `proof_count`; different mechanic replaces with count=1), consent-gates contribution to `mechanic_priors`. All best-effort, never raises.
- **Dispatch flow:** `ExploreDispatchRequest.proven_mechanics` carries the dict to the explorer. `explorations.py` calls `mechanic_memory.recall_all()` at dispatch time, populates the field. `_log_safe` logs `mechanics_count` only (never content).
- **Completion callback:** `internal.py` calls `mechanic_memory.harvest_mechanics()` after `advance_memory.harvest_completion()`, same best-effort doctrine.
- **Explorer recall:** `PlaywrightBrowserPort.__init__` accepts `proven_mechanics`. `_act_with_ladder` computes the control's `field_signature`, looks up the proven variant, tries that rung FIRST before the full ladder walk. Falls through to the normal ladder if the proven mechanic fails (the app may have changed).
- **Mechanic tracking:** `_fill_one` returns `(action, mechanic_used)` — the verified rung variant. Fill loop stamps `entry["mechanic"]` on the field-ledger entry when non-empty. `harvest_mechanics` reads `coverage["field_ledger"]` for entries with `signature` + `mechanic` + `filled=True`.
- **Consent:** contributing to `mechanic_priors` reuses `share_advance_priors` tenant opt-in (OFF by default). Recall of the pool is open — only rung variant names, never control labels or values.
- **Tests:** 10 pure-logic tests (extraction, dedup, malformed tolerance, empty handling); 3 DB round-trip tests (recall→harvest→replace→reinforce lifecycle).
- **Verifier:** R0. **Proof gate:** second crawl of a previously-crawled app operates controls with zero ladder iterations for every previously-seen control.

### Phase R5 — Vision Medic (multimodal escalation) · SHIPPED 2026-08-05

For DOM-opaque surfaces (canvas, unlabeled custom widgets, iframes without accessible names) — the last rung, rare, expensive, optional. Screenshot + bounding box → propose a click region. Governed by the `hard_ui_healing_research` law: **refuse without an orthogonal oracle** — a vision pick is accepted only when R0 verifies the resulting intent.

**As-built:**

- **`app/services/vision_medic.py`** — full pure-function pipeline:
  - `is_vision_candidate(control)` — classifies DOM-opaque controls into surface types: `canvas` (any `<canvas>`), `svg` (any `<svg>`), `iframe` (cross-origin or unnamed), `unlabeled` (no accessible name + opaque role: `generic`/`none`/`presentation`/empty). Normal DOM controls return `{candidate: False, surface_type: "dom"}`.
  - `build_vision_prompt(control, element_bbox, page_context)` — text portion of the multimodal prompt. Includes tag, role, kind, css_hint, up to 8 attributes, bbox dimensions, page title/URL. Screenshot sent as separate image attachment.
  - `parse_vision_proposal(raw)` — tolerant JSON extraction (handles ```json fences, dict input, malformed). Maps to `VisionDecision` dataclass. Three-action vocabulary: `click_region` (with x/y relative to element bbox), `display_only` (decorative/read-only, skip honestly), `unavailable` (cannot determine). Invalid coordinates → `unavailable` with reason.
  - `validate_proposal(proposal, element_bbox)` — rejects clicks outside element bounds, negative coordinates, zero-dimension elements. Non-click actions always valid.
  - `consult_vision(tenant_id, control, screenshot_b64, element_bbox, page_context, propose_fn)` — async top-level. Guards: not-a-candidate → unavailable; no screenshot → unavailable; no propose_fn → unavailable; LLM exception → unavailable with error. Proposal validated; out-of-bounds → rejected. **Never raises.**
  - System prompt: instructs model to reply with strict JSON, coordinates relative to element bbox top-left.

- **`app/clients/platform_api.py`** — `complete_vision(prompt, image_b64)` added:
  - Multimodal LLM call via `POST /api/v1/llm/vision` with `image` field (base64 PNG).
  - Same resilience contract as `complete_llm` (never raises, `ok=False` on failure). Timeout 90s (vs 60s for text).

- **`app/routers/internal.py`** — `POST /internal/vision-operate` endpoint:
  - HMAC-authenticated (same pattern as `/operate-control`). Receives `{tenant_id, control, screenshot_b64, element_bbox, page_context}`.
  - Returns `{action, status, click_x, click_y, reason}`. Wires `platform_api.complete_vision` as the propose_fn.

- **Safety contract:**
  1. **Flag-gated:** `QEC_CRAWL_VISION_ENABLED` OFF by default, per-tenant.
  2. **Call-bounded:** hard cap per crawl (`QEC_VISION_MAX_CALLS`, default 10). Circuit breaker after consecutive failures (`QEC_VISION_BREAKER`, default 3).
  3. **R0 verification:** the explorer clicks the proposed region and R0 verifies the resulting intent. A vision pick is accepted ONLY when R0 confirms. This module proposes; it never asserts success.
  4. **Refuse without orthogonal oracle:** governed by the `hard_ui_healing_research` law.

- **39 unit tests** — classification (12), prompt building (3), proposal parsing (9), validation (7), async consultation (8). All green in full suite (1384 passed).

---

## PILLAR B — Coverage Engine

### Phase E1 — Systematic option enumeration (the HLQ pattern) · SHIPPED 2026-08-05

Every discovered decision-point option now gets its own walk plan — single-variable enumeration. The autowalk loop drives recursively until the branch ledger has no `discovered` option left. Excess branches are `deferred` with an honest count, never silently truncated.

**As-built:**

- **`branch_planner.py` `plan_walks()`** — rewritten from "one plan per journey" to "one plan per option":
  - For each journey's discovered branches on the proven path, generates a separate plan forcing exactly ONE option. Everything else takes its default. This is single-variable enumeration — one dimension varies at a time, so each walk's outcome is attributable to the forced option.
  - Plans are ordered: outcome-bearing journeys first, then by step proximity to the entry within each journey.
  - Total plans capped per-cycle by `QEC_BRANCH_WALKS_PER_CYCLE` (default 4) across all journeys.
  - **Explosion control:** when a journey's discovered backlog exceeds `QEC_JOURNEY_PATH_ENUM_CAP` (default 64), excess branches are transitioned to `BRANCH_DEFERRED` with an attributed reason: *"deferred: N options exceed the per-journey enumeration cap (64)"*. Logged as a WARNING. Never silently truncated.
- **`journey_fold.py`** — new `BRANCH_DEFERRED = "deferred"` status. The status lifecycle is now: `discovered → planned → walked | blocked | deferred`. `walked` never downgrades; `deferred` is first-class honest: "option exists, exceeds cap."
- **`internal.py` autowalk guard** — changed from `not walk_plan` (fire-once) to depth-bounded recursion:
  - Each walk plan carries a `walk_depth` counter (starts 0 for organic crawls).
  - A branch walk's completion triggers further autowalks if `walk_depth < QEC_AUTOWALK_MAX_DEPTH` (default 3).
  - New branches discovered by a branch walk are enqueued as `discovered` by the fold (unchanged) and picked up by the next autowalk cycle (the recursive HLQ explosion).
  - Depth-capped walks are logged but do not error.
- **`config.py`** — new `QEC_AUTOWALK_MAX_DEPTH` (default 3).
- **Tests (3 new, 13 total in `test_journey_graph.py`):** `BRANCH_DEFERRED` status exists and fits the column; single-option identity_ref; DB round-trip: 9-option page yields 8 plans (one per unchosen option, each with a single branch_id and single choice_override); DB round-trip: explosion cap defers excess (cap=3 → 3 plans dispatched, 5 branches deferred with honest reason).
- **Verifier:** branch status lifecycle (discovered→planned→walked|blocked|deferred), all counts honest. **Cost:** one crawl per option; bounded per cycle and per depth; memoized advances/mechanics keep each cheap.
- **Proof gate:** a 9-option HLQ page → 8 plans, each walking one unchosen option. Cap set to 3 → 3 plans, 5 deferred with count. Recursive autowalking: a branch walk revealing new forks triggers further walks up to depth 3.

### Phase E2 — Combination strategy (risk-prioritized) · DETERMINISTIC + optional agent · BUILD · **SHIPPED 2026-08-05**

Full Cartesian combinations are exponential and dishonest to promise. Prioritize which multi-fork combinations to walk.

**As-built:**

- **Service:** `app/services/pairwise.py` — pure, dependency-free pairwise covering-array generator. `Factor(key, levels)` models one decision control and its options. `factors_from_branches(branches)` extracts factors from journey branch rows, grouped by `control_signature`, each option a level. Only factors with >=2 levels are retained (single-option controls have nothing to combine). `generate_pairwise(factors, must_walk, max_configs)` produces the minimum set of configurations covering every (factor_i, level_a) × (factor_j, level_b) pair. Algorithm: greedy covering — seed with must-walk scenarios first (they count toward pair coverage), then iteratively pick the configuration covering the most uncovered pairs. Returns `PairwiseResult{configurations, total_pairs, covered_pairs, must_walk_count}`.
- **Must-walk client scenarios:** `rule_oracle.normalize_scenarios(raw_rules)` extracts `kind: "scenario"` entries from the answer_key's rules. Format: `{kind: "scenario", choices: {control_sig: option, ...}}`. Must-walks are seeded before the greedy phase so they count toward pair coverage and are never duplicated.
- **Branch planner extension:** `plan_pairwise_walks(tenant_id, app_id, journey_id, must_walk, limit)` — reads the journey's decision branches, builds factors, generates the pairwise covering array, filters out already-walked identity_refs, and returns plans with multi-choice `choice_overrides`. Each plan carries `pairwise: True` for audit. Capped by `QEC_PAIRWISE_WALKS_PER_CYCLE` (default 8).
- **API endpoints:**
  - `GET /apps/{app_id}/journeys/{journey_id}/pairwise-plan` — dry-run preview: returns the pairwise configurations, factor breakdown, and coverage stats without dispatching.
  - `POST /apps/{app_id}/journeys/walk-pairwise` — dispatches pairwise combination walks. Same double-gate as branch walks (env switch + tenant flag). Reads must-walk scenarios from the app's answer_key. Each combination becomes one crawl with multi-choice overrides.
- **The claim this earns:** "every option exercised; pairwise combination coverage; your named scenarios guaranteed; the rest visible and deferred" — survivable, and stronger than "all combinations".
- **Tests (25 new):** factor extraction (grouping, dedup, single-option skip, tolerant on garbage), pair counting (2/3 factors, asymmetric), pairwise generation (covers all pairs, fewer than Cartesian, must-walk seeding, must-walk coverage credit, invalid key/level skipped, max_configs cap, single factor, empty), scenario normalization (basic, kind filtering, single-choice skip, overrides alias, tolerant on garbage, blank key stripping).
- **Proof gate:** pairwise plan generated for a 3-fork journey; client scenario forced as must-walk; all pairs covered.

### Phase E3 — The catalog as source of truth · EXTEND — SHIPPED 2026-08-05

The vision's "single source of truth": pages, fields, allowed values, mandatory flags, validations-observed, locators, buttons, links, navigation, business-rules-observed — all surfaced per node with honest provenance.

**As-built:**

- **The surfacing insight:** the crawl manifest already captures rich per-page-state data — `form_snapshot_signals` (type, options, required, depends_on), `displayed_values` (label, selector, value_type), and the `field_ledger` (signature, semantic_type, options, provenance). The journey graph stored only coarse booleans per node (`is_decision`, `is_boundary`, `has_outcome`). E3 bridges that gap: it projects the manifest's control-level detail onto journey nodes during fold, and surfaces it through a catalog API with provenance badges.
- **DB schema (migration `qec_010`):** two nullable JSONB columns added to `journey_nodes`:
  - `controls_inventory` — merged list of controls observed at this node. Each entry: `{name, type, signature, options, required, depends_on, semantic_type}`.
  - `displayed_outcomes` — outcome display locations observed at this node. Each entry: `{label, selector, value_type}`.
- **Fold extraction (`journey_fold.py`):** during the fold's node-processing loop, the fold builds a `{fingerprint → page_state}` index from `coverage.states` and a `{url → [ledger_entries]}` index from `coverage.field_ledger`. For each node, it looks up the matching page state by `ax_fingerprint`, extracts controls via `catalog.extract_controls()` and outcomes via `catalog.extract_outcomes()`, and merges them into the node's existing inventory (latest observation wins; capped at 200 controls / 100 outcomes).
- **Catalog service (`catalog.py`):** pure, dependency-free functions:
  - `extract_controls(page_state, ledger_by_url)` — merges `form_snapshot_signals` (type, options, required, depends_on) with `field_ledger` entries (signature, semantic_type) matched by URL + normalized name. Signal options win when non-empty; ledger options fill the gap. Deduplicates by normalized name.
  - `extract_outcomes(page_state)` — extracts `displayed_values` entries (label, selector, value_type). Deduplicates by normalized label.
  - `merge_controls(existing, incoming)` / `merge_outcomes(existing, incoming)` — upsert-by-normalized-name semantics; incoming updates existing, new entries append.
  - `build_states_index(coverage)` / `build_ledger_by_url(coverage)` — index builders for the fold.
  - `apply_provenance(controls, baseline_status, rule_fields)` — stamps each control with its effective provenance badge (query-time, not stored):
    - `"observed"` — default for everything
    - `"confirmed"` — the journey baseline is approved/validated (O0 lifecycle)
    - `"client_declared"` — the control's name matches a client-authored rule field (O1 rule oracle); wins over confirmed
  - `apply_outcome_provenance(outcomes, baseline_status, rule_fields)` — same logic for outcome displays.
  - `catalog_summary(nodes)` — computes `{node_count, total_controls, controls_with_options, required_controls, total_outcomes}`.
- **API endpoint:** `GET /apps/{app_id}/journeys/{journey_id}/catalog` — returns the full catalog view:
  - Per-node: `{fingerprint, url, title, is_decision, is_boundary, has_outcome, controls (with provenance), displayed_outcomes (with provenance), branches}`.
  - Journey-level: `{journey_id, business_name, baseline_status, node_count, total_controls, controls_with_options, required_controls, total_outcomes}`.
  - Reads answer_key rules to identify client-declared fields (O1 integration).
- **The claim this earns:** "every control the crawl observed — its name, type, allowed values, required flag, dependencies, semantic type — surfaced per journey node with provenance that honestly distinguishes observed-vs-confirmed-vs-client-declared" — the single source of truth for QA.
- **Tests (41 new):** extract_controls (signals, ledger merge, ledger-only, depends_on, dedup, empty, tolerant, option fill, option precedence), extract_outcomes (basic, dedup, empty, tolerant), merge_controls (append, update, case-insensitive, none, cap), merge_outcomes (append, update, cap), build_states_index (basic, empty fp, tolerant), build_ledger_by_url (groups, tolerant), provenance (observed/confirmed/validated/drifted), apply_provenance (observed, confirmed, client_declared override, wins-over-confirmed, case-insensitive), outcome_provenance (observed, confirmed, client_declared), catalog_summary (counts, empty, tolerant).
- **Proof gate:** catalog endpoint returns a journey's pages/fields/locators/allowed-values with honest provenance badges; 41 tests exercise every extraction, merge, and provenance path.

---

## PILLAR C — Oracle / Approval lifecycle (make "expected results" honest)

### Phase O0 — Capture → Approve → Validate → Drift · BUILD — SHIPPED 2026-08-05

The missing hinge. A captured outcome is an **observation**; it becomes an **expectation** only when a human who knows the business approves it.

**As-built detail:**

- **Baseline lifecycle** on `JourneyRow`: `baseline_status` ∈ `captured → approved → validated → drifted`. Eight new columns (migration `qec_008`): `baseline_status`, `baseline_traversal_id`, `baseline_outcome_hash`, `baseline_snapshot` (JSONB), `baseline_approved_at`, `baseline_approved_by`, `drift_traversal_id`, `drift_detected_at`. Existing rows read as `captured` (no baseline yet).
- **Approval gate**: `POST /apps/{app_id}/journeys/{journey_id}/baseline/approve` — requires a completed traversal and an e-signature. Builds an immutable snapshot (outcome_values + path metadata), computes `outcome_hash = sha256(canonical_json(sorted_outcomes))`, chains via the existing `qec_approval_events` table with `subject_kind = "journey_baseline"`. Hash-chained, tamper-evident.
- **Drift detection in the fold**: `journey_fold.fold_crawl()` now collects completed traversals with outcomes; after the main fold session commits, calls `journey_baseline.detect_drift()` per journey. Compares `outcome_hash` of the new traversal against the approved baseline. Match → `validated`; differ → `drifted` (with `drift_traversal_id` + `drift_detected_at`). No-baseline journeys stay `captured` — no-op.
- **Drift adjudication**: `POST /apps/{app_id}/journeys/{journey_id}/baseline/adjudicate` — human rules `intended_change` (baseline moves to the drift traversal's outcomes, new approval event chained) or `defect` (baseline revoked, journey returns to `captured` / UNVERIFIED). Both require e-signature for audit. Never auto-absorb drift; never auto-fail on it.
- **Baseline view**: `GET /apps/{app_id}/journeys/{journey_id}/baseline` — lifecycle status, approved snapshot, drift diff (per-label: added/removed/changed/unchanged), and the full hash-chained approval history.
- **Surface integration**: `baseline_status` and `baseline_approved_at` surfaced in the journey list and detail API responses. Everything unapproved wears `captured` openly — UNVERIFIED.
- **Pure comparison primitives** (`outcome_hash`, `outcomes_match`, `diff_outcomes`, `build_snapshot`): all pure functions, unit-testable with no database. Canonical outcome normalization: labels lowercased, sorted by (label, value_type), tight-separator JSON.
- **Service module**: `app/services/journey_baseline.py` — 5 lifecycle states, 2 adjudication verdicts, pure comparison core + async persistence. Reuses `approval.append_event` for hash-chaining.
- **Tests**: 19 pure-logic tests (hash determinism, order-insensitivity, case-insensitivity, value-change detection, diff shapes, snapshot shape/anchor, malformed tolerance) + 4 DB round-trip tests (approve→validate→drift→adjudicate full cycle, incomplete-traversal rejection, non-drifted adjudication rejection, baseline view with drift diff). All pass alongside 1145 existing tests (0 regressions).
- **Proof gate:** approve a traversal → `approved`; fold a matching crawl → `validated`; fold a differing crawl → `drifted` with per-label diff; adjudicate as `intended_change` → re-approved with moved baseline; adjudicate as `defect` → reverted to UNVERIFIED. ✅

### Phase O1 — Client-rule oracle (first-crawl validation) · EXTEND · **SHIPPED 2026-08-05**

The only oracle that validates on the *first* crawl. Client rate tables / eligibility rules → machine-checkable expectations, via the existing answer_key value/rule oracle stack.

**As-built:**

- **Service:** `app/services/rule_oracle.py` — pure, dependency-free rule evaluation engine. `normalize_rules(raw_rules)` parses `answer_key.rules` entries with `kind: "outcome_rule"` into validated `RuleSpec` objects. `evaluate_rules(rules, outcome_values)` checks each rule against a traversal's outcome values and returns `RuleResult` per rule (status: confirmed | unconfirmed | not_applicable). `summarize_evaluation(results)` reports totals and `all_applicable_pass` (True when at least one rule is applicable AND every applicable rule confirmed).
- **Rule format:** `{kind: "outcome_rule", field: "<outcome_label>", expected: <value>, match: "numeric"|"exact"|"contains"|"range", tolerance: <float>, source: "<audit_trail>"}`. Field aliases: `name`, `label`. Value aliases: `equals`, `value`. Match auto-detected if omitted (numeric when expected is a number, exact otherwise). Range: `{low, high}` bounds. Currency/noise in observed values tolerated (`$28.40/mo` → `28.40`). All comparisons case-insensitive and whitespace-normalized.
- **Auto-validation in journey_baseline.py:** `auto_validate_from_rules()` — when all applicable rules confirm a captured journey's outcomes, auto-approves the baseline with `action="rule_auto_approve"`, `actor="rule_oracle"`, `signature="rule:auto:<source>"`. Hash-chained approval event records the rule summary for audit. Only fires when `baseline_status == "captured"` (no existing baseline) AND traversal is completed — never overwrites a human approval.
- **Integration in completion callback:** after the journey fold, reads the app's `answer_key.rules` via `value_oracle_contract`, normalizes rules, queries for journeys still in `captured` status, evaluates rules against each journey's latest completed traversal, and auto-approves matches. Stats reported as `rule_auto_validation: {rules_count, captured_journeys, auto_approved}`.
- **The FROZEN reducer law:** a rule that cannot be grounded (non-numeric expected for numeric match, missing field, unrecognised kind) is SKIPPED, never fabricated as passing. Only `outcome_rule` kind is processed; other kinds pass through for future expansion. The rule is client-authored, auditable, and the approval event carries the full rule summary.
- **Tests (53 new):** rule normalization (auto-detect, aliases, currency, tolerant on garbage, skip guards), numeric evaluation (exact, tolerance, mismatch, currency-stripping, non-numeric observed), exact/contains/range evaluation (boundaries, case, partial), field matching (case-insensitive, whitespace normalization, missing fields, empty/None outcomes), batch evaluation (multiple rules, mixed results), summary reporting (all_applicable_pass contract, source audit trail).
- **Proof gate:** a client rule confirms a captured premium on the first crawl, no human approval needed for that value.

### Phase O2 — NL Case Builder · SHIPPED 2026-08-05

**As-built.** A new service (`app/services/nl_case_builder.py`) and endpoint (`POST /api/v1/qec/apps/{app_id}/nl-case`) that turns a plain-English test-case request into a grounded, honestly-labelled case specification.

**Pipeline (mirrors brief_compiler's LLM-PROPOSES → grounding-gate-VALIDATES pattern):**

1. **Vocabulary extraction** (`collect_vocabulary`) — deduplicated union of control names, outcome labels, and journey names from the app's catalog (E3 data). Each control carries its type, options, and signature.
2. **LLM prompt** (`build_nl_prompt`) — the model sees ONLY real vocabulary (journeys, controls with options, outcomes). Constrained to output a strict JSON intent: `{journey_hint, fields[{label, value}], expected_outcomes[{label, expected, match, tolerance}], unmatched}`.
3. **Proposal parsing** (`parse_nl_proposal`) — tolerant JSON extraction (handles ```` ```json ```` fences, surrounding prose, dict input). Malformed → empty (honest: nothing proposed).
4. **Grounding gate** (`ground_nl_intent`) — every proposed field/value is re-validated against the real catalog:
   - Journey matching: `match_label()` (exact → token-subset → substring, reused from brief_compiler). Fallback: auto-match by field overlap when LLM gives no hint.
   - Enumerable controls (options + signature): value matched tolerantly to a real option → `choice_overrides[signature] = norm_option`. Value not in options → `fill` (treated as free-text).
   - Free-text controls (no options): → `fill[control_name] = value`.
   - Outcomes: matched against displayed_outcome labels. Unmatched outcomes are included but flagged `needs_confirmation`.
5. **Outcome verification** (`verify_outcomes`) — each expected outcome checked against O1 `answer_key.rules`:
   - **confirmed** — a rule covers it (numeric within tolerance, exact match, or contains). Safe to assert.
   - **unverified** — no rule supports it. The assertion is generated but MUST be approved by an SME before it becomes a regression gate. This is the honest position.
6. **Case assembly** (`assemble_case`) — returns `{journey_id, journey_name, choice_overrides, fill, expected_outcomes (with verification), review, grounded/ungrounded counts, outcomes_confirmed/outcomes_unverified}`.

**Endpoint:** `POST /api/v1/qec/apps/{app_id}/nl-case` — body `{text, journey_id?}`. Loads all journey catalogs for the app, calls `platform_api.complete_llm()` (the single LLM gateway), runs the pure pipeline, returns the case spec. Optional `journey_id` pre-filters to one journey. LLM failure degrades honestly (empty proposal + `llm_error`), never a fabricated case.

**Reuse:** `match_label` from brief_compiler; rule evaluation logic from rule_oracle (adapted inline for the verification check); catalog data from E3; choice_overrides format from branch_planner/E2.

**Tests:** 38 new tests in `test_nl_case_builder.py` covering: vocabulary (3), prompt (1), parsing (4), journey matching (5+2), grounding (9), verification (8), assembly (2), end-to-end with fake LLM (4). Total: 1345 passed, 60 skipped.

### Phase O3 — Jira / manual-test ingestion · BUILD connector · LAST

A connector, not a differentiator — build once the catalog is trustworthy.

- BUILD Jira ingestion: import manual test cases → map to captured journeys by page/field/step similarity → generate executable case + Playwright grounded in the real captured flow → flag steps the manual test asserts that the crawl never observed (a gap either in the app or the manual test — surfaced, never silently dropped).
- **Proof gate:** a Jira manual case becomes a runnable grounded script; unobserved asserted steps flagged.

---

## Cross-cutting — Environment postures · SHIPPED 2026-08-05

**As-built.** Per-environment crawl posture enforced end-to-end:

| env_kind | Posture | Behaviour |
|---|---|---|
| `disposable` / `staging` / `uat` / `production_test` | **full** | Full-depth exploration + attested submit (existing disposable-env gate). `production_test` added as a stepping-stone between test and prod — full-depth, submit only on explicit disposable attestation (same as staging/uat). |
| `prod` | **observe** | Capture pages / fields / locators / navigation. **Never** fill a mutating field, **never** submit, **never** advance a commit. The crawl catalogs prod; it never mutates it. |

**Vocabulary:** `POSTURE_FULL = "full"`, `POSTURE_OBSERVE = "observe"`. `posture_for_env_kind(env_kind)` derives posture from env_kind (case-insensitive). Unknown/blank kinds default to `full` (fail-safe — they're caught earlier by the attestation gate).

**Enforcement layers:**

1. **`prod_guard.resolve_effective_fences()`** — for `prod` env_kind: forces `allow_submit=False` AND `observe_only=True` in the effective fences dict. Non-prod env_kinds are unchanged (no `observe_only` injected).
2. **`explorations.py` dispatch** — wires `observe_only=bool(fences.get("observe_only"))` from resolved fences into the explorer dispatch request.
3. **`crawler.py` form detection** — when `observe_only=True`, `is_form` is forced `False`. This single guard gates ALL downstream mutation: form filling (requires `is_form`), wizard walking (requires `is_form and fill is not None and fill.filled`), and submit (requires `submit_approvals` which won't be passed for prod). One guard point, full coverage.
4. **`explorer_client.ExploreDispatchRequest`** — `observe_only: bool = False` field added to the typed dispatch model.

**Set algebra:** `CRAWLABLE_ENV_KINDS = NON_PROD_ENV_KINDS | OBSERVE_ENV_KINDS`. `NON_PROD_ENV_KINDS = {disposable, staging, uat, production_test}`. `OBSERVE_ENV_KINDS = {prod}`. Production apps can now be onboarded (RoE + attestation + preflight + authorization) and crawled — but ONLY in observe-only mode.

**Tests:** 10 new tests in `test_prod_guard.py` covering posture derivation, fence resolution (observe_only injection, case-insensitivity), set membership, prod-reaches-LIVE-status, prod-still-refuses-submit, non-prod-no-observe-only. 1 existing test updated (`test_prod_env_kind_is_crawlable_in_observe_only_mode` — was `test_prod_env_kind_is_never_a_valid_crawl_target`). Total: 1307 passed, 60 skipped.

---

## 3. Sequence, extend-vs-build, effort, risk

| # | Phase | Build/Extend | AI? | Effort | Why here |
|---|---|---|---|---|---|
| 1 | R0 intent contracts | EXTEND | No | S | Everything stands on honest sensors; fixes the class of bug that hid the client failure |
| 2 | R1 deterministic ladder | BUILD+EXTEND | No | M | Fixes most real custom controls with zero AI — the demo-fixer |
| 3 | ~~R2 diagnostician~~ | EXTEND | deterministic | S | **SHIPPED.** Three named-stop codes (INTERACTION_BLOCKED, WALK_BLOCKED_VALIDATION, DECISION_UNRESOLVED) |
| 4 | ~~E1 systematic enumeration~~ | EXTEND | No | M | **SHIPPED.** One plan per option, explosion cap, recursive autowalk |
| 5 | ~~O0 approve/validate/drift~~ | BUILD | No | M | **SHIPPED.** Baseline lifecycle (captured→approved→validated→drifted), drift adjudication, hash-chained approval |
| 6 | ~~R4 mechanic memory~~ | EXTEND | No | M | **SHIPPED.** Proven mechanic recall + harvest, zero-ladder-walk for previously-seen controls |
| 7 | ~~R3 Crawl Medic~~ | BUILD | Yes | M | **SHIPPED.** Caged LLM agent with closed 7-action vocabulary; fires after ladder exhausts; R0 verifies; R4 memoizes |
| 8 | ~~O1 client-rule oracle~~ | EXTEND | No | S | **SHIPPED.** Rule oracle auto-validates captured outcomes on first crawl; rule_auto_approve event chain |
| 9 | ~~E2 combination strategy~~ | BUILD | opt | M | **SHIPPED.** Pairwise covering array + must-walk scenarios + preview/dispatch endpoints |
| 10 | ~~E3 catalog surfacing~~ | EXTEND | No | M | **SHIPPED.** Per-node control inventory + displayed outcomes + provenance badges; catalog API |
| 11 | ~~O2 NL depth~~ | EXTEND | Yes | M | **SHIPPED.** NL → grounded case spec; 38 tests; verification confirmed/unverified |
| 12 | ~~Postures~~ | EXTEND | No | S | **SHIPPED.** Observe-only prod crawl; `production_test` env_kind; 10 tests |
| 13 | ~~R5 vision~~ | BUILD | Yes | L | **SHIPPED.** Vision Medic: canvas/svg/iframe/unlabeled classification + multimodal propose + validate; 39 tests |
| 14 | O3 Jira | BUILD | Yes | M | Connector; last |

**Two releases to a defensible product:** *Release E (Interaction Reliability)* = ~~R0~~+~~R1~~+~~R2~~+~~E1~~ — **COMPLETE.** The crawl operates real UIs, names what it can't, and systematically enumerates every option. R0+R1+R2+E1 shipped 2026-08-05. *Release F (System of Record)* = ~~O0~~+~~R4~~+~~R3~~+~~O1~~ — **COMPLETE.** O0+R4+R3+O1 ALL SHIPPED 2026-08-05 (baseline lifecycle + drift adjudication + mechanic memory + caged medic agent + first-crawl rule oracle). Everything after is expansion.

The frozen factory, the runner, and Releases A–D behavior are untouched throughout; every phase is additive and flag-guarded where behavior could shift.

---

## 4. Acceptance — the founder sign-off checklist

- [x] The crawl never reports an errored action as a success (R0); every unoperated control is named with locator + attempts. **SHIPPED 2026-08-05.**
- [x] Custom radio/card/select/slider/date controls are operated by a deterministic ladder without AI (R1); the client funnel opens and reaches HLQ. **SHIPPED 2026-08-05.**
- [x] A blocked crawl produces a named, business-language, remediable diagnosis in the existing portal surface (R2). **SHIPPED 2026-08-05.**
- [x] Every discovered decision-point option is exercised or attributably deferred with an honest count; nothing is silently truncated (E1). **SHIPPED 2026-08-05.** (E2 combination strategy is separate.)
- [x] A captured journey outcome becomes an expectation only via SME approval; drift is adjudicated, never auto-absorbed (O0). **SHIPPED 2026-08-05.**
- [x] A control operated once is remembered and not re-fought next crawl; agent-escalation rate trends down (R4). **SHIPPED 2026-08-05.**
- [x] The novel widget that beats the ladder is operated via a caged, enumerated-vocabulary agent whose pick is deterministically verified (R3). **SHIPPED 2026-08-05.**
- [x] The catalog view renders a journey's pages/fields/locators/allowed-values with honest provenance badges (E3). **SHIPPED 2026-08-05.**
- [x] Production crawls are observe-only, guard-verified zero-mutation (postures). **SHIPPED 2026-08-05.**
- [x] DOM-opaque controls (canvas, svg, cross-origin iframe, unlabeled widgets) are classified and operated via multimodal vision escalation, flag-gated OFF by default, R0-verified (R5). **SHIPPED 2026-08-05.**
- [ ] No claim of "every combination" or "captures your business rules" survives in product copy — replaced by "every option exercised, every gap visible" and "observed behavior your SME confirms once, then validated forever."

---

## 5. The two lines to retire from the pitch, permanently

- ❌ "captures every possible combination of user journeys" → ✅ "enumerates every decision point, exercises every option, covers combinations by risk and your named scenarios, and shows every path not yet walked."
- ❌ "captures validations, business rules, expected outcomes" → ✅ "captures observed behavior and candidate rules; your SME confirms them once; from then on everything is validated automatically, with a signed certificate."

You lose nothing but exposure. You gain the only claim worth making to a regulated buyer: one that is true, checkable, and certifiable.
