# Meridian Life — the M2.2 catalogue-evidence proving ground

A real, static, self-contained application whose behaviour the **catalogue** has
to be able to describe from evidence alone.

## Why a fourth proving ground

`acme-life`, `vkpower-life` and `summit-life-carrier` are crawled by
`tests/browser/test_proving_grounds.py` with `observe_only=True` — deliberately,
because they are maintained for other purposes and a crawl must never mutate
them. But `observe_only` disables form filling outright (`discovery.py`:
`is_form`), and with no filling there is no ACT-THEN-DIFF and no unblock
experiment. The two behaviours M2.2 most needs to prove — a dependency the page
does not declare, and a rule that exists nowhere in the markup — are therefore
*structurally* unobservable in that lane.

This ground exists to be filled. It holds no state worth protecting, its submit
does nothing but render a sentence, and it is crawled by
`tests/browser/test_m22_catalog_evidence.py` with `observe_only=False`.

## What it asks the catalogue to prove

| # | Behaviour | Declared in the markup? | Milestone |
|---|---|---|---|
| 1 | `Continue to review` is disabled until **one specific** checkbox is answered | **No** — the rule lives in `refreshGate()` | T-BR-01 |
| 2 | `County` is empty until `State of residence` is chosen | **No** — nothing links the two elements | T-BR-02 |
| 3 | `Country of citizenship` offers 250 options + a placeholder | yes | T-BR-05 |
| 4 | `Face amount` declares min/max/step; `Existing policy number` a pattern + length | yes | T-BR-04 |
| 5 | Handles of every strength — testid, id, label-only, and **none at all** | partly | T-BR-03 |
| 6 | Two tobacco radios are ONE question with two answers | yes | T-BR-03 |

Rows 1 and 2 are the point. Neither can be learned by reading the DOM, so a
catalogue that reports them has run an experiment, and a catalogue that reports
them *without* running one is fabricating.

## The control group

`Send me product updates` is an ordinary optional checkbox that gates nothing
and depends on nothing, sitting directly beside the one that gates everything.
If it ever acquires a business rule, the rule join is matching on **shape** — a
checkbox near a disabled button — rather than on what the application did.

Likewise the unlabelled referral-code input: it carries no id, no `data-testid`,
no label association and no class. The correct outcome is that it does not
become a catalogue question at all (a question with no text is not a row anyone
can review) — and specifically **not** that it is kept and given a positional
selector nothing ever resolved.

## Running it

```bash
# from engines/qe-explorer
python -m pytest tests/browser/test_m22_catalog_evidence.py -q

# re-record the coverage qe-central's half of the proof reads
QEC_M22_RECAPTURE=1 python -m pytest tests/browser/test_m22_catalog_evidence.py -q
```

The captured coverage lands at
`platform/qe-central/tests/contract/fixtures/m22_real_crawl_coverage.json`, where
`test_m22_catalog_from_real_crawl.py` runs it through the production catalogue
path. The two services cannot be imported into one interpreter — both ship a
top-level `app` package — so the proof is written in halves, and that file is
what ties the halves to the same crawl.

## Rules

Deterministic: no clock, no randomness, no network beyond the fixture server.
The 250-country list is generated from a fixed formula so the count is a fact of
the file rather than a transcription anyone has to trust.

**Editing this file invalidates the recorded crawl.** Re-record with the flag
above; a capture that describes an application that no longer exists is not
evidence of anything.
