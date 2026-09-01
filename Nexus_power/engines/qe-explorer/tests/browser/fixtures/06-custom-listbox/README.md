# Fixture 06 — `custom-listbox`

## Purpose

Isolate ONE capability: reading a **custom ARIA choice control** — the widget class every
modern component library (Radix/shadcn, MUI, Headless UI) ships instead of `<select>`.

Four triggers, one per branch of `resolveListbox()` / `optionsAndTotalOf()` /
`valueCommitted()`:

| Trigger | Branch under test | Expected |
|---|---|---|
| `#term-trigger` | `aria-controls` id-ref, closed listbox in DOM | 4 options, `value_committed` `"20 years"` |
| `#rider-trigger` | Radix `data-placeholder` — unselected | 3 options, `value_committed` `""` |
| `#payment-mode` | descendant `[role=listbox]` fallback | 3 options |
| `#lazy-trigger` | no listbox in DOM (built on open) | `[]`, `options_total` 0 |

## Expected controls

Four, as tabled above. `expected.json` asserts the full option label lists, so a change
that reordered, deduplicated or truncated them fails.

## Expected manifest

`tests/browser/golden/manifest_06-custom-listbox.json`.

## Targeted defect

None — **regression guard**. It pins two hard-won behaviours that the source documents at
length:

1. **A custom trigger's value is its rendered text** (`inventory_js.py:320-328`). A
   `<button role="combobox">` has no `.value`, so before v8 every custom dropdown read back
   empty — a filled form reported `Gender: ""` even after a human chose one, and every
   automated selection was discarded as unverifiable.

2. **A placeholder is not a value** (`inventory_js.py:353-367`). `#rider-trigger` renders
   `"Select a rider"` but carries `data-placeholder`, so the committed value must be `""`.
   Recording the placeholder would make an empty form look filled — worse than recording
   nothing, because the crawl would then click a still-disabled Continue and loop.

`#lazy-trigger` pins the honest-empty case: a widget that builds options only on open
yields `[]`, never a fabricated set.

### Known gap this fixture deliberately does NOT hide

The `data-placeholder` marker is **Radix-specific**, as the source says (`:358-366`). An
unselected MUI or Headless UI trigger carries no such attribute and its `"Select…"` text
*will* be recorded as a committed value. That gap is written down rather than guessed at;
when a client app's real markup is read, its marker gets added here and a new subject is
added to this fixture.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_jsdom_execution.py -k 06-custom-listbox -v
python -m pytest tests/browser/test_playwright_execution.py -k 06-custom-listbox -v
```
