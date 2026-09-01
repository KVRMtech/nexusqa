# Auto-Heal — Phased Build Plan (TIER 0 → TIER 5)

**Status:** living doc · last updated 2026-06-23
**Owner:** Nexus QA / Auto-Heal subsystem
**Scope:** the complete control-kind self-heal engine — *grounded, human-gated, never green-washes.*

## The doctrine (applies to every item below)
> **"99% auto-heal" = 99% of GENUINE drift HEALED *and PROVEN*** — recompile the owned Playwright
> spec → re-run **headed** → verify the recorded value/outcome via an oracle **orthogonal to
> element-resolution** → confirm on a **2nd independent green** → human-gate the persist — **with the
> rest HONESTLY escalated and ZERO real defects ever turned green.**

**Guiding principle:** *every new layer raises coverage only behind a grounded proof + a human gate.
Coverage never comes at the cost of the never-green-wash guarantee.* A heal that flips a test green
without a grounded committed-value oracle is a green-wash and is forbidden.

## Status legend
| Mark | Meaning |
|---|---|
| ✅ | Built + deployed + committed to git |
| 🟩 | Built + deployed (running on VM) — **not yet committed to git** (see Durability, T0.4) |
| 🟡 | In flight / partial |
| ⬜ | Not started |
| 🔒 | Blocked on infra/auth (GPU node, consented tenants, prod migration approval) |

> **Divergence warning:** the deployed heal code lives in the running containers + local `_vm_*.py`
> working copies; the repo is *behind*. "🟩" items are real and live but must be reconciled (T0.4)
> before they count as durable. Verify a 🟩/✅ against the deployed file before relying on it.

Effort: **S** ≤½d · **M** ~1–2d · **L** ~3–5d · **XL** >1wk. Risk: **Lo/Md/Hi**.

---

## CURRENT SNAPSHOT (what's live right now — updated 2026-06-23 PM)
- **TIER 2 ENGINE deployed:** the **UACR `universal_control`** runtime-introspecting recipe (resolve handle →
  introspect live element once → dispatch combobox/slider/switch/checkbox/accordion/conditional/native-select →
  grounded committed-value oracle → RED on unresolvable). `detect_interaction` widened to fire on the FIRST
  control-kind failure (kills the 2-iteration `.fill→.selectOption` dance). Back-compat recipes
  (combobox/slider/switch/conditional) still registered. **8/8 static unit tests pass.** Runtime behavior
  (esp. accordion handle-resolve) deferred to the final live test per the build-all-then-test decision.
- **TIER 1 deployed:** DATA_PRECONDITION_UNMET + VARIANT_SUSPECTED refuse causes; **metamorphic suite-grounding
  roll-up** (stamps grounded/outcome_not_grounded, escalates a fully-hollow green); shared capture store
  (DB `aput` + in-memory fallback, no migration); **anchored-URL helper**. Verified non-breaking. **Proactive
  content oracle DEFERRED** (as-designed always-on would false-RED on transient/dynamic `after` — steps 11/20;
  needs pattern-aware refinement → T1.1-refine).
- **Guards live:** assertion-immutability ✅, AUTH refuse ✅, outcome_not_grounded labeling ✅, confirmation gate ✅.
- **Foundations:** T0.2 ✅, T0.3 re-anchor token-coverage ✅ (deployed), tracked plan + memory ✅.
- **In flight:** `autoheal-tier4-layers` workflow — T4.1 headless-parallel prove + T4.2 queue/fairness/SLA/flake
  + L4 a11y-state grounding + L5 timing/materialize/portal/frame (additive, default-off, patches to assemble).
- **Decision (2026-06-23):** build ALL remaining tiers (T4 → T2-layers → T3 → T5) with static verification,
  hold the single live functional test for the end (user-directed; big-bang integration risk accepted).

---

## TIER 0 — Finish the in-flight PR (closest to done)
| # | Item | Status | Effort | Risk | Notes / verification |
|---|---|---|---|---|---|
| T0.1 | **End-to-end demo** — set Environment URL → Auto-Heal heals the suite → Clean Run V1; `?break=regression` REFUSES | 🟡 | M | Md | Heals 13+14 today; blocked on the full recipe library (in-flight workflow). Done = one run → Clean Run V1 on the 24-step test + a refuse on `?break=regression`. |
| T0.2 | **AST assertion-immutability guard** — `assert_assertions_unchanged`: a heal may rewrite locators/interactions but can never drop/weaken an `expect(...)` (AST-counted, not line-diff) | ✅ | — | — | `self_heal.py` + wired into `heal_step` and `_run_auto_heal`. Committed `f9c6ebc`. Unit-verified in container. |
| T0.3 | **Re-anchor `_similarity` substring bug** — token-coverage, not bare substring ("Class" must not match "First Class Cabin Upgrade Class") | ⬜ | S | Md | Mis-steers re-anchor heals. In `self_heal.py` re-anchor similarity. |
| T0.4 | **Durability / repo reconciliation** — reconcile the whole heal subsystem (`compiler`/`self_heal`/`test_factory`/`interaction_resolver`/`heal_evidence` + SDK `HealEventRow` + migration) into git | 🟡 | M | Md | `self_heal`+`compiler`+combobox ✅ `f9c6ebc`; slider+test_factory+heal_evidence+SDK row **not** in repo. Migration needs approval (T-auth). |

---

## TIER 1 — Correctness / anti-green-wash (highest-value remaining)
| # | Item | Status | Effort | Risk | Notes / verification |
|---|---|---|---|---|---|
| T1.1 ★ | **Grounded VALUE / content oracle** — assert the recorded post-action value/content (orthogonal to element-resolution); harden `toHaveURL` from *contains* → **anchored** (`/account` ≠ `/account/closed`) | 🟡 | L | Hi | The single biggest hole: same-URL/SPA wrong-actions heal green today. Recipe-level committed-value oracles ✅ (combobox/slider); the **general same-URL content oracle + anchored URL** is open. Until this ships, "99% proven"/"zero defects green" are *targets, not claims*. |
| T1.2 | **Drift-vs-defect precision causes** — high-precision escalations never to be healed | 🟡 | M | Md | `AUTH_PRECONDITION` ✅ (`f9c6ebc`). **`DATA_PRECONDITION_UNMET`** ⬜, **`VARIANT_SUSPECTED` (A/B)** ⬜. Must land before broadening accepted causes. |
| T1.3 | **End-to-end metamorphic acceptance** — a whole-suite heal must require the full recorded outcome chain; label oracle-less steps `outcome_not_grounded` | 🟡 | M | Md | `step_outcome_grounded` ✅ (per-step label, deployed). Suite-level metamorphic chain ⬜ — so an all-green suite that no longer does the business flow is *visible, not hidden*. |
| T1.4 | **Capture store → shared + observable** — move failure-state a11y capture off per-worker in-memory to a shared store; add capture-success-rate observability | ⬜ | M | Md | Per-worker in-memory silently over-escalates (the silent-degradation lesson). |

---

## TIER 2 — Coverage: more control-kinds + remaining healing layers
| # | Item | Status | Effort | Risk | Notes |
|---|---|---|---|---|---|
| T2.1 | **More L3 interaction recipes** — typeahead/autocomplete, multi-select, date-picker grid, contenteditable/rich-text, **toggle switches**, file-input, **sliders/segmented**, **accordion/disclosure**, conditional reveal | 🟡 | L | Md | combobox ✅, slider 🟩. switch/accordion/checkbox/conditional/shadow/iframe/autocomplete **being generated now** by the engine workflow. datepicker/multiselect/contenteditable/file = next batch. |
| T2.2 | **L4 — accessibility-tree deep grounding** — use captured AX tree (role + state: expanded/checked/editable) to disambiguate | ⬜ | L | Md | Requires capture fix: today's `flatten_aria` drops state. |
| T2.3 | **L5 — timing / materialize / portal / frame** — scroll-until-materialize (virtualized), retry-un-scoped-at-root (portals/modals), `frameLocator(url-pattern)`, baseline-relative wait budget + perf-regression flag | ⬜ | L | Md | New wait/scope channel. Needs a per-step latency baseline. |
| T2.4 | **L6 — visual / embedding fallback** (propose-only, human-gated) — visual candidate → snap to DOM/AX node → derive selector → prove | ⬜ | XL | Hi | For empty a11y tree (icon-only/div-soup). |
| T2.5 | **L7 — self-hosted VLM fallback** (propose-only, human-gated) — VLM picks a candidate from OUR list (never a raw coordinate) → snap-to-node → prove | 🔒 | XL | Hi | Needs capture enrichment + separate GPU node + AGPL/weight-license clearance. |

---

## TIER 3 — Any-UI-technology robustness
| # | Item | Status | Effort | Risk | Notes |
|---|---|---|---|---|---|
| T3.1 | **Shadow DOM** — open roots via roles; closed/slotted → detect + escalate (never green-wash) | 🟡 | M | Md | `shadow_dom_input` recipe in flight (open root). Closed → escalate. |
| T3.2 | **Iframes** — capture owning frame by **url-pattern** (never index) + re-anchor frame *and* inner control | 🟡 | M | Md | `iframe_field` recipe in flight. Capture-side frame traversal ⬜ (today doesn't traverse cross-origin). |
| T3.3 | **Canvas / Flutter (no DOM)** — semantics/ARIA layer first; else VLM-propose + CDP input, strongest proof, always human-gated; never claim coverage on empty-AX canvas | 🔒 | XL | Hi | Depends on T2.5 (VLM). |
| T3.4 | **Virtualization & dynamic timing** — scroll-to-find before declaring absent (false-not-found trap); resilient non-static-sleep waits | ⬜ | M | Md | Overlaps T2.3. |

---

## TIER 4 — Execution substrate (scale & cost)
| # | Item | Status | Effort | Risk | Notes |
|---|---|---|---|---|---|
| T4.1 | **Prove headless + parallel** — run the prove via headless `/run` (workers>1); reserve the single headed display only for the watched demo | ⬜ | M | Md | Today a 12-iteration headed full-suite loop monopolizes the one runner ~1h. Sequenced **right after TIER 1** so we can *afford* to prove. |
| T4.2 | **Job queue + per-tenant fairness + per-heal SLA; measure harness flake** (every published number flake-corrected) | ⬜ | L | Md | |

---

## TIER 5 — The compounding moat (longest horizon, highest defensibility)
| # | Item | Status | Effort | Risk | Notes |
|---|---|---|---|---|---|
| T5.1 | **Induced-drift benchmark** on `?break=` proving grounds (rename / custom-combo / control-kind / regression) → measured **false-heal rate** + calibration (ECE) | ⬜ | M | Md | Correct heal is known → ground truth. Gate for any published number. |
| T5.2 | **Consented failure→fix safety flywheel** — k-gated, DP-noised, default-OFF priors transferring widget-class mechanics + which-oracle-catches-green-wash, never tenant content | 🔒 | XL | Hi | Substrate founded (ledger/featurize). Needs N consented tenants + GPU. Safety claim gated on T1.1 precision. |
| T5.3 | **Part-11 hash-chained, signed evidence ledger** over capture → diagnosis → candidate → proof → approval | 🟡 | L | Md | `heal_evidence.record_heal_event` writes events 🟩; hash-chain + signing ⬜ (version lineage isn't tamper-evident yet). |
| T5.4 | **RBAC on approve** — the human gate is "theater without RBAC"; evidence chain records *who* approved | ⬜ | M | Hi | Required before any "human-gated" claim. |
| T5.5 | **3-tier heal policy** — auto-apply+prove+confirm above threshold / human-approve marginal / hard-fail real defects; publish false-heal rate only after T1.1 + T5.1 + flake correction | 🟡 | M | Md | auto-apply+prove+confirm ✅; marginal-approve + hard-fail-defect partial. |

---

## Sequence (build order)
1. **TIER 0** — finish demo (full recipe library) + commit (T0.2 ✅; T0.1 via workflow; T0.3; T0.4).
2. **TIER 1** — value/content oracle (T1.1 ★) + drift-vs-defect causes (T1.2) → *this is what makes "99% proven" honest.*
3. **TIER 4.1** — headless-parallel prove → so we can *afford* to prove at scale.
4. **TIER 2** — more recipes + timing/frame layers (T2.1 → T2.3).
5. **TIER 3** — any-UI-tech robustness.
6. **TIER 5** — flywheel + evidence chain + RBAC + benchmark (the durable moat).

> Note: the current `autoheal-universal-engine` workflow front-loads **T2.1** (recipe library) +
> parts of **T1.2** (refuse causes) because they're needed to make the T0.1 demo heal the whole
> 24-step test in one pass. Their committed-value oracles are the per-recipe instance of T1.1;
> the *general* same-URL content oracle remains the headline TIER 1 deliverable.
