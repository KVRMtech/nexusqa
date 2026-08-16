# Fixture 08 — `radio-groups`

## Purpose

Isolate ONE capability: **`group_key`** — the field that says *which question* a control
answers. It is structure, never a value.

Get it wrong and the manifest describes a form the application does not have: either N
unrelated toggles where there is one question, or one question where there are N.

## Expected controls

Fifteen controls across five subjects:

| Subject | Markup | Expected `group_key` | Branch under test |
|---|---|---|---|
| A | `<form id="term-form">` radios `name="product"` | `name:term-form:product` | form-scoped native radio |
| B | `<form id="whole-form">` radios `name="product"` | `name:whole-form:product` | **same attribute, different question** |
| C | `role="radiogroup" id="plan-group"` cards | `grp:id:plan-group` | ARIA set, no `name` attribute |
| D | checkboxes `name="conditions[]"` in a fieldset | `name:conditions-form:conditions[]` | checkbox group = one question |
| E | two unrelated checkboxes | `name:doc:remember` / `name:doc:newsletter` | **must NOT merge** |

`expected.json` also asserts the **partition**: exactly 6 distinct non-empty `group_key`
values. A per-control assertion cannot catch a collapse or a shatter on its own; the
partition count can.

## Expected manifest

`tests/browser/golden/manifest_08-radio-groups.json`.

## Targeted defect

None — **regression guard** for `groupKeyOf()` (`inventory_js.py:486-528`). Two rules it
pins, both of which the source argues for explicitly:

1. **Form scoping.** *"two forms on a page may each use `name="product"` and they are NOT
   the same question."* Subjects A and B are that exact page. Without scoping, the crawler
   would force one option and believe it had answered both questions.

2. **Declared signals only.** *"Never on mere proximity: a 'Remember me' sitting beside a
   'Subscribe to newsletter' is two questions, and merging them would answer one and
   silently drop the other from the residue."* Subject E is that exact pair.

Subject C also pins `parentAcross()`, which walks out of shadow roots via `getRootNode().host`
— the card set is reached by ancestor traversal, not by a `name` attribute.

## Note on subject E

Both checkboxes carry a `name`, so they take the `name:` branch and get *distinct* keys
(`name:doc:remember`, `name:doc:newsletter`) rather than `""`. `doc` is the form-id
placeholder used when `el.form` is null. Distinct keys are the correct outcome — they are
two questions — and that is what is asserted.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_jsdom_execution.py -k 08-radio-groups -v
python -m pytest tests/browser/test_playwright_execution.py -k 08-radio-groups -v
```
