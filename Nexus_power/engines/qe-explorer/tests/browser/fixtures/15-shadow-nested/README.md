# Fixture 15 — `shadow-nested`

## Purpose

The **anti-overfit variation** for `BUG-SHADOW-NAME`. Fixture 01 proves that one open shadow
root resolves its own labels. This fixture proves the rule is the *general* one — an element's
`label[for]` and `aria-labelledby` references resolve against the root that **owns** it, at any
nesting depth — and, critically, that the fix **scopes** correctly rather than widening the
search to the whole document.

```text
document
├── label[for="account"]  "Outer Account Number"
├── input#account.outer                      ← the decoy: SAME id as one inside the shadow
├── label[for="deep-field"] "Outer Label For A Shadow Id"   ← points INTO a shadow tree
└── beneficiary-panel
    └── shadowRoot
        ├── label[for="account"] "Inner Account Number"
        ├── input#account.inner              ← must take the INNER label
        ├── label[for="share"] ×2            ← first in document order wins
        ├── input#share.pct
        └── inner-panel
            └── shadowRoot
                ├── label[for="deep-field"] "Deep Nested Field"
                └── input#deep-field.deep    ← two boundaries deep
                └── input#deep-aria.deeparia ← aria-labelledby, two boundaries deep
```

## Expected controls

Five. All five are asserted as ordinary `expect_controls`, because after the fix they are
simply correct behaviour rather than aspirational.

The decisive one is `input#account.inner`: it must be named **"Inner Account Number"**.

## Expected manifest

`tests/browser/golden/inventory_15-shadow-nested.json`. Every control is in the main frame
(`frame_selector: ""`) — an open shadow root changes the resolution root, never the selector.

## Targeted defect — BUG-SHADOW-NAME (variation)

`walk()` forwarded the outer `doc` into `host.shadowRoot`, so shadow-scoped id lookups ran
against the host document. Fixture 01 catches that as `name: ""`.

This fixture catches something **worse**, which fixture 01 cannot: a *wrong but plausible*
name. Measured against the pre-fix walker:

| Control | Pre-fix name | Correct name |
|---|---|---|
| `input#account.inner` | `"Outer Account Number"` | `"Inner Account Number"` |
| `input#deep-field.deep` | `"Outer Label For A Shadow Id"` | `"Deep Nested Field"` |
| `input#share.pct` | `""` | `"Share Percentage"` |
| `input#deep-aria.deeparia` | `""` | `"Trust Registered name"` |

An empty name fails loudly. A name borrowed from a same-id element outside the shadow root
binds silently to the **wrong field** at replay. A fix that resolved shadow names by searching
the top-level document would make fixture 01 pass and this one fail — which is the whole
reason this fixture exists.

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer
python -m pytest tests/browser -q -k 15-shadow-nested
```
