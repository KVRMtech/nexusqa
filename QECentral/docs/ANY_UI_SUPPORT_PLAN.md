# Any-UI Support — Universal Perception & Interaction Plan

**Goal:** the CRAWL tests an application built with **any UI technology** — not a
list of frameworks we special-case, but a **universal stack** where every screen
is perceived by the highest-fidelity layer that works and acted on by the matching
primitive, with an always-present fallback so there is **never a dead end — only a
lower-autonomy rung, reported honestly.**

The wrong model is "we support React and HTML but not canvas." The right model is:
**one perception ladder + one interaction ladder that together bottom out at
record-once and human**, so no technology is "unsupported" — each is handled at the
layer its rendering allows.

---

## 1. The core idea — two universal ladders

A UI is only ever three things to a machine: **a semantic tree** (accessibility/DOM),
**pixels** (what's rendered), or **an opaque box you were shown how to use once**.
Every UI technology reduces to one of those. So the engine needs exactly two ladders.

### 1a. Perception ladder — how we SEE any UI
| Layer | What it reads | Covers | Today |
|---|---|---|---|
| **L1 Accessibility tree** | roles, accessible names, states (the a11y semantics every compliant framework emits) | Server HTML, **every SPA** (React/Vue/Angular/Svelte/Solid), Blazor, most design systems | **Built** — `inventory_js` reads the a11y name ladder, ARIA roles, required/disabled/expanded/haspopup |
| **L2 Shadow + frame piercing** | open shadow roots, nested iframes | Web Components / custom-element apps, embedded vendor widgets (payment, e-sign) | **Partial** — iframes captured via `frame_selector`; shadow-DOM piercing is the gap |
| **L3 Vision (VLM)** | the screenshot itself | Canvas apps (**Flutter Web / CanvasKit**), WebGL/3D, PDF viewers, image-only controls, anything DOM-opaque | **Designed** — `vision_medic` proposes coordinate actions from a screenshot + bbox, R0-verified; not yet fully wired |
| **L4 Record-once** | a human demonstration, replayed | Exotic widgets, gesture-only controls, CAPTCHA-adjacent flows | **Exists for login** — record-once is proven for auth; needs generalizing to any widget |
| **L5 Human-flagged** | — | Anything the above cannot ground | **Policy built** — `fallback_ladder` |

### 1b. Interaction ladder — how we ACT on any UI
| Primitive | Drives | Today |
|---|---|---|
| **Semantic locator** (role + accessible name) | L1/L2 controls | **Built** |
| **Positional** (`match_index` → `.nth`) | identical controls (17 bare Yes/No buttons) | **Built** |
| **Coordinate** (mouse x,y / tap) | vision-perceived controls on canvas/WebGL | **Designed** (`click_region`) |
| **Gesture** (drag, draw stroke, slider, multi-touch) | signature pads, drag-drop, range sliders | **Gap** |
| **Keyboard sequence** (type, arrow/enter, shortcuts) | comboboxes, rich-text editors, date grids | **Partial** (fill/type built) |
| **Recorded macro** | L4 record-once | **Exists for login** |

**The universal rule:** for each control, descend the perception ladder until one
layer understands it, then act with the highest primitive that layer supports;
verify (R0 / re-observe); if nothing grounds or verifies, descend to record-once,
then human — and **report which rung was used** (`fallback_ladder.coverage_by_rung`,
already built). That reporting is what makes "any UI" an honest claim instead of a
green-wash.

---

## 2. Coverage matrix — every UI technology, by how it's handled

No technology is absent from this table; each maps to a perception layer + primitive.

| UI technology / paradigm | Perception | Interaction | Status |
|---|---|---|---|
| Server-rendered HTML | L1 | semantic | **Built** |
| React / Vue / Angular / Svelte / Solid (client SPA) | L1 (a11y tree — framework-agnostic) | semantic + positional | **Built** |
| Blazor / WASM that renders real DOM | L1 | semantic | **Built** |
| Web Components / Shadow DOM (Lit, Stencil, Salesforce LWC) | **L2 shadow pierce** | semantic | **Gap → U1** |
| iframes / cross-origin embeds (payment, e-sign vendors) | L2 frames | semantic (in-frame) | **Partial → U1** |
| Canvas UIs — **Flutter Web (CanvasKit)**, Google-Docs-class | **L3 vision** | coordinate + gesture | **Designed → U2** |
| WebGL / 3D / map canvases | L3 vision | coordinate | **Designed → U2** |
| PDF / document viewers | L3 vision + download capture | coordinate | **Gap → U2/U4** |
| Native-in-webview / hybrid (Cordova, Electron, RN-web) | L1 (DOM) | semantic | **Built** |
| Date pickers / calendars | L1 + keyboard grid | keyboard/coordinate | **Partial → U3** |
| Comboboxes / typeaheads / listboxes | L1 (`haspopup`/`expanded` captured) | keyboard + semantic | **Partial → U3** |
| Drag-and-drop / sortable / kanban | L1/L3 | **gesture (drag)** | **Gap → U3** |
| Range sliders / dials | L1/L3 | **gesture** | **Gap → U3** |
| Rich-text / WYSIWYG editors | L1/L3 | keyboard sequence | **Gap → U3** |
| File / media upload | L1 | set-input-files / recorded | **Partial → U3** |
| Virtualized / infinite-scroll lists | L1 + scroll driver | scroll + semantic | **Gap → U3** |
| Modals / overlays / toasts / tooltips | L1 (dialog roles) | semantic | **Built** |
| Multi-step wizards / steppers | L1 | semantic | **Built** |
| Signature pads | L3 + `esign.classify` | **gesture (draw)** | **Recognizer built → U2/U3** |
| CAPTCHA / bot-challenge | detect only | **human rung** (never auto-solve) | **Gap → U4** |
| SSO / OAuth redirect auth | flow capture | record-once | **Built** (record-login) |
| MFA / OTP / magic-link | record-once + slot | recorded + slot value | **Partial** (recipe slots exist) |

---

## 3. The build plan — raise every layer to production

Phases are named **U#** (UI-universal) to sit beside the P0–P6 pipeline work.
Each is additive, behind flags, honest-coverage reported.

### U0 — Make the accessibility tree the primary perception (foundation)
The a11y tree is already framework-agnostic — a React button and an Angular button
present the same `role=button` + accessible name. **Harden `inventory_js` to prefer
the full a11y computed name/role/state** and emit an `a11y_confidence` per control,
so L1 is explicitly the universal semantic layer (not "HTML parsing"). Add the
browser AX-snapshot as a cross-check where the injected read is weak.
*Deliverable:* L1 stated + measured as framework-agnostic. *Size S.*

### U1 — Shadow-DOM + deep-frame piercing (close the encapsulation gap)
Extend the injected inventory to **recursively pierce open shadow roots** and
**nested iframes** (compose the `frame_selector` chain already captured). This makes
Web-Component apps (LWC/Lit/Stencil) and embedded vendor widgets first-class at L1/L2.
*Deliverable:* controls inside shadow roots / nested frames appear in the inventory
with a correct pierce-path locator. *Size M.*

### U2 — Vision Perceiver (the DOM-opaque layer — this is P5, generalized)
Wire `vision_medic.consult_vision`'s `propose_fn` to `platform_api.complete_vision`
(the one unjoined seam) into a **Perceiver** that, for any canvas/WebGL/opaque
region, returns a structured control map from the **screenshot** and drives it by
**coordinate**. Add a `perceive_page(screenshot) → controls[]` pass that activates
when L1/L2 yield too few controls for a visibly-interactive page (the "empty DOM,
full screen" signal of Flutter/CanvasKit). R0-verify every proposed action.
*Deliverable:* a Flutter-Web / canvas page is catalogued and driven by vision.
*Size XL.* Guardrails: `crawl_vision_enabled`, breaker + max-calls (already in
`vision_medic`), coverage tagged `G_INFERRED` until R0-confirmed.

### U3 — Universal widget interaction library (the gesture/keyboard set)
A caged, **verified** interaction library for the hard widget classes, each with a
recognizer + a driver + an R0 check: **date grids** (keyboard/coordinate), **combobox
/ typeahead** (type→arrow→enter), **drag-and-drop** (mouse down-move-up), **sliders**
(drag/keyboard), **rich-text** (focus + keystroke), **file upload** (set-input-files
or recorded), **virtualized lists** (scroll-until-found). Signature-pad *draw* lands
here, feeding the `esign` recognizer already built.
*Deliverable:* each widget class has a driver that operates it and proves it
registered. *Size XL, incremental — ship one widget at a time behind a per-widget flag.*

### U4 — Record-once for the exotic tail + human-in-the-loop
Generalize the proven record-once login mechanism to **any widget/flow**: a human
performs the interaction once on the disposable env, the choreography is captured
(the `ground_truth_recorder` sidecar) and replays. This is the honest home for
CAPTCHA (a human solves it; we never auto-solve or evade), gesture-only controls
vision can't reliably drive, and vendor flows behind exotic UIs.
*Deliverable:* an operator records a widget once → the crawl replays it; unresolved
→ flagged human, counted. *Size M.*

### U5 — Anti-automation posture (honest, not evasive)
Detect bot-challenges/rate-limits and **respond honestly**: back off, respect the
target's controls, and route CAPTCHA to the human rung — never build detection
evasion. Report these as coverage boundaries, not failures.
*Deliverable:* challenge detection → honest ladder descent + report. *Size S.*

### U6 — Auth universality
Extend record-once to the full auth matrix: **SSO/OAuth redirect chains**, **MFA/OTP**
(recipe slot for the code, as member cards already do for member_number/pin),
**magic-link** (poll a provisioned inbox on the disposable env). Login is a
perception problem solved once and replayed — no per-app auth code.
*Deliverable:* an app behind SSO+MFA is crawled after one recorded login. *Size M.*

---

## 4. Why this genuinely is "any UI" — and stays honest

- **Nothing is out of scope.** Every technology maps to L1, L2, L3, or (L4/L5)
  record-once/human. A UI we've never seen is handled by whichever layer its
  rendering permits — worst case, a human demonstrates it once.
- **The claim is always earned.** `fallback_ladder` reports coverage **by rung**:
  deterministic (L1/L2) → agentic-verified (L3) → record-once (L4) → human (L5). A
  "covered" control names the rung that resolved it and whether it was R0-verified.
  So "we tested this app" is never a blanket assertion — it's a per-control ledger.
- **No hardcoding, no per-app code.** The ladders are value-free mechanisms; the
  app's specifics are discovered (L1/L2/L3) or demonstrated once (L4). We never
  write `if framework == react`.

## 5. Current state, stated plainly (no "can/can't" hand-waving)
- **L1 (a11y tree) is built and is already framework-agnostic** — this is *most*
  web apps, not "HTML": any React/Vue/Angular/Blazor/web app that emits standard
  accessibility semantics is perceived the same way.
- **L2 partial** (iframes yes, shadow-DOM piercing is U1), **L3 designed** (vision
  exists, Perceiver wiring is U2), **L4 exists for login** (generalize in U4),
  **L5 policy built.**
- The interaction ladder has **semantic + positional built, coordinate designed,
  gesture the main gap (U3).**

**Sequence to full any-UI:** U0 → U1 → U2 → U3 (widget-by-widget) in parallel with
U4; U5/U6 fold in. Each phase raises the floor of what's handled deterministically
and shrinks what falls to record-once/human — but from day one, **every** UI has a
rung, and coverage tells the truth about which one.
