# Auto-Heal: Why We Fail One-by-One, and the SOTA-Grounded Architecture That Ends It

**Date:** 2026-06-28 · **Status:** strategy + roadmap (P0/P1 shipped; P2–P7 sequenced)
**Provenance:** 7-agent SOTA research workflow (academic papers + industry tools + locator
strategies + failure taxonomy + scaling) cross-grounded against our deployed heal code.

---

## TL;DR

We are **not** failing for lack of research effort or because the team is slow. We are failing
because of **one architectural decision**: our locator pins a *single* signal (the recorded
accessible **name/caption**) and dresses it up as five fallbacks (`getByLabel`/`getByRole(name)`/
`getByText(name)`…). The moment that one signal moves, **all rungs break at once**, and every new
failure mode becomes a new bespoke "tier." That is the one-by-one treadmill.

The state of the art (Similo, VON-Similo, VON-Similo-LLM, ROBULA+, VISTA) solves exactly our
situation — **no recorded DOM** — by resolving intent against the **live page over many weighted
signals every run**, gated by an orthogonal oracle. Adopting that collapses ~5 of our failure
classes into **one** scored, oracle-gated pipeline. Our never-green-wash oracle (already built and
ahead of the market) is what lets us heal aggressively without ever shipping a hidden bug.

---

## 1. Why we're failing one-by-one (root cause)

**(a) Single-strategy locator.** `compiler._ladder` builds a `.or()` chain where *every rung
targets the same `observed.label`*. It is **one signal wearing five hats** — zero
name-independent resilience. Similo (TOSEM 2023) halved failures precisely by *not* trusting one
property: it scores candidates over ~14 properties with weights that put near-zero weight on
id/class and high weight on visible-text / accessible-name / role / location.

**(b) Reactive rename recovery instead of resilience in the locator.** Because the locator is
brittle, we bolt on a separate re-anchor tier that only fires *after* a failure, *only if* an AX
capture happened to run, *only if* a name+state-Jaccard clears 0.62. Testim/mabl/testRigor have no
"rename tier" — they never pin the brittle thing in the first place.

**(c) We diagnose more classes than we can repair.** `diagnose()` classifies REAL_REGRESSION /
AUTH / DATA / VARIANT / FLAKE / control-kind / drift well, but **repair exists for only ~2.5
classes**. FLAKE, iframe, canvas, env, i18n → dead-end `NEEDS_REVIEW` = "wait for an engineer."

**(d) Video-only weakness, uncompensated.** Record-time gives label/kind/value/anchor/url — no
DOM, no bbox. That is *fine* (VISTA, Tosca Vision AI, testRigor all work with no recorded DOM) —
**but they compensate at resolve time by reading many live signals.** We read the live page only
once, post-failure, as a flat name+state snapshot. We have the hardest part of the problem without
adopting the technique that exists to solve it.

**One sentence:** *we pin a single brittle signal, so every failure class that isn't "same name
moved slightly" needs a new bespoke tier — and we're discovering those classes one customer at a
time instead of covering them by architecture.*

---

## 2. Gap map — 14-class taxonomy × SOTA × us

| # | Failure class | SOTA technique | What we do today | Verdict |
|---|---|---|---|---|
| 1 | Locator drift / rename / restructure | Similo/VON weighted multi-property (HybridSimilo ~98.8%) | single name-keyed `.or()` + reactive re-anchor | **PARTIAL** |
| 2 | Attribute / id volatility | never pin generated attrs | we never emit id/class | **COVERED** |
| 3 | Caption ≠ accessible-name | reconcile OCR caption → live accname run 1 | bake `observed.label` into locator | **MISSING** |
| 4 | Dynamic / async timing (≈45% of flake) | auto-wait + wait-synthesis (WEFix) | gen-time auto-wait only; no diagnosis→wait | **PARTIAL** |
| 5 | Control-kind mismatch | re-derive recipe from live role | `INTERACTION_RECIPES` + per-kind oracle | **COVERED** (strong) |
| 6 | Value / format / data-dependence | tolerant/pattern assert + source-of-truth | token-tolerant value oracle | **PARTIAL** |
| 7 | A/B & feature-flag variants | pin flag via API | detect+refuse (VARIANT_SUSPECTED) | **PARTIAL** |
| 8 | i18n / locale | bind to role/key not caption | caption baked in | **MISSING** |
| 9 | Shadow DOM (open) | Playwright pierces by default | role/text locators pierce | **COVERED** |
| 9c | Shadow DOM (closed) | detect-and-refuse / app opt-in | opt-in only; else looks "not found" | **PARTIAL** |
| 10 | Iframes | discover frame → FrameLocator | no frame discovery → "not found" | **MISSING** |
| 11 | Canvas / WebGL / non-DOM | VISTA visual template → snap pixel→DOM | honest REFUSE (no VLM) | **MISSING (honest)** |
| 12 | Auth / precondition / state | storageState login; seed/reset | detect+refuse (AUTH_PRECONDITION) | **PARTIAL** |
| 13 | Real product regression | orthogonal oracle + auto-repro | short-circuit + defect_report + 2× confirm | **COVERED** (ahead of market) |
| 14 | Environment / bot-block / flake | classify env; pin browser; CAPTCHA test-keys | lands in NEEDS_REVIEW/FLAKE | **MISSING** |

**Classes #1, #3, #8, #10, #11 would all collapse into one** if the locator were a live
multi-signal resolver instead of name-only synthesis.

---

## 3. The comprehensive architecture — cover all classes at once

**Principle:** stop healing brittleness; **resolve intent against the live page every run, over
many signals, gated by an orthogonal oracle.**

### Pillar A — heal-time MULTI-SIGNAL LOCATOR (the core change)
A Similo-style live candidate ranker that runs on *every* step (the correct model for a no-DOM
record anyway):
1. Snapshot live candidates from the **accessibility tree** + a `page.locator('*')` enumeration.
2. **VON visual grouping** — merge live nodes whose bounding rects overlap the recorded target
   region into one "visual element"; **on a visual-overlap tie, REFUSE, never random-pick.**
3. Score each candidate `Σ similarity(property) × weight` with optimized weights (visible-text,
   accessible-name/aria, role/type high; id/class ≈ 0; location/area/shape medium). This subsumes
   today's name-Jaccard as *one term among many*.
4. Take **top-K**.
5. **LLM tie-break only on ambiguity** (VON-Similo-LLM): serialize top-10 → one bounded LLM call.
   This is our existing `agentic_heal.propose`, repurposed as a *ranker over a shortlist*, not a
   whole-page reasoner — ≤1 call per ambiguous step.
6. **Emit a robust, customer-owned locator** (ROBULA+/Playwright ladder) and write it back so we
   don't re-rank every run.

→ rename (#1), caption≠accname (#3), i18n (#8), and no-stable-name controls **become one solved
class.**

### Pillar B — complete diagnosis → repair mapping
Keep our strong `diagnose()`; close the repair gaps so **every class routes to a repair OR an
honest refuse — never a dead-end NEEDS_REVIEW**: FLAKE → wait-synthesis; iframe → FrameLocator;
closed-shadow → precise refuse; canvas → VISTA visual match else refuse; AUTH/DATA/VARIANT/ENV →
actionable remediation precondition.

### Pillar C — never-green-wash oracle (already strong; make it universal)
Every Pillar-A candidate must pass **(i)** a similarity floor, **(ii)** the stored **intent
oracle** ("does the new element still serve the step's purpose"), **(iii)** an **orthogonal oracle**
(network/value/visual). Green only when all three agree; **ambiguity or disagreement = REFUSE +
escalate**, never silent-pick. This is our moat — the part rivals green-wash through.

**A new failure mode is then COVERED, not patched.** Example — a date field becomes a custom
combobox *and* its label is translated to Spanish (classes #5 + #8 at once): Pillar A still scores
it high on role + location + neighbor-text + option-content; the recipe re-derives the interaction
from the live role; the oracle proves the value committed; the proven locator is written back.
Zero engineer involvement.

---

## 4. Scale to 100+ clients / 10,000+ tests

- **Heal ledger** — promote `heal_capture_store` to a persistent, RLS-isolated store keyed by
  (tenant, step, control-fingerprint): old/new binding, score, intent-satisfied, oracle verdict,
  screenshot, deploy SHA (Healenium's Postgres model).
- **Consented failure→fix DATA FLYWHEEL (the moat)** — raw DOM/screenshots **never leave the
  tenant**; only de-identified, k-anonymized heal *outcomes* tune the shared Similo weights. A heal
  proven once auto-resolves the same drift everywhere at **$0**. This only exists if Pillar A
  produces a property-vector to learn over (today's name-only locator has nothing to train).
- **Confidence gating + human triage** — auto-apply only when score ≥ threshold AND intent-satisfied
  AND oracle-pass; else a review queue. Release-gating pipelines forbid auto-heal; heal-frequency
  caps prevent a heal-storm on a bad deploy; RBAC/SSO/immutable audit wrap every heal.
- **False-heal prevention = #1 invariant** — SLO **wrong-element rate < 1%**, enforced by the
  three-oracle gate + VON-tie-refuse.
- **Observability / heal-rate SLOs** — a spike of low-confidence heals in one flow right after a
  deploy is a **likely regression to escalate, not absorb** (drift monitoring as a product signal).
- **On-prem + LLM cost control** — deterministic Similo resolves ~95% at **$0**; text-LLM only for
  ties (pre-filtered to top-10); vision-LLM/VISTA only for no-DOM; flywheel reuse so the same drift
  never pays for the LLM twice.

---

## 5. Roadmap — each step retires a CLASS (not a bug)

- **P0 — Honest diagnosis + orthogonal-oracle gate. ✅ DONE.** (`diagnose`,
  `assert_assertions_unchanged`, committed-value oracle, hollow-refusal, 2× confirm, +
  error-message classification: locator-not-found vs env-block vs real-regression.)
- **P1 — Content/role-anchored rungs + interaction recipes. ✅ DONE.** (content-anchored `<select>`
  rung; first non-name signal — generalize it.)
- **P2 — Auto-capture the live page on every run + reconcile caption→accname.** Effort M, risk Low.
  *Keystone substrate.* Retires #3 and #8 at the source.
- **P3 — Live multi-signal Similo ranker (Pillar A core).** Effort L, risk Med (hold the <1%
  wrong-element budget via the three-oracle gate + VON-tie-refuse). *Highest leverage:* collapses
  #1/#3/#8 and produces the property-vector the flywheel needs.
- **P4 — Diagnosis→repair gap-closers (parallel).** FLAKE→wait-synthesis (#4); iframe→FrameLocator
  (#10); env classifier + pinned browser (#14); AUTH/DATA/VARIANT remediation (#7,#12).
- **P5 — VISTA visual-template tier for no-DOM** (canvas/WebGL/icon-only, #11) — heal where an
  oracle exists, else honest refuse.
- **P6 — Heal ledger + confidence-gated triage + heal-rate SLO dashboard.** Operationalizes 10k
  scale + the <1% false-heal SLO.
- **P7 — Consented failure→fix flywheel (the moat).** De-identified outcomes tune the weights
  centrally; raw data stays on-prem.

---

## Key citations (techniques, not marketing)

- **Similo** — weighted multi-property element localization, ~14 properties, ~84% top-1 →
  extended-benchmark ~96–99% on broken-locator subset. *Nass, Alegroth, Feldt — ACM TOSEM 2023 /
  arXiv:2208.00677; property table + optimized weights arXiv:2505.16424.*
- **VON-Similo** — visual-overlap node grouping; 94.7% vs 83.8%; HybridSimilo ~99%. *arXiv:2301.03863.*
- **VON-Similo-LLM** — deterministic shortlist (top-10) + one GPT-4 tie-break; 91.3% → 95.0% with
  bounded LLM cost. *Nass et al., STVR 2024 / arXiv:2310.02046.* (The canonical
  cheap-deterministic-shortlist + LLM-disambiguate pattern — maps onto our agentic tier.)
- **ROBULA+** — robust XPath by greedy refinement + attribute black-list; ~90% fragility reduction.
  *Leotta, Stocco, Ricca — JSEP 2016.*
- **VISTA** — visual template match → snap pixel→DOM for no-DOM controls. *FSE'18.*
- **WEFix / WEFix-style** — explicit actionability-wait synthesis for async flake. *arXiv:2402.09745.*
- **Healenium** — Postgres locator history + ML scoring (history weighted into confidence) — the
  reference for the heal ledger + flywheel.
- **Industry**: Testim "smart locator" (weighted attributes), mabl, Functionize, testRigor
  (re-resolve intent every run; no heal event when a stable signal holds), Tricentis Vision AI.

> The throughline: **we pinned one brittle signal and patched around our own brittleness. P2+P3
> replace it with a live multi-signal resolver — the exact technique the literature proves solves
> the no-recorded-DOM case — so five failure classes collapse into one scored, oracle-gated
> pipeline. Our never-green-wash oracle is what lets us do this aggressively without shipping a
> hidden bug. That is the defensible product.**
