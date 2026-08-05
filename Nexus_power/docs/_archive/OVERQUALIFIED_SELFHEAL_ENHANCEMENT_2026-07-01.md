# Enhancement shipped: over-qualified repeated-control self-heal (P6)

**Date:** 2026-07-01 · **Branch/deploy:** VM repo (source of truth) rebuilt + live · **Gate:**
`NEXUS_OVERQUALIFIED_HEAL` (durable in `docker-compose.override.yml`, ON for this deployment;
default-OFF in code). Follows the founder directive *"proceed / get to 10/10 / never break current
code / pure generic"* and the never-green-wash doctrine.

This is the concrete follow-through on Enhancement #3 from the True-Healing report: teach the
self-heal engine to resolve an **over-qualified accessible name that names a REPEATED control**
(the classic saucedemo *"Add to cart - Sauce Labs Onesie"* in a 6-card grid), which the deterministic
Similo resolver correctly **refused** (6 identical "Add to cart" names → ambiguous → refuse).

---

## What it does (grounded, oracle-gated, never green-wash)

When the deterministic resolver refuses a re-anchor, a **gated** fallback runs:

1. **Decompose** the recorded label on a separator: `"Add to cart - Sauce Labs Onesie"` →
   control `"Add to cart"` + block anchor `"Sauce Labs Onesie"`. (Generic: `\s+[-–—:|»>/]\s+`; no app strings.)
2. **Ground against the LIVE capture** — fires only if BOTH hold, else REFUSE:
   - the control name (`"Add to cart"`) appears on **≥2** live nodes (genuinely ambiguous), and
   - the anchor text (`"Sauce Labs Onesie"`) is actually present in the captured a11y tree.
   The control's role is taken from the **live nodes** (not the recorded kind, which the heal loop
   often reports imprecisely — this was the key bug; see below).
3. **Emit an anchor-scoped locator** — the compiler's `_anchor_scope` gained a `"block"` strategy:
   `page.locator(<generic containers>).filter({ hasText: anchor }).filter({ has: getByRole(control) }).last().getByRole(control)`.
   `.last()` = the innermost matching ancestor (document order puts outer containers first) → the one card/row.
4. The step's own **outcome oracle still proves green** — a wrong scope fails RED and the loop refuses,
   so this can never green-wash.

**Files (all additive; byte-identical when the gate is off / no anchor present):**
- `services/diff_and_heal/action_resolver.py` — `ReAnchor.anchor/anchor_kind`, `_overqualified_anchor()`, wired after Similo refuses.
- `services/script_factory/compiler.py` — `_anchor_scope` `"block"` strategy, `_CONTROL_ROLE`, `_BLOCK_CONTAINERS`, anchor threaded through the re-anchor channel.
- `services/diff_and_heal/self_heal.py` — anchor threaded through `build_reanchor_candidate` + `resolve_reanchor_for_step`.
- `routers/test_factory.py` — anchor threaded at the **autopilot loop** apply point (the one that actually runs; the missing 4th thread that caused the first "half-worked" run).

---

## Proof it works (measured on the live deployed system, in-window session)

Step 3 (`Click 'Add to cart - Sauce Labs Onesie'`) locator progression across autopilot iterations,
from the real `test_run_step` records:

| Stage | Emitted locator | Result |
|---|---|---|
| As-generated | `getByRole('button', {name:'Add to cart - Sauce Labs Onesie'})` | ❌ 0 match (over-qualified) |
| Decomposed, **unscoped** (first cut, before the 4th thread) | `getByRole('button', {name:'Add to cart'})` | ❌ **strict-mode violation: 6 elements** |
| Decomposed **+ block-scoped** (final) | `…filter({hasText:'Sauce Labs Onesie'}).filter({has:…}).last().getByRole('button',{name:'Add to cart'})` | ✅ **1 match — CLICKS** |

**Heal-trace outcome after the fix:** `reanchor_capture → reanchor_applied → run → stop_needs_human`
with stop reason changed from `"step 3: Locator not found"` to:

> **"step 3: Real regression — Do NOT heal — file a defect … Confirm the expected outcome against the recorded baseline."**

i.e. the click **now resolves and fires**; step 3's remaining failure is a **different, correctly-diagnosed**
defect — the `toHaveURL(/cart.html/)` assertion fails because clicking Add-to-cart doesn't navigate
(the mis-attributed "impossible transition" from the recording). The system **honestly refuses to
green-wash it** and routes it to engineering. **Never green-wash held throughout.**

Net: the autopilot now autonomously heals **two** defect classes on this flow — native-`<select>`
legibility (step 2) and over-qualified repeated-control locators (step 3) — and honest-stops on the
third (the impossible-transition expected outcome, a generation/extraction defect that is its own fix).

---

## Regression safety

- Behavior test **14/14 PASS** (gate-off refuses; existing row/card/frame anchors byte-identical; scope emission correct; non-repeated control & absent-anchor both refuse). File: `tests/.../test_overqualified.py` (run against the deployed image via stdin).
- Import-smoke: all 3 modules + `similo` import cleanly in the rebuilt image; `_anchor_scope` byte-identical for no-anchor / existing anchor kinds.
- No startup errors; `/health` 200. Debug logging removed after diagnosis.
- Gate default-OFF in code → **any other deployment is byte-identical**; ON only where the compose override sets it.

---

## Remaining to fully green the saucedemo flow (each a separate, known fix)

1. **Impossible-transition oracle (step 3+):** the recording's `next_url: cart.html` was pinned onto a
   non-navigating click. Fix at extraction/test-case time (don't assert a navigation the recording
   doesn't show the action caused) — the test case already flags this step `review`.
2. **Native-`<select>` at generation time (step 2):** stop compiling a dropdown-value selection as
   `checkbox.check()`; emit `selectOption`. (Autopilot heals it at runtime today; fixing generation
   removes the need to heal.)
3. **Auth/session robustness:** verify the session is live at run start + generic login-page detection
   (the `login_signals → login_detected` path already exists; make it fire reliably).

The over-qualified self-heal (this doc) is **done, deployed, gated, tested, and proven**.
