# Cascade Life — questionnaire proving ground (M2.1)

A single-page underwriting questionnaire, built to be the proving ground for one
claim: **the catalogue holds the questions this application actually asks, in the
words it asks them, once each.**

It is static (`index.html`, no build), so it is crawlable both in CI and on a
developer machine — a proving ground that has never been crawled is a plan.

## What each subject isolates

| Subject | Markup | What it proves |
|---|---|---|
| **A** — bare-button questions | `<fieldset><legend>` + `<button>Yes/No` | T-QT-01. This is the shape that produced `"Question 1"…"Question N"`: the answers are buttons, so no choice-grouping applied and the only handle on a question was its DOM ordinal. The catalogue must now carry the `<legend>` sentences. |
| **A′** — the trigger | tobacco = **Yes** reveals `#q-cigarettes` | T-QT-03. Exactly one question on this page reveals anything, so a reveal recorded anywhere else is a fabrication and this ground will catch it. |
| **B** — a question with no wording | `<fieldset>` with no legend, no `aria-label`, no heading | T-QT-01's other half. The honest entry is **UNVERIFIED** — catalogued, answerable, stably identified, and *not* given invented text. |
| **C** — choice groups | native radios, an ARIA `role=radiogroup` card set, a checkbox group | T-QT-02 / T-QT-04. `Gender → [Female, Male, Prefer not to say]` must be ONE question carrying three answers, not three questions named after the answers plus a fourth from the branch rows. |
| **D** — ungrouped controls | a number input, a `<select>`, a lone checkbox | The regression guard. Each of these IS its own question and its accessible name IS the wording; folding must not touch them. |

## The questions this application asks

Nine, and this is the number the catalogue must report:

1. Have you used tobacco or nicotine products in the last 12 months?
2. How many cigarettes per day do you smoke? *(revealed by 1 = Yes)*
3. Do you consume more than 14 units of alcohol per week?
4. Do you take part in scuba diving, motorsport or private aviation?
5. *(no wording declared — UNVERIFIED)*
6. Gender
7. Which product are you applying for?
8. Which of these have you been diagnosed with?
9. …plus `Height in centimetres`, `State of residence`, `I consent to a medical
   records check` as ungrouped controls.

Before M2.1 the same page catalogued as ~20 rows: four `"Question N"` inventions,
`Female` / `Male` / `Prefer not to say` as three separate questions offering no
answers, and a duplicate of each group minted from its own branch rows.

## Run it

```bash
# served straight from source — no build, no Docker
python -m http.server 8099 --directory .
# → http://localhost:8099
```

The crawl lane that uses it is
`tests/browser/test_questionnaire_catalog_e2e.py` (marker: `proving_ground`).
