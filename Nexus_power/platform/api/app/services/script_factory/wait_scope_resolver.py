"""Timing / materialize / portal / frame WAIT+SCOPE recipes for Auto-Heal (ADDITIVE).

A NEW heal channel that sits BESIDE the `interactions` channel (control-KIND
re-synthesis) and the `reanchors` channel (renamed-control re-binding). Where
those two change WHAT a step does, this channel changes only the timing / scope
*around* a step whose action and oracles are otherwise correct — the four
"the control is there, the test just couldn't reach it yet" traps:

  * SCROLL-CONTAINER-UNTIL-MATERIALIZE — a virtualized / windowed list only
    renders the rows near the viewport, so a real, present option throws a
    false "not found". We scroll the scroll-container in bounded steps until the
    recorded name materializes, then let the normal action run. If it NEVER
    materializes we THROW (fail RED) — a genuinely-absent control is never
    silently skipped.
  * RETRY-UN-SCOPED-AT-ROOT — a portal / modal / toast is React-rendered at
    document root, OUTSIDE the control's anchor subtree, so an anchor-scoped
    locator misses it. We emit a bounded preamble that waits for the recorded
    name to be attached EITHER in the anchor scope OR at page root, so the
    subsequent action can resolve it. (The action's own locator + oracle are
    unchanged; this only removes the timing/scope race.)
  * FRAMELOCATOR(url-pattern) — an <iframe> whose index is unstable but whose
    src URL is stable; we resolve the frame by a URL substring/regex and prove
    the recorded control is present inside it before the action runs.
  * RESILIENT WAIT BUDGET + PERF-REGRESSION FLAG — a baseline-relative wait
    budget (so a slow-but-correct app does not false-RED on the default 15s
    expect timeout) PLUS, when a per-step latency baseline is supplied, a
    flag-ONLY perf-regression note. We NEVER auto-heal a perf regression: a step
    that got slower than baseline is a real signal a human must see, so the
    recipe only writes a `console.warn` + a greppable comment, never relaxes the
    oracle to hide it.

DOCTRINE (identical to the rest of TrueFix):
  * DEFAULT-OFF: the channel is keyed by step_number; a step with no recipe gets
    NOTHING emitted, so existing specs are byte-identical. `build_wait_scope_for`
    returns None unless a grounded signal is present.
  * GROUNDED, never a blind guess: every recipe is produced from a real,
    classified failure signal (a false-not-found on a known-present option, an
    element rendered outside its subtree, an iframe-hosted control, or a
    measured latency baseline) — not from a hunch.
  * NEVER GREEN-WASH: every preamble is a WAIT/SCOPE/ASSERT that can only make a
    real defect fail RED faster or surface a flag. A materialize-scroll that
    never finds the row THROWS. A frame that never loads the control THROWS. The
    perf path only flags. None of them weakens or removes the step's own
    committed-value / outcome oracle (those still run AFTER the preamble).
  * BOUNDED: every locator / wait / loop carries an explicit timeout (~6s) and a
    hard iteration cap, so a mis-routed recipe fails FAST + RED, never hanging to
    the 60s test timeout.

The recipe library lives HERE, outside the frozen compiler; the compiler only
adds a sibling `waits` channel that emits this preamble BEFORE the verb branch.
"""
from __future__ import annotations

import re

# Recipe kinds this channel understands. A persisted override naming an unknown
# kind is ignored (emits nothing) so a forward-incompatible job is byte-identical.
_KINDS = {
    "scroll_until_materialize",
    "retry_unscoped_at_root",
    "frame_by_url",
    "wait_budget",
}


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


# ── grounded signal classifiers (recorded fields + the live error only) ────────

# A "the option is there, we just couldn't see it" error: Playwright could not
# find an element by an accessible name that the recording PROVED exists. This is
# the virtualized-list / windowed-render false-not-found trap.
_FALSE_NOT_FOUND_RE = re.compile(
    r"waiting for (?:locator|getby)|element is not visible|"
    r"did not find some options|0 elements|resolved to 0 elements|"
    r"strict mode violation.*resolved to 0",
    re.I,
)

# An element rendered OUTSIDE its expected subtree — the portal/modal/toast trap.
_PORTAL_RE = re.compile(
    r"outside (?:the )?(?:subtree|scope)|portal|rendered at (?:body|root)|"
    r"detached from|not under",
    re.I,
)

# A control hosted in an <iframe> (the error or the recorded context names a frame).
_FRAME_RE = re.compile(r"\bframe\b|\biframe\b|frameLocator", re.I)


def build_wait_scope_for(observed: dict, error_message: str = "",
                         baseline_ms: float | None = None,
                         frame_url: str = "") -> dict | None:
    """Return a grounded WAIT/SCOPE recipe for this step, or None (default-off).

    Fires ONLY on a grounded signal:
      * ``frame_url`` recorded for this step (or the error names a frame)  -> frame_by_url
      * the live error reads like a portal/out-of-subtree render           -> retry_unscoped_at_root
      * the live error reads like a false-not-found on a present control   -> scroll_until_materialize
      * a ``baseline_ms`` latency baseline is supplied                     -> wait_budget (+ perf flag)
    Absent every signal -> None -> the compiler emits nothing -> byte-identical.

    The recipe is a thin descriptor; ``emit_wait_scope_lines`` renders the JS. We
    deliberately key off the SAME recorded ``label`` the action uses, so the
    preamble can never wait on / scope to a DIFFERENT control than the step acts on.
    """
    err = error_message or ""
    label = observed.get("label") or observed.get("value") or ""
    if not label:
        # No accessible name to wait on/scope to -> we cannot ground a recipe.
        # (A perf baseline alone still grounds a wait_budget, handled below.)
        if baseline_ms and baseline_ms > 0:
            return {"kind": "wait_budget", "baseline_ms": float(baseline_ms), "label": ""}
        return None

    if frame_url or _FRAME_RE.search(err):
        return {"kind": "frame_by_url", "label": label, "frame_url": (frame_url or "").strip()}
    if _PORTAL_RE.search(err):
        return {"kind": "retry_unscoped_at_root", "label": label}
    if _FALSE_NOT_FOUND_RE.search(err):
        return {"kind": "scroll_until_materialize", "label": label}
    if baseline_ms and baseline_ms > 0:
        return {"kind": "wait_budget", "baseline_ms": float(baseline_ms), "label": label}
    return None


# ── emitter ────────────────────────────────────────────────────────────────────


def emit_wait_scope_lines(observed: dict, recipe: dict) -> list[str]:
    """Render a WAIT/SCOPE PREAMBLE (a list of JS lines) for one recipe.

    The preamble runs BEFORE the step's normal action lines; it only waits /
    scopes / flags. It NEVER emits the action or weakens the oracle, so the
    step's own committed-value / outcome assertion still gates green afterward.

    Returns ``[]`` for an unknown / empty recipe so a malformed override is
    byte-identical (the step compiles exactly as if no recipe were present)."""
    from .locators import js_str  # lazy: avoid import cycle / keep this file leaf-ish

    kind = (recipe or {}).get("kind", "")
    if kind not in _KINDS:
        return []
    label = (recipe or {}).get("label", "") or observed.get("label", "") or ""
    lab = js_str(label)
    out: list[str] = []

    if kind == "scroll_until_materialize":
        # Virtualized list: scroll the nearest scroll-container in bounded steps
        # until the recorded name renders, then let the normal action run. NEVER
        # green-wash: if it never materializes within the cap we THROW (RED).
        out.append("// WAIT/SCOPE heal (scroll-until-materialize): a virtualized/windowed list")
        out.append("// only renders rows near the viewport. Scroll the scroll-container until")
        out.append("// the recorded control materializes, then run the normal action. If it")
        out.append("// never appears we THROW (RED) — a genuinely-absent control is never skipped.")
        out.append(f"const _wsTarget = page.getByRole('option', {{ name: '{lab}', exact: false }})"
                   f".or(page.getByText('{lab}', {{ exact: false }}))"
                   f".or(page.getByRole('row', {{ name: '{lab}', exact: false }})).first();")
        out.append("{")
        out.append("  let _wsSeen = await _wsTarget.isVisible({ timeout: 1200 }).catch(() => false);")
        out.append("  if (!_wsSeen) {")
        out.append("    // find a scrollable ancestor of the first visible row, else the document.")
        out.append("    const _wsScroller = page.locator("
                   "\"xpath=(//*[self::ul or self::ol or @role='listbox' or @role='grid' or @role='list'])[1]\""
                   ").first();")
        out.append("    const _hasScroller = await _wsScroller.count().catch(() => 0);")
        out.append("    for (let _i = 0; _i < 40 && !_wsSeen; _i++) {")
        out.append("      if (_hasScroller) {")
        out.append("        await _wsScroller.evaluate((el) => { el.scrollTop = el.scrollTop + Math.max(200, el.clientHeight * 0.8); })"
                   ".catch(() => {});")
        out.append("      } else {")
        out.append("        await page.mouse.wheel(0, 600).catch(() => {});")
        out.append("      }")
        out.append("      _wsSeen = await _wsTarget.isVisible({ timeout: 400 }).catch(() => false);")
        out.append("    }")
        out.append("  }")
        out.append("  if (!_wsSeen) { throw new Error("
                   f"'scroll-until-materialize: \\'{lab}\\' never rendered after scrolling the "
                   "virtualized container — refusing to skip a possibly-absent control "
                   "(RED, not green-wash)'); }")
        out.append("  await _wsTarget.scrollIntoViewIfNeeded({ timeout: 6000 }).catch(() => {});")
        out.append("}")
        return out

    if kind == "retry_unscoped_at_root":
        # Portal/modal/toast rendered at document root, outside the anchor subtree.
        # Prove the recorded name is attached EITHER in scope OR at page root before
        # the action runs. Pure wait — the action's own locator/oracle are unchanged.
        out.append("// WAIT/SCOPE heal (retry-un-scoped-at-root): a portal/modal/toast is")
        out.append("// rendered at document root, outside the control's anchor subtree. Wait for")
        out.append("// the recorded name to attach EITHER in scope OR at page root (bounded).")
        out.append(f"await page.getByText('{lab}', {{ exact: false }})"
                   f".or(page.getByRole('dialog').getByText('{lab}', {{ exact: false }}))"
                   f".or(page.getByRole('button', {{ name: '{lab}' }}))"
                   ".first().waitFor({ state: 'attached', timeout: 6000 }); "
                   "// portal-aware wait; never a hang")
        return out

    if kind == "frame_by_url":
        # Control hosted in an <iframe> resolved by URL pattern (index is unstable).
        # Prove the recorded control is present inside the matching frame; if no
        # frame matches / the control never appears, THROW (RED).
        frame_url = (recipe or {}).get("frame_url", "") or ""
        fu = js_str(frame_url)
        out.append("// WAIT/SCOPE heal (frameLocator by URL): the control is inside an <iframe>")
        out.append("// whose index is unstable; resolve the frame by its src URL pattern and")
        out.append("// prove the recorded control is present before the action runs.")
        if frame_url:
            out.append(f"const _wsFrame = page.frameLocator(\"iframe[src*='{fu}']\");")
        else:
            # No recorded URL — fall back to the single iframe; still bounded + RED.
            out.append("const _wsFrame = page.frameLocator('iframe');")
        out.append(f"await _wsFrame.getByText('{lab}', {{ exact: false }})"
                   f".or(_wsFrame.getByRole('textbox', {{ name: '{lab}' }}))"
                   f".or(_wsFrame.getByLabel('{lab}'))"
                   ".first().waitFor({ state: 'attached', timeout: 6000 }); "
                   "// frame control present, else RED")
        return out

    if kind == "wait_budget":
        # Baseline-relative resilient wait + perf-regression FLAG (flag only — we
        # NEVER auto-heal a perf regression; a slower-than-baseline step is a real
        # signal a human must see, so we widen the WAIT but keep the oracle intact).
        baseline_ms = float((recipe or {}).get("baseline_ms", 0) or 0)
        # Budget = generous multiple of baseline, hard-capped so it can never hang.
        budget = int(min(max(baseline_ms * 3.0, 6000.0), 30000.0))
        out.append("// WAIT/SCOPE heal (baseline-relative wait + perf-regression FLAG): a")
        out.append("// slow-but-correct app should not false-RED on the default timeout, so we")
        out.append("// widen the WAIT budget. We do NOT auto-heal a perf regression — if the")
        out.append("// step exceeds its baseline we FLAG it (console.warn) for a human; the")
        out.append("// step's own oracle is unchanged, so a real defect still fails RED.")
        out.append(f"const _wsBaselineMs = {int(baseline_ms)};")
        out.append(f"const _wsBudgetMs = {budget}; // 3x baseline, capped [6s,30s]")
        out.append("const _wsT0 = Date.now();")
        if label:
            out.append(f"await page.getByText('{lab}', {{ exact: false }})"
                       f".or(page.getByRole('button', {{ name: '{lab}' }}))"
                       f".or(page.getByLabel('{lab}'))"
                       ".first().waitFor({ state: 'attached', timeout: _wsBudgetMs })"
                       ".catch(() => {}); // resilient wait; the step's own oracle still gates green")
        out.append("{")
        out.append("  const _wsElapsed = Date.now() - _wsT0;")
        out.append("  if (_wsBaselineMs > 0 && _wsElapsed > _wsBaselineMs * 1.5) {")
        out.append("    console.warn('[nexus-perf] PERF-REGRESSION FLAG: step took ' + _wsElapsed +")
        out.append("      'ms vs baseline ' + _wsBaselineMs + 'ms (>1.5x) — NOT auto-healed; review.');")
        out.append("  }")
        out.append("}")
        return out

    return []


__all__ = ["build_wait_scope_for", "emit_wait_scope_lines"]
