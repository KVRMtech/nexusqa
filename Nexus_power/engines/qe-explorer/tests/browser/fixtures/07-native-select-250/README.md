# Fixture 07 — `native-select-250`

## Purpose

Isolate ONE capability: reading a native `<select>`'s **complete answer set**, and reporting
honestly when the answer set exceeds the capture ceiling.

The enumeration a `<select>` offers *is* the test data for the positive, negative and
boundary cases generated from that question. A truncated enumeration silently produces a
truncated test suite.

## Expected controls

| Control | Options | `options_total` | Branch under test |
|---|---|---|---|
| `#country` | 250 | 250 | complete read, well past the old 60 ceiling |
| `#birth-year` | **300** | **320** | clipped read — the gap is the honesty signal |
| `#state` | 4 | 4 | unset placeholder option counted, `value_committed` `""` |
| `#riders` | 2 | 2 | `multiple` → `implicitRole()` must be `listbox`, not `combobox` |

`expected.json` asserts list *lengths* and *first/last edges* rather than all 250 strings —
that catches truncation, reordering and off-by-one at both ends without a 250-line
assertion nobody would maintain. The full list is byte-compared by the characterization
golden.

## Expected manifest

`tests/browser/golden/manifest_07-native-select-250.json`.

## Targeted defect

None — **regression guard** for the `MAX_OPTIONS` contract (`inventory_js.py:90-97`):

> Sized for COMPLETENESS of the enumerations a business form actually asks: 50 US states,
> ~250 countries, a 100-year date-of-birth range. The previous 60 silently truncated every
> one of those but the states.

`#birth-year` is the important subject: it is the only fixture that makes `options.length`
and `options_total` disagree. If a refactor ever dropped `options_total`, or set it to
`options.length`, every over-ceiling question in the fleet would start reporting a prefix
as the complete set of answers and nothing else in the suite would notice.

## Defect this fixture UNCOVERED — BUG-CATALOG-TRUNCATION-60

Recording this fixture's characterization golden surfaced a defect nothing in the
repository was testing for:

```
walker  (inventory_js.py, MAX_OPTIONS = 300)  → 250 options captured  ✅
refiner (inventory.py:123, _MAX_OPTIONS = 60) →  60 options kept      ❌
manifest form_snapshot_signals["Country of Residence"].options → 60
```

`inventory_js.py:90-97` raised its ceiling from 60 to 300 with an explicit rationale —
*"The previous 60 silently truncated every one of those but the states"* — but
`app/inventory.py:123` still holds `_MAX_OPTIONS = 60` and re-truncates the list before it
reaches `form_snapshot_signals`, which is what the catalogue and the scenario deriver
actually read. The JS-side fix is nullified one layer down.

Reproduced by `test_known_bugs.py::test_catalogue_completeness_survives_the_refiner`.
Not fixed here — M0.2 builds the harness; the fix belongs to Capture.

This is also why the characterization suite records **two** goldens per fixture: the
manifest golden alone would have been blind to it.

## Regenerating

The HTML is generated deterministically (no clock, no randomness) — see the generator
recorded in `tests/browser/fixtures/README.md`. It is committed as a static file so the
fixture is byte-identical on every machine.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_jsdom_execution.py -k 07-native-select -v
python -m pytest tests/browser/test_playwright_execution.py -k 07-native-select -v
```
