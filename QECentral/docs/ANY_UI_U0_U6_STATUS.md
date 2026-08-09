# Any-UI U0–U6 — Implementation Status (honest)

**Branch:** `feat/qec-dynamic-catalog-p0-p6` · all local, **not pushed / not deployed**.
**Suites:** explorer **634 passed** · qe-central **1448 passed / 70 skipped**. New
code pyflakes-clean.

> **What "done" means here.** Each phase's **pure decision core** is implemented and
> **unit-tested**. The parts that require the Playwright VM runtime, injected JS, the
> vision LLM relay, or the runner are implemented to the port's contract where
> possible but are **not** live-proven — marked **PENDING LIVE**. Nothing is claimed
> proven that wasn't executed. This matches the plan (§3): "Playwright primitives are
> VM-only — unit-test the pure logic; the browser side needs live proof."

---

## Per-phase status

### U0 — a11y confidence · ✅ CORE DONE + TESTED
`inventory.name_confidence_for` grades a control's accessible name high|medium|low|
none by which name rung produced it; `build_control_record` stamps
`qec.name_confidence`; `name_confidence_summary` rolls a page up. The
escalate-to-vision signal. 6 tests.

### U1 — deep frames · 🟡 ROUTING CORE DONE · ⏳ JS/VM PENDING
`perception.route_opaque_surfaces` classifies each opaque surface: cross-origin
iframe → **enterable** (Playwright `frame_locator` crosses origins), canvas/closed-
shadow → **vision**. **PENDING LIVE:** `OPAQUE_JS` emitting a `frame_selector` for
cross-origin iframes + the walk dispatching a frame-scoped re-observe (injected JS +
VM).

### U2 — vision Perceiver + pixel-evidence · ✅ PURE CORE DONE + TESTED · ⏳ WIRING PENDING
The evidence layer that keeps canvas journeys real: **G1** `state_fingerprint`
mixes a perceptual hash only when the DOM is sparse (canvas screens become distinct
nodes; rich-DOM pages byte-identical to before); `perception.average_hash` /
`perceptual_hash_png`; **G3** `vision_control_signature` (jitter-tolerant, so
question_ids don't churn); **G2/G3** `synthesize_vision_controls` /
`synthesize_vision_outcomes` (Perceiver output → walk shapes, outcomes carried as
proof). 11 tests. **PENDING LIVE:** `_make_vision_oracle` (clone `_make_medic_oracle`),
`/internal/perceive-controls`, `BrowserPort.click_at`, and enforcing the vision
flag/tenant gate in `vision_operate` (VM + HTTP).

### U3 — widget drivers + verification oracle · ✅ CRUX DONE + TESTED · ⏳ PRIMITIVES PENDING
The load-bearing part (Δ2): `gesture_verify` gives each gesture a pure read-back —
drag proven by ordered-sequence change, draw by empty→inked, slider by
`aria-valuenow` moving — returning True (proven) / False (refuted) / None
(unverifiable → `G_INFERRED`, descend). Never a false PROVEN. 4 tests. **PENDING
LIVE:** `BrowserPort.click_at`/`drag`/`draw_stroke`/`press_keys`/`scroll_until`
(Playwright `page.mouse`/`keyboard`), the matcher recognizer→dispatch wiring, and
capturing the before/after signals the read-backs consume.

### U4 — record-once for any widget · ✅ CORE DONE + TESTED · ⏳ RUNNER PENDING
`login_recorder.recipe_from_observed_macro`: a domain-agnostic replayable macro from
an observed interaction, reusing the already-generic `_recipe_from_sequence` without
the login tail; `_macro_key` = value-free id. 4 tests; login recipe unchanged.
**PENDING LIVE:** runner `/macro-capture/start|save` + replay wiring (clone
`/auth-capture`).

### U5 — coverage-by-rung ledger · ✅ CORE DONE + TESTED · ⏳ WALK-RECORDING PENDING
`fallback_ladder.rung_for_capture` maps a control's capture mode
(dom→deterministic, vision→agentic-if-verified, record_once→record-once, else human)
to a ladder decision; `coverage_for_controls` rolls a set into an honest by-rung
ledger. Unverified vision descends to human (anti-green-wash). 2 tests. **PENDING
LIVE:** recording `capture_mode` + `verified` per control in the crawl walk.

### U6 — passwordless auth · ✅ CORE DONE + TESTED · ⏳ DOM-SIDE PENDING
`Credentials.from_payload` relaxed: identifier falls back to aliases (member_number/
email/policy_no…), secret to aliases (pin/passcode…), empty secret allowed under
MFA/`passwordless`; refuse only with no identifier. Bare {username,password}
unchanged. 6 tests. **PENDING LIVE:** `match_login_controls` still requires an
`input_type=password` control — relax to accept a PIN/secret field for full
passwordless drive.

---

## What this adds up to
Every any-UI phase now has a **tested pure core** in the tree — the *decision* logic
that was the genuinely hard/novel part (perceptual state identity, jitter-tolerant
vision identity, gesture verification, capture→rung mapping, passwordless
construction, opaque-surface routing, record-once generalization). What remains for
each is the **browser/JS/HTTP wiring** — mechanically well-defined by the maps
(exact `file::function` seams) but only provable on the VM with a live app.

**Next, in leverage order:** U2 wiring (`_make_vision_oracle` + `click_at` +
`/internal/perceive-controls`) — the server-side vision path already exists, so this
is the fastest route to "drives a real canvas app," and the G1–G3 cores it needs are
already in place and tested.
