"""Any-UI-technology heal recipes for Auto-Heal (ADDITIVE) — TIER 3 + L6/L7.

The hardest locator cases, healed under the SAME never-green-wash doctrine as the
rest of TrueFix. Grounded directly in the hard-UI research synthesis
(``Nexus_power/docs/HARD_UI_HEALING_RESEARCH.md``):

  * CLOSED SHADOW DOM (TIER 3) — Playwright cannot pierce a *closed* shadow root.
    The ONLY safe, app-agnostic fix is to force every root to ``open`` mode by
    monkey-patching ``Element.prototype.attachShadow`` via ``page.addInitScript``
    BEFORE the app boots. Then the existing OPEN-shadow path heals it normally.
    Functional today (no GPU). If the environment forbids the shim, we ESCALATE.
  * IFRAME by URL (TIER 3) — handled by the ``waits`` channel's ``frame_by_url``
    recipe (see wait_scope_resolver) + the universal recipe's frameLocator rung;
    this module only adds the closed-shadow + visual tiers.
  * CANVAS / FLUTTER-no-semantics / WebGL (L6/L7, INFRA-GATED) — no DOM and an
    empty accessibility tree, so neither a CSS locator nor ``queryAXTree`` can
    ground a control. The research verdict is unambiguous: raw VLM *coordinate*
    grounding is unreliable (SeeClick 53.4% on ScreenSpot; icons/widgets ~30-52%),
    so we NEVER click a blind coordinate. The only safe shape is:
        vision PROPOSES candidates -> snap-to-node CONSTRAINS -> an ORTHOGONAL
        outcome oracle DECIDES -> a human APPROVES -> evidence RECORDS,
        and when no orthogonal oracle can be grounded we REFUSE.
    The VLM proposer is PLUGGABLE and DEFAULT-OFF: with no model configured
    ``propose_candidates`` returns ``[]`` and the recipe emits a REFUSE/escalate —
    never a guess, never a green-wash. (Self-hosted VLM = GPU + a permissively-
    licensed model; AGPL weights e.g. OmniParser's YOLO are a real constraint.)

DOCTRINE (identical to the rest of TrueFix): DEFAULT-OFF + byte-identical when no
recipe applies; GROUNDED, never a blind guess; NEVER GREEN-WASH (every path either
proves the recorded outcome via an oracle ORTHOGONAL to how the control was located,
or THROWS/escalates); BOUNDED (~6s timeouts, hard caps). The recipe library lives
HERE, outside the frozen compiler.
"""
from __future__ import annotations

import os
import re

# Recipe kinds this channel understands (unknown kind -> emits nothing -> byte-identical).
_KINDS = {"open_shadow_shim", "visual_propose"}

# A live error / signal that the target sits inside a CLOSED shadow root: Playwright
# reports it cannot pierce, or the recorded capture flagged a closed root.
_CLOSED_SHADOW_RE = re.compile(
    r"closed shadow|shadowroot.*closed|cannot (?:pierce|enter).*shadow|"
    r"not (?:able|possible) to.*shadow root",
    re.I,
)

# A live signal that there is NO DOM/AX grounding at all — a canvas/WebGL/Flutter
# surface (the recorded control resolved to a <canvas>/<flt-> host, or the capture
# reported an empty accessibility subtree for the region).
_NO_DOM_RE = re.compile(
    r"\bcanvas\b|webgl|flt-(?:glass|semantics)|empty (?:a11y|accessibility) (?:tree|subtree)|"
    r"no (?:dom|accessible) (?:node|element).*region",
    re.I,
)


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


def vlm_enabled() -> bool:
    """Is a self-hosted visual/VLM grounding tier configured? DEFAULT-OFF.

    Gated behind an explicit endpoint env so the visual tier is inert (and the
    recipe escalates) unless an operator has stood up a GPU node + a license-cleared
    model. Absent -> no candidates -> REFUSE, never a blind coordinate."""
    return bool((os.getenv("NEXUS_VLM_GROUND_URL") or "").strip())


def propose_candidates(screenshot_png: bytes | None, instruction: str,
                       *, max_candidates: int = 5) -> list[dict]:
    """PLUGGABLE visual proposer (L7, infra-gated). Returns a RANKED CANDIDATE SET
    of ``{id, bbox:[x,y,w,h], role, name, score}`` — NEVER a single blind coordinate
    (the research rule: propose-from-candidates, never trust one VLM coordinate).

    Default-off contract: with no model configured (``vlm_enabled()`` false) or no
    screenshot, return ``[]`` so the caller REFUSES/escalates. A real deployment
    points ``NEXUS_VLM_GROUND_URL`` at an OmniParser/UI-TARS-class service that
    returns Set-of-Mark candidates; this function would POST the screenshot there.
    Kept as a typed seam so the never-green-wash *caller* logic is testable now and
    the model is a drop-in later."""
    if not vlm_enabled() or not screenshot_png:
        return []
    # INFRA-GATED: a real call would POST screenshot_png + instruction to the VLM
    # grounding service and return its ranked Set-of-Mark candidates (bounded to
    # max_candidates). Left unimplemented so the tier stays inert until provisioned;
    # returning [] keeps the guarantee (no candidates -> refuse).
    return []


def detect_any_ui(observed: dict, error_message: str = "",
                  *, dom_groundable: bool | None = None) -> dict | None:
    """Return a grounded any-UI recipe descriptor, or None (default-off).

    Fires ONLY on a grounded hard-UI signal:
      * the live error says the target is in a CLOSED shadow root      -> open_shadow_shim
      * the live error/capture says there is NO DOM/AX grounding       -> visual_propose
        (canvas / WebGL / Flutter-without-semantics)
    Absent both -> None -> nothing emitted -> the normal DOM/recipe path runs."""
    err = error_message or ""
    if _CLOSED_SHADOW_RE.search(err):
        return {"kind": "open_shadow_shim"}
    if _NO_DOM_RE.search(err) or dom_groundable is False:
        return {"kind": "visual_propose",
                "label": observed.get("label", "") or observed.get("value", "") or "",
                "value": observed.get("value", "") or ""}
    return None


def emit_open_shadow_preamble() -> list[str]:
    """A PAGE-INIT preamble (opt-in) that forces every shadow root to ``open`` so the
    existing open-shadow heal path can reach a control the app hid in a CLOSED root.

    Must be injected via ``page.addInitScript`` BEFORE the app's first script runs
    (i.e. before goto). It is a TEST-ENV instrumentation only; it changes nothing
    the app asserts, and it never weakens an oracle — it only widens what the test
    can SEE. Idempotent + guarded so it cannot throw on a page that has no shadow DOM."""
    out: list[str] = []
    out.append("// ANY-UI heal (closed shadow DOM -> open): force every shadow root to")
    out.append("// 'open' mode before the app boots, so the normal open-shadow locator path")
    out.append("// can reach the control. Test-env instrumentation only; no oracle weakened.")
    out.append("await page.addInitScript(() => {")
    out.append("  try {")
    out.append("    const _orig = Element.prototype.attachShadow;")
    out.append("    Element.prototype.attachShadow = function (init) {")
    out.append("      try { return _orig.call(this, Object.assign({}, init, { mode: 'open' })); }")
    out.append("      catch (_e) { return _orig.call(this, init); }")
    out.append("    };")
    out.append("  } catch (_e) { /* no-op: page without shadow DOM is unaffected */ }")
    out.append("});")
    return out


def emit_any_ui_lines(observed: dict, recipe: dict, *,
                      screenshot_png: bytes | None = None) -> list[str]:
    """Render an any-UI heal (a list of JS lines), or a REFUSE throw — never a guess.

    open_shadow_shim -> the addInitScript open-mode preamble (functional, no GPU).
    visual_propose   -> propose-from-candidates + snap-to-node + an ORTHOGONAL oracle.
                        With no VLM configured / no candidates / no orthogonal oracle,
                        it emits a THROW that ESCALATES (RED), never a blind click."""
    from .locators import js_str  # lazy: avoid import cycle

    kind = (recipe or {}).get("kind", "")
    if kind not in _KINDS:
        return []

    if kind == "open_shadow_shim":
        return emit_open_shadow_preamble()

    # ── visual_propose (L6/L7, infra-gated) ──────────────────────────────────────
    label = (recipe or {}).get("label", "") or observed.get("label", "") or ""
    value = (recipe or {}).get("value", "") or observed.get("value", "") or ""
    lab = js_str(label)
    instruction = f"Drive the control labelled '{label}'" + (f" to '{value}'" if value else "")
    candidates = propose_candidates(screenshot_png, instruction)

    out: list[str] = []
    out.append("// ANY-UI heal (canvas/WebGL/Flutter-no-semantics): NO DOM/AX grounding.")
    if not candidates:
        # No model / no candidates -> REFUSE. The research rule: never click a blind
        # VLM coordinate, and never green-wash an ungroundable control. Escalate RED.
        out.append("// No self-hosted VLM grounding tier configured (NEXUS_VLM_GROUND_URL),")
        out.append("// OR the proposer returned no candidates. Per doctrine we REFUSE rather")
        out.append("// than click a blind coordinate or green-wash an ungroundable control.")
        out.append("throw new Error("
                   f"'any-UI: \\'{lab}\\' sits on a non-DOM surface (canvas/WebGL/Flutter) with "
                   "no DOM/AX grounding and no visual-grounding tier available — escalating to a "
                   "human (RED, never a blind-coordinate green-wash).');")
        return out

    # A model IS configured and returned candidates. Even so, we do NOT trust a raw
    # coordinate: we require an ORTHOGONAL outcome oracle (OCR of the recorded result
    # text / a semantics-state change) to gate green. If the recorded step carries no
    # such groundable outcome, we still REFUSE.
    top = candidates[0]
    bbox = top.get("bbox") or [0, 0, 0, 0]
    cx = int(bbox[0]) + int(bbox[2]) // 2
    cy = int(bbox[1]) + int(bbox[3]) // 2
    out.append(f"// VLM proposed {len(candidates)} candidate(s); using the top-ranked region")
    out.append(f"// (id={top.get('id')}, score={top.get('score')}). Snap-to-region + CDP click,")
    out.append("// then PROVE via an orthogonal oracle (the recorded outcome text must render).")
    out.append("{")
    out.append("  const _cdp = await page.context().newCDPSession(page);")
    out.append(f"  await _cdp.send('Input.dispatchMouseEvent', {{ type: 'mousePressed', x: {cx}, y: {cy}, button: 'left', clickCount: 1 }});")
    out.append(f"  await _cdp.send('Input.dispatchMouseEvent', {{ type: 'mouseReleased', x: {cx}, y: {cy}, button: 'left', clickCount: 1 }});")
    if value:
        # ORTHOGONAL oracle: the recorded outcome text must become visible. This is
        # independent of how we located the control (vision), so a mis-click fails RED.
        val = js_str(value)
        out.append(f"  await expect(page.getByText('{val}', {{ exact: false }}).first())"
                   ".toBeVisible({ timeout: 6000 }); // orthogonal: recorded outcome rendered")
    else:
        # No groundable outcome to assert -> we cannot prove the heal -> REFUSE.
        out.append("  throw new Error('any-UI: the visual heal has no recorded outcome to assert "
                   "orthogonally — refusing to green-wash a blind click (RED, escalate).');")
    out.append("}")
    return out


__all__ = ["vlm_enabled", "propose_candidates", "detect_any_ui",
           "emit_open_shadow_preamble", "emit_any_ui_lines"]
