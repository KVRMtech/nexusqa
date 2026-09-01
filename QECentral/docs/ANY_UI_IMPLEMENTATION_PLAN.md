# Any-UI Support — Phase-by-Phase Production Implementation (U0–U6)

**Status:** authored 2026-08-09 · grounded in a full code map of the perception +
interaction stack (workflow `wf_23147280-aeb`, 6 parallel readers, 130 reads).
**Companion:** [`ANY_UI_SUPPORT_PLAN.md`](./ANY_UI_SUPPORT_PLAN.md) (the ladder
model + coverage matrix).

Goal: the CRAWL tests an app built with **any UI technology**, via two universal
ladders (perceive: a11y tree → shadow/frame → vision → record-once → human; act:
semantic → positional → coordinate → gesture → recorded macro), with a rung for
every control and **coverage reported by rung** so a "covered" claim is always earned.

**Scope (stated, not implied — G5):** "any UI" = anything a Chromium page renders —
HTML/SPA/WASM/Flutter Web/canvas/WebGL/embedded vendor frames/Electron-style web
content. **Native** mobile/desktop (iOS, Android, WPF) is OUT of this engine's
scope; if the product wants it, that is a separate initiative with an Appium-class
driver. Stating the boundary is part of the honest claim.

---

## 0. How to read this plan (and the big surprise)

The mapping found that **far more of the any-UI stack is already built than the
"HTML-only" framing implied.** This plan is therefore mostly **wiring already-built
halves together** and adding the few genuinely-missing primitives — not a rewrite.

Three rules govern every phase (identical to the P0–P6 discipline):
1. **Additive** — extend a named function or add a module beside it. Zero rewrites.
2. **Generic mechanism; discovered content** — the ladders are value-free; the app's
   specifics are discovered (a11y/vision) or demonstrated once (record-once). No
   `if framework == react`.
3. **Honest coverage** — every action reports its rung + provenance
   (`G_DETERMINISTIC` / `G_LIVE_CONFIRMED` / `G_INFERRED`) through the built
   `fallback_ladder` + `coverage.py`. An action that can't be **verified** never
   counts as proven.

---

## 1. Grounding — what is ALREADY built (the reveal)

| Layer | Already built (reuse) | Real gap |
|---|---|---|
| **L1 a11y perception** | `inventory_js.py` (`inv-js-v6`): `walk()` over a curated interactive `SELECTOR`; 7-rung accessible-name ladder; ARIA role; states (required/disabled/expanded/haspopup/pressed/checked/min-max-step/draggable). `ControlRecord` (`inventory.py:129-174`). | No per-control **confidence**; name ladder is a **subset** of W3C accname; `SELECTOR` misses JS-listener-only elements. |
| **L2 shadow + frames** | **Open shadow roots ALREADY pierced** (`walk` 548-555 recurses `host.shadowRoot`). **Same-origin nested iframes ALREADY traversed + composed** (`frameSelector + " >>> " + childSel`). `_locator` chains `frame_locator` on `>>>`. | **Cross-origin iframes** + **closed shadow** only *named opaque* (`OPAQUE_JS`), not entered. Vendor payment/e-sign frames need `frame_locator` into cross-origin. |
| **L3 vision** | **Entire server-side path built + reachable:** `vision_medic.consult_vision` (screenshot+bbox → bbox-relative `click_x/y`), `is_vision_candidate`, `validate_proposal`; `platform_api.complete_vision` → platform-api `/api/v1/llm/vision`; **`internal.py::vision_operate` (`POST /internal/vision-operate`) — the one place `propose_fn` is joined to `complete_vision`.** Flags `QEC_CRAWL_VISION_ENABLED`(False)/`QEC_VISION_MAX_CALLS`/`QEC_VISION_BREAKER`. | **The explorer never calls it** — `/internal/vision-operate` has **zero callers**; the whole path is dead-ended. No per-element **bbox capture**; no page-level **Perceiver** (`controls[]`). |
| **Interaction** | `BrowserPort` (click/hover/fill/select_option/set_checked/**set_input_files**/screenshot_png/storage_state); `_locator` (role+name→`.nth(match_index)`, anchor, frame chain); **upload wired** (`forms._maybe_upload`); single-key `press_key` + `press_sequentially`. | No **coordinate** `mouse.click(x,y)`; no **drag/draw** (`matcher.is_drag_drop` ledgers "no interaction primitive yet"); `scroll` is read-only; no `press_keys(seq)`; `hover` built but **unwired**. |
| **Walk / widgets** | observe→`build_inventory`→`cur_controls` cycle; `matcher.primitive_for` recognizer; `forms._fill_one` dispatch; **`interaction_ladder`** (per-archetype rungs, each **R0-gated** by `verify_intent`); **opaque detection built** (`OPAQUE_JS`→`collect_opaque`→`_opaque_surfaces`). | Opaque surfaces **named, never driven**; custom (non-input) slider/date/richtext/drag/virtualized have no driver. |
| **Record-once / auth** | Login record-once end-to-end (`login_observer.js` records ALL clicks/fills/navs value-free; `_recipe_from_sequence` replays **generic** goto/fill/click/wait). `ground_truth_recorder.js`. Autonomous MFA (`auth.py MfaConfig`, TOTP). SSO handled by record-once (federated flag + per-origin sessionStorage poll). | **No non-login macro** recorder/replayer (choreography is login-shaped). **`Credentials.from_payload` hard-requires username+password** → passwordless (member/PIN/OTP, magic-link) refused. Magic-link inbox: none. |
| **Guardrails / coverage** | `guard.classify_request` (EXPLORE blocks ALL mutation; SUBMIT under attestation+approval), fail-closed refuse pack, R0 `verify_intent`, **`fallback_ladder` (built this session)**, `coverage.py`, `tier_label`, `touch_meter`, the `branch_planner.autonomy_flags` **double-gate** pattern. | Vision flag **read nowhere**; **no tenant vision column** (no double-gate); **`fallback_ladder` not wired to any crawl path**; coordinate clicks bypass the label-based irreversible check. |

---

## 2. Architecture deltas (the hard truths the code imposes)

**Δ1 — U1/U2 are JOINs, not builds.** Shadow/open-frame perception already works;
vision's whole server side already works. The work is: enter cross-origin vendor
frames, and **wire the explorer to the dead-ended vision path** (clone one oracle
factory). This collapses the two "biggest" phases into mostly-integration.

**Δ2 — The real hard problem is VERIFYING non-DOM actions.** `verify_intent` /
`_read_value` have **no vocabulary for gesture/coordinate outcomes** (a canvas draw
or drag-reorder has no committed value to read back), so those actions degrade to
`intent_met=None` and **earn no PROVEN credit**. Without a verification oracle,
vision/gesture coverage can only ever be `G_INFERRED`. **Building the verification
layer is the load-bearing work of any-UI honesty** — it's what keeps "we tested it"
true when the action was a coordinate click.

**Δ3 — Coordinate clicks bypass label-based safety.** `classify_action_verb` on a
coordinate click sees an empty `button_name`, so the irreversible-verb guard can't
fire. Safety then rests on `classify_request` (EXPLORE blocks *all* mutation) + the
phase gate + URL match. Any-UI coordinate/gesture actions must run **only** under
that network-containment + phase posture, never trusting a label.

**Δ4 — The universal coverage ledger isn't wired.** `fallback_ladder.resolve_rung` /
`coverage_by_rung` exist but nothing calls them. Wiring them as the **per-control
rung recorder** on every walk is what turns "any UI" into an auditable claim.

---

## 3. Cross-cutting production rules

- **Flag discipline (double-gate, fail-closed).** Every new autonomy: an env flag on
  `config.py::Settings` (default `False`) AND a `TenantProvisioningRow` column, ANDed
  in a helper mirroring `branch_planner.autonomy_flags`. Vision reuses the existing
  `QEC_CRAWL_VISION_ENABLED` + a **new tenant `vision_enabled` column** (migration).
- **Coordinate/gesture safety.** These run only in a non-prod/disposable phase under
  `classify_request` containment; `vision_operate` must **enforce the flag/tenant
  gate** (today HMAC-only). A coordinate action in AUTH/SUBMIT is refused unless the
  URL passes the irreversible check (label can't).
- **Verification-or-inferred.** Every non-DOM action routes through the `_act`
  pattern and R0 `verify_intent`; unverifiable → recorded `G_INFERRED` and descends
  the `fallback_ladder`. No gesture is ever reported PROVEN without a read-back.
- **Deploy.** Vision needs the LLM/vision relay (platform-api `/api/v1/llm/vision`)
  reachable + `QEC_CRAWL_VISION_ENABLED`. No `sdk/nexus-sdk` change → no base rebuild.
  Explorer changes rebuild `qe-explorer` (its own Playwright image).
- **Testing.** Playwright primitives are VM-only (untestable locally) — unit-test
  every new BrowserPort verb through the **scripted fake** per the port's design, and
  the pure recognizers/oracles directly (the pattern used across the suite today).

---

## 4. The phases

Each: **Objective · Build on · Changes (file::function) · Flag · Tests · Guardrails ·
Coverage · Definition of Done · Depends on.**

### U0 — a11y as the stated universal layer + confidence  ·  size S
**Objective.** Make explicit (and measured) that L1 is framework-agnostic, and add
the per-control **confidence** the ladder needs to decide when to escalate to vision.
**Changes.** `inventory_js.py::describe` (485-535) — emit `name_confidence` graded by
the accessible-name rung that fired (label-for/aria → high, content → mid,
title/placeholder/best_effort → low, empty → zero) + the computed role. Add
`name_confidence` to `inventory.py::ControlRecord` (129-174) + populate in
`build_control_record` (473-523). Add a `capture_mode` telemetry atom (dom vs
vision) for coverage.
**Flag.** None (telemetry only). **Tests.** Confidence grading per rung; unchanged
control shape otherwise. **DoD.** Every control carries a confidence; a low-confidence
interactive page is a measurable signal. **Depends on.** Nothing.

### U1 — enter cross-origin vendor frames + capture the SELECTOR tail  ·  size M
**Objective.** Close the L2 edges: embedded **cross-origin** payment/e-sign vendor
iframes (DocuSign/Adobe/Stripe), and interactive elements the curated `SELECTOR`
misses.
**Changes.** (a) `inventory_js.py` — when a nested iframe is cross-origin (today
named opaque by `OPAQUE_JS`), still emit a **frame handle** (its `frameSelectorFor`
recipe) so `_locator`'s `frame_locator` chain can enter it via Playwright (which
crosses origins) and inventory *inside* it on a follow-up observe. (b) Widen the
`SELECTOR` allow-list cautiously (elements with `onclick`/cursor:pointer + role
inference) or add a bounded second pass for listener-only clickables, flagged
low-confidence. (c) Closed shadow stays correctly ledgered opaque (cannot pierce) →
routes to vision/record-once.
**Flag.** `QEC_DEEP_FRAMES_ENABLED` (default `False`). **Tests.** cross-origin frame
handle emitted + `_locator` enters it; listener-only clickable captured at
low-confidence; closed shadow still opaque. **Guardrails.** In-frame mutation still
under `classify_request` phase gate. **DoD.** A control inside a cross-origin vendor
iframe appears in the inventory and is actionable. **Depends on.** U0.

### U2 — vision Perceiver + the pixel-evidence layer  ·  size XL
**Objective.** Give the explorer eyes **and keep the evidence real**: drive
DOM-opaque surfaces (canvas / Flutter Web / WebGL / PDF) that `OPAQUE_JS` already
**detects** but nothing **drives** — while preserving state identity (G1), outcome
capture (G2), and stable catalog identity (G3) on those surfaces. Without G1–G3 a
canvas app produces garbage journeys, no evidence, and a churning catalog — so they
are in-phase, not follow-ups.
**Changes.**
(a) **Per-element operate** — `main.py` clone `_make_medic_oracle` (487) as
`_make_vision_oracle`: capture the element **bounding box** (`locator
.bounding_box()`) + a **cropped** screenshot (fixing the bbox-relative vs full_page
mismatch), POST `/internal/vision-operate`, translate the returned bbox-relative
`click_x/y` to a page coordinate, execute via a new `click_at`, **R0-verify**. Wire
onto `PlaywrightBrowserPort.__init__` (891) beside `medic_oracle`; add the vision
rung in `_act_with_ladder` (1209) *after* the text medic.
(b) **Page Perceiver** — new `vision_medic` fn + `/internal/perceive-controls`
returning `controls[]` with bboxes; hook at `crawler._expand` opaque block
(1455-1462) and the `_walk_wizard` sparse-controls point (2895): when
`build_inventory` is sparse AND `collect_opaque` says canvas/cross-origin,
perceive → synthesize coordinate-addressable control records → feed the **same**
`build_inventory→fill` path → R0-verify each.
(c) **G1 — state identity on pixel UIs.** `fingerprint.state_fingerprint` keys the
walk AND the journey nodes; a canvas app changes screens with the same URL and a
near-empty DOM, collapsing every screen into one node. When the DOM is sparse, mix
a **coarse perceptual hash** of the screenshot (downscaled, pure, explorer-side)
into the fingerprint; add **pixel-stability settle** for canvas (DOM quiescence
never fires on repaints).
(d) **G2 — evidence reading.** The Perceiver contract also returns
`displayed_values[]` (label / text / `value_infer` type), provenance-tagged
vision, feeding the same outcome-capture path — so a canvas journey carries its
premium/decision/policy-number proof, not just navigation.
(e) **G3 — stable identity.** Vision control signature = normalized perceived
label + role + **coarse bbox grid bucket** (jitter-tolerant), feeding
`question_id_for`; vision-sourced catalog rows carry provenance so `catalog_diff`
damps expected OCR wobble instead of crying wolf every re-crawl.
**Flag.** `QEC_CRAWL_VISION_ENABLED` (exists) AND a **new** `TenantProvisioningRow
.vision_enabled` (migration) — ANDed in `autonomy_flags`. `vision_operate` must
**enforce** this gate (today HMAC-only). Explorer-side breaker + max-calls in
`_make_vision_oracle`. Reconcile the 10-vs-20 default.
**Tests.** oracle factory (scripted fake vision), bbox→page-coord translation,
Perceiver synthesizes control records, R0 gates every vision action, breaker trips;
G1: fingerprint mixes the hash only when sparse + two distinct canvas screens get
distinct fingerprints; G2: displayed_values parsed + typed; G3: signature stable
across jittered bboxes/labels.
**Guardrails (Δ3).** Coordinate clicks refused in AUTH/SUBMIT unless URL passes the
irreversible check; only run under `classify_request` containment.
**Coverage.** Vision actions tagged `G_INFERRED` until R0-confirmed, then
`G_LIVE_CONFIRMED`; recorded via the U5 ladder.
**DoD.** A Flutter-Web / canvas page yields **distinct journey nodes per screen**,
a **captured outcome value**, **stable question_ids** across two perceives, and a
control on it **clicked + verified**. **Depends on.** U0; the server-side vision
path already exists.

### U3 — universal widget interaction (gesture/keyboard + the verification oracle)  ·  size XL
**Objective.** Operate the hard widget classes — and, the load-bearing part (Δ2),
**verify** them so they earn PROVEN credit.
**Changes.** (a) **BrowserPort primitives** (`browser.py::BrowserPort` +
`main.py::PlaywrightBrowserPort`): `click_at(x,y)`, `drag(path)`,
`draw_stroke(points)`, `press_keys(seq)`, `scroll_until(control)` — implemented via
`self._page.mouse.*` / `keyboard.*` / `wheel`, reusing `_locator.bounding_box()`,
each wrapped in the `_act` pattern. (b) **Recognizers** — one rule per widget in
`matcher.primitive_for` (custom slider, calendar-popup, combobox type-ahead,
richtext/contenteditable, drag-drop, virtualized list), promoting
`matcher.is_drag_drop` from *detection* to *dispatch*. (c) **Drivers** — a branch in
`forms._fill_one` or a new `_act` kind + `interaction_ladder._LADDERS` rung per
widget. (d) **The verification oracle (the crux)** — extend `browser.verify_intent`
(200) with read-backs for gesture outcomes: a drag proven by DOM order change, a
draw proven by a canvas non-empty / pixel-delta check, a slider by
`aria-valuenow`/value, a combobox by committed option. Where no read-back exists →
honest `intent_met=None`, `G_INFERRED`, descend. (e) **Replay compile (G4)** — a
vision/gesture step must compile into the generated, runnable journey step:
prefer a semantic locator when one exists, else the **recorded bbox + `click_at`
with a drift guard**, else re-perceive at run time / a recorded macro (U4). Each
generated step carries its **rung** on the script-fidelity scorecard, so a client
sees which steps are DOM-proven vs vision-replayed.
**Flag.** `QEC_WIDGET_DRIVERS_ENABLED` + per-widget sub-flags — ship one widget class
at a time. **Tests.** each recognizer; each driver through the scripted fake; each
verify_intent gesture read-back (proven vs unverifiable); a vision step compiles to
a replayable step carrying its rung. **Guardrails.** every rung already R0-gated;
gestures inherit the coordinate-safety posture.
**Coverage.** a widget is PROVEN only when its read-back fires; else `G_INFERRED`.
**DoD.** signature pad drawn + verified (feeds the built `esign` recognizer); a
drag-reorder proven by order change; a vision step replays through the runner.
**Depends on.** U2 (coordinates); incremental.

### U4 — generalize record-once from login to ANY widget/flow  ·  size M
**Objective.** For anything vision/gesture can't ground, a human demonstrates it
once; replay. The machinery is **already generic** — this lifts it out of the login
wrapper.
**Changes.** `login_observer.js` already records all clicks/fills/navs value-free —
**no change**. Extract `login_observation.observation_from_events` pass-5 (the
`sequence` build, 327-343) and `login_recorder._recipe_from_sequence` into a
domain-agnostic **`recipe_from_observed_macro`** (drop `_verify_document_steps` /
`_assert_home_step` / `login_type_key`). Add runner `/macro-capture/start|save`
(clone `/auth-capture`) + a replay path reusing the recipe interpreter. Unresolved →
`TOUCH_WIDGET_RECORD` (built) → replays; still-unresolved → `TOUCH_WIDGET_RESOLVE`
human, counted. Recorded macros are also the **runtime fallback for vision steps**
that cannot re-ground at run time (G4).
**Flag.** `QEC_MACRO_RECORD_ENABLED`. **Tests.** a recorded non-login macro replays;
CAPTCHA routes to the human rung, never auto-solved. **DoD.** an operator records a
widget once → the crawl replays it. **Depends on.** the record-once login stack (exists).

### U5 — wire the universal coverage ledger + anti-automation posture  ·  size S
**Objective.** Make "any UI" auditable: every control records which rung handled it;
challenges are handled honestly.
**Changes.** Wire `fallback_ladder.resolve_rung` / `coverage_by_rung` (built, **unwired**)
into the walk: at each control, record the rung (deterministic L1/L2 → agentic-vision
L3 → record-once L4 → human L5) + provenance; roll up per app with **no averaged
number** (`touch_meter` law). Detect bot-challenges/rate-limits → back off, respect
the target, route CAPTCHA to the human rung — **never build evasion**.
**Flag.** none (reporting). **Tests.** a mixed page yields a rung ledger; a
challenge descends honestly. **DoD.** per-app coverage names the rung + provenance
for every control. **Depends on.** U2–U4 (rungs to record).

### U6 — auth universality (passwordless + MFA/OTP + magic-link)  ·  size M
**Objective.** Log into any app after one recorded login, regardless of auth shape.
**Changes.** (a) **Unblock passwordless** — `auth.py::Credentials.from_payload`
(184-185) and `match_login_controls` (317-318) hard-require username+password; relax
to accept any identifier slot (member#+PIN, OTP-first) — the record-once path already
has no such requirement, so align the live-crawl path. (b) **MFA/OTP as a per-run
slot** — let a recorded OTP slot self-renew via `MfaConfig` TOTP in the replay
interpreter (today record-once records OTP as a static secret). (c) **Magic-link** —
poll a provisioned disposable inbox, extract + follow the link (new; none exists).
**Flag.** `QEC_AUTH_UNIVERSAL_ENABLED`. **Tests.** passwordless recipe drives a
member/PIN login; TOTP slot self-renews; magic-link extracted from a stub inbox.
**DoD.** an app behind SSO+MFA (or passwordless) is crawled after one recorded login.
**Depends on.** record-once (exists), U4 (macro engine).

---

## 5. Sequencing, critical path & the honesty crux

```
U0 ─► U1 ─► U2 ─► U3 (widget-by-widget) ─► U5
                      └── U4 (parallel) ──┘
U6 folds in anytime after U0
```
- **Critical path to "drives a canvas/Flutter app":** U0 → U2. The vision server
  side already exists, so the operate wiring is fast — but U2 also carries the
  **pixel-evidence layer** (G1 state identity, G2 outcome reading, G3 stable
  identity): a canvas app without those produces garbage journeys and a churning
  catalog. Ship the wiring first; **U2 is not DONE without G1–G3.**
- **The load-bearing phase is U3's verification oracle (Δ2).** Until gesture/coordinate
  actions have read-backs, any-UI coverage on those surfaces is honestly `G_INFERRED`,
  not PROVEN. Invest there — it's what makes "any UI" a *proof*, not a *click-through*.
- **U5 makes it auditable**; without it, "any UI" is a capability with no ledger.

## 6. Risk register
| Risk | Phase | Mitigation |
|---|---|---|
| Gesture/coordinate actions can't be verified → no PROVEN credit | U3 | The verification oracle (drag=order-change, draw=pixel-delta, slider=valuenow); else honest `G_INFERRED`. |
| Coordinate click bypasses label-based irreversible guard | U2/U3 | Run only under `classify_request` containment + phase gate; refuse in AUTH/SUBMIT unless URL passes. |
| `vision_operate` is HMAC-only (no flag/tenant gate) | U2 | Enforce `crawl_vision_enabled AND tenant.vision_enabled` in `vision_operate` + `_make_vision_oracle`. |
| bbox-relative coords vs full_page screenshot → clicks land wrong | U2 | Send element-cropped images; translate offsets. |
| `fallback_ladder` unwired → "any UI" has no ledger | U5 | Wire `resolve_rung`/`coverage_by_rung` into the walk per control. |
| Passwordless logins refused at construction | U6 | Relax `Credentials.from_payload`/`match_login_controls`; align with record-once. |
| Vision cost / hallucination at fleet scale | U2 | `vision_medic` breaker + explorer-side counter + R0 gate + ladder fallback. |
| Vision flag default drift (10 vs 20) | U2 | Reconcile before enabling. |
| Domain leak into a mechanism | all | Ladders stay value-free; `refuse_pack.yaml` insurance lexicon is data, auditable. |

---

## 7. Product-goal gap review — what interaction alone misses

Adversarial self-review against the product goal (*prove every business journey →
replayable catalog → evidence, at 1000-app scale*): the U-phases above are the
**interaction** layer. The product is an **evidence** product. Clicking a canvas
app is worthless if we cannot identify its states, read its outcomes, and replay
the proof. Five gaps, now folded into the phases:

**G1 — State identity on pixel-only UIs (→ U2, prerequisite).** The entire walk and
journey graph key on `fingerprint.state_fingerprint(url, controls, dialog_flags)`,
and nodes key on that fingerprint. A canvas/Flutter app changes screens with the
SAME url and a near-empty DOM → every screen collapses into ONE node (or loop
detection misfires) — vision clicks would "work" while the journey graph and
catalog become garbage. Fix: when the DOM is sparse, mix a **coarse perceptual
hash** of the screenshot (downscaled, pure, explorer-side) into the fingerprint;
and add **pixel-stability settle** for canvas (DOM-quiescence never fires on
repaints — reuse the `perceptual_diff` idea, $0). *U2's DoD now requires: a canvas
app yields DISTINCT journey nodes per screen.*

**G2 — Evidence reading on opaque surfaces (→ U2).** Proof, not navigation:
`collect_displayed_values` is DOM-based, so a canvas app's premium / decision /
policy-number outcomes are invisible → journeys complete with NO outcome values —
"a walk, not evidence" by the flow-ledger's own doctrine. Fix: the Perceiver
contract returns `displayed_values[]` (label/text/`value_infer` type) alongside
`controls[]`; provenance-tagged vision so the oracle stack treats them honestly.
*U2's DoD now requires: an outcome value captured from a canvas page.*

**G3 — Stable identity for vision-perceived controls (→ U2, feeds P2).** The
Master Catalog's `question_id_for` needs a stable signature; vision controls have
none, and OCR'd labels jitter → question_ids churn → `catalog_diff` noise on every
re-crawl (a regression engine that cries wolf is dead at fleet scale). Fix: vision
control signature = normalized perceived label + role + **coarse bbox grid bucket**
(jitter-tolerant), and vision-sourced catalog rows carry provenance so diff can
damp expected wobble.

**G4 — Replayability of vision/gesture steps (→ U3/U4).** The product ships
runnable journeys / owned Playwright. A coordinate step must compile to something
replayable: (1) a semantic locator when one exists, else (2) the recorded bbox +
`click_at` with a drift guard, else (3) re-perceive at run time (the runner needs
the vision capability) or a recorded macro (U4). The generated script's fidelity
scorecard must carry each step's **rung**, so a client sees which steps are
DOM-proven vs vision-replayed. Without this, any-UI crawling produces catalogs the
runner cannot prove again — breaking replayable-proof, the category claim.

**G5 — Scope honesty: web-delivered UIs.** "Any UI technology" here means anything
a Chromium page renders — HTML/SPA/WASM/Flutter Web/canvas/WebGL/embedded vendor
frames/Electron-style web content. **Native** mobile/desktop apps (iOS, Android,
WPF) are OUT of this engine's scope — a different driver (Appium-class) and a
separate initiative if the product wants it. Stating the boundary is part of the
honest claim; implying it silently is how green-wash starts.

*Carry-forward confirmations:* multi-tab (`window.open`) flows during a crawl
(record-once handles the SSO second tab; the crawl-side path needs confirming);
the heal story for vision steps (re-perceive as the heal action).

---
*Grounded in code map wf_23147280-aeb. Sizes (S/M/L/XL) are directional effort. Every
`file::function` reference was read; anything unconfirmed is flagged "UNKNOWN /
confirm during implementation" in the maps.*
