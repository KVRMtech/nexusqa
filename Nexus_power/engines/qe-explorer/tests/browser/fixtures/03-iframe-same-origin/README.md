# Fixture 03 — `iframe-same-origin`

## Purpose

Isolate ONE capability: descending **same-origin iframes** and stamping each control with a
`frame_selector` that re-resolves to the frame it came from.

Three child frames, chosen so the selector recipe is exercised across its safe path and its
two unescaped paths:

| Frame | Attribute used | Emitted selector | Valid? |
|---|---|---|---|
| `<iframe id="billing">` | `id` | `iframe#billing` | yes |
| `<iframe id="pay.frame">` | `id` | `iframe#pay.frame` | **no** — parses as `#pay` + `.frame` |
| `<iframe name='quote"frame'>` | `name` | `iframe[name="quote"frame"]` | **no** — unparseable |

## Expected controls

Six visible controls: one in the main frame, two in `billing`, one in `pay.frame`, one in
`quote"frame` (a `<select>` whose `options_total` is 3, including the unset placeholder).

The main-frame control and the `billing` controls are asserted normally. The two escaped
selectors are asserted in the known-bug suite.

## Expected manifest

`tests/browser/golden/manifest_03-iframe-same-origin.json`. `crawl_meta.frame_count`
records how many frames the walker actually entered.

## Targeted defect — BUG-IFRAME-SELECTOR

```js
// app/inventory_js.py:546-557
function frameSelectorFor(iframeEl, index) {
  if (iframeEl.id) return 'iframe#' + iframeEl.id;                 // ← unescaped
  var nm = attr(iframeEl, "name");
  if (nm) return 'iframe[name="' + nm + '"]';                      // ← unescaped
  ...
}
```

Twelve lines earlier the same file gets this right for a different lookup:

```js
doc.querySelectorAll('label[for="' + CSS.escape(el.id) + '"]')    // ← escaped
```

So the escaping requirement is already understood in this module; the frame recipe simply
does not apply it.

Downstream consequence: the module docstring states the recipe exists so *"a
`frame_selector` we emit resolves the SAME way `page.frameLocator(...)` resolves it."*
When it does not, the generated Playwright script targets a frame that does not exist and
every step inside that frame fails at replay — while the crawl manifest reports the
controls as successfully captured. That is a green-wash: capture says covered, replay
cannot bind.

`iframe#pay.frame` is the more dangerous of the two, because it is *valid CSS that matches
something else* rather than a parse error — it fails silently.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_jsdom_execution.py -k 03-iframe-same-origin -v
python -m pytest tests/browser/test_playwright_execution.py -k 03-iframe-same-origin -v
python -m pytest tests/browser/test_known_bugs.py -k iframe -v
```
