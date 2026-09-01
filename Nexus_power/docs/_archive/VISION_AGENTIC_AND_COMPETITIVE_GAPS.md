# Vision Agentic + Auto-Heal/Run Competitive Gaps

**Date:** 2026-06-24 · **Status:** research + design (multi-agent: 12 competitor-research + 6 codebase + 16 fairness-verified + synthesis/critic). NOT committed.
**Standard applied:** video-grounded, owned-code, on-prem, **never green-wash** (every heal proven by an orthogonal oracle + 2× confirm; real bugs escalated honestly).

> Fairness note: several headline figures (Mabl "35+", Testim "<70%", Healenium "~0.5", Playwright Healer "~75%/25%") are vendor-stated or thinly third-party-sourced; flagged inline. Where a vendor *publicly concedes* a limitation, that is the strongest (self-admitted) citation, not a strawman.

---

## Part 1 — Where the market lags (the 7 gaps)

The whole field converges on one admitted weakness: **heals prove an element *resolves / resembles*, never that the step *behaved correctly*** — and the **bug-vs-test verdict is pushed onto a human** (or onto the customer's own assertions).

| Player | Self-heal | Bug-vs-selector | Auto repro/bug | Vision | Green-wash risk | On-prem/owned |
|---|---|---|---|---|---|---|
| **Mabl** | confidence-gated attr scoring; fails step if low-confidence | **None — self-admitted unsolved** | pre-fills Jira draft (manual trigger) | passive visual-regression warnings | moderate (own docs: "could result in false passes") | cloud only |
| **Testim/Tricentis** | ML scoring; validate-then-replace at <70% | none at locator layer; human RCA surface | rich human-triage bundle, **no repro** | screenshot diff (human) | moderate→real (auto-applies after *resolvability* check) | cloud |
| **Tosca Vision AI** | "find a **similar control** and continue" | none documented | none | strongest visual (image-recognition, not video) | **real, highest at heal layer** | on-prem but closed engine |
| **Functionize** | ML fingerprint; SmartFix | **best published** (6-family RCA; heal can't override failed verification) | Jira deep-link + steps | CV screenshot-diff | moderate, **assertion-dependent**, unquantified | hosted SaaS, proprietary |
| **testRigor** | selector-avoidance + intent-anchored heal | implicit only; no refuse-as-regression | video + PDF/Word export (not replayable repro) | semantic screen interpretation | moderate (tuned to keep green) | SaaS, closed |
| **Applitools Eyes** | visual oracle (not a healer) | visual axis only; **blind to no-pixel-change functional bugs** | baseline-vs-actual + RCA (DOM-web only) | CV at checkpoints | low-moderate | cloud |
| **Applitools Exec Cloud** | attribute-fingerprint heal; **reports PASS + wand** | weak/implicit, no oracle | none (PASS by construction) | none for healing | **highest** (canonical silent heal) | cloud lock-in |
| **Katalon** | priority-ordered fallback → LLM tier; human approve | none semantic | insights, no repro | single-frame + AX tree | moderate (human gate, but mis-bind executes once) | closed |
| **Healenium (OSS)** | LCS tree-compare + attr weighting; silent runtime heal | **absent** ("purely structural") | Postgres audit, no repro | screenshots as evidence only | **high** (silent, no pre-gate) | self-hosted, **no oracle** |
| **MS Playwright Healer** | LLM: replay→inspect live UI→patch→re-run; PR gate | **best mainstream attempt**: pass **or skip if it believes broken** (belief, not proof) | code patch + reasoning; no bug report | reasons over live DOM/AX | real (~25% wrong-but-similar) | **owned-code** (best ownership story) |
| **Playwright Trace Viewer** | none (evidence tool) | none (human) | richest **evidence**, no verdict/repro-report | passive film-strip | ~none (never declares green) | owned/local |
| **WE (target)** | grounded re-anchor → proven per-kind recipes; **oracle + 2× confirm** | **core thesis**: escalate REAL_REGRESSION honestly | **target = auto-authored repro+bug** (work remaining) | **video-grounded intent** as ground truth | **lowest by design** | **owned-code, on-prem-first** |

**The 7 gaps (where everyone lags):**
1. **No orthogonal *functional* oracle gates the heal** — they validate the substitute *resolves*, not that the step *did the right thing*. (Testim "validate-then-replace"; Healenium structural LCS; Applitools "closest fingerprint → PASS".)
2. **Bug-vs-selector discrimination is deferred to a human** — Mabl concedes it's unsolved ("a renamed button is cosmetic; a removed validation is a bug"); Testim merely *poses* the question on an RCA screen.
3. **Healing maximizes *continuation*, which IS the green-wash mechanism** — Tosca: "find a similar control… execution continues"; Healenium *assumes* every NoSuchElement is selector drift.
4. **Heal-safety is *assertion-dependent*, not intrinsic** — Functionize: healing "cannot override a failed verification" but "doesn't determine whether the business outcome was correct." Correctness is outsourced to the customer's assertions.
5. **No auto-generated repro + bug report tied to the verdict** — Mabl pre-fills a draft, Functionize deep-links, none **auto-author a replayable repro as a direct output of a bug-vs-test verdict.** ← *the genuinely unbuilt thing in the whole field.*
6. **Vision is checkpoint/screenshot, not video-grounded run intent** — all "visual" is single-frame/passive-diff, not a model watching the run to derive + validate intent.
7. **Cloud/SaaS + proprietary models dominate** — owned-code-AND-proven-heal is an open lane; the OSS owned-code options (Playwright Healer, Healenium) lack an oracle.

**Where WE already win (by design — not yet *measured*):** gaps 1, 2, 3, 4, 6, 7. The honest open fronts: **publish quantified oracle/escalation/false-pass metrics** (everyone is unquantified — first mover wins credibility), and **build the auto-repro bug report** (gap 5, the missing half of "honest escalation").

---

## Part 2 — The Vision Agentic tier (watch like a human; bug vs test; raise the bug)

A senior-test-engineer agent that **watches** an execution, decides per failing step **real bug vs test issue**, then either heals (grounded, proven) or **raises a bug with exact repro + the precise failure point + evidence** — and never green-washes. It is built **on top of existing seams**: it feeds the same `self_heal.diagnose()` taxonomy, routes through the same `agentic_heal.propose()` name-grounding boundary, applies via the same additive compiler channels, and proves through the same orthogonal-oracle + 2×-green gate. **Net-new = richer perception + authoring the bug report.**

### Perception bus (what it watches)
| Channel | Seam | Status |
|---|---|---|
| per-step screenshots (before/during/after) | runner reporter + after-extractor frames | reuse |
| live video / frames | `/run-live` headed + noVNC | reuse (sampling = net-new) |
| live a11y/DOM at failure | `capture-failure-state` → `heal_capture_store` `{nodes:[{name,role}]}` | reuse |
| **console + network (HAR)** | runner reporter ingest | **promote to an oracle axis (net-new)** |
| recorded baseline (grounded steps) | `ProductionTestStep` (verb/label/kind/value/after_outcome) | reuse |
| outcome contradiction | `outcome_contradicted_from_error()` + `classify_failure()` | reuse — the spine of the bug call |
| VLM grounding (closed-shadow/canvas) | `any_ui_resolver.propose_candidates()` (`NEXUS_VLM_GROUND_URL`) | reuse (default-off, propose-from-candidates) |

**How it watches:** *Sampled-watch (default)* — perceive only at event boundaries (step start / fail / settle): before/after frame (**Set-of-Mark** annotated — number every interactive control, never a raw coordinate), live a11y nodes, console/network delta. *Continuous-watch (opt-in)* — ~1–2 fps noVNC sampling to catch visual-only regressions (error toast over a "passing" step, never-resolving spinner, wrong-content same-URL SPA).

**Where it sits:** the **adjudicator between `diagnose` and `route`** — vision is an *additional signal, never an override of a contradicted oracle.*

### Bug-vs-test decision — FIVE grounded axes (the critic added N + flake)
- **V** visual: is the recorded outcome visible, or an error/blocked/wrong-content state?
- **D** DOM/a11y: does a control matching the recorded label/kind exist? renamed? kind-changed? absent? portal/scroll?
- **O** oracle: was the recorded outcome *contradicted*, merely *unreached*, or *trivially true*?
- **N** network/console **(new, near-dispositive + cheapest)**: a `4xx/5xx` in the step window (e.g. `POST /confirm → 503`) is a strong real-bug signal — promote from evidence to adjudication.
- **R** recorded-vs-live diff: what specifically changed.

| Signal pattern | Verdict | Action |
|---|---|---|
| outcome absent + error visible; **or 5xx in window**; action executed | **real_bug** | **Raise bug. Never heal.** |
| target not visible, similar-name node exists (same role) | selector_drift | heal via `reanchors` (re-point by live name) → prove |
| target visible but different control type (combobox vs textbox) | control_kind | heal via `interactions` recipe → prove |
| target not yet rendered (spinner/virtualized/portal) | timing/scope | heal via `waits` (scroll/retry-at-root/frame) → prove |
| empty list / "no records"; login page; variant marker | data/auth/variant **precondition** | **Refuse + escalate** (never heal) |
| **intermittent across re-runs** | **flake** (new axis) | re-run N×/quarantine; classify only when stable — don't file a bug on a race |
| recorded outcome legitimately changed (intended spec change) | **expected-result drift** (new axis) | **human spec-confirm** (avoid cry-wolf — the inverse of green-wash) |
| ambiguous (no contradiction proof, no single confident match) | NEEDS_REVIEW | escalate; author a *suspected*-bug **draft**, don't auto-file, don't heal |

**Conservatism rules:** contradiction is supreme (a contradicted oracle is `real_bug` even if the VLM "sees" a clickable look-alike — exactly the Mabl/Tosca failure we refuse); **two independent signals to heal** (deterministic cause + vision-confirmed grounded target, else NEEDS_REVIEW); refuse-families off-limits to the agent; no single VLM coordinate trusted (pick a target **by accessible name present in the live snapshot**; non-DOM → ranked `propose_candidates`, never a raw point).

### Bug report output (`build_defect()` — the wedge nobody ships)
```
DefectReport {
  title            "<App> — <recorded outcome> does not occur after step N (<action>)"
  severity         FLOW-mechanical (blocks/doesn't-block recorded flow) — NOT business criticality (renamed; or human-set)
  precise_failure  { scenario_id, step_number, locator (compiled ladder), action (verb+kind), value }
  repro_steps[]    the RECORDED grounded steps 1..N-1 verbatim (verb, label, value, url) — independently replayable
  expected         grounded recorded after_outcome / expected_outcome at step N
  actual           outcome_contradicted detail + perceived visual state + network excerpt
  evidence         { SoM annotated frame, baseline-vs-live frames, noVNC clip N-1→N,
                     console excerpt, network/HAR slice (the 5xx), Playwright trace.zip,
                     part11_ref (heal_evidence row_hash, signed + chain-verifiable) }
  classification   REAL_REGRESSION   confidence  diagnose+vision agreement
  routes_to        Jira connector | "Copy defect" | "Download .md"
}
```

**Example A — step-13 `Plan tier`/`Platinum — Concierge` = TEST ISSUE (not a bug).** D: live nodes have `{name:"Plan tier", role:"combobox"}`, no control literally named "Platinum — Concierge". V: dropdown labeled "Plan tier" with "Platinum — Concierge" as an *option*. O: not contradicted. → **rebind** `{name:"Plan tier", kind:"select"}` → compiler re-points + re-kinds → committed-value oracle + 2× green → PROPOSED v+1. **No bug filed.**

**Example B — true bug = REAL_REGRESSION.** After "Confirm booking" (step 14) recorded outcome is "Booking reference visible". D: button clicked OK. V: red "Payment service unavailable", no reference. N: `POST /api/bookings/confirm → 503`. O: contradicted. → **real_bug.** Files: title, severity=critical (blocks recorded flow), repro steps 1–13 verbatim, failing step 14 + locator + action, expected vs actual, evidence bundle incl. the 503 + trace, Part-11 row_hash. **The engine does NOT turn step 14 green. It refuses and files this.**

### Never-green-wash guarantees (inherited; vision may only strengthen)
1. propose-from-grounded-candidates (drop invented names; non-DOM → ranked set + snap-to-node).
2. orthogonal oracle (vision is the *finder*, never the *judge*).
3. 2× green confirm → PROPOSED only, never silently active.
4. human-gate (promotion + defect filing).
5. refuse-on-contradiction; **+ new visual gate ON by default for the cheap single-frame post-step check** (error/wrong-content overlay over a "green" step → re-classify to suspected-bug — extends the SPA same-URL green-wash gate to the visual channel).

---

## Part 3 — Corrections folded in (from the completeness critic)
- **Promote network/API (N) to a first-class adjudication axis** — cheapest + most deterministic real-bug oracle; ship honest discrimination *before* any VLM.
- **Add the flake/non-determinism axis** — re-run/quarantine; never file a bug on a race.
- **Add the expected-result-drift branch** — intended spec change → human spec-confirm (avoid false-defect cry-wolf).
- **Visual green-wash gate ON by default** for the cheap single after-frame check (we already capture it); continuous fps stays opt-in.
- **`severity` = flow-mechanical, not business** (rename or human-set — don't overclaim).
- **Set-of-Mark is a DOM/a11y-tier technique**; the closed-shadow/canvas tier relies on ranked `propose_candidates`, not raw coordinates (reconcile the contradiction explicitly).
- **Bound VLM cost** — frames-per-failure + failures-per-run budget (sonnet-default, opus-on-ambiguity).
- **Competitor matrix additions:** QA Wolf (direct GTM threat — same "we tell you if it's a real bug" pitch), Meticulous (auto-asserts — partial answer to gap 4), Autify/Reflect/Octomind. **Platform risk:** computer-use agents (OpenAI Operator / Claude Computer Use) commoditize "VLM watches a screen and decides."
- **Stop scoring unquantified capabilities as outright "wins"** → "win by design, unproven in numbers." **Publish false-pass / escalation / oracle-coverage metrics** — first mover on *measured* honesty wins credibility.
- **Soften "none auto-generate repro"** → "nobody auto-authors a structured, independently-replayable repro *as the direct output of a bug-vs-test verdict*."

---

## Part 4 — Build order (smallest risk → biggest wedge first)
- **V0 — Auto-authored repro+bug bundle + the network/API oracle axis (BUILD FIRST).** Pure assembly over seams that already exist (`diagnose`→REAL_REGRESSION, `heal_evidence` Part-11 ledger, client "Copy defect"/"Download .md"); **zero ML, zero GPU, zero hallucination surface.** Structure console/network from the reporter payload; `build_defect()` deterministic assembler; defect markdown. This is the one capability *nobody* in the field ships — it converts our "honest red" into "actionable red."
- **V1 — Sampled vision adjudication.** SoM frame annotation at event boundaries + the §2 adjudicator + two-signals-to-heal. Models: **sonnet-4-6 vision default**, **opus-4-8 on ambiguity**, **self-hosted UI-TARS/OmniParser** via `NEXUS_VLM_GROUND_URL` for on-prem/non-DOM (the on-prem moat). Risk contained: vision can only *propose a grounded name*; the oracle + 2× green is the only thing that turns a step green.
- **V2 — Continuous-watch.** ~1–2 fps noVNC sampling for visual-only regressions → escalate to human-review (never auto-file). Opt-in/infra-gated.

**Durable moat (the technique is catchable):** owned-code + on-prem + the Part-11 hash-chained heal/defect ledger + **published oracle metrics**. SaaS incumbents structurally can't follow on-prem; measured honesty is the part rivals can't quickly copy.
