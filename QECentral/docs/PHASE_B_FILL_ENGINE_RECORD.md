# Phase B — Fill Engine: what was built, what was measured, what is still open

**Team B, 2026-08-31.** Written against the commits it describes, with every
verdict re-run rather than recalled. Where a claim is not proven, it says so.

---

## 1 · The commits

| SHA | What |
| --- | --- |
| `3105768` | **B2 closed loop** — a named refusal drives one repair and one retry; B1-S hardened with the forward-walk text licence |
| `3894fae` | **Generalisations** — sub-form row commit, consent wall, product + premium cadence |
| `4b4526f` | **B4 second half** — a `needs_input` row says WHY; the live ledger shape pinned |
| `ac7b4f1` | **Fixture 32** — the silent two-step refusal, proven in real Chromium |
| `ff8f150` | **`--plan-pattern`** — the gate-2 instrument can be told which funnel a journey is about |
| `b3ce60a` | **Evidence** — acme's admissible crossing and vkpower's 11/11, archived |

B1-S itself (`a07cb59`) was **inherited, not rewritten**: another session
landed `step_back.py`, its vocabulary and its wiring, and Team B's job was to
verify, harden and extend it. Its pinned unit suite is untouched.

---

## 2 · B1-S — verified, then hardened

**The shipped mechanism was correct and its refusal was right.** a07cb59 adopted
ANCHORED rejections only on a stepped-back page, because a page the reader was
not standing on when the commit was refused has no before-snapshot, and an
after-only read would score a step's standing helper text as a verdict.

**What was missing is a licence that already existed and was thrown away.** The
walk STOOD on that step on its way to the commit. `walker._note_step_texts`
now snapshots each step's pre-click texts, keyed by its actionable-set
signature (`_candidate_sig` — the same discriminator the step-back scan uses,
because a page fingerprint cannot see a step swap on one URL). Text present at
the stepped-back read that is ABSENT from that snapshot **appeared as a result
of the refused commit** — a real ACT-THEN-DIFF, the act being the commit.

* No snapshot for the step's signature ⇒ the rung stays withheld, exactly as
  shipped. Every pre-existing caller and test double is unaffected.
* Adopted records carry `steps_back`, so the weaker claim stays legible.
* This is what makes a **vkpower-style** application readable — bare `<p>`
  errors inside a multi-step form. Summit's shadcn errors are ARIA-anchored
  and never need it, which is why one proving ground could not have found it.

Proven by `test_the_forward_walk_is_the_before_side.py`: the adoption, plus
three controls (text already in the snapshot is never a verdict; no snapshot
licenses nothing; another step's snapshot licenses nothing).

---

## 3 · B2 — the closed loop

    refused silently → step back through EVERY step, naming as it goes →
    re-fill the named fields WHERE THEY LIVE, under constraints tightened by
    the application's own sentence → walk forward to the commit → retry ONCE
    → both attempts stay on the record.

**Why a retry is ever safe** (`app/refusal_repair.py`, pure, fail-closed on
every axis — every argument an OBSERVATION, never an intention):

1. the boundary is spent and the crossing is on the record;
2. the application refused it **by name** — no signal, no retry (rule 1 of the
   fill-time repair loop, held at the commit);
3. the commit did not navigate (same-document, fragment- and slash-insensitive);
4. **zero mutating requests were allowed through in the crossing window** —
   drained before the scan can pollute it; a `buffer_truncated` marker counts
   as a mutation, because a window we did not see all of cannot be certified;
5. the repaired wizard is standing at its commit again;
6. once — `QEC_REFUSAL_RETRY_MAX` (default 1, 0 disables, malformed → 0).

**The ledger owns the arithmetic.** `CrossingLedger.refund_app_refused`: one
refund per boundary **ever**; the budget subtracts it; `is_spent` keeps
answering True (the step-back gate and resume both read it); and refunds are
deliberately **not** journal-restored — a resumed run sees both journalled
reservations as spent, so a kill between refund and retry cannot yield a
duplicate click. Two independent brakes, because the failure mode of a loose
retry loop is a submission spree against a client's application.

**The retry is not a special path.** It goes through
`_execute_approved_submit` like every crossing: guard, journal, its own
milestone, its own crossing id. A retry that is also refused is recorded
unverified, and there is no third attempt.

### The proof, and why its fixture had to be new

A closed loop cannot be proven against a page that ignores the values typed
into it. `tests/characterization/harness.py` gained `Fixture.port_factory`, and
`test_a_refused_commit_is_repaired_and_retried_e2e.py` supplies a
`ScriptedBrowser` subclass that **remembers fills and judges the commit** — the
zod half of the transcription. The first attempt genuinely fails
`(999) 999-9999` with the generator's ten bare digits; the field is named FOUR
steps back; the retry carries `(445) 555-0110`; the banner is observed;
`journeys_completed=1` and `boundaries_crossed=2`, because the refused attempt
is not erased.

Controls flip one axis each: the dial at 0 leaves the refusal standing with the
boundary spent exactly once; a POST in the crossing window refuses the retry
outright.

---

## 4 · Fixture 32 — the mechanism in a real browser

Everything above was proven against scripted ports and a transcription of
summit — real crawler, fake browser. Fixture 32 is a two-step form whose
refusal is written into step 1's **hidden plain `<p>`** while the commit lives
on step 2: the summit failure wearing vkpower's markup.
`tests/browser/test_two_step_silent_refusal_live.py` drives the production
Crawler over it twice, one axis apart, in real Chromium:

* **loop OFF** → names `Phone Number`, the mask sentence, `steps_back=1`,
  `anchored_by=text_names_control` (nothing anchored exists to read, so only
  the forward-walk licence can carry it), boundary spent exactly once,
  `journeys_completed=0`;
* **loop ON** → `journeys_completed=1` on the application's own banner, TWO
  milestones, rejection record carries `repaired=true`.

4/4 in 108s. Characterization goldens minted and verified.

---

## 5 · The generalisations

| Mechanism | Was | Now | The belt |
| --- | --- | --- | --- |
| Sub-form row commit | inline `\badd\b` (vkpower's button only) | `vocab.SUBFORM_COMMIT_PACKS` as data — reaches invoice lines, dependants, drivers, "Insert row" | **new** commit/advance-word veto: "Add & Pay Now" pays, "Add and Continue" advances |
| Consent wall | `kind == "checkbox"` | `checkbox` **or** `toggle` — a `role=switch` wall was invisible while behaving identically to the user | licence unchanged: the operator's NAMED grant for the gated commit; revert-on-decline asserted |
| Product / premium cadence | first option of the list | persona attributes — the household's `term_years` term-life product, monthly cadence | a list with no term product is never forced; bare "Premium" stays money; "Payment Method" is not a cadence |

First-option-of-the-list was a **different answer on every differently-ordered
page** — the cross-step contradiction an underwriting rule checks, arriving
through the catalogue.

---

## 6 · B4's second half — measured before it was built

The Phase-1 exit re-scope recorded five wizard fields as "absent from the field
ledger entirely". That premise was already disproven by `a07cb59`'s collision
fix, and **today's live run proves the fix in production shape**: `First Name`,
`Last Name`, `Date of Birth`, `Email Address` **and `Gender`** each carry
exactly one row, filed under the page that met them first, with the wizard
named in `also_seen_at`. Gender — the required enum four seeded rounds never
reached — is `harvested` and filled. Pinned as a unit so it cannot regress.

**What was still missing: the residue said WHAT it needed and never WHY.**
Every `needs_input` row now carries a reason from a closed vocabulary:

    widget_unhandled:<class>   no primitive drives this control — a seed
                               cannot fix it; the class is named
    secret_never_invented      password / OTP
    choice_left_to_client      an enumerable business fork in user data mode
    no_value_rung_answered     every rung ran and none had an answer

Additive and conditional — a row that answered owes no excuse — and the
characterization goldens held **without re-minting**, which is the measured
proof that nothing else moved.

---

## 7 · Live measurement

### 7.1 · acme-life — ADMISSIBLE

    crossed: 1 ['Bind policy'] · confirmation observed: True ['aria_status']
    journeys_completed: 1 · milestone verified: True
    produced_by ff8f150, dirty: false
    t3_verify_crossing_evidence.py --sha ff8f150 → [ADMISSIBLE] 1/1, exit 0

`seed_near_misses: []`, `data_account: {needs_input: 2, synthesized: 11}` — no
silent-stall class anywhere on the account. Archived at
`Nexus_power/evidence/gate2/phaseB_acme_ff8f150/`.

### 7.2 · vkpower-life — the floor holds, with headroom

    crossed: 1 ['Sign & Submit Application'] · journeys_completed: 1
    deepest_flow 11/11 proven, capped=false · milestone verified: True
    produced_by c8dfd29, dirty: false

The Phase-B generalisations are visible in production shape: the product card
answered as *Term Life Insurance…*, the signature page cleared as *consent wall
(6 acknowledgements)*. Every `advance_blocked` record is NAMED; two were
resolved by the agent live.

**The t3 gate REFUSED this bundle**, verbatim: *"no confirmation observed — a
crossing without an observed outcome proves a click, not an effect"*. The
harness's `confirmation_observed` counts only `outcome=confirmation`
milestones, and this crossing's verified milestone is `rung=navigation`. That
is the instrument's confirmation filter, not an unverified journey — recorded
here rather than argued with. Archived at
`Nexus_power/evidence/gate2/phaseB_vkpower_c8dfd29/`.

### 7.3 · summit-life-carrier — NOT crossed, and the blocker is now named

Four runs today, none reaching a crossing. Two distinct causes, both measured:

**Cause 1 — instrument ordering variance.** The application signs in per page
load; every relogin lands on `/dashboard/overview`, whose tables out-populate
the frontier. Two full-budget runs died at `budget_max_wall_ms` with 13 states
and `boundaries_crossed: 0`, never entering the funnel — while the SAME
instrument at the SAME defaults had crossed two days earlier (summit4, engine
`768a3c8`). Re-rolling that dice per run is not a measurement, so `ff8f150`
exposes the engine's existing `plan.priority_patterns` as `--plan-pattern`:
pure frontier ORDERING, restricting nothing, byte-identical without the flag.

**Cause 2 — a data-mode dial, not an engine gap.** A run entering at the wizard
URL reached step 0, filled six of seven fields, and the application disabled
its own Continue:

    qec.wizard.advance_blocked label='Continue' missing=['Gender']

`Gender` is summit's portal-rendered shadcn `<Select>`; in `--data-mode user`
(the gate-2 default) a semantic CHOICE is deliberately left to the client, and
the open-and-pick path is agent-mode only. **That is exactly the
`choice_left_to_client` reason this milestone shipped** — the residue naming
its own blocker on the first live run after it was built. An agent-mode pinned
run and its user-mode control are the outstanding measurement.

---

## 8 · What is NOT claimed

* **Nothing here is deployed.** Every commit is on
  `feat/qec-dynamic-catalog-p0-p6` and none has been through CI (this machine
  cannot push to `origin`; see G8).
* **Summit does not cross on this engine yet**, and B1-S/B2 therefore have
  **no live trigger on summit** — both are proven against a transcription of
  that application and against fixture 32 in real Chromium, not against the
  application.
* vkpower's quote-start product **card grid** goes through the card picker,
  not the product rung; its coherence with later product questions is
  unmeasured.
* Disabled and nameless controls still get no ledger row; summit's
  health-condition toggle siblings stay `needs_input` after "None" answers
  their question, because the page declares no grouping
  (`declared_questions=0` live) and releasing them would be a guess. That tie
  needs Tier-2 inventory grouping, not a ledger patch.
* `QEC_REFUSAL_RETRY_MAX` is read from the environment rather than
  `config.Settings`, because `config.py` was under concurrent edit by the
  fleet workstream. Semantics are identical and documented at
  `refusal_repair.max_retries_configured`.

## 9 · Suite state

Explorer, CI's plugin set, on the merged tree (Team A + Team B):

    python -m pytest tests --ignore=tests/browser -q -p no:cacheprovider -p no:randomly
    2774 passed, 2 xfailed

Browser lane: fixture 32's characterization goldens and the four live Chromium
tests pass; the rest of that lane was not re-run (it is ~900 tests and over an
hour).
