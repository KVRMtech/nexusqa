# Fixture 18 — `select-edge-cases`

## Purpose

Fixture 07 pins the option ceiling **at scale** (250 and 320 options against a ceiling of
300). This fixture pins the **boundaries and the read-back** — the shapes at which a clipping
or value bug hides while a 250-option assertion sails past it.

| Control | Shape | What would otherwise hide here |
|---|---|---|
| `select#empty` | 0 options | an empty enumeration dropped as "not really a select" |
| `select#single` | 1 option | off-by-one in the capture loop |
| `select#late-selected` | 60 options, the **60th** selected | a `value_committed` that degraded to "the first option" |
| `select#duplicates` | 4 options, 3 with identical text | a de-duplicated answer set reporting 4 answers as 2 |
| `select#long-labels` | one 422-char label | non-deterministic or back-end clipping |
| `select#with-disabled` | one disabled `<option>` | a disabled answer silently dropped from the enumeration |
| `select#disabled-select` | disabled `<select>` | conflating a disabled control with an empty one |

## Expected controls

Seven native selects, all in the main frame. Every one asserts `options_total`,
`len(options)` and — where a selection exists — `value_committed`.

The load-bearing one is `select#late-selected`: the committed value is `opt060`, the **last**
of sixty. A read that degraded to the first option would report `opt001`, and every
verification generated from that question would assert the wrong answer while looking
perfectly green.

`select#duplicates` is the second: three options render the identical string `"Other"`.
They are three distinct answers, and `options_total` must be 4. Collapsing them would
describe a form the application does not have.

`select#long-labels` pins the per-option clip at `MAX_OPTION` (200), taken from the **front**
so a truncated label is still identifiable.

## Expected manifest

`tests/browser/golden/inventory_18-select-edge-cases.json` and the matching `manifest_`
golden.

## Targeted defect — regression guard for BUG-CATALOG-TRUNCATION-60

No live defect is reproduced here. This fixture exists because the option ceiling was
unified from three accidental values (300 → 60 → 48) to one documented value (300), and a
ceiling is only as trustworthy as its boundaries. The counts `0, 1, 10, 48, 49, 60, 61, 250,
299, 300, 301` are swept through the refiner in
`test_capture_contract.py::test_refiner_option_counts_are_exact_up_to_the_ceiling`; this
fixture is the browser-level half — 48/49/60/61 are precisely where each of the three
historical ceilings would have revealed itself.

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer
python -m pytest tests/browser -q -k 18-select-edge-cases
```
