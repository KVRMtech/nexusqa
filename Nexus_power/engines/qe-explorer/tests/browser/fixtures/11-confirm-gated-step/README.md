# Fixture 11 — `confirm-gated-step`

## Purpose

Isolate ONE capability: reading **interactability** — disabled state and visibility — so
the crawler is never handed a control the browser will refuse.

A control reported enabled that is not produces an action timeout, which the classifier
records as outcome `none`. That reads as "we tried, nothing happened" rather than "we were
blocked", and the walk loops on a gate it cannot see.

## Expected controls

Six visible controls; four hidden ones that must be absent.

| Subject | Markup | Branch under test | Expected |
|---|---|---|---|
| A | `<button disabled>` | `el.disabled === true` | `disabled: true` |
| B | `<div role="button" aria-disabled="true">` | `aria-disabled` fallback | `disabled: true` |
| C | `<input type=checkbox checked>` + enabled button | positive path | `value_committed: "true"`, `disabled: false` |
| D | `hidden` / `aria-hidden` / `display:none` / `input[type=hidden]` | `isVisible()` | **absent** |
| E | `.sr-only` label | clipped, not hidden | still names `#promo` |
| F | `role="alert"` | read by `error_texts()`, not the walker | not a control |

## Why subject B matters

A `<div role="button">` is not a form element, so `el.disabled` is `undefined` and only
`aria-disabled` carries the truth (`inventory_js.py:392-397`). This is the standard
shape for a design-system button, and it is the one a property-only check gets wrong.

## Why subject E matters

An `.sr-only` label is *clipped*, not `display:none`, so it is present in the accessibility
tree and must still name its control. `accText()` handles this by falling back to
`textContent` when `innerText` is empty — the source comment says so explicitly:
*"It is empty for hidden elements by definition, so sr-only labels fall back to textContent
rather than losing their name entirely."*

## Expected manifest

`tests/browser/golden/manifest_11-confirm-gated-step.json`.

## Targeted defect

None — regression guard.

## Lane note

The `display:none` subject uses an **inline style attribute**, which jsdom's
`getComputedStyle` resolves, so it is asserted in both lanes. Stylesheet-driven visibility
(cascade, media queries, `:has()`) is Playwright-only and this fixture deliberately does
not depend on it.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_jsdom_execution.py -k 11-confirm-gated -v
python -m pytest tests/browser/test_playwright_execution.py -k 11-confirm-gated -v
```
