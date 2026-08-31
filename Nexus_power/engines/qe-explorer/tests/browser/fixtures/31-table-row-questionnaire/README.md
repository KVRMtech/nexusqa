# Fixture 31 — the table-row questionnaire

## Purpose

A health questionnaire rendered the way a great many real ones are: a `<table>`,
one question per row, the wording in the row's label cell and the answers as
bare `<button>`s beside it. No `<fieldset>`, no `role=group`, no
`role=radiogroup` — none of the containers the M2.1 question ladder knew about.

This fixture exists to prove such a page is catalogued in the **application's
own words**, and — just as importantly — that the four table rows which are
*not* questions still produce no wording at all.

## Targeted defect

`BUG-QUESTION-ROW-BLIND`. `questionContainerOf` recognised only ARIA/HTML
grouping elements. On this layout it climbed past the row, found nothing, and
returned `""`. Downstream:

* `question_label` empty → the catalogue stamped the question `UNVERIFIED`;
* `question_key` empty → `QUESTION_ASSEMBLE` never grouped the pair, so each
  answer button stood alone and the question could only be identified by its
  DOM ordinal.

The application's own wording was one cell away in the page the entire time.

### Why the row is a DECLARED container, not proximity

The row is the DOM's own grouping of one question with its answers — the same
relationship `<fieldset>` states, spelled the way table markup spells it, and
the one `<th scope="row">` exists to declare. This fixture uses `<th scope=row>`
for the first question and a plain `<td>` for the second, because both are the
page stating a row header.

It is **not** "the text just above", which the ladder still refuses.

## Expected controls

Twelve, exactly. Eight expectations are declared in `expected.json`, and **four
of them are cases that must produce no wording** — they are what stops this
fixture from passing for the wrong reason:

| Case | Row | Must yield |
|------|-----|------------|
| A | label cell + two answer buttons | the row's wording, `question_label_source: row-label`, and a `question_key` derived from the wording rather than the position |
| B | one control beside a label cell | a FIELD — accessible-name rung 8 names it from that same cell, `question_label` stays `""` |
| C | `<thead>` filter row, two controls | `""` — a filter is not a question |
| D | first cell holds a control | `""` — that is not a label cell |
| E | `<fieldset>` inside the cell | the `<legend>`, never the row's wording |

Case E's row wording is deliberately wrong ("Row wording that must NOT be
used"), so a rung that displaced the stronger declaration fails loudly rather
than passing on a string that happens to look plausible.

## Expected manifest

Nothing beyond the control inventory. The question this fixture asks is settled
entirely in `question_label` / `question_label_source` / `question_key`, which
`INVENTORY_JS` emits per control; no navigation, network or lifecycle behaviour
is involved.

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer
PYTHONPATH=. python -m pytest tests/browser/test_jsdom_execution.py \
  -q -p no:cacheprovider -p no:randomly -k "31-table-row-questionnaire"
```

To see it go red, make `questionContainerOf` return `null` instead of
`questionRowOf(el)` — case A's `question_label` comes back `''`, which is the
defect above.
