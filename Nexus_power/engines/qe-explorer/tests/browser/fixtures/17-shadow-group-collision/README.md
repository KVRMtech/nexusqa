# Fixture 17 — `shadow-group-collision`

## Purpose

Guard a collision introduced by the **CAP-02 shadow-scoping fix itself**.

Scoping accessible-name resolution to the owning shadow root also scopes
`groupContainerKey()`'s last-resort **positional** lookup
(`doc.querySelectorAll("[role=radiogroup],fieldset")`). Two components that each render an
unlabelled radiogroup therefore both index as `0` *within their own root* — and collide.

```text
document
├── question-card#q-tobacco
│   └── shadowRoot → div[role=radiogroup]  (no id, no aria-label, no aria-labelledby)
│                    ├── div[role=radio] "Tobacco Yes"
│                    └── div[role=radio] "Tobacco No"
└── question-card#q-alcohol
    └── shadowRoot → div[role=radiogroup]  (likewise anonymous)
                     ├── div[role=radio] "Alcohol Yes"
                     └── div[role=radio] "Alcohol No"
```

Neither radiogroup carries an `id`, `aria-label` or `aria-labelledby`, and neither set of
radios carries a `name` attribute — so the positional key is the only handle either question
has. An ARIA card set with no `name` attribute is exactly how a design-system questionnaire is
built, so this is the shape that would hit it in the field.

## Expected controls

Four `role="radio"` controls, all in the main frame, forming **two** questions of **two**
answers each. The assertion that matters is `group_key_partition`: `expect_distinct_nonempty:
2` and `expect_group_sizes: 2`.

## Expected manifest

`tests/browser/golden/inventory_17-shadow-group-collision.json`. The two group keys are
host-qualified:

```text
grp:question-card#q-tobacco:1|ix:0
grp:question-card#q-alcohol:2|ix:0
```

The prefix is the shadow **host chain** (`tag#id:sibling-index`). It is empty for the main
document, so every light-DOM group key stays byte-identical to what it has always been — which
is not cosmetic: `group_id` hashes key the remembered branch-walk overrides a previous crawl
recorded, and re-hashing them would orphan every recorded plan.

## Targeted defect — regression introduced by the CAP-02 fix

Measured with the root discriminator disabled:

```text
WITHOUT root discriminator   distinct=1   keys=['grp:ix:0']
WITH    root discriminator   distinct=2   keys=['grp:question-card#q-alcohol:2|ix:0',
                                                'grp:question-card#q-tobacco:1|ix:0']
```

With one key, `GROUP_ASSEMBLE` merges the two radiogroups into a single question offering four
answers — so the crawl records a form the application does not have, and answers one question
with the other's option. The same discriminator also closes a **pre-existing** collision: an
`id`-keyed container inside a shadow root previously collided with an identically-id'd one
outside it, which is the very thing shadow encapsulation exists to allow.

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer
python -m pytest tests/browser -q -k 17-shadow-group-collision
```
