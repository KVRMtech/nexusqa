# Healing Hard / Non-DOM UIs Without Green-Washing — Research Synthesis

**Status:** synthesized 2026-06-23 from a deep-research run (23 sources, 110 claims, 25 adversarially
verified). The harness's own synthesis step was cut off by a usage limit, so this report is hand-synthesized
from the **11 independently confirmed claims** (3-0 / 2-0 votes) + cited primary sources. Claims marked
*(unverified)* came from a primary source but their verifier was interrupted — treat as plausible, not proven.

**The question:** how can an auto-heal system **LOCATE → DRIVE → PROVE** an interaction on closed shadow DOM /
canvas / Flutter / WebGL / any low-DOM UI, **without ever falsely passing a test (green-washing)?**

---

## TL;DR — the one rule the research validates
**Raw vision-language-model (VLM) coordinate grounding is unreliable** — the SOTA open agent SeeClick lands only
**53.4%** of clicks on the ScreenSpot benchmark (vs GPT-4V 16.2%), and **icons/widgets are worst (~30-52%)**
[arxiv 2401.10935, 3-0]. Pure-vision coordinate prediction "is much more difficult than choosing from
candidates" [ibid, 3-0]. **Conclusion: never click a blind VLM coordinate.** The correct shape is
**propose-from-a-candidate-set → snap-to-node → drive → prove with an ORTHOGONAL oracle → human-gate** — which
is exactly the never-green-wash architecture we already use for the DOM, extended to pixels. GUI-Actor formalizes
this (propose candidate regions + a separate grounding *verifier*, coordinate-free) *(unverified, arxiv 2506.03143)*.

---

## Per-technology feasibility verdict (priority output #1)

| UI tech | LOCATE | DRIVE | PROVE (orthogonal oracle) | Verdict |
|---|---|---|---|---|
| **Open shadow DOM** | Playwright pierces open roots | normal | the control's own committed-value oracle | ✅ **Heal + prove TODAY** (our universal recipe does this) |
| **Closed shadow DOM** | no built-in pierce; **monkey-patch `attachShadow`→open via `addInitScript`** before app boot *(unverified, PW#23047)* | normal once opened | committed-value oracle | 🟡 **Heal if we can force-open in the test env; else ESCALATE** |
| **Same-origin iframe** | `frameLocator(url-pattern)` | normal | committed-value oracle | ✅ **Heal + prove TODAY** |
| **Cross-origin iframe** | frameLocator limited | limited | hard | 🔴 **Escalate** (capture doesn't traverse it today) |
| **Flutter Web** | **SemanticsNode tree** — `bySemanticsLabel` (exact+regex) [flutter, 3-0] via `--web-renderer` + enabled semantics; or CDP `queryAXTree` | semantics actions / CDP input | semantics-state change + OCR/visual | 🟡 **Heal + prove IF semantics enabled; else VLM/escalate** |
| **Canvas apps** (Docs/charts/draw) | a11y shim if present (CDP `queryAXTree`); else **VLM propose-from-candidates** (OmniParser Set-of-Mark) | **CDP `Input.dispatchMouseEvent`** at snapped coords [CDP, 3-0] | **must** be orthogonal: OCR of result text / visual-diff / app-state | 🟠 **VLM-only + strict orthogonal oracle + human-gate; REFUSE if no oracle** |
| **WebGL / game UIs** | VLM only | CDP input | visual/telemetry only | 🔴 **Rarely safely provable → escalate by default** |

---

## LOCATE — the grounded ladder (cheap → expensive)
1. **Accessibility / semantics tree first.** CDP **`Accessibility.queryAXTree`** searches a node's a11y subtree by
   accessible name + role — a **DOM-independent** locate path [CDP docs, 3-0]. *Caveat:* the CDP Accessibility
   domain is **Experimental** and needs `enable()` to keep `AXNodeId`s stable [CDP, 2-1] — wrap it, don't depend
   on it blindly. **Flutter** exposes its own semantics finder `bySemanticsLabel` [flutter, 3-0] — for Flutter,
   *turn semantics on and use it* before any pixel approach.
2. **Closed shadow DOM:** force every shadow root to `open` mode by monkey-patching `Element.prototype.attachShadow`
   via `page.addInitScript` *before* the app runs *(unverified, PW#23047)*. Then heal as normal DOM. If the env
   forbids the shim → escalate.
3. **Vision parse → candidate set (NOT a coordinate).** OmniParser converts a screenshot into **structured,
   labeled elements** via interactable-region detection + icon-functionality captioning + **Set-of-Mark** (numeric
   IDs on boxes), no DOM [arxiv 2408.00203, 2-0]. The VLM then **picks an ID from that set** — never emits a raw
   coordinate. SeeClick / UI-TARS are screenshot-only agents [3-0] but at ~53% raw accuracy they are a **proposer**,
   not a decider.

## DRIVE — actuation
- **CDP `Input.dispatchMouseEvent`** injects mouse events at viewport CSS-pixel X/Y, independent of any DOM
  element — the core actuation primitive for canvas/WebGL [CDP, 3-0]. (Keyboard via `dispatchKeyEvent` is similar
  but one verifier disputed the exact field list — confirm against current CDP before relying on it.)
- Prefer the **framework's own hook** when it exists (Flutter `integration_test` / semantics actions) over blind
  pixel clicks.

## PROVE — the part that makes it honest (and where competitors fail)
The heal may only go green if the **recorded post-action OUTCOME** is verified by an oracle **orthogonal to how we
located/drove** the control. Options, strongest first:
- **Semantics/state change** (the a11y/semantics node now reads checked/expanded/selected) — strongest, grounded.
- **OCR of the specific result text** (a reference id, an amount) — grounded in rendered pixels, independent of the click.
- **App-state / telemetry assertion** if the app exposes it.
- **Perceptual-hash / visual-diff with tolerance** — *weakest*; brittle to anti-aliasing/animation; only as a
  corroborator, never the sole oracle.
**Never-green-wash rule:** the oracle must NOT be "the VLM that clicked says it worked," and NOT "the test re-ran
to green." If no orthogonal oracle can be grounded → **REFUSE / escalate.**

---

## Competitor reality (why this is a wedge, not a me-too)
- **Microsoft Playwright Healer agent** operates on **DOM locators** and its verification oracle is **"re-run the
  test until it passes"** — *not* an independent outcome oracle *(unverified, playwright.dev/docs/test-agents)*.
  That is precisely the **green-wash risk** our doctrine forbids: re-running to green can pass a real regression.
- Most visual tools (Applitools etc.) bring strong **visual diff** but a diff alone is element-resolution-coupled
  and tolerance-tuned — prone to both false-red (animation) and false-green (it "looks right").
- **Takeaway:** nobody combines *propose-from-candidates + snap-to-node + an ORTHOGONAL outcome oracle + human
  gate + tamper-evident evidence* on-prem. That combination is the defensible position.

## Infrastructure + licensing (self-hosted VLM tier)
- A self-hosted grounding tier needs a **GPU** (the open agents are 7B+ VLMs: UI-TARS, Qwen-VL-based SeeClick).
- **License landmine:** OmniParser's **YOLO icon-detection weights are AGPL**, while its captioning models are MIT
  *(unverified, github.com/microsoft/OmniParser)* — **AGPL is a real constraint for an on-prem commercial product.**
  Prefer permissively-licensed detectors/VLMs (verify each model's weight license before shipping).
- Implication: the VLM tier is **infra-gated** (GPU + license clearance) — keep it **propose-only + human-gated**,
  never on the auto-persist path.

---

## Recommended architecture + build sequence for Nexus (priority outputs #2 & #3)
Every step stays inside our existing doctrine: recompile the **owned** spec → prove an **orthogonal** outcome →
**human-gate** → **tamper-evident evidence**. Build in this order (cheapest + most grounded first):

1. **L4 semantics-layer locate (DONE/started):** use the captured a11y tree + state for re-anchor & diagnose.
   Add CDP `queryAXTree` as a DOM-independent locate fallback (wrap the Experimental API).
2. **Closed-shadow open-mode shim** (`addInitScript` attachShadow→open) — cheap, no GPU; **detect + escalate** if
   the env forbids it. **(TIER 3)**
3. **Iframe-by-URL** capture + re-anchor of frame *and* inner control. **(TIER 3)**
4. **Visual propose-from-candidates + snap-to-node** (Set-of-Mark candidate set; pick an ID, snap to the nearest
   real node/box — **never a raw coordinate**). **(L6, propose-only, human-gated)**
5. **Self-hosted VLM** (permissive-license model on a GPU node) as the proposer for step 4's candidates — **infra-
   gated**; propose-only. **(L7)**
6. **CDP `Input.dispatchMouseEvent`** as the actuation of last resort, only after a candidate is snapped + an
   orthogonal oracle exists. **(TIER 3 canvas)**
7. **The hard line — REFUSE rather than risk a false green:** if (a) no semantics + (b) no orthogonal outcome
   oracle can be grounded, the heal is **not provable** → escalate to a human. This is the canvas/WebGL default.

**One-sentence design rule:** *vision may PROPOSE, the candidate set + snap-to-node CONSTRAINS, an orthogonal
oracle DECIDES, a human APPROVES, and the evidence chain RECORDS — and when we can't ground the oracle, we refuse.*

---

## Sources (verified-claim subset)
Primary: arxiv 2501.12326 (UI-TARS), 2401.10935 (SeeClick/ScreenSpot), 2408.00203 (OmniParser), 2506.03143
(GUI-Actor), flutter.dev (bySemanticsLabel), chromedevtools.github.io CDP Accessibility + Input, github.com/
microsoft/playwright#23047 (closed shadow DOM), playwright.dev/docs/test-agents (Healer). Full source list (23)
in the run output `wiu1zrm2h`.

> **Note:** the synthesis + ~14 verifications were interrupted by a usage limit (resets 7:50pm America/Chicago).
> A re-run after reset would deepen the competitor + licensing sections; the architecture conclusions above rest
> on the confirmed claims and are stable.
