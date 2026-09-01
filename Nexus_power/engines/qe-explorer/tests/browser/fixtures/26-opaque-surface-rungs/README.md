# Fixture 26 — `opaque-surface-rungs`

## Purpose

Two capabilities that only exist on the far side of an origin boundary, and that fixtures
04 and 25 structurally cannot reach.

**1. Every rung of the frame-selector recipe, executed from `OPAQUE_JS`.** That snippet is
the one that names a frame the walker cannot read, so it is the one that decides whether
the port can enter it at all — and both existing cross-origin fixtures embed a single frame
carrying an `id`, so only the first rung of a five-rung chain had ever run there — and
under it, only the id rung of the recipe that addresses the shadow HOST. The five embeds
here drop to a different rung each:

| embed | rung | selector shape |
|---|---|---|
| (a) | title, disambiguated | `iframe[title="vendor step"] >> nth=0` |
| (b) | title, disambiguated | `iframe[title="vendor step"] >> nth=1` |
| (c) | src only | `iframe[src="…/child-c.html"]` |
| (d) | inside an open shadow root, host addressed positionally | `div >> nth=1 >> iframe[src="…"]` |
| (e) | inside an open shadow root, host addressed by unique tag | `aside >> iframe[src="…"]` |

(d) and (e) are the rungs a page-global ordinal cannot express at all: the **host** has to
be addressed first. (d)'s host is one of two `div.widget-slot` elements — the *second* — so
it needs an ordinal; (e)'s is the only `aside` in its root, so its bare tag is enough. Both
shapes matter, because a component library that never expected to be addressed from outside
gives its hosts neither ids nor unique tags reliably.

**2. The one closed shadow root the capture hook genuinely cannot observe.**
`<template shadowrootmode="closed">` is attached by the HTML parser and never calls
`Element.prototype.attachShadow`, so wrapping that method cannot see it. This is a stated
limitation of M3.2 / T-FR-02, and it is fixtured so that it is a measured fact rather than
a sentence in a docstring: the surface must stay an honest, named `closed_shadow` blind
spot, and its two controls are in `forbid_controls`. If a future change starts capturing
them, this fixture goes red and the limitation gets rewritten deliberately.

## Lane

**Playwright only**, for two independent reasons. jsdom has no true cross-origin isolation,
so the five embeds would simply be readable and none of them would reach `OPAQUE_JS`; and
jsdom does not implement declarative shadow DOM, so the honest blind spot would not exist
there either. Both halves need a real browser.

## Expected controls

Six: `input#order-total` from the main frame, and one field from inside each of the five
foreign embeds, every one of them stamped `capture_scope: "cross_origin_frame"`. The two
`legacy-disclosure-panel` controls are forbidden.

Three of the five frame selectors carry the fixture server's ephemeral port, so they are
asserted by **resolution** rather than by spelling — which is the only assertion that means
anything about a selector anyway.

## Expected manifest

`tests/browser/golden/manifest_26-opaque-surface-rungs.json`, whose `coverage.opaque` must
carry **five** `cross_origin_iframe` rows — one per embed — plus one `closed_shadow` row
and five `frame_entered` rows.

Five is the number that pins the targeted defect. `OPAQUE_JS` deduped on `kind|label`, and
a frame's label is its **host**; all five embeds here share one host, so four of them were
dropped before anything could enter them. A real checkout embedding a card frame and a
3-D-Secure frame from the same vendor hits this exactly, and the ledger could not tell it
apart from a page with one embed.

## Targeted defect

`BUG-OPAQUE-FRAME-DEDUP` (M3.2) — see `expected.json`.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_playwright_execution.py -k 26-opaque-surface-rungs -v
python -m pytest tests/browser/test_m32_frames_and_shadow.py -k rungs -v
```
