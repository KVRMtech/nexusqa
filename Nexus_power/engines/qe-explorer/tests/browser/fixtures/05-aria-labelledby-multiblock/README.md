# Fixture 05 — `aria-labelledby-multiblock`

## Purpose

Isolate ONE capability: computing an accessible name from `aria-labelledby` when the
referenced element contains **block-level children**.

Per W3C accname, an element's name contribution is its *rendered* text, so two block
children contribute `"A B"`. `textContent` concatenates them to `"AB"`.

## Expected controls

Four inputs:

| Control | Rung | Correct name |
|---|---|---|
| `#income` | `aria-labelledby`, single text node | `Annual Income` |
| `#tobacco` | `aria-labelledby`, one ref with 2 blocks | `Question 7 Have you used tobacco in the last 12 months?` |
| `#beneficiary` | `aria-labelledby`, two refs, 2nd has blocks | `Beneficiary Full legal name as printed on the policy` |
| `#height` | `label[for]`, 2 blocks | `Height feet and inches` |

`#income` is the control group (both implementations agree). `#height` is the **in-fixture
proof** that the correct behaviour is already implemented one rung over. `#tobacco` and
`#beneficiary` are the reproduction.

## Expected manifest

`tests/browser/golden/manifest_05-aria-labelledby-multiblock.json`.

## Targeted defect — BUG-ARIA-LABELLEDBY-TEXTCONTENT

```js
// app/inventory_js.py:158-164
function idText(doc, id) {
  var t = doc.getElementById(id);
  return t ? norm(t.textContent) : "";     // ← textContent
}
```

versus the helper the same file wrote for exactly this problem, with a 19-line comment
explaining it:

```js
// app/inventory_js.py:185-191
function accText(el) {
  var t = "";
  try { t = norm(el.innerText); } catch (e) {}    // ← rendered text
  if (!t) { try { t = norm(el.textContent); } catch (e) {} }
  return t;
}
```

`accessibleName()` calls `accText()` for the `label[for]` rung (line 203) and for the
wrapping-`<label>` rung (line 227), but the `aria-labelledby` rung (lines 209-219) calls
`idText()`, which never got the fix.

Downstream consequence is the one already recorded in the source comment for the sibling
case: `get_by_role(name=…)` matches **zero** elements, every fill on the control times out
and is recorded `intent_unmet`, and a generated script binding by that name fails the same
way. `aria-labelledby` pointing at a title+body pair is the standard markup for a
questionnaire question, so this hits question-heavy insurance forms hardest — precisely
the target domain.

## Lane restriction

The block-separation assertions are **Playwright only**. jsdom does not implement
`HTMLElement.innerText`, so `accText()` falls through to `textContent` there and a correct
implementation would be indistinguishable from the defective one. The jsdom lane still
runs the fixture for structural assertions; `expected.json` declares the restriction, and
`test_jsdom_execution.py::test_jsdom_capability_probe` proves the missing API rather than
assuming it.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_playwright_execution.py -k 05-aria-labelledby -v
python -m pytest tests/browser/test_known_bugs.py -k labelledby -v
```
