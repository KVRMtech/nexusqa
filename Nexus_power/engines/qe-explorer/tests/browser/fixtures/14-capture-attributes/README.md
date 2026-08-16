# Fixture 14 — `capture-attributes`

## Purpose

Isolate ONE capability: emitting the four attributes the **deterministic field classifier**
reads, so that `field_semantics.classify()` rung 1 can fire on a live crawl.

Rung 1 is `autocomplete` — a W3C-standard vocabulary. When an application sets it, the
application has *named the field's semantics itself*, which is why the classifier weights it
above every reading of a label, at confidence 0.98. `inputmode` is a weaker declaration of the
same kind. `placeholder` and `id` are `field_signature.compute()`'s token fallbacks for a
control that has **no accessible name at all**.

All four were read by those consumers and emitted by nothing.

| Control | Declares | Why it is here |
|---|---|---|
| `input#email` | `autocomplete`, `inputmode`, `placeholder` | the primary case, all four attributes at once |
| `input#field-a7` | `autocomplete="given-name"` | **the decisive one** — its label is "Field A7", whose tokens classify to nothing, so only the declaration can name it |
| `input#zip` | `autocomplete="shipping postal-code"` | multi-token; `classify()` scans in reverse |
| `input#phone` | `AUTOCOMPLETE="TEL"` | unusual casing — enumerated keywords are case-insensitive |
| `input#nickname` | nothing | ABSENT must capture as `""` |
| `input#empty-ac` | `autocomplete=""` | PRESENT-BUT-EMPTY must be indistinguishable from absent |
| `input#opted-out` | `autocomplete="off"` | an explicit REFUSAL to declare — must never reach rung 1 |
| `input#x1` | `placeholder` only, no label | the placeholder token fallback |
| `input#date-of-birth` | `id` only, no name of any kind | the id token fallback |
| `textarea` / `select` / `password` / `number` / `cc-number` | assorted | the contract holds across control types, not just text inputs |

## Expected controls

Fifteen visible controls. `expect_controls` asserts only what was already true before the fix
(names, roles, `options_total`), so the generic lane tests stay green either way; the four new
attributes are asserted in `describes_correct_behaviour`, which is what failed before the fix.

## Expected manifest

`tests/browser/golden/inventory_14-capture-attributes.json` and the matching `manifest_`
golden. Every control carries `autocomplete`, `inputmode`, `placeholder` and `id`.

## Targeted defect — BUG-CAPTURE-ATTRS

`describe()` (`inventory_js.py`) emitted none of the four, and `build_control_record()`
(`inventory.py`) did not carry them either — so the fix has to land in **both** layers.

Before the fix, `input#field-a7` produced the signature
`{'autocomplete': '', 'tokens': ['field', 'a7'], ...}` and classified to `free_text` on the
weakest structural rung. Rung 1 was unreachable code on every control a crawl had ever seen,
and the placeholder/id fallbacks for nameless fields were dead.

The acceptance is **not** "the attribute appears in the payload" — it is
`verdict["basis"] == "autocomplete"`, i.e. the classifier actually decided on the declaration.

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer
python -m pytest tests/browser -q -k 14-capture-attributes
python -m pytest tests/browser/test_capture_contract.py -q   # incl. the rung-1 acceptance
```

To see the defect as it was, run the reproductions in hard-failure mode:

```bash
QEC_BUG_REPRO_STRICT=1 python -m pytest tests/browser/test_capture_contract.py -q
```
