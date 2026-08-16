# Fixture 02 — `shadow-closed`

## Purpose

Isolate ONE capability: **honest blindness**. A `mode:"closed"` shadow root cannot be
pierced by any DOM API, so the only correct capture is *nothing*, plus a named entry in
the opaque-surface ledger.

This fixture exists to make green-washing impossible to ship silently. It is the
counterpart to fixture 01: 01 proves we *do* read what is readable, 02 proves we *do not*
invent what is not.

## Expected controls

One: `input#zip` (light DOM). The three controls inside `<closed-quote-widget>` —
`Coverage Amount`, `Get Quote` — must be **absent**. They are listed in `forbid_controls`,
so a change that started fabricating them fails this fixture.

## Expected manifest

`tests/browser/golden/manifest_02-shadow-closed.json`. The manifest must show a page state
with the single ZIP control and a `coverage.opaque` entry naming the closed-shadow host.

## Targeted defect

None — this is a **regression guard**, not a bug reproduction. It pins two contracts:

1. `walk()` recurses `host.shadowRoot`, which is `null` for a closed root, so the subtree
   is skipped with no error and no invented control (`inventory_js.py:647`).
2. `OPAQUE_JS`'s third detector finds a dash-tagged custom element with no light DOM, no
   readable text and no open `shadowRoot`, and pushes `{kind: "closed_shadow"}`
   (`inventory_js.py:708-719`).

Detector (2) is **Playwright-lane only**: it gates on `getBoundingClientRect().height >= 40`,
and jsdom has no layout engine, so every rect is zero. The `opaque` block in
`expected.json` declares that lane restriction explicitly rather than letting the
assertion quietly not run.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_jsdom_execution.py -k 02-shadow-closed -v
python -m pytest tests/browser/test_playwright_execution.py -k 02-shadow-closed -v
```
