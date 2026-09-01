# Fixture 04 — `iframe-cross-origin`

## Purpose

Isolate ONE capability: **crossing an origin boundary the supported way**.

`iframe.contentDocument` throws a `SecurityError` for a foreign origin. That is a real
browser security boundary, and injected JavaScript must not try to defeat it. The walker
therefore still does exactly what it always did:

1. catches the `SecurityError` and skips that frame, and
2. keeps capturing the rest of the page — the exception must never unwind the whole walk
   and take the main frame's controls with it.

What M3.2 adds is the other half. The walker stops at the boundary because JavaScript
running inside the page structurally cannot cross it; the **port** then crosses it with
Playwright's own frame APIs. `content_frame()` asks the browser for the frame's own
execution context, and the walker runs inside it under that frame's origin, exactly as the
frame's own scripts do. Nothing is injected across the boundary; origin isolation is used,
not circumvented.

This is the shape of every real payment / captcha / KYC embed, so it is the difference
between "we captured the checkout page except the part that takes the money, and here it
is by name" and "we captured the checkout page".

## What this fixture used to assert

That the three controls inside the foreign frame — `Card Number`, `CVC`, `Pay Now` — were
in `forbid_controls`: capturing nothing from the embed and naming it in the opaque ledger.
They are now `expect_controls`, each carrying `frame_selector: "iframe#card-entry"`. The
embed is still NAMED in the ledger, because that row is what carries the deterministic
selector the port enters with; whether the entry succeeded is a separate `frame_entered`
row, so a frame that was named and a frame that was read are never confused.

## Lane

**Playwright only.** jsdom does not enforce true cross-origin isolation, so simulating a
second origin there would exercise a different code path than the one under test, and it
has no frame-locator equivalent to enter one with. The `lanes` key in `expected.json`
states this, and the jsdom suite *skips this fixture by name* rather than silently not
asserting.

The fixture server substitutes `__ALT_ORIGIN__` with a second `http://localhost:<port2>`
origin at serve time, so the embed is genuinely cross-origin.

## Expected controls

Four: `input#amount-due` from the main frame with `value_committed` `"129.00"` and an empty
`frame_selector` (entering the embed must not disturb it), plus `Card Number`, `CVC` and
`Pay Now` from inside the foreign frame, each stamped `iframe#card-entry`.

`frame_selectors_must_resolve` additionally hands that selector back to the browser: it
must resolve to exactly one frame, and each control captured through it must be findable
inside that frame — i.e. the catalogued payment fields are ACTIONABLE at replay, not merely
recorded.

## Expected manifest

`tests/browser/golden/manifest_04-iframe-cross-origin.json`, whose `coverage.opaque` must
carry one `cross_origin_iframe` row and one `frame_entered` row.

## Targeted defect

Regression guard, re-aimed by M3.2 / T-FR-01 — see `expected.json`.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_playwright_execution.py -k 04-iframe-cross-origin -v
```
