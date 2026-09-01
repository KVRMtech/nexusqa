# Tasks 1 & 2 — what moved, and the final blockers

**Crossings remain 1 of 3.** acme crosses. summit now crosses its commit control
but observes no confirmation. vkpower does not cross and now names why.

| | before | after | SHA |
|---|---|---|---|
| **summit** | `crossed: 0`, never reached the wizard | **`crossed: 1 ['Submit Application']`**, `confirmation_observed: False` | `6aedd6a` |
| **vkpower** | `crossed: 0`, silent stall at payment | `crossed: 0`, payment **solved**, beneficiary reached, 2 blockers named | `6aedd6a` |

Neither number was forced. Both runs are recorded exactly as they came out.

---

## BLOCKER A — summit: the wizard's own field instances are not reachable by the seed seam

**The measurement that settles it.** Four seeded rounds. Round 4 used values read
directly off the app's `applicationSchema` (SSN and ZIP regexes, a real email,
`faceAmount >= 10000`). Result, unchanged from round 3:

```
crossed              : 1 ['Submit Application']
confirmation observed: False
outcome_milestone    : outcome "none", navigated false, url_after == url_before
/api/v1/ calls fired : 0        (against 1333 network events observed)
```

`<form onSubmit={form.handleSubmit(handleSubmit)}>` validates the whole zod
schema first. It rejects, so `handleSubmit` never runs, so the API cascade never
fires, so `ApiCallTracker` never renders its "Complete" banner. **The crossing is
a real click with no effect** — which is why `confirmation_observed` is correctly
False and must not be argued around.

**Why more seeding will not fix it.** The seeds DID apply — every one shows
`provenance: recalled`. But seeding is keyed by a value-free field SIGNATURE, and
the wizard's instances hash differently from same-named fields elsewhere in the
app:

* `Face Amount ($)` on the wizard vs `Face Amount` on `/actuarial/product-pricing`
  — different label, different signature. Wizard copy stayed **synthesized**, and
  the schema demands `>= 10000`.
* `Last Physical Exam` on the wizard vs `Last Exam Date` — same, stayed
  **synthesized**.
* Worse, the wizard's step-0 fields — **First Name, Last Name, Date of Birth,
  Email Address, Gender** — appear in `form_snapshot_signals` but are **absent
  from the field ledger entirely**. They were never filled or recorded, so they
  have no signature to seed against. `Gender` is the same unfillable custom
  combobox R9 documented (`options: []`, rendered only on open), and
  `gender: z.enum(['male','female'])` is required.

So a fifth round cannot succeed: the fields that fail validation are the ones the
seed seam cannot address. **Seeding is the wrong instrument for this blocker.**

**What would fix it:** make the wizard's own instances fillable — the custom
combobox opener R9 named — or key operator seeds by (label, url) as well as
signature. Both are product work; neither is data.

## BLOCKER B — vkpower: beneficiary needs a sub-action plus a numeric rule

The payment step is **solved**. The card-grid driver clicked through the grid and
on attempt 5 `Credit / Debit Card` returned `cleared=True` — the application
itself re-enabled Continue — and `/apply/beneficiary/` was reached for the first
time. Two blockers are now first-class on the coverage ledger instead of a silent
stop:

```
Continue to Beneficiary Designation   advance_disabled_by_app_validation   (payment)
Continue to Signature                 advance_clicked_but_app_declined     (beneficiary)
```

`handleSubmit` on the beneficiary step refuses unless primary allocations total
exactly 100%, and a beneficiary must first be committed with **Add Beneficiary**.
Filling the visible fields is not enough: it needs a **sub-action, then a numeric
constraint across a list**. That is a third widget class, beyond a card grid.

Beyond it, `/apply/signature/` has still never been reached — five consent
checkboxes plus a typed signature matching a legal name entered seven steps
earlier.

## BLOCKER C — the reporting gap underneath both

`fields_needing_seed` means *"could not be filled"*, never *"filled with something
the application rejected"*. Both apps stop on the second case. summit's list
converged 6 → 4 → 8 → **1** across four rounds while the actual blockers — a
synthesized `Face Amount ($)` and an unrecorded `Gender` — never appeared on it
at all.

This is the same class as the 2026-08-21 Member Number finding, and it is now the
highest-leverage item left: without it, every future blocker of this shape costs
the same four blind rounds.

---

## Decisions for the ARB

1. **Is a crossing with `outcome: none` admissible?** summit clicks its commit
   control and the app does nothing. If the effect is required — and the T3 gate's
   wording says it is — summit is not one seeding round away, it is Blocker A away.
2. **Who owns Blocker C?** It unblocks both applications and prevents the next
   instance costing what this one did.
3. **Is 3/3 still the gate**, given vkpower needs two further widget classes
   (list sub-action + cross-step signature coherence) that are product work?

## What was not done

No application was modified. No crossing was fabricated. Seeding stopped after
four rounds because the fifth could not have worked, and saying so is worth more
than another run.
