# Fixture 13 — `canvas`

## Purpose

Isolate ONE capability: handling a **DOM-opaque rendered surface** honestly, and keeping the
three injected snippets in their lanes.

A canvas has no DOM controls to read. The failure mode is not missing it — it is letting
it produce either a *silently clean* scan (nothing captured, nothing said) or a *fabricated*
capture. The correct outcome is a named ledger row plus complete capture of everything else
on the page.

## Expected controls

`INVENTORY_JS` captures **two**: `#illustration-age` and `#recalculate`. No canvas, and no
premium figure — a `<dd>` is not an interactive control.

`OPAQUE_JS` produces **two** rows (Playwright lane):

| Canvas | Area | Ledgered? | Label |
|---|---|---|---|
| `#premium-chart` 640×360 | 230,400 px | yes | `Premium projection chart` (its `aria-label`) |
| `#signature-pad` 400×160 | 64,000 px | yes | `canvas region` (no aria-label → generic) |
| `#sparkline` 120×24 | 2,880 px | **no** | under the 40,000 px gate — decorative chrome |

`DISPLAYED_VALUES_JS` produces **two**: `$1,284.00` labelled `Annual Premium`, `$38,520.00`
labelled `Total Paid at 65` — resolved through the `<dt>`/`<dd>` pairing branch.

## The three-snippet separation this fixture pins

| Snippet | Answers | On this page |
|---|---|---|
| `INVENTORY_JS` | what can I interact with? | 2 controls |
| `OPAQUE_JS` | what can I not read at all? | 2 canvases |
| `DISPLAYED_VALUES_JS` | what value is being shown? | 2 amounts |

A regression that let any one of them answer another's question would break this fixture.

## Why `#sparkline` is here

A ledger full of decorative chrome is as useless as an empty one. The 40,000 px gate is what
keeps the opaque ledger meaningful, and `#sparkline` is the subject that proves the gate is
still applied.

## Lane restriction

The `OPAQUE_JS` assertions are **Playwright only**: the detector gates on
`getBoundingClientRect()` area, and jsdom has no layout engine, so every rect is zero and
*nothing* would ever be detected. Asserting it in jsdom would be asserting a false negative
— the fixture says so in `expected.json` instead of quietly not running.

`DISPLAYED_VALUES_JS` runs in both lanes: it gates on text content and `display`, not on
geometry.

## Expected manifest

`tests/browser/golden/manifest_13-canvas.json`.

## The seven label rungs

`DISPLAYED_VALUES_JS` exists so a value oracle can ground an expected outcome **without** a
client-authored `source_hint`. That grounding is only sound if the label actually belongs to
the figure, so `labelOf()` tries seven ways in order. All seven are on this page:

| Rung | Markup | Value | Resolves to |
|---|---|---|---|
| 1 | `aria-label` | `$107.00` | `Monthly Premium` |
| 2 | `<dd>` after `<dt>` | `$1,284.00` | `Annual Premium` |
| 3 | `aria-labelledby` | `$14,905.00` | `Cash Value at 20 Years` |
| 4 | previous sibling | `$500,000.00` | `Death Benefit` |
| 5 | parent's previous sibling | `$1,250.00` | `Surrender Charge` |
| 6 | labelled element found by class in the parent | `$7.50` | `Policy Fee` |
| 7 | **nothing to go on** | `$99.99` | must be `""` |

Six resolve correctly. The seventh is a defect.

## Targeted defect — BUG-VALUE-LABEL-BLEED

`$99.99` has no label of any kind on the page. It is captured labelled **`Surrender
Charge`** — which belongs to `$1,250.00`, two blocks earlier.

The sibling scan is what does it. `labelOf()` walks up to 3 previous siblings and 2 of the
parent's previous siblings, **skipping any whose text looks like a value**. So it rejects
the `$7.50` block, rejects the `$1,250.00` block, and keeps walking until it reaches prose
that belongs to neither.

Consequence: the value oracle grounds `Surrender Charge == $99.99` on a page that states
`Surrender Charge = $1,250.00`. That is a *confident wrong answer* produced from correct
capture of the number and incorrect capture of what it means — strictly worse than no
grounding, which degrades honestly to `UNVERIFIED`.

Correct behaviour: a label may be borrowed from a sibling only while nothing between them
is itself a value. Once the scan passes a figure, the prose beyond it belongs to that
figure. An unlabelled value gets `label: ""`.

Reproduced by
`test_known_bugs.py::test_an_unlabelled_value_is_not_given_someone_elses_label`, which also
asserts the six correct rungs as a control group — if those break, the diagnosis is wrong
and the problem is broader than the scan distance.

Found by this harness while closing the `DISPLAYED_VALUES_JS` coverage gap: before rungs
3–7 had fixtures, that whole fallback chain was unexecuted code.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_jsdom_execution.py -k 13-canvas -v
python -m pytest tests/browser/test_playwright_execution.py -k 13-canvas -v
```
