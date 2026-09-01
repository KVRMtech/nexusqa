# R9 — why vkpower-life stalls at the payment step

**Diagnosis: the payment step is a FORMLESS picker. The crawler sees fourteen
controls and recognises ZERO questions, so nothing can be filled, nothing is
reported missing, and the stall is silent.**

Confirms the F6 "formless plan-picker" hypothesis, and is sharper than it: the
step does not merely resist filling, it is **never recognised as a question at
all**, which is why no diagnostic names it.

| | |
| --- | --- |
| Diagnosed at | `d3ed533`, from the R1 crawl bundle produced at `8c443f2` |
| Evidence | `Nexus_power/evidence/gate2/r1_vkpower_live/` (+ crawl coverage) |
| Supersedes | the retracted claim that `rp.verb.transfer` on the ACH label blocked R1 |

---

## 1 · The measurement

From the R1 crawl's own coverage, the payment state:

```
location          http://127.0.0.1:8101/life-insurance/apply/payment/
controls_total    14
danger_controls   2   ['Sign out', 'ACH Bank Transfer Direct debit from checking or savings']
question_groups   []          <-- empty
form_snapshot_signals  {}     <-- empty
displayed_values  []
field_ledger entries for this url:  NONE
fields_needing_seed:        []
fields_needing_seed_detail: []
advance_blocked entries for this url:  NONE
```

Fourteen controls, zero questions, zero fields, **and no `advance_blocked`
record**. The crawl stood on the step that ends its journey and recorded nothing
about why it could not pass.

## 2 · The contrast that isolates the cause

The same application has a picker that **works**, two steps earlier. Same visual
idiom — a grid of selectable cards — different DOM:

| | `/quote/start/` (product) | `/apply/payment/` (method) |
| --- | --- | --- |
| markup | `<input type="radio" name="product">` | `<button type="button" onClick={updatePayment({method:'ach'})}>` |
| `question_groups` | **1**, members `kind: "radio"` | **0** |
| `form_snapshot_signals` | populated, `bindable: true`, grouped by `group_id` | `{}` |
| `advance_blocked` | recorded, with the business rule | none |
| outcome | **resolved by the agent** — `resolved_by_agent: "Term Life Insurance…"` | silent stall |

The product picker is a real radio group, so it becomes a question, the blocked
advance is recorded with its rule, and the tier-3 agent answers it. The payment
picker is bare buttons with no form semantics, so it becomes nothing.

`vkpower-life/src/app/life-insurance/apply/payment/page.tsx`:

```tsx
<button type="button" onClick={() => updatePayment({ method: 'ach' })}>…</button>
<button type="button" onClick={() => updatePayment({ method: 'card' })}>…</button>
…
<button type="submit" className="btn-primary" disabled={!method}>
  Continue to Beneficiary Designation
</button>
```

`disabled={!method}` — the funnel cannot advance until a method is chosen, and
choosing one is not something the fill engine can see to do.

## 3 · What this is NOT

* **NOT the refuse pack.** `Credit / Debit Card` is `danger=False` and
  `Continue to Beneficiary Designation` is `danger=False`; only the ACH card is
  flagged (`rp.verb.transfer`, on its own label). **A safe path past the step
  existed and was not taken**, which is what rules out the pack as the cause and
  is why the ACH whitelist was withdrawn.
* **NOT the state-collapse defect.** 19 states / 19 distinct identities, all four
  same-URL pairs split.
* **NOT the login,** which completed through member-number → password → PIN.
* **NOT the Member Number,** which this crawl walked past — it reached seven
  funnel steps, against a historical baseline that dead-ended at step 1.

## 4 · The reporting half, which is the worse half

The vkpower Member Number case (2026-08-21) was a field that existed and was
mis-typed: `fields_needing_seed` stayed empty because the value *was* filled,
just wrongly. **This is a degree worse.** There is no field at all, so:

* the fill engine has nothing to fill;
* `field_ledger` records nothing for the URL;
* `fields_needing_seed` is empty and *correct* — nothing failed to fill;
* no question group exists, so no `advance_blocked` record is produced.

Every reporting channel is silent, and each is silent *correctly by its own
definition*. The crawl is honest that it did not complete and has no vocabulary
to say why. **A step that cannot advance and cannot say so is the defect**, over
and above the picker itself.

## 5 · Remedies, in order of preference

1. **Recognise formless pickers as questions.** A set of sibling, mutually
   exclusive `type="button"` controls that mutate shared state and gate a
   disabled submit is a radio group in everything but DOM. Recognising it feeds
   the existing machinery — question group → `advance_blocked` → tier-3 agent —
   which is already proven to answer the equivalent question at `/quote/start/`.
2. **Fail loudly when a step is unadvanceable and unexplained.** Independent of
   (1): if a walk stops on a step where no advance succeeded and no
   `advance_blocked` was recorded, that is a reportable condition. It would have
   named this step on the first crawl, and would name the next instance of a
   class we have not thought of.

(2) is worth doing even if (1) lands, because (1) fixes this shape and (2) is
what tells us about the shape after it.

## 6 · Not claimed

* Not claimed that fixing this makes R1 cross — the signature step beyond it is
  gated on five consent checkboxes plus a typed name matching an entry seven
  steps earlier, and has never been reached.
* Not claimed that (1) is implemented. This record is a diagnosis; neither remedy
  is built.
* The measurement is from a crawl produced at `8c443f2`; the payment page markup
  and the classification results were re-verified at `d3ed533`.
