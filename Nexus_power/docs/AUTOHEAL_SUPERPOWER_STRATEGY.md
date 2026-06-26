# Auto-Heal "Super Powerful" — Research-Backed Strategy

**Date:** 2026-06-24 · **Status:** strategy (research synthesized; build in priority order)
**Goal:** make Auto-Heal cover *every* UI-test failure family, GROUNDED (match the live page, never fabricate), and **never green-wash** (every heal proven by an orthogonal oracle + confirm; uncertain heals human-gated).

Research: deep multi-source pass (105 agents, 23 sources, 25 claims verified → 14 confirmed; final synthesis cut by a session limit so this doc is hand-synthesized). Claims tagged **[V]** = independently verified 3-0/2-0; **[R]** = research-sourced/extracted, not independently re-verified.

---

## 1. The headline (why this matters)
- **Locator drift is only a MINORITY of failures.** Practitioner data: selectors ≈28%, **timing ≈30%**, test-data ≈14%, visual-assertion ≈10%, missing-prerequisite-step ≈10%, runtime ≈8% [R, QA Wolf]. Academic: **Async-Wait timing = 45% of flaky UI tests** — the single biggest cause [V, arXiv 2103.02669]. → A "super powerful" heal must cover **timing, data/preconditions, and missing-steps**, not just locators.
- **Our "never green-wash" doctrine is exactly what the field says is REQUIRED — and what the market leaders LACK.** This is our moat, now externally validated (see §4).

## 2. Validated failure-mode taxonomy (map the engine to this)
| Family | Evidence | Our coverage today |
|---|---|---|
| **Timing / async** (network load, render, animation, debounce) | #1 cause, 45% [V] | wait-scope (opt-in) — needs to be default + condition-based |
| **Locator drift** (rename/move/attr/id/i18n — e.g. "full name"→"full legal name") | 58% of XPath locators broke release-to-release [R, ROBULA+] | ⚠️ re-anchor exists but **under-routed** (the step-22 gap) |
| **Ambiguous locator** | — | partial (anchor-scoping) |
| **Control-kind mismatch** (custom dropdown/slider/toggle/date/accordion) | — | ✅ strong (proven recipes) |
| **State / precondition** (auth, data, order, A/B, consent) | Concurrency 20% + Test-Order 12% [V] | partial |
| **Missing / extra / mis-recorded step** | ≈10% [R] | dup-guard (new); needs auto-repair |
| **Navigation** (SPA same-URL, wizard, redirect) | — | ⚠️ SPA-oracle green-wash gap (known) |
| **Value/oracle** (mask, autocomplete, dynamic) | — | ✅ token oracle |
| **Scope** (iframe, shadow, canvas) | — | 🟡 any-UI scaffold |
| **Visual/layout** | ≈10% [R] | 🟡 perceptual-diff scaffold |

## 3. How to actually heal each (techniques, ranked by proven effectiveness)
- **Timing → condition-based waits, NOT sleeps.** waitFor fully removes flakiness in 55% of async cases; sleeps only *decrease* it and never fully fix [V, researchgate 301428664]. Implement auto-wait on the right condition (visible/attached/network-idle), bounded.
- **Locator drift → weighted multi-attribute similarity vs the LIVE DOM (Similo).** Score each candidate over {tag, class, name, id, xpath, location, text} × reliability-weight; pick the highest [V, arXiv 2208.00677]. Similo halved localization failure (24%→12%) [V]. This is the **step-22 fix**: "E-signature — type your full name" vs live "…full **legal** name" is a ~0.9 token-similarity match.
- **Robustness (prevention) → ROBULA+-style robust locators** reduce fragility ~90% vs absolute XPath [R]. Our resilient `.or()` ladder already follows this; keep strengthening accessible-name-first.
- **Missing step → "interaction healing"**: when a control is hidden until a prerequisite, insert the grounded prerequisite step [R, QA Wolf].
- **Multi-clue fallback (Katalon/COLOR):** try alternative grounded locators by priority; escalate to similarity/AI only if all fail [R].

## 4. The non-negotiable: NEVER green-wash (our moat, research-validated)
The research shows the incumbents heal *unsafely*:
- **Similo will bind the WRONG element if there's no minimum-similarity threshold** — "always returns a matching element… even if the target is not present" [V]. → **Gate every similarity match with a minimum-confidence threshold + rerun-on-fail.**
- **Healenium** takes the highest-score locator and acts — **no confirmation, no oracle, feedback only post-hoc** [V]. Green-wash by design.
- **Playwright Healer** re-runs "until it passes, **or skips** if it thinks the feature is broken" — a skip is non-red yet verifies nothing [V]. Structural green-wash.
- **WATER (DOM-only)** missed 100% of "mis-selection" wrong-binds and "can lead to false positives" [R]; **VISTA** adds an **orthogonal visual oracle** that validates the DOM pick and, on disagreement, **reports to a human instead of silently rebinding** [R]. → orthogonal oracle + human-gate.
- **UITESTFIX** defines "correctly fixed" as **functional coverage AND a passing assertion** — not merely passing [R]. → our acceptance criterion.

**Our gate (keep + enforce on every heal):** min-similarity threshold → re-run → orthogonal oracle (the step's grounded outcome, NOT the thing we healed) → 2× confirm → **human-gate anything mid-confidence**. This is precisely what Healenium/Playwright-Healer don't do.

## 5. The engine: one unified DIAGNOSE → CLASSIFY → ROUTE → FIX → PROVE loop
1. **Classify** the failure into exactly one family (the current gap: a renamed *field* fell through to the control-kind fixer instead of re-anchor).
2. **Route** to the family's grounded fixer.
3. **Fix grounded against the LIVE page** (similarity for locators; condition for timing; prerequisite for missing-step).
4. **Prove**: threshold + re-run + orthogonal oracle + 2× confirm.
5. Else **escalate honestly** (never skip-to-green).

## 6. Roadmap (priority = proven impact × current gap)
1. **🔴 Live-page re-anchor for locator drift** (Similo-weighted similarity vs live DOM, min-threshold, human-gate mid-confidence). Fixes step 22 + the whole renamed-control class. *Grounded + human-gated per decision.*
2. **🟠 Condition-based auto-wait by default** (timing = #1 cause; waitFor not sleep).
3. **🟠 Recording-quality auto-linter** (mis-label, duplicate, out-of-order, missing-submit → propose grounded corrections; extends the dup-guard).
4. **🟠 SPA / same-URL navigation oracle** (content/state oracle when path doesn't change — closes the known green-wash gap).
5. **🟡 Ambiguity + scope hardening** (anchor-aware everywhere; iframe/shadow auto-detect).
6. **🟡 Visual + precondition layers** (perceptual-diff promote; auth/data/A-B detection).

**Honest ceiling:** even the best locator repair tops out ~88–98% (Similo/HybridSimilo), WATER 57%, VISTA 81% [V/R] — self-heal is not 100%; the residual MUST escalate to a human. "Super powerful" = broad coverage + grounded + honest, not magic.

## Sources
arXiv 2103.02669 (UI flaky taxonomy) · researchgate 301428664 (flaky fixes: waitFor>sleep) · arXiv 2208.00677 (Similo) · arXiv 2505.16424 (HybridSimilo) · healenium.io/docs · playwright.dev/docs/test-agents · VISTA (FSE'18) · WATER (ETSE'11) · ROBULA+ (JSME) · UITESTFIX (ASE'23) · QA Wolf (6 self-heal types) · Katalon docs.
