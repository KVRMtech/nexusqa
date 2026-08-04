# E2E Advance Hardening — Production Implementation Plan

**Date:** 2026-08-03
**Status:** AWAITING FOUNDER GO-AHEAD — no phase starts until approved
**Scope:** qe-explorer (crawler), qe-central (advance agent + internal API), qecentral DB, verdict-portal (journey display)
**Prime directive:** the crawl must never cross the commit boundary without attestation, and must never report a journey as covered when it was not walked to its end. Every phase below is measured against those two sentences.

---

## 1. Defect register this plan closes

| ID | Severity | Defect | Where |
|----|----------|--------|-------|
| B1 | BLOCKER (safety) | Tier-3 oracle candidates are filtered only by `disabled`/`danger` — commit-word buttons and operator-approved submit names are sent to the LLM, which will pick them, and the crawler clicks them. A real Submit/Pay/Sign can fire on a production app with no Phase-B attestation. | `qe-explorer/app/crawler.py` `_pick_advance_e2e` Tier 3 (lines 1860-1873) |
| B2 | BLOCKER (honesty) | Oracle failure (timeout, 429, outage, unparseable reply) returns the same `None` as an honest "nothing advances". Terminal classification then emits `no_advance`, which is a COMPLETING terminal — an infrastructure failure is reported as a covered journey. | `crawler.py` lines 1971-1974; `advance_agent.py` lines 71-86; `main.py` lines 401-407; `flow_ledger.py` line 50 |
| G3 | HIGH (cost) | No memoization: the wizard entry check and the loop's first iteration call the oracle twice for the same page; re-crawls re-pay every LLM answer; nothing is ever learned. | `crawler.py` lines 1897, 1930 |
| G4 | HIGH (resilience) | 30 s oracle timeout, no circuit breaker, no per-crawl call cap. A dead LLM can stall a crawl ~40 minutes and then green-wash via B2. | `main.py` line 399 |
| G5 | MEDIUM (scale) | Advance, commit, and submit-boundary vocabularies are English-only. Non-English apps pay Tier 3 on every page and the commit boundary is invisible to the regex layer. | `crawler.py` `_WIZARD_ADVANCE_RE` / `_WIZARD_COMMIT_RE` |
| G6 | MEDIUM (audit) | No evidence records which tier produced each advance. An agent-driven walk is indistinguishable from a regex walk — an audit gap for the certification story and the telemetry needed to tune the vocabulary. | `flow_ledger.py` step schema |
| G7 | MEDIUM (safety-adjacent) | Tier 2 lifts the commit veto with no judgment: a label where the advance word and the commit verb are conjoined ("advance-word & commit-verb" shape) is clicked as if it were navigation. | `crawler.py` lines 1849-1857 |

## 2. Doctrine constraints (unchanged, non-negotiable)

1. **Submit boundary:** the crawl stops at commit controls; only the attested Phase-B path may cross, and only for operator-approved names on disposable envs.
2. **Never green-wash:** `completed` is derived from the terminal, never asserted; unknown ≠ covered.
3. **Never blame the app:** a platform failure (LLM down) must attribute to the platform, not the tenant's application.
4. **Values never leave the tenant:** learned artifacts carry shapes (labels, kinds, signatures) — never user values, never URLs/hosts.
5. **Extend, don't rebuild:** the frozen factory and explore/target behavior are untouched; every change is additive and flag-guarded where behavior could shift.
6. **Fail closed:** when a tier cannot decide safely, the walk ends honestly rather than guessing.

## 3. Release map

Two releases. Release A is the blocker release and ships alone — small, reviewable, provable. Release B builds the scale layer on top of Release A's evidence.

```
RELEASE A (blockers)              RELEASE B (scale)
Phase 0  Safety: commit filter    Phase 3  Evidence: tier per advance
Phase 1  Honesty: new terminal    Phase 4  Learning: advance memory + priors
Phase 2  Resilience: breaker      Phase 5  Vocabulary: language packs + Tier-2 tightening
Phase A-P Live proof gate         Phase B-P Live proof gate
```

---

# RELEASE A — the blocker release

## Phase 0 — Safety: Tier 3 can never choose a commit control (closes B1)

The commit boundary becomes structurally unreachable from Tier 3, enforced at **three independent layers** so no single regression reopens it.

### 0.1 Explorer-side candidate filter (layer 1 — the load-bearing gate)

`qe-explorer/app/crawler.py`, `_pick_advance_e2e` Tier 3:

- Exclude any candidate whose name matches `_WIZARD_COMMIT_RE` (word-boundary search, same pattern object Tier 1 uses — one vocabulary, one source of truth in this service).
- Exclude any candidate whose lowercased name is in `self._submit_approvals` (parity with Tiers 1-2; today Tier 3 skips this check).
- Exclude nameless candidates. A control with no accessible name gives the LLM zero signal and gives the commit regex nothing to veto — picking it is a blind click. Fail closed. (Icon-only wizards become a Tier-3 miss and end honestly; vision-based advance is a future phase, not this one.)
- Links remain eligible (framework apps render advance controls as anchors) but pass the same name filters.
- Correct the `_walk_wizard` docstring claim "the advance control already passed the danger + commit-word gates" — after this change it is true again for every tier; the docstring must say the gates are applied per-tier.

### 0.2 Server-side filter in the advance agent (layer 2 — defense in depth)

`qe-central/app/services/advance_agent.py`:

- The service maintains its own commit-veto pattern and drops commit-labeled, danger, disabled, and nameless controls from the prompt **before** the LLM sees them, re-mapping the LLM's 1-based pick to the caller's original indices. The endpoint then cannot return a commit pick to any caller, present or future — the explorer's filter is not the only wall.
- qe-central and qe-explorer are separate containers with no shared library, so the vocabulary is deliberately duplicated; **parity is pinned by test**: each service carries a test asserting the exact pattern source string, and the two test files reference each other by path so a change to one fails review without the other.

### 0.3 Model instruction (layer 3 — belt and braces)

`advance_agent.SYSTEM` gains an explicit rule: controls that submit, pay, purchase, sign, finalize, or otherwise commit the transaction are never a valid answer; if the only way forward commits, the flow is at its boundary — reply 0. This layer is not trusted for safety (layers 1-2 are), but it reduces wasted picks that layers 1-2 would discard.

### 0.4 Terminal correctness at the boundary

With commit candidates filtered, a page whose only forward control is a commit control produces Tier-3 "no candidates" → `trig is None` → the existing boundary check sees the commit button → terminal `submit_boundary`, `completed: true`. That is the honest and intended outcome: the funnel was walked TO its boundary. Covered by test, not assumption.

### 0.5 Tests (Phase 0 exit gate)

`qe-explorer/tests/test_e2e_advance.py` (extend):
- Tier-3 candidate list, captured via injected oracle: commit-word buttons, `submit_approvals` names, and nameless controls are absent; links present; danger/disabled absent.
- Signature-page scenario: controls = one commit-labeled button + one back button → `_pick_advance_e2e` returns `None` even with an oracle that would answer 1 → walk terminal is `submit_boundary`, journey `completed: true`, and **no click was issued** on the commit control.
- Prompt-injection scenario: a control named as an instruction to the model cannot cause a commit click, because commit filtering happens before any prompt is built.

`qe-central/tests/test_advance_agent.py` (extend):
- Server-side drop of commit/danger/disabled/nameless controls; prompt contains none of them.
- Index re-mapping: LLM answers relative to the filtered list; the returned index addresses the caller's original list.
- Vocabulary parity test (mirrored in the explorer suite).

Golden corpus: add the signature-page scenario to the escape→guard corpus per the standing law (every escape becomes a permanent guard).

---

## Phase 1 — Honesty: oracle failure is its own terminal (closes B2)

### 1.1 Three-state oracle outcome, end to end

The binary `int | None` becomes a three-state outcome carried through every hop:

| State | Meaning | Produced when |
|-------|---------|---------------|
| `picked` | LLM chose a control | Parsed reply maps to a valid filtered candidate |
| `none` | Honest "nothing advances" | Parsed reply is exactly 0 |
| `unavailable` | Unknown — the decision could not be made | Transport error, non-200, timeout, `result.ok` false, unparseable reply, empty candidate list after server-side filtering when the caller sent candidates |

Contract changes:
- `advance_agent.pick_advance` returns a structured result (status + optional index) instead of `int | None`. An unparseable reply is `unavailable`, never `none` — "the model said something we couldn't read" is not "the model said stop".
- `POST /internal/pick-advance` response becomes `{"control_index": int|null, "status": "picked"|"none"|"unavailable"}`. The endpoint keeps returning HTTP 200 for `unavailable` (best-effort contract unchanged); HMAC auth unchanged.
- The explorer oracle callable (`main.py::_make_advance_oracle`) returns the structured outcome; any exception, non-200, or malformed body maps to `unavailable`.
- `_pick_advance_e2e` returns the pick plus the oracle outcome so `_walk_wizard` can classify the terminal. Tier-1/Tier-2 hits never consult the oracle and carry no oracle state.

### 1.2 New terminal in the flow ledger

`qe-explorer/app/flow_ledger.py`:
- New constant `TERMINAL_ORACLE_UNAVAILABLE = "oracle_unavailable"`, exported, **not** added to `COMPLETING_TERMINALS` (line 50) — `completed` stays derived and this terminal derives to `false`.
- Summary prose for the new terminal states plainly: the walk stopped because the advance-decision service was unavailable; the journey is **not proven complete**; re-crawl when the service is healthy.

`_walk_wizard` terminal classification (crawler.py lines 1969-1980) gains one rung, ordered before the submit-boundary/no-advance pair: `trig is None` **and** the Tier-3 consultation for this page ended `unavailable` → `TERMINAL_ORACLE_UNAVAILABLE`. An honest LLM `none` (or Tier-3 never reached because Tiers 1-2 decided, or no candidates existed) keeps today's classification.

### 1.3 Attribution and display

- Attribution: `oracle_unavailable` maps to the platform side of the never-blame-the-app ladder (same family as env/infra rungs in the Attribution Engine). The tenant's app is never faulted for our LLM being down.
- verdict-portal journeys panel: the new terminal renders as honest copy — stopped mid-flow, platform advance service unavailable, journey not covered — visually distinct from both `completed` and `budget_exhausted`.
- Logging: WARNING-level event on every `unavailable` outcome in both services (platform-api suppresses INFO — standing gotcha), carrying tenant, crawl, fingerprint, and tier context. No page content in logs.

### 1.4 Tests (Phase 1 exit gate)

- Oracle raises / times out / returns non-200 / returns garbage → step terminal `oracle_unavailable`, `completed: false`.
- LLM honest 0 on a true dead-end page → `no_advance`, `completed: true` (the honest case still completes).
- `COMPLETING_TERMINALS` membership pinned by test: exactly `{submit_boundary, no_advance}`.
- Endpoint contract test: all three statuses; `unavailable` still HTTP 200.
- Ledger prose test for the new terminal.

---

## Phase 2 — Resilience: timeout, breaker, memo, cap (closes G4, half of G3)

### 2.1 Timeout

Oracle POST timeout drops 30 s → 8 s (`main.py`). A stuck page is worth seconds, not half a minute; the honest terminal from Phase 1 makes fast failure safe.

### 2.2 Per-crawl circuit breaker

State lives in the per-crawl oracle closure (one oracle per crawl already — no cross-crawl leakage):
- After **3 consecutive** `unavailable` outcomes, the circuit opens for the remainder of the crawl; further Tier-3 consultations return `unavailable` immediately with no HTTP call.
- One WARNING when the circuit opens, carrying the failure count and crawl id. `picked`/`none` resets the consecutive counter.

### 2.3 Within-crawl memoization (kills the double call)

- Tier-3 outcomes memoized per state fingerprint in the Crawler instance: `fingerprint → (picked control identity | none)`. The wizard entry check (line 1897) and the loop's first iteration (line 1930) hit the same fingerprint — second consultation is free.
- `unavailable` is **not** memoized (a later page may succeed once transient trouble passes; the breaker handles systemic failure).
- Bounded by states visited (already budget-bounded); no eviction machinery needed.

### 2.4 Per-crawl oracle call cap

- Hard cap on Tier-3 HTTP calls per crawl, env `QEC_ADVANCE_ORACLE_MAX_CALLS`, production default 40 (half the E2E advance budget). At the cap, further consultations return `unavailable` (honest terminal, WARNING once). A pathological app cannot burn unbounded tokens.

### 2.5 Tests (Phase 2 exit gate)

- Breaker opens after exactly 3 consecutive failures; no further HTTP attempts observed; success resets the counter.
- Memo: two consultations for one fingerprint issue one HTTP call; `unavailable` not cached.
- Cap honored; cap hit logs once and yields honest terminals.
- Timeout value pinned by test.

---

## Phase A-P — Release A live proof gate (deploy + prove before Release B starts)

Deploy: `docker compose -f docker-compose.qec.yml build qe-central qe-explorer` + `up -d --force-recreate` on verdict-box (BUILD+RECREATE, never docker cp; KEK/data-volume trap rules apply — no platform-api recreate is needed for this release).

Live proofs on VKPower Life (all three must pass; results recorded in the crawl manifest and screenshotted for the register):

1. **Safety proof:** E2E crawl reaches the signature page; journey terminal `submit_boundary`; VKPower admin/data shows **zero** applications submitted by the crawl identity.
2. **Honesty proof:** E2E crawl with the LLM route deliberately broken (dead upstream) at the quote health-check page; journey terminal `oracle_unavailable`, `completed: false`; portal shows the honest copy; attribution blames platform, not app.
3. **Cost proof:** healthy E2E crawl issues exactly one oracle call for the quote health-check fingerprint (memo covers the entry double-call); total Tier-3 calls ≤ cap.

Rollback: images are tagged pre-deploy; `oracle_unavailable` is additive to the ledger so a rollback re-reads old manifests without error.

---

# RELEASE B — the scale release

## Phase 3 — Evidence: every advance records who decided (closes G6)

- `_pick_advance_e2e` returns the deciding tier with the pick; explore/target advances are implicitly tier 1.
- Flow step schema (`flow_ledger.build_flow` steps) gains `advance: {tier, control_name, oracle}` per advanced step; journey rollup gains `oracle_advances` count and `tiers_used`. Control names are UI shape, already present in evidence — no value egress.
- Crawl coverage summary gains per-tier advance counts; the portal journeys drill-down badges agent-decided steps. This is the audit trail for the certification story and the raw telemetry Phase 5 consumes.
- Completion callback carries the enriched flows unchanged in transport (additive fields only).
- Tests: step schema, rollup counts, additive-compatibility with pre-Phase-3 manifests (old flows without `advance` render without error).

## Phase 4 — Learning: advance memory + consent-gated priors (closes G3 fully)

Mirrors the field-learning architecture (P0-P5) — same doctrine, same seams, same DB.

### 4.1 Advance signature (value-free by construction)

Signature = hash over the decision point's shape: normalized candidate labels + kinds + page-title token shape. **No URLs, no hosts, no user values** (F1 URL-guard doctrine applies to learned artifacts). The chosen control is stored as its normalized label, matched against candidates by label at recall time.

### 4.2 Tenant-private advance memory

- New qecentral DB migration (next number in sequence): `advance_memory` — tenant_id, signature, chosen_label_norm, proof_count, last_proven_at; unique (tenant_id, signature); **RLS from day one** (standing production-readiness requirement for new tables).
- **Recall:** inside `advance_agent.pick_advance`, before the LLM — a signature hit returns `picked` with zero LLM cost. The endpoint seam already exists; the explorer changes not at all for recall.
- **Write-back only on proof:** the LLM's guess is not knowledge until the crawler observed a genuine advance (effect + new unseen state). Harvest happens at completion-callback time from Phase-3 evidence: steps where `advance.tier == 3` and the walk genuinely advanced upsert into `advance_memory` (increment proof_count). No mid-crawl write path, no unproven memory.

### 4.3 Cross-tenant priors (OFF by default)

- Consent-gated, per tenant, using the existing consent machinery from field learning. Contribution is the normalized advance **label pattern** only — value-free, tenant-anonymous.
- Recall as "Tier 2.5": a candidate whose normalized label matches a high-confidence shared prior advances without an LLM call. Sits after Tier 2, before Tier 3; same commit/danger/approval filters apply (Phase 0's gates are tier-independent).

### 4.4 Tests

Recall hit skips the LLM; only proven tier-3 advances are written; consent gate honored both directions; signature contains no URL/host material (asserted on the hash input builder); RLS policy test; migration up/down.

## Phase 5 — Vocabulary: language packs + Tier-2 tightening (closes G5, G7)

### 5.1 Language packs, union-matched

- Advance and commit vocabularies become per-language data packs compiled into union patterns at service start — a word is an advance word if it is one in **any** supported language; commit likewise. Union matching removes per-page language detection as a failure point, and a wider commit union only ever fails **closed**.
- Both services load the same pack data; the Phase-0 parity tests extend to pack content.
- Initial pack set is driven by Phase-3 telemetry (which languages actually hit Tier 3 in the fleet), not guessed. The submit-boundary check and Tier-1/2 regexes consume the union patterns; the refuse-pack danger signal remains an independent layer.

### 5.2 Tier-2 tightening

- Tier 2 lifts the commit veto **only** for destination-shaped labels: advance word followed by a destination preposition before the commit word (the "Continue to Payment" shape). Conjunction shapes ("advance-word & commit-verb") no longer pass Tier 2; with Phase 0 they are also unreachable from Tier 3, so such a page ends `submit_boundary` — honest and safe, because a label that *says* it commits is treated as committing.
- Tests: destination shape passes, conjunction shape ends at the boundary, per-language destination prepositions covered by pack tests.

## Phase B-P — Release B live proof gate

1. **Learning proof:** two consecutive E2E crawls of VKPower Life; crawl 2 issues zero LLM calls for fingerprints proven in crawl 1 (memory recall visible in logs/telemetry).
2. **Evidence proof:** portal journey drill-down shows tier badges; manifest rollups match step-level records.
3. **Privacy proof:** dump of `advance_memory` rows for the tenant contains no URLs, hosts, or values; consent OFF tenant contributes nothing to shared priors (asserted in DB).
4. Post-deploy: update the architect artifact so the published architecture matches shipped behavior (three-state oracle, new terminal, learning layer).

---

## 4. Order, dependencies, and estimates

| Phase | Depends on | Size | Risk |
|-------|-----------|------|------|
| 0 | — | S (two filters + one server-side filter + tests) | Low — additive gates, explore/target untouched |
| 1 | 0 | M (contract change through 4 hops + ledger + portal copy) | Medium — touches the completion path; additive terminal |
| 2 | 1 | S (closure state + memo dict + env cap) | Low |
| A-P | 0-2 | S (deploy + 3 scripted proofs) | — |
| 3 | A-P | S (schema additive) | Low |
| 4 | 3 | M (migration + recall/write-back + consent wiring) | Medium — new table, RLS, harvest path |
| 5 | 3 (telemetry) | M (pack format + parity + Tier-2 shape rule) | Medium — vocabulary changes need corpus re-run |
| B-P | 3-5 | S | — |

Explore/target mode behavior is bit-for-bit unchanged through every phase — `advance_oracle` remains `None` there, all new gates live behind the E2E path, and the existing 415-test explorer suite plus the frozen-factory guarantee are the regression net.

## 5. Acceptance criteria (founder sign-off checklist)

- [ ] A crawl can no longer click any commit-labeled or operator-approved-submit control from any tier, proven live on the signature page with zero submissions recorded.
- [ ] An LLM outage during a walk yields `oracle_unavailable`, `completed: false`, platform-side attribution, and honest portal copy — proven live with a broken LLM route.
- [ ] One oracle call per unique stuck page per crawl; breaker and cap proven under fault injection.
- [ ] Every advanced step's evidence names its deciding tier; certification view exposes agent-decided steps.
- [ ] Second crawl of the same app resolves previously-proven decision points from tenant memory with zero LLM calls.
- [ ] No learned artifact contains URLs, hosts, or user values; cross-tenant contribution requires explicit consent and ships label patterns only.
- [ ] Non-English commit vocabulary unioned into the boundary check before any non-English tenant onboards to E2E mode.
