# Fixture 09 — `questionnaire-20-samefingerprint`

## Purpose

Isolate ONE capability: keeping **20 structurally indistinguishable questions distinct**.

Every one of the 40 radios on this page has:

* the same role — `radio`
* one of only two accessible names — `Yes` or `No`
* the same tag, the same input type, the same wrapping-label naming rung

So the `(role, name)` fingerprint of the whole page collapses to **two** values. The only
thing that tells question 3 from question 17 is `group_key`.

This is the adversarial case for any dedupe-by-fingerprint logic anywhere between the
walker and the manifest — and it is not hypothetical: a health questionnaire with 20
yes/no questions is the single most common page shape in the target domain.

## Expected controls

41: forty radios plus one `Continue` button.

Three properties are asserted, and each catches a different failure:

1. `exact_control_count: 41` — catches wholesale loss.
2. `group_key_partition: 20 groups × 2` — catches a **collapse** (questions merged) and a
   **shatter** (a question split). This is the load-bearing assertion.
3. `name_fingerprint_collapse: 2 distinct (role, name) pairs` — records the adversarial
   property itself, so if a future fixture edit accidentally made the names unique, the
   fixture would stop testing what it claims to test and would say so.

## Expected manifest

`tests/browser/golden/manifest_09-questionnaire-20-samefingerprint.json`.

## Targeted defect

None — **regression guard**. The radios are not inside a `<form>`, so `el.form` is null and
`groupKeyOf()` uses the `doc` placeholder: keys are `name:doc:q01` … `name:doc:q20`.

## Regenerating

Deterministically generated (fixed question list, no clock, no randomness) and committed as
a static file — see `tests/browser/fixtures/README.md`.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_jsdom_execution.py -k 09-questionnaire -v
python -m pytest tests/browser/test_playwright_execution.py -k 09-questionnaire -v
```
