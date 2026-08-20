# M3.1 — Vision Cores Integration

**The law:** *A vision prediction is never catalog truth.* Only a coordinate action
followed by a successful R0 verification may promote a perceived control into
catalog evidence.

---

## Step 1 — The flow AS FOUND (re-grounded 2026-08-19, branch `feat/qec-dynamic-catalog-p0-p6`)

Line numbers below are the ones measured in the tree, not the ones in the roadmap.

### Producers / consumers actually present

| Symbol | Where it is PRODUCED | Where it is CONSUMED |
|---|---|---|
| `should_perceive` | `qe-explorer/app/perception.py:173` | `qe-explorer/app/discovery.py:526` — **one** call site |
| `synthesize_vision_controls` | `qe-explorer/app/perception.py:110` | **nothing** — only `tests/test_perception.py:104` |
| `synthesize_vision_outcomes` | `perception.py:200` | **nothing** |
| `vision_control_signature` | `perception.py:88` | **nothing** |
| `click_at` | `playwright_port.py:1519`, `browser.py:408` (protocol), `tests/characterization/harness.py:314` | **nothing in `app/`** |
| `perceptual_hash_png` | `perception.py:46` | `walker.py:1389,1696` (walk only) |
| vision oracle callable | `main.py:_make_vision_oracle:1048` | `OracleGateway.perceive:` `oracle_gateway.py` |
| `/internal/perceive-controls` | `qe-central/app/routers/internal.py:748` | explorer `_make_vision_oracle` |
| `/internal/vision-operate` | `qe-central/app/routers/internal.py:665` | **nothing** — no explorer caller |
| vision flags | `qe-central/app/services/branch_planner.autonomy_flags:80` (`env AND tenant`) | `routers/explorations.py:1067` → `ExploreDispatchRequest.vision_enabled` → `main.py:1361` |
| pixel egress guard | `qe-central/app/services/pii_egress_guard.guard_image:193` | `clients/platform_api._assert_image_egress_clean` |

### The loop as it actually ran

```
_expand (discovery.py)
  → observe → build_inventory → fingerprint          # DOM only, no phash
  → …fill / probe / discover…
  → collect_opaque()                                  # opaque surfaces ledgered
  → if oracle.vision_configured AND should_perceive:
        screenshot_png → base64 → oracle.perceive()
        → append ONE ledger row {kind:"vision_perceived", controls:[…]}   ← STOPS HERE
  → record_state(controls=snapshot_controls)          # vision controls NOT included
```

**Vision was observe-only by construction.** `discovery.py:522-527` says so in its own
comment: *"This records what vision SAW; it does NOT act on it (coordinate action + R0
is the next increment)."* The synthesis, signature and coordinate rungs were all built
and all unreachable.

### The eight defects this milestone closes

1. **D-1 · No action rung.** `synthesize_vision_controls` → `click_at` → `verify_intent`
   was never wired. Perceived controls landed in `coverage.opaque_surfaces` as prose.
2. **D-2 · Shared budget.** `_make_vision_oracle` (main.py:1075, 1130) spends
   `settings.medic_oracle_max_calls`, `medic_oracle_timeout_s` and
   `medic_oracle_breaker_threshold`. qe-central declares `vision_max_calls` /
   `vision_breaker_threshold` (config.py:260,264) that **nothing reads**. Vision
   exhaustion silently consumed the medic/DOM reasoning budget.
3. **D-3 · Two contradictory system prompts.** `internal.py:778` sends
   `system=vision_medic.SYSTEM` — the *click-region* prompt demanding
   `{"action":"click_region","x","y"}` — while `perceive_controls` builds
   `_PERCEIVE_SYSTEM` (a `{"controls":[…]}` contract) into the *user* prompt. The model
   received two mutually exclusive output contracts on every perceive call.
4. **D-4 · The gate is env+tenant, not attested+tenant.** Nothing on the vision path
   consults the signed provisioning attestation. A non-attested target with the tenant
   flag on ran vision.
5. **D-5 · Pixels leave unredacted.** `pixel_egress_allowed` is an *acknowledgement*
   flag, and `guard_image` states plainly `pixels_scanned` is always `False`. A filled
   application's SSN, name and account number egress as pixels.
6. **D-6 · Canvas states collapse in discovery.** Rung 4 (perceptual hash) exists in
   `WalkIdentity` but ONLY the walk supplies one. `_expand`'s `fingerprint()` call
   (discovery.py:351) passes no `perceptual_hash`, so two visually distinct canvas
   screens at one URL with one DOM hash to one state and the second is dropped by the
   `_visited_fingerprints` dedup.
7. **D-7 · Stale contract note.** `state_identity.py:20` still claims "Nothing computes
   one yet" — untrue since M1.1.
8. **D-8 · No negative evidence.** A perception that is wrong produces no record at
   all, so "vision found nothing" and "vision found something false" are
   indistinguishable in the manifest.
9. **D-9 · The escalation decision read a CRAWL-WIDE list.** `discovery.py:526`
   passed `self._opaque_surfaces` — which accumulates across every state — to
   `should_perceive`. Once any page in the crawl rendered a canvas, every
   subsequent sparse page looked opaque and became a vision candidate. The probe
   is now per-state (`_collect_opaque_now`), taken once and shared by the
   identity rung and the coverage ledger, so the digest and the ledger cannot
   describe two different readings of one state.
10. **D-10 · `click_at` took VIEWPORT coordinates while documenting PAGE ones.**
    Found by the proving ground — see "The defect the proving ground found".

---

## The flow AS BUILT

```
_expand (discovery.py)
  → observe → build_inventory
  → collect_opaque()                      ← ONE probe, before the digest
  → should_perceive? → perceptual_hash_png(screenshot)      [T-VIS-02]
  → fingerprint(..., perceptual_hash=…)   ← canvas screens stay distinct
  → …fill / probe / discover…
  → VisionEscalation.run()                                   [T-VIS-01]
        should_perceive
          → screenshot + collect_pii_regions → redact_screenshot   [T-VIS-05]
              └ None ⇒ DO NOT SEND (fail-closed, budget unspent)
          → VisionBudget.try_spend()                               [T-VIS-03/04]
              └ gate shut / cap spent / breaker open ⇒ refused + counted
          → oracle.perceive(masked b64, {pixel_redaction: receipt})
          → synthesize_vision_controls
          → screen(control)  ← refuse pack + classify_boundary
          → prove the surface STILL  (two phashes, one settle apart)
          → click_at(page x, y)
          → R0: verify_intent True  OR  still-surface repaint
          → promoted  ⇔  verified          |  attempts[] ⇔ everything
  → snapshot_controls += promoted          ← THE ONLY promotion path
  → record_state → coverage.states[].form_snapshot_signals → catalog
```

### Files

| File | Change |
|---|---|
| `qe-explorer/app/vision_gate.py` | **new** — T-VIS-04 truth table, attestation ladder, T-VIS-03 budget/timeout/breaker |
| `qe-explorer/app/vision_loop.py` | **new** — T-VIS-01 escalation, two-rung R0, ledger, role→kind |
| `qe-explorer/app/pixel_redaction.py` | **new** — T-VIS-05 masking + sha256-bound receipt + unloggable container |
| `qe-explorer/app/inventory_js.py` | `PII_REGIONS_JS` — where a screenshot would render something sensitive |
| `qe-explorer/app/playwright_port.py` | `collect_pii_regions()`; **`click_at` now really takes PAGE coordinates** |
| `qe-explorer/app/browser.py` | protocol verb `collect_pii_regions` (absent ⇒ refuse, not degrade) |
| `qe-explorer/app/discovery.py` | opaque probe before the digest; the observe-only block replaced by the loop |
| `qe-explorer/app/crawler.py` | builds the escalation; `_screen_vision_control`; `_vision_ledger` |
| `qe-explorer/app/coverage.py` | publishes `vision_ledger` / `vision_verified` / `vision_refused` / `vision_budget` |
| `qe-explorer/app/config.py` | `QEC_VISION_MAX_CALLS` / `_ORACLE_TIMEOUT_S` / `_BREAKER` / `_MAX_ACTIONS_PER_STATE` |
| `qe-explorer/app/main.py` | gate resolved in the crawl path; oracle spends the vision budget; receipt on the wire |
| `qe-central/app/services/vision_medic.py` | `system_prompt_for(task)` + `effective_prompt(task)`; user prompt carries no contract |
| `qe-central/app/routers/internal.py` | both endpoints select by task; relay the receipt; return `prompt` |
| `qe-central/app/services/pii_egress_guard.py` | `verify_redaction_receipt`; `guard_image` refuses unredacted pixels |
| `qe-central/app/clients/platform_api.py` | `redaction=` threaded to the wire chokepoint, defaulting to BLOCK |

### The defect the proving ground found

`click_at` documented "absolute page coordinates" and passed them straight to
`page.mouse.click`, which takes VIEWPORT coordinates. The only producer of those
coordinates is a perception of a `full_page=True` screenshot, whose space is the
PAGE. On a page no taller than the viewport the two coincide; on any page that
scrolls, **every vision coordinate was wrong by the scroll offset**. The failure
is silent and plausible — the click lands on nothing, R0 honestly reports
unverified, and the perception is discarded as a hallucination — so a
systematically mis-aimed rung would have read as a model that is always wrong.
`_page_point_to_viewport` scrolls the point into view and re-reads the offset
*after* the scroll (a scroll can be clamped at the document edge).

---

## Evidence — crawl `vis06-canvas`

Real Chromium, production port, production crawler, fixture
`tests/browser/fixtures/23-canvas-app/`. Archived at
`tests/browser/_crawl_out/vis06-canvas/vision_evidence.json`.

**Feature-gate state**
```json
{"enabled": true, "reason": "ok", "attested": true, "tenant_enabled": true,
 "attestation_rung": "signed_provisioning_proof"}
```

**Budget / breaker**
```json
{"calls": 1, "max_calls": 6, "timeout_s": 20.0, "failures": 0,
 "breaker_open": false, "breaker_threshold": 3, "latency_ms": 110, "refusals": {}}
```

**Screenshot PII** — the fixture prints `SSN: 123-45-6789`, `DOB: 1978-04-12`,
`Account: 4539 1488 0343 6467` and an email as ordinary text. The receipt the
model call carried:
```json
{"applied": true, "method": "dom-region-blackout-v1", "regions": 1,
 "page_w": 1280, "page_h": 752,
 "image_sha256": "345781cba15379f0f28c88ac78943aa0106f9a18c90a59c886dd65fed01ac806"}
```
`test_the_visible_PII_was_MASKED_in_the_bytes_that_left` samples ten points
inside that strip **in the image the perceiver received** and requires every one
to be `(0,0,0)`.

**Vision call trace / coordinate action / R0 / negative example**

| label | status | R0 rung | click | why |
|---|---|---|---|---|
| Annual Income | **verified** | `pixel_stable_surface` | (208, 274) | a still surface repainted in response to the click |
| Recalculate | **verified** | `dom` | (208, 394) | the page responded (url / DOM / dialog) |
| Social Security Number | **refused_unverified** | — | (760, 180) | R0 unverified: neither the DOM nor the pixels changed |

**Final catalog entry** — `coverage.states[ccfc4e19…]`:
```json
{"Annual Income": {"type": "text", "options": [], "options_total": 0,
                   "required": false}}
```
qe-central's `extract_controls` → `build_master_catalog` turns that into the
question **Annual Income** and produces **no** "Social Security Number" row
(`tests/contract/test_vis06_vision_catalog_crossing.py`).

**Perceptual identity** — entry / field-focused / recalculated hash to three
distinct values, and a re-observation with no interaction hashes identically.

---

## Test results

| Suite | Result |
|---|---|
| qe-explorer unit | **1866 passed**, 2 failed |
| qe-explorer browser — fixture 23 lanes + library contract | **154 passed** |
| qe-explorer browser — `test_vision_canvas_proving_ground.py` | **14 passed** |
| qe-central | **2234 passed**, 146 skipped, **0 failed** |

Fixture 23 joins the library properly: `expected.json` + `README.md` + minted
goldens (`golden/inventory_23-canvas-app.json` is `[]` — capture finds nothing,
which is the point). It is **playwright-only**, because jsdom implements no
canvas 2D context and no layout engine: there the application paints nothing and
every rect is zero, so every assertion would pass as a *false negative* — the
same reason fixture 13 restricts its opaque block. The page guards `getContext`
and returns early rather than throwing, since a fixture that raises is a fixture
that can mislead.

### Failures NOT belonging to this milestone

Verified individually, not assumed:

* `test_resume_crossing_journal_m34.py` ×2 — an M3.4 file another author added to
  this tree while M3.1 was being built. **Passes in isolation**; fails only when
  `test_characterization.py` runs first, because that harness's `TickClock` /
  `FrozenDate` patches leak into it. Proven unrelated: running the failing pair
  with `--log-cli-level=INFO` emits **zero `qec.vision.*` lines** — the M3.1 path
  never executes in it (no vision budget is wired, so `Crawler._vision` is
  `None`).
* `test_coverage.py::…[INVENTORY_JS]` and `…[OPAQUE_JS]` — the same author
  rewrote both snippets (`inventory_js.py`: 617 insertions / 50 deletions;
  `INVENTORY_JS_VERSION` v10 → v12). OPAQUE_JS is at 98.26% lines against a 99%
  floor, uncovered `[9, 118, 147, 168]` — all inside the new code. Structurally
  cannot be fixture 23's doing: snippet coverage is a **union over the corpus**,
  so adding a fixture can only raise it.
* `test_browser_characterization.py::test_golden_covers_every_fixture` — reports
  `26-opaque-surface-rungs`, another of that author's new fixtures, with no
  golden yet.
* `08-radio-groups`, `30-network-retry-poll-ratelimit`,
  `test_lanes_agree_on_structure`, `test_manifest_golden[13-canvas]` — same
  origin; fixture 13 and fixture 08 are untouched by M3.1.

The four `test_characterization` goldens that were stale earlier in this session
have since been re-recorded by that author and now pass. M3.1 does **not** move
them: re-recording and diffing the key sets showed no `vision_*` key, so the
change was reverted rather than absorbed.

## What is NOT proven

* **The model call is stubbed.** The proving ground's `PixelPerceiver` performs
  a real perception — it scans the redacted screenshot for button-coloured
  blocks and returns their centres, with no access to the DOM or the fixture —
  but the LABELS come from a legend standing in for OCR, and no multimodal
  provider is contacted. A network LLM is not a function; the characterization
  harness stubs its oracles for the same reason.
* **Not deployed.** No VM, no migration, nothing live.
* **`/internal/vision-operate` still has no explorer caller.** It is now
  correctly prompted and redaction-gated, but the crawler reaches vision only
  through `perceive-controls`.
* **The signed-attestation rung has no issuer.** `RUNG_SIGNED_PROOF` is reachable
  only where a platform provisioning proof exists; today's fleets will authorise
  vision at `RUNG_DISPOSABLE_ATTESTATION`, which is recorded as such.
