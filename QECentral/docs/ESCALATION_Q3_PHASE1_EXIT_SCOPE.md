# ESCALATION — Q3: is 3-of-3 still Phase 1's exit?

**For: the human programme owner and the ARB. This is a scope decision, not an
engineering one, and it decides whether Gate 5 is days or weeks away.**

The team is not asking to lower a bar. It is asking which bar it is measuring
against, because the two answers imply different work and only one of them is
schedulable this week.

---

## Where the number actually is

| app | crossing | confirmation | what stands between here and both |
|---|---|---|---|
| **acme-life** | ✅ 2 | ✅ `dialog` | nothing — admissible today |
| **summit-life-carrier** | ✅ 1 `Submit Application` | ❌ | client-side schema rejects the submit; **0** `/api/v1/` calls fire |
| **vkpower-life** | ❌ 0 | ❌ | two widget classes, below |

**1 of 3 admissible.** summit's crossing is real as a click and empty as an
effect, so the T3 gate refuses it — correctly, and the team is not arguing with
that.

## What is genuinely left, costed honestly

**summit — one capability.** The submit is rejected by the app's own zod schema
before its handler runs. The two offending values (`Face Amount ($)` synthesized
below the `>= 10000` floor; `Gender` never recorded at all) are invisible to
`fields_needing_seed` by construction. The reporting half of that — name the
rejected field and the rule the app cited — **is being built now** and is the
one build the team was told matters this week. Once a run names the field,
summit is a seed away, not a capability away.

**vkpower — two capabilities, and they are product work.**

1. **List sub-action + numeric rule.** The beneficiary step needs a beneficiary
   committed via `Add Beneficiary` *and* allocations totalling exactly 100%.
   Filling the visible fields does nothing; `handleSubmit` refuses silently.
2. **Cross-step coherence.** `/apply/signature/` — never reached — needs five
   consent checkboxes plus a typed signature *matching a legal name entered
   seven steps earlier*.

The payment step that blocked vkpower for weeks **is solved** (the card-grid
driver made the app itself re-enable Continue). These two are what remain, and
neither is a seed, a config, or a bug fix.

---

## FORK A — hold 3-of-3

Phase 1 exits only when three first-party applications each produce a crossing
with an observed confirmation.

* **Cost:** the summit reporting build (in progress), then **two new crawler
  capabilities** for vkpower — list sub-actions with a numeric constraint, and
  cross-step value coherence. Each is the same order of work as the card-grid
  driver, which took a day plus seven red-team rounds to get right.
* **Schedule:** **weeks**, and the second capability (coherence across seven
  steps) has no precedent in the codebase to copy.
* **What it buys:** the exit criterion means exactly what it says. No asterisk in
  the certification record, and the product is demonstrably able to complete a
  real funnel on three unrelated applications.
* **Risk:** the two capabilities are specified by two applications. Building to
  them risks fitting the crawler to these funnels rather than to the class — the
  thing the R7 rounds kept punishing.

## FORK B — re-scope the exit to what is proven, in writing

Phase 1 exits on **acme (crossing + confirmation)** plus **summit and vkpower
each producing a named, first-class blocker** on the coverage ledger — with the
residual gap recorded as a deferred item carrying an owner and a trigger.

* **Cost:** the summit reporting build, and nothing else. vkpower already
  satisfies it — its two blockers are on the ledger now, not in a log:
  ```
  Continue to Beneficiary Designation   advance_disabled_by_app_validation
  Continue to Signature                 advance_clicked_but_app_declined
  ```
* **Schedule:** **days.**
* **What it buys:** Gate 5 becomes reachable, and the claim made is one the
  evidence actually supports — *"the crawler walks three real funnels and, where
  it cannot finish, says precisely why"* — which is closer to the product's
  stated value than a crossing count is.
* **Cost of it, stated plainly:** the programme has said "a crossing on three
  first-party apps" for a long time. Changing that at the gate is exactly the
  move this repository's culture is most suspicious of, and it must be recorded
  as a **re-scope with a named owner**, never as a quiet redefinition. If it is
  taken, the certification record has to say Phase 1 exited on 1 crossing and 2
  named blockers, in those words.

---

## What the team recommends, and what it will not do

The team does **not** have a recommendation here, deliberately. Fork B is
cheaper and Fork A is what was promised, and choosing between "what we said" and
"what we can prove this month" is the owner's call, not the implementer's.

What it will not do either way: force the number. summit's `outcome: none` and
vkpower's `crossed: 0` are recorded as they came out, and no seeding round will
be run that the team already knows cannot work.

**Needed to proceed:** one fork, chosen, with a name against it.
