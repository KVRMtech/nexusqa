# Nexus QA — Autonomous Agentic-QE Master Plan

> Generated 2026-06-27 from a 20-agent, code-grounded workflow (ground → 4-cluster competitor
> teardown → 8 capability designs → triage taxonomy → adversarial review → build plan). Every
> capability cites real files; "extend vs new" is honest. Triggering case: a test failed at
> *"Enter Florida in State/Province"* while **Country = Canada** — a cross-field data-validity
> inconsistency a human SME catches in one second, that today lands silently in `needs_review`.

## North-star
**The autonomous senior-QE that, on *every* failure, reasons like a human across the live app + the
recorded form + the run forensics → a grounded plain-English diagnosis and a *fix-it vs build-it vs
flag-it* verdict — and that structurally NEVER green-washes** (it proposes, an orthogonal oracle
disposes, it refuses-and-auto-authors a defect on real regressions, and stays **inert when it can't
ground its claim**).

## What the competitor teardown found (all 4 clusters agree)
Every incumbent's self-heal **"finds a plausible element and continues"** — the documented false-pass
that *hides real bugs*. Specifically, **nobody** ships:
1. **Cross-field / business-logic data-validity reasoning** (the Florida-vs-Canada class) — Mabl/Testim score *one locator*, Functionize keys off *assertions*, testRigor re-interprets *one instruction*.
2. **Never-green-wash as a structural contract** — Tosca/BrowserStack/Healenium rebind-and-pass even when a **bug** renamed the control; testRigor/Copilot are *tuned to stay green*.
3. **Auto-run to a MACHINE verdict** — QA Wolf auto-investigates but routes to a **paid human**; everyone else's RCA is a separate **click/report-time** surface.
4. **Grounded PRODUCT vs SCRIPT vs ENVIRONMENT triage** with log/console/network/trace forensics — Katalon buckets *post-hoc* without routing; Sauce *explains*, doesn't adjudicate.
5. **An auto-authored, replayable defect** as the direct output of a bug-vs-test verdict — *nobody*.

**That quintet is our ownable whitespace.** And the durable moat on top (technique is catchable in
~quarters): owned-code + on-prem + the Part-11 hash-chained ledger + **PUBLISHED measured
never-green-wash metrics** (every rival's heal/flake number is vendor-stated and silent on honesty).

## The good news: it's mostly EXTEND
The never-green-wash spine already exists and is verified in code — `self_heal.diagnose()` (deterministic
REFUSE families + heal-able causes, `$0`/read-only), `agentic_heal.propose()` (LLM grounded to the live
a11y snapshot, drops any pick not verbatim), `defect_report` + `network_oracle` (auto-author repro on
REAL_REGRESSION/5xx), `assert_assertions_unchanged` (can't weaken to go green), `heal_policy` tiers.
The **only genuinely net-new engineering** is the **cross-field semantic reasoner** + its **live-option
capture**.

---

## Capability catalog (priority · extend/new · effort)

| # | Capability | Pri | E/N | Eff | What to build (grounded) |
|---|---|---|---|---|---|
| 1 | **Auto-run deterministic diagnosis on every failure** (zero-click + keep on-click) | **P0** | EXTEND | M | Fire-and-forget hook at run-ingest (`routers/test_factory.py` ~2869 + the timeline builders); call `self_heal.first_failures` → `analyze_step` (read-only, `$0`); persist to a **new nullable `diagnosis` JSONB** on the step row; render in `StepTimeline.tsx` with no click. Auto + on-click call **one** function so they can't diverge. |
| 2 | **Cross-field data-validity reasoner** — *flagship* Florida-vs-Canada | **P0** | NEW | M | Net-new `semantic_diagnosis.py` modeled on `agentic_heal.propose` (forced-tool + verbatim-drop). Feeds the LLM: failing step + **sibling recorded field values** (Country=Canada) + the **LIVE option set**. Emits a REFUSE-class cause `DATA_VALIDITY_CROSS_FIELD` (enriches, never short-circuits a REFUSE family). Any suggested value must be **verbatim in the live options or it's dropped**. |
| 3 | **Live-option capture** (the grounding spine for #2) | **P0** | EXTEND | M | **Critical fix:** `build_field_meta` reads the *recorded* visits, not the live page, and heal-capture nodes are name+role only with **no option set**. So extend `NEXUS_HEAL_CAPTURE` to **open the failing chooser and record its real options** (or harvest sibling radio/checkbox names). Without this, #2 is a hallucination engine wearing a "grounded" label. |
| 4 | **Inference-provenance + "possible" chip + budget/dedupe guards** | **P0** | EXTEND | S | Per-run **hard cap** on semantic LLM calls + **dedupe by step fingerprint** (a *tested* invariant — avoids a 50-scenario regression firing 50 calls → 429 cascade). Stamp `grounding_quality`; render a stale/re-analyze affordance; emit a metric on the swallow. |
| 5 | **Failure-source triage** — *is it our PRODUCT, the SCRIPT, or the ENVIRONMENT?* | **P1** | NEW | M | Net-new `failure_source_triage.py` over signals we already compute. **PRODUCT** = outcome-contradicted / 5xx-at-origin → `build_defect` (never heal). **SCRIPT** = control-kind/selector-drift/cross-field → heal. **ENVIRONMENT** (the missing lane) = net::ERR/DNS/transport-timeout-without-5xx, auth-bounce, variant, data-precondition → **flag + quarantine** (never bug, never heal). |
| 6 | **Fix-it vs Build-it vs Flag-it** — explicit machine verdict, oracle-gated | **P1** | EXTEND | S | The triage class becomes the per-failure **decision chip**. SCRIPT→fix (gated by `heal_policy` AUTO/APPROVE/FAIL); PRODUCT→file replayable defect; ENVIRONMENT→flag. Agents only **propose**; the orthogonal oracle + 2× green confirm + assertion-immutability remain the only thing that turns a step green. |
| 7 | **Requirement / Intent oracle** (RTM-grounded) | **P2** | EXTEND | L | Wire the `/rtm` endpoint into the per-step diagnosis so a contradicted outcome is checked vs the linked **requirement** (was it wrong vs the *requirement*, not just the recording). Cites the violated requirement in the defect — the compliance evidence-chain moat. Out of V1. |

## Phased sequence (one coherent arc)
- **Phase 0 — Always-on honest diagnosis (~1–1.5 days).** Cap #1. Every plain-run failure shows a
  `$0`, grounded, never-green-wash cause+evidence+suggested-fix card **automatically**. Safe to ship
  first: Tier-1 is read-only and *cannot* fake green.
- **Phase 1 — The flagship cross-field reasoner (~3–4 days).** Caps #3 → #2 → #4 (build **live-option
  capture FIRST** so the reasoner is honest from day one). Reproduces the one-second SME insight
  (Florida≠Canada) on a plain run with zero clicks — **only** when a genuinely-live option set exists,
  inert otherwise. The highest-legibility "it thinks like a human" demo.
- **Phase 2 — The triage brain + fix/build/flag verdict (~2–3 days).** Caps #5 + #6. One machine
  verdict per failure; add the ENVIRONMENT lane; harden `network_signal_from_error` against in-content
  "500" text and recorded expected-error steps.
- **Phase 3 — Forensics depth + Requirement oracle (later).** Cap #7 + deeper console/trace/HAR.

## Moat vs each competitor (one line)
- **vs Mabl:** proves resolves/resembles, bug-vs-test self-admittedly unsolved → human. We auto-author a defect on a contradicted outcome + reason across fields + refuse.
- **vs Testim/Tricentis:** validate-then-replace auto-applies a substitute (green-wash) + RCA only *poses* the question. We never apply what the oracle+2×-green didn't prove; we emit a machine verdict.
- **vs Functionize:** best RCA but heal-safety is *assertion-dependent* (weak-assertion flow still green-washes), no cross-field. Our safety is intrinsic (orthogonal oracle, not the customer's assertions).
- **vs testRigor:** intent re-interpretation *tuned to keep green*, no refuse-as-regression. We have an explicit REFUSE taxonomy that short-circuits before any heal.
- **vs Tosca:** Vision-AI green-washes by design (even mocks an unavailable dependency to stay green). Our `network_oracle` refuses + auto-authors the 503 repro.
- **vs Katalon:** closest on triage framing but classifies without a never-green-wash spine or cross-field reasoning.
- **vs QA Wolf:** auto-investigates to a **paid human**; we auto-investigate to a **grounded machine verdict** (and on-prem, not a managed cloud service).
- **vs Momentic/Reflect/Autify/Spur/Rainforest:** locator/vision-renavigation, green-wash-prone, triage asserted at blog altitude, no refusal contract.

## Biggest risks (honest — build these guards in, not after)
1. **GROUNDING SUBSTITUTION (highest).** The cross-field reasoner must be **INERT unless a genuinely-live option set is captured** — else the LLM "confidently asserts Florida isn't a Canadian province" as a fact it can't see. Recorded options must **not** satisfy the verbatim clamp. (This is why Cap #3 ships before #2.)
2. **Confidently-wrong auto-posted diagnosis** is *worse* than a stack trace (users act on it). Run **after** the full REFUSE chain + a render-race/auth recheck; require **two grounded facts** (sibling value present AND a live option set that genuinely excludes the recorded value); declarative voice **only** when confirmed against live options; stamp inference provenance.
3. **Cost/reliability fan-out** (50 scenarios → 50 LLM calls → 429). Hard per-run cap + dedupe as a *tested* invariant; Tier-1 deterministic stays the always-on default.
4. **Persisted-diagnosis staleness.** `schema_version` + `grounding_quality` on the row; stale/re-analyze affordance; loud metric on the fire-and-forget swallow.
5. **Triage misroute both ways.** Scope the 5xx signal to **actual network entries at app origin** (not rendered "500" text); keep 4xx advisory; require outcome-contradiction corroboration before filing a defect.
6. **Migration/deploy friction.** The `diagnosis` JSONB column is additive/nullable, fail-open if absent; deploy via **repo-rebuild, not docker-cp** (repo is behind the VM).

## Recommended first build
**Phase 0 (Cap #1)** — auto-run the deterministic diagnosis on every failure. It's read-only,
`$0`-LLM, can't green-wash, ~1–1.5 days, and immediately delivers the "no-click, it just tells me
what's wrong" experience on *every* run — the foundation the flagship cross-field reasoner plugs into.
