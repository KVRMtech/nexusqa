# Fixture 21 — new tab adoption (M1.5 / T-ND-01 + T-ND-04)

## Purpose

Prove that the journey can move to a **different browser page** and that the
crawler moves with it.

A browser context holds many pages, and the page a user is looking at changes
without any DOM event the inventory walker can see. Before M1.5 the crawler
created exactly one page and never registered `context.on("page")`, so a
`target="_blank"` step created a tab the crawler did not know about and every
later action, screenshot and fingerprint went to the page the journey had
already left. Nothing failed loudly; the crawl simply recorded the opener over
and over.

Six popup shapes, chosen because they fail in six different ways:

| control | shape | correct outcome |
|---|---|---|
| `Open Details` | `target="_blank"` anchor | **adopt** |
| `Open Underwriting Window` | `window.open(url)` | **adopt** |
| `Open Deferred Window` | `window.open('')` then a deferred `location.href` | **adopt** — on the URL it settles on |
| `Open Identical Copy` | `window.open(location.href)` | **adopt**, with a DISTINCT identity |
| `Open Partner Site` | `window.open` onto a foreign origin | **retain** — recorded, never adopted |
| `Open Transient Window` | opens and immediately closes | **retain** — never adopt a dead handle |

`Open Identical Copy` is the T-ND-04 case and the reason this fixture is not
just about following a link. The popup's URL template, interactive controls and
dialog flags are all identical to the opener's, so every signal the base
fingerprint reads collapses and the second page would silently inherit the first
page's identity — the "fingerprint the stale page" failure the milestone names.
It is also the case where the naive fix (fold the page identity into every
digest) is wrong, because that fractures the identity of every page whose
Playwright object merely changed. The fixture holds both directions.

`View Details In Place` is an ordinary same-tab link, present so the fixture
also shows ordinary navigation still behaves ordinarily.

## Expected controls

Seven interactive controls: two links (`Open Details`, `View Details In Place`,
both resolving to an absolute href ending `/details.html`) and five buttons.
The foreign-origin popup URL is written with the `__ALT_ORIGIN__` token, which
the fixture server substitutes for its second, genuinely-foreign origin — the
same mechanism fixture 04 uses for its cross-origin iframe.

## Expected manifest

The capture golden (`golden/manifest_21-new-tab-adoption.json`) is recorded
under the standard characterization crawl (`max_states=1`, `observe_only=True`),
so it holds the inventory of this page only — no popup is opened during it.

The adoption expectations are asserted by
`tests/browser/test_page_lifecycle_execution.py`, which drives the production
port and checks that:

* the port's active URL becomes `details.html` after `Open Details`, while the
  opener is still open;
* the deferred popup is adopted on `deferred.html`, not on `about:blank`;
* the adopted page carries a non-empty page token and the identical copy
  receives a state fingerprint that differs from its opener's;
* the foreign-origin and self-closing popups are recorded with
  `disposition="retain"` and never become active;
* closing the active page promotes an open one and records the promotion.

## Targeted defect

`BUG-M15-POPUP-NEVER-ADOPTED`. No `context.on("page")` existed anywhere in the
crawler, so a second page was invisible; and with it, `BUG-M15-STALE-PAGE-
IDENTITY`, where a popup identical to its opener inherits the opener's
fingerprint because every DOM signal collapses.

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer
python -m pytest tests/browser -k 21-new-tab-adoption
python -m pytest tests/browser/test_page_lifecycle_execution.py -k popup
```
