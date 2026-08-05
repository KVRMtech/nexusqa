# Auto-Heal — Coverage Confirmation (every failure family)

**Date:** 2026-06-24 · **Status:** all layers BUILT + DEPLOYED + GATED + UNIT-VERIFIED on `nexus-vm` (`nexus-platform-api`). NOT committed to git (deployed via `docker cp`, per the established flow).
**Companion:** strategy + research in [`AUTOHEAL_SUPERPOWER_STRATEGY.md`](./AUTOHEAL_SUPERPOWER_STRATEGY.md).

This confirms — against the research-validated failure taxonomy — that **every** UI-test failure family is either (a) healed by a grounded recipe proven by an orthogonal oracle + 2× confirm, or (b) escalated **honestly** with a precise, actionable reason. Nothing is ever skipped-to-green. The honest ceiling stands: even best-in-class locator repair tops out ~88–98%; the residual MUST reach a human, and here it does — by design.

## The one engine: DIAGNOSE → CLASSIFY → ROUTE → FIX → PROVE → (else) ESCALATE

`_run_auto_heal` re-runs the suite, then for the first failing step: `self_heal.diagnose()` classifies it into exactly one family; the loop routes to that family's grounded fixer; the fix is re-compiled into the owned spec and **re-proven** (the step's own grounded oracle + two independent green re-runs); anything uncertain or un-groundable **escalates to a human** with a precise reason. A genuine green is frozen as *Clean Run – V1* with a Part-11 evidence record.

## Coverage matrix

| # | Failure family (research) | Handler in the engine | Behaviour | Verified |
|---|---|---|---|---|
| L1 | **Locator drift** — rename / move / i18n (e.g. "full name" → "full **legal** name") | Live-page re-anchor (Similo-style weighted multi-attribute similarity vs the live a11y snapshot): `self_heal.resolve_reanchor_for_step` + loop `_reanchor_capture` → `__reanchors__` channel | **≥0.85 confidence → auto-apply + re-prove** (oracle gates); mid-confidence → human-gate (`stop_reanchor_confirm`); no match → escalate | matcher 0.98 on the step-22 rename; unrelated → None |
| L2 | **Timing / async** — load, render, animation, debounce (**#1 cause, ~45%**) | Condition-based wait/scope recipes (`wait_scope_resolver.build_wait_scope_for`), **default-ON** | `scroll_until_materialize` / `retry_unscoped_at_root` / `frame_by_url` / wait-budget — **`waitFor`, never a fixed sleep**; no grounded signal → None → escalate | unit: timing→recipe, no-signal→None, condition-based + no-sleep asserted |
| — | **Open `<iframe>` scope** | `frame_by_url` (resolve frame by URL pattern) — part of L2 | Pierce + prove the control in the matching frame; no frame → THROW (RED) | unit: frame signal → `frame_by_url` |
| — | **Portal / out-of-subtree** (modal/toast rendered at body) | `retry_unscoped_at_root` — part of L2 | Wait for the name at anchor-scope OR root (bounded), then act; never materialises → THROW | covered by L2 recipe set |
| L3 | **Recording quality** — double-capture / duplicate step | Heal-time `recording_quality.classify_recording_quality` (grounded in the recording's own steps) | Back-to-back same-label same-page re-capture of a **passed** step → escalate `stop_recording_quality` with a concrete fix; **never skips** | unit: dup fires; **wizard "Next" NOT false-flagged**; earlier-not-passed → None |
| — | **Missing submit** (form filled, never submitted) | `recording_quality.scenario_missing_submit` — advisory | Surfaced alongside an escalation; advisory only | unit: fires on fills-with-no-commit; clean when a commit pins a nav |
| L4 | **Navigation — SPA same-URL** (known green-wash gap) | `recording_quality.detect_same_url_nav_greenwash` at the **freeze gate** | A `toHaveURL` whose recorded path == the current path (URL never changes) **with no content oracle** → **refuse to freeze**, escalate `stop_same_url_greenwash` | unit: same-URL-no-content → flagged; real multi-URL → clean; same-URL **with** content oracle → clean |
| L5 | **Scope — closed shadow DOM** | `any_ui_resolver.detect_any_ui` → `open_shadow_shim` | **Opt-in** (`enable_closed_shadow_shim`, default OFF) → page-init open-mode preamble (test-env only, no oracle weakened) + re-prove; default → escalate `stop_closed_shadow` with the opt-in offer | unit: error → `open_shadow_shim`; loop wiring present |
| L5 | **Scope — canvas / WebGL / Flutter** (no DOM/AX) | `any_ui_resolver` → `visual_propose` | **REFUSE**: no DOM grounding → escalate `stop_non_dom_surface`; the VLM tier stays inert (no blind coordinate) unless a GPU node is provisioned | unit: canvas→`visual_propose`; **REFUSES with a throw, no `.click()`**; `vlm_enabled()=False` |
| L6 | **State / precondition — auth / session** | `diagnose` → `AUTH_PRECONDITION` (REFUSE) | Escalate with the grounded `recommended_action` ("restore the login session — fix the session, not the controls") | unit: 401/login → AUTH_PRECONDITION + guidance surfaced |
| L6 | **State / precondition — data / fixture** | `diagnose` → `DATA_PRECONDITION_UNMET` (REFUSE) | Escalate ("seed / restore the required data — fix the precondition, not the locator") | unit: "no records" → DATA_PRECONDITION_UNMET + guidance |
| L6 | **State — A/B / feature-flag variant** | `diagnose` → `VARIANT_SUSPECTED` (REFUSE, grounded marker only) | Escalate ("pin the variant / record both — healing would lock in one bucket") | grounded on observed flag / variant cookie / error phrasing |
| — | **Real regression** (recorded outcome contradicted) | `diagnose` → `REAL_REGRESSION` (REFUSE) | Escalate ("do NOT heal — file a defect"); never re-bound | precedes all heal branches in the elif chain |
| — | **Control-kind mismatch** (native→custom dropdown/slider/toggle/date/accordion) | `diagnose` interaction re-synthesis → compiler `interactions` channel (proven per-kind recipes) | Open + pick + committed-value oracle; re-prove | previously live-verified across 5 control kinds |
| — | **Value / oracle** (mask, autocomplete, dynamic) | Compiler runtime-token (`__nxTok`) tolerant value oracle | Strong field-value assertion that survives masking/normalisation | shipped (assertion-upgrade v3) |
| — | **Visual / layout** (pixel/color/spacing) | **Out of the functional heal loop by design** | Our compiler emits **functional** oracles, not pixel snapshots — a pure visual diff is not a functional failure, so there is nothing to mis-route; auto-healing a visual diff would be exactly the green-wash we forbid. Perceptual-diff remains a separate **advisory** scaffold (flywheel), never auto-healed | honest scope statement (no fabricated route) |

## The never-green-wash gate (enforced on every heal, every freeze)
1. **Min-confidence threshold** on any similarity match (Similo binds the wrong element without one).
2. **Re-run** after the fix — and a **second** independent green confirm.
3. **Orthogonal oracle** — the step's *recorded grounded outcome* (value token / outcome region / navigation), NOT the thing we healed.
4. **Hollow-suite refusal** — a suite where no executed step asserts a recorded outcome is refused, never frozen.
5. **SPA same-URL refusal (L4)** — a navigation asserted by a URL that never changes, with no content oracle, is refused.
6. **Human-gate** anything mid-confidence; **REFUSE** anything un-groundable (canvas, auth, data, variant, real regression).
7. **Crash-guard** — a mid-flow skip that yields "all-green=False with zero failing steps" escalates honestly instead of crashing (`stop_no_failures_no_green`).

This is exactly what the market leaders lack: **Healenium** acts on the top-score locator with no oracle/confirm; **Playwright Healer** re-runs "until pass, or **skips**" (a skip verifies nothing); **Similo** binds a wrong element absent a threshold. Our refuse-and-escalate paths are the moat.

## What is NOT claimed
- Not 100% auto-fix. The residual (canvas without an oracle, broken auth/data preconditions, real regressions, mid-confidence renames, A/B flips) is **escalated to a human** — that is the correct, honest outcome, not a gap.
- The closed-shadow shim and the visual/VLM tier are **opt-in / infra-gated** and inert by default.
- Deployed to the VM; **not committed to git**.

## Deployed verification (2026-06-24)
- Router AST valid + clean import; all coverage modules import in-container; `vlm_enabled()` default-off.
- Every layer's wiring marker present in the deployed `routers/test_factory.py` (L1–L6 + crash-guard).
- Per-layer unit tests PASS (timing recipe + no-sleep; duplicate vs wizard-Next; SPA same-URL three cases; any-UI detect + REFUSE-throw; auth/data diagnose + guidance).
- `nexus-platform-api`, `nexus-gateway`, `nexus-runner` healthy.

**Next:** user end-to-end retest from the UI (Auto-Heal on the live artifact with the captured login session).
