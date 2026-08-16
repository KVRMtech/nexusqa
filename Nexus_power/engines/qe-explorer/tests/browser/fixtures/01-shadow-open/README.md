# Fixture 01 — `shadow-open`

## Purpose

Isolate ONE capability: descending an **open shadow root** and naming the controls found inside it.

The fixture holds three shadow-scoped controls chosen so each exercises a different
accessible-name rung, and one light-DOM control that acts as the control group:

| Control | Rung under test | Needs a document lookup? |
|---|---|---|
| `input#policy-number` (light DOM) | `label[for]` | yes — against the main document |
| `input#pin` (shadow) | `label[for]` | yes — against **the shadow root** |
| `input#dob` (shadow) | `aria-labelledby` | yes — against **the shadow root** |
| `button#verify-btn` (shadow) | name-from-content | no |

## Expected controls

Four visible controls. The light-DOM input and the shadow button are asserted in the
normal fixture suite (`expect_controls`). The two shadow-scoped *named* controls are
asserted in the known-bug suite (`describes_correct_behaviour`) because the current
implementation cannot satisfy them.

## Expected manifest

`tests/browser/golden/manifest_01-shadow-open.json` — the normalized crawl manifest.
The golden records **today's** behaviour byte-for-byte (including the defect), so any
capture change shows as a diff. The `describes_correct_behaviour` block records what a
correct browser implementation must produce.

## Targeted defect — BUG-SHADOW-NAME

`walk()` recurses into `host.shadowRoot` but forwards the **outer** `doc`:

```js
// app/inventory_js.py:648
if (host.shadowRoot) {
  walk(host.shadowRoot, doc, frameSelector, sink, seenDocs);
  //                    ^^^ the OUTER document, not the shadow root
}
```

`accessibleName(el, doc)` then runs `doc.querySelectorAll('label[for="pin"]')` against the
main document, where no such label exists, and `idText(doc, "dob-label")` calls
`doc.getElementById` on the main document, which returns `null`. Both shadow inputs fall
all the way through the ladder to `{name: "", source: "none"}`.

Downstream consequence: the compiler binds by accessible name only
(`compiler.py` `_ladder`), so an unnamed control has **no bindable rung** — it is dropped
from the generated script entirely. A shadow-DOM design system (Salesforce Lightning,
Vaadin, any `lit` app) presents as a page with no fillable fields.

## Running this fixture alone

```bash
# jsdom lane
python -m pytest tests/browser/test_jsdom_execution.py -k 01-shadow-open -v
# real Chromium lane
python -m pytest tests/browser/test_playwright_execution.py -k 01-shadow-open -v
# the defect it targets
python -m pytest tests/browser/test_known_bugs.py -k shadow -v
```
