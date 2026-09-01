# 27 — `wizard-20-step-samefingerprint`

## Purpose

Isolate ONE capability: **knowing that the walk moved**, across twenty steps that
give it no help whatsoever.

Fixture 09 already puts twenty structurally indistinguishable questions on a page
and asks whether capture keeps them apart. This fixture asks the opposite
question, and it is the one the target domain actually poses: the twenty
questions are on **twenty separate steps**, one at a time, and every step renders
exactly the same four controls —

* `Yes` (radio)
* `No` (radio)
* `Back` (button)
* `Continue` (button)

so the `(role, name)` fingerprint of the whole page is **identical on all twenty
of them**. Nothing in the control set changes between step 3 and step 17. The
only things that differ are the `<legend>` wording and the radio `name`
attribute, which is the DOM's own declaration of which question these two inputs
answer.

## Targeted defect

**Regression guard for F1 (same-shape state collapse) and for the Gate 1
journey-crossing adjudication.**

F1, from the 2026-08-15 architecture review: the state fingerprint is a function
of the control-name set, so same-shape wizard steps collapse to one state and the
walk quits believing it is looping. On this fixture that failure is total — a
20-step funnel becomes a single state, and the walk abandons it after step 1.

The second half is what makes it a *Gate 1* fixture. A walk that abandons the
funnel at step 1 still records twenty observations of the entry page, still
writes a manifest, and — before Gate 1 — still reported `completed`. That is the
zero-crossing completion `app.completion.adjudicate` now refuses: journeys were
walked, no step was ever crossed, and the crawl described page discovery using
the vocabulary of journey execution.

## Expected controls

Four, on every step. `expected.json` pins the FIRST step, because that is the
page a single `collect()` observes:

| control | role | notes |
|---|---|---|
| `Yes` | radio | `group_key` from the `name` attribute, rewritten per step |
| `No` | radio | same group as `Yes` — one question, two answers |
| `Continue` | button | **disabled** until the question is answered |
| `Back` | button | **disabled** on step 1 only |

Two properties are asserted about the shape itself:

* `group_key_partition` — the two radios must form **one** group of two. A
  shattered group would offer each answer as its own question, and on a
  twenty-step funnel that is forty fabricated questions.
* `name_fingerprint_collapse` — the radios take exactly **two** distinct
  `(role, name)` pairs, recorded explicitly so the adversarial property is a
  measured fact rather than a claim in prose.

## Expected manifest

A crawl that walks this fixture correctly produces **one journey with at least
twenty steps** (twenty-one when the step-7 branch is taken) and a
`journey_crossings` count of at least nineteen. A crawl that collapses the states
produces one step and zero crossings, and is adjudicated
`journey_zero_crossing` — not `completed`.

The four runtime behaviours in `expected.json` are adjudicated by
`tests/browser/test_wizard_20_step.py`:

1. **The validation checkpoint is invisible to markup.** `Continue` is disabled
   until the step's question is answered, and that rule lives in script — HTML's
   `required` on a radio does not mean "this question must be answered". This is
   precisely the shape `walker._answer_to_unblock`'s radio path (Gate 1 /
   T-RG-01) exists to discover by experiment rather than by reading.
2. **State persists across backward navigation.** Answer, press `Back`, and the
   previous step returns with its own earlier answer selected and its `Continue`
   already enabled. A re-visited step is the same step.
3. **One conditional transition.** Answering `Yes` at step 7 inserts step 7a and
   the route becomes twenty-one long; `No` leaves it at twenty. The route is
   recomputed from the answers on every transition, so reversing that answer on a
   Back-and-forward pass removes the step again — a real branch, not a skipped
   number.
4. **Deterministic replay.** Every transition is a synchronous function of
   `(step, answers)`. No timer, no network, no randomness, no framework. Two
   crawls that make the same choices produce the same route in the same order.

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer

# the capture contract (both lanes)
python -m pytest tests/browser -q -k "27-wizard-20-step"

# the runtime behaviours — real Chromium navigation
python -m pytest tests/browser/test_wizard_20_step.py -q

# serve it by hand and look at it
python -c "import tests.browser._harness as H; s=H.FixtureServer().start(); \
print(s.url('27-wizard-20-step-samefingerprint')); input()"
```

## Why the number is 27 and not 22

The Gate 1 brief called for this fixture as "Fixture 22". Slot 22 was already
occupied by `22-collapsed-disclosure` (M2.6 / BUG-DISCLOSURE-BLIND), which is a
different fixture with a live contract and golden files. Renumbering it would
invalidate its goldens and every reference to it in the library table for no gain,
so this fixture takes the next free slot. `23` through `26` were also in use;
`27` is the first unused number.
