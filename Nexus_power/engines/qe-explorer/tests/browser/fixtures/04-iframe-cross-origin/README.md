# Fixture 04 — `iframe-cross-origin`

## Purpose

Isolate ONE capability: **failing honestly at an origin boundary**.

`iframe.contentDocument` throws a `SecurityError` for a foreign origin. The walker must

1. catch it and skip that frame,
2. keep capturing the rest of the page (the exception must not unwind the whole walk), and
3. surface the frame in the opaque ledger so an operator sees a named blind spot rather
   than a page that merely looks fully covered.

This is the shape of every real payment / captcha / KYC embed, so it is the difference
between "we captured the checkout page" and "we captured the checkout page except the part
that takes the money, and here it is by name."

## Lane

**Playwright only.** jsdom does not enforce true cross-origin isolation, so simulating a
second origin there would exercise a different code path than the one under test. The
`lanes` key in `expected.json` states this, and the jsdom suite *skips this fixture by
name* rather than silently not asserting.

The fixture server substitutes `__ALT_ORIGIN__` with a second `http://127.0.0.1:<port2>`
origin at serve time, so the embed is genuinely cross-origin.

## Expected controls

One: `input#amount-due` from the main frame, with `value_committed` `"129.00"`. The three
controls inside the foreign frame (`Card Number`, `CVC`, `Pay Now`) are in
`forbid_controls`.

## Expected manifest

`tests/browser/golden/manifest_04-iframe-cross-origin.json`, whose `coverage.opaque` must
carry one `cross_origin_iframe` row.

## Targeted defect

None — regression guard. It pins two branches:

```js
// app/inventory_js.py:657 — the honest skip
try { cdoc = ifr.contentDocument; } catch (e) { cdoc = null; }
if (cdoc && seenDocs.indexOf(cdoc) === -1) { ... }
```

```js
// app/inventory_js.py:692-698 — the named blind spot
var readable = false;
try { readable = !!f.contentDocument; } catch (e) { readable = false; }
if (!readable) { push("cross_origin_iframe", host || "embedded frame", ...); }
```

## Running this fixture alone

```bash
python -m pytest tests/browser/test_playwright_execution.py -k 04-iframe-cross-origin -v
```
