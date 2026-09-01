# Fixture 23 — `canvas-app`

## Purpose

Isolate ONE capability: being **honest about a page the DOM cannot describe at all**.

Fixture 13 has canvases *on* a page. This fixture **is** a canvas. That difference is the
whole hinge of the M3.1 vision milestone:

| | fixture 13 | fixture 23 |
|---|---|---|
| readable interactive controls | 2 (`#illustration-age`, `#recalculate`) | **0** |
| `should_perceive` | **false** — the DOM explains the page | **true** — it explains nothing |
| how a control is reached | a locator | a **coordinate**, and only after R0 proves it |

The failure mode this guards is not "we missed the canvas". It is capture *quietly getting
better* — a future walker that starts reporting the painted labels as controls would make
`should_perceive` false here, the vision escalation would stop firing, and the proving
ground would silently become a test of nothing while staying green.

## Expected controls

`INVENTORY_JS` captures **none**. Every operable thing on this page is drawn with
`fillRect` + `fillText`: no element, no role, no accessible name, no bounding box the
accessibility tree can reach, no locator any generated script could bind.

Four things are explicitly forbidden from the inventory:

| subject | why it must not be captured |
|---|---|
| `<canvas id="app">` | a surface, not a control — capturing it hands the crawl an unbindable locator and hides that the page is unreadable |
| `Annual Income` | painted, not marked up |
| `Recalculate` | painted, not marked up |
| `<p id="case-strip">` | a rendered PII strip is not something to act on |

### Playwright lane only

jsdom implements no canvas 2D context (`getContext` is unimplemented) and has no layout
engine. In that lane this application paints nothing and every rect is zero — so its
inventory would be empty because the fixture never rendered, not because capture correctly
refused to fabricate a control out of pixels. Every assertion here would pass as a **false
negative**, which is the same reason fixture 13 restricts its opaque block. The page still
guards `getContext` and returns early rather than throwing: a fixture that raises is a
fixture that can mislead.

`OPAQUE_JS` produces **one** row:

| canvas | area | ledgered? | label |
|---|---|---|---|
| `#app` 900×620 | 558,000 px | yes | `illustration studio` (its `aria-label`) |

## Expected manifest

A crawl of this fixture with vision DISABLED records one state whose control inventory is
empty and whose coverage ledger names one opaque surface. That is the honest answer, and it
is what the crawl produced before M3.1.

With vision ENABLED (`tests/browser/test_vision_canvas_proving_ground.py`, crawl
`vis06-canvas`) the same state records:

| perceived label | R0 rung | outcome |
|---|---|---|
| `Annual Income` | `pixel_stable_surface` | **verified** → `form_snapshot_signals["Annual Income"] = {"type": "text", …}` → a catalogue question |
| `Recalculate` | `dom` | **verified** (clicking it appends `<button id="apply-btn">`) |
| `Social Security Number` | — | **refused_unverified** — perceived at the inert crest, clicked, nothing happened |

The refused row appears in `coverage.vision_ledger` **and nowhere else**: not in
`form_snapshot_signals`, not in `question_groups`, not in any action, not in any flow.

## Targeted defect

None (regression guard). It pins the *precondition* of the entire vision path. Every M3.1
claim — perception, coordinate action, R0 verification, catalogue promotion — is only
meaningful while DOM capture genuinely cannot describe this target.

It also carries the T-VIS-05 subject: the case strip renders a real-shaped SSN, date of
birth, account number and email as ordinary text. No input holds them, so no text scan of a
prompt could ever see them — they travel in the image. The proving ground asserts they are
black in the bytes the model was handed.

## What the canvas actually does

* **`Annual Income`** — clicking focuses it: the canvas repaints with a caret and an editor
  panel opens. **Nothing in the DOM moves.** This is the only subject that can be verified
  by the perceptual R0 rung, and it is the one that reaches the catalogue.
  The editor panel is deliberately large: the identity ladder's perceptual rung is an 8×8
  average hash, coarse on purpose so a blinking cursor cannot fragment a canvas app into an
  infinite frontier. A 4 px caret is invisible to it, and rightly so.
* **`Recalculate`** — repaints *and* appends a real `<button>`, so the interactive signature
  changes and R0 verifies on the ordinary, strongest rung.
* **the crest** — inert. No repaint, no DOM change. Where the deliberately incorrect
  perception is aimed.

`window.__canvasApp` exposes the application's own view of events (`clicks`, `runs`,
`focus`). It is ground truth for the test and is never read by the crawler, so "R0 said
verified" can be checked against "the app really did react".

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer/tests/browser
pytest -k 23-canvas-app -q                        # the capture lanes
pytest test_vision_canvas_proving_ground.py -q    # the real crawl (Chromium, ~3 min)
```
