# Phase 1 — exit re-scope record

**Decided by the architect, 2026-08-24, under delegation from the programme
owner. This record is the re-scope, written down as one, and not a quiet
redefinition.**

---

## 1 · The decision

**Phase 1 exits with ONE admissible crossing — `acme-life`, non-author
reproduced, `t3_verify_crossing_evidence.py` exit 0 — plus two applications
driven to their commit boundaries with their remaining capability gaps
MEASURED and NAMED.**

The prior exit criterion was a crossing with an observed confirmation on three
first-party applications. It is not met and is not being claimed as met. What is
claimed is narrower and is what the evidence supports:

> the crawler walks three real funnels, completes one end to end with an observed
> confirmation, and where it cannot finish it says precisely why — on the
> coverage ledger, not in a log.

**Why the change was taken rather than one more build.** The decision to certify
at *two* crossings, taken earlier the same day, rested on summit's remaining work
being bounded: its rejected fields fail declared rules the code already captures.
That premise broke on measurement — the rejections are not readable at all,
because both proving grounds state their rules in markup no accessibility-based
reader can see. Four honest seed rounds could not converge without that feedback.
This was the fourth consecutive "one more bounded build", each honestly sized,
each revealing another layer; a gate that waits for the end of that sequence
never convenes.

---

## 2 · The evidence

### 2.1 · acme-life — the admissible crossing

| | |
| --- | --- |
| Bundle | `Nexus_power/evidence/gate2/r3_acme_reproduced/journey.json` |
| Verdict | `crossed: 2 ['Bind policy', 'Bind policy']` · `confirmation observed: True ['dialog']` |
| `produced_by` | head `e24bcf5`, **dirty `false`** |
| Gate | `t3_verify_crossing_evidence.py` → `[ADMISSIBLE]`, **exit 0** |

**Non-author reproduction:** `Nexus_power/evidence/gate2/r3_acme_reproduced/NON_AUTHOR_REPRODUCTION.md`
— peer session `nexusqa-21`, fresh blobless clone of the pushed SHA `24da99f`,
app built from its own Dockerfile on its own port, its own `--out` directory.
Identical on all three claimed lines; `[ADMISSIBLE]`, exit 0. Recorded at the
moment it happened, because `GATE_5_CEREMONY.md §3` establishes it cannot be
reconstructed afterwards. **It fills no signatory seat.**

### 2.2 · summit-life-carrier — crossed, and REFUSED

| | |
| --- | --- |
| Bundle | `Nexus_power/evidence/gate2/r1r2_task1_task2/summit_seeded_journey.json` |
| Verdict | `crossed: 1 ['Submit Application']` · `confirmation observed: False` |
| Milestone | `outcome "none"`, `navigated false`, `url_after == url_before` |
| Corroboration | **0** `/api/v1/` calls fired against **1333** network events observed |

The gate's own words, quoted verbatim:

```
[REFUSED]  summit_seeded_journey.json: no confirmation observed
           — a crossing without an observed outcome proves a click, not an effect

admissible: 0/1
exit 1
```

**This refusal is cited as evidence, not apologised for.** The crawler clicked the
real commit control on a real application and the gate refused the bundle anyway,
because the click produced no observable effect. A gate that admitted this would
admit anything; the programme's central claim is that its evidence standard has
teeth, and this is the instance where it bit its own most-wanted result. The
bundle **remains refused** and is not counted toward the exit.

### 2.3 · vkpower-life — driven to its boundary, blockers named

| | |
| --- | --- |
| Bundle | `Nexus_power/evidence/gate2/r1r2_task1_task2/vkpower_carddriver_journey.json` |
| Verdict | `crossed: 0` · `confirmation observed: False` |
| `produced_by` | head `6aedd6a`, **dirty `false`** |

Named on the coverage ledger — first-class records, not log lines:

```
Continue                             advance_disabled_by_app_validation   /life-insurance/quote/start/
Continue to Beneficiary Designation  advance_disabled_by_app_validation   /life-insurance/apply/payment/
Continue to Signature                advance_clicked_but_app_declined     /life-insurance/apply/beneficiary/
```

The payment step that blocked this funnel for weeks is **solved**: a card-grid
driver answers the formless picker by experiment and the application itself
re-enabled Continue (`cleared=True` on `Credit / Debit Card`), reaching
`/apply/beneficiary/` for the first time. The two records above are what remains,
and the second is a distinct diagnosis from the first — the app disabled nothing;
it accepted the click and did nothing.

---

## 3 · The four capability gaps → Phase-5 Fill-Engine backlog

Each carries **"Phase-5 entry" as its trigger**. Each is a capability, not a
configuration, a seed, or a bug fix — which is precisely why none of them is
being built inside the certification window.

**On owner names.** Technical ownership is recorded at session level, which is
what this programme has been operating on (evidence independence is
session-level; accountability is human-level, and no agent writes a human name
into a record). A named human owner for each item is a **programme-owner
appointment at Phase-5 entry** and is marked as pending below rather than
invented.

### B1 · DOM-diff rejection reader

Read the rejection an application renders as **plain text**, with no
accessibility annotation.

* **Why it is first.** It is the blocker for summit AND the deepest of vkpower's
  three, and plain-text errors are the **common real-client case** — an app that
  annotates its errors is the exception, not the rule.
* **Measured cause.** `error_texts()` (`app/playwright_port.py:1614`) queries
  exactly `[role="alert"]` and `[aria-live="assertive"]`. vkpower renders
  `<p className="text-sm text-red-700 font-medium">{error}</p>` — neither. The
  rule *"Primary beneficiary allocations must total 100%"* is on screen in plain
  words and nothing looks at it. summit exposes no control-anchored rejection
  either.
* **Acceptance — binding.**
  1. **It may not reference any application's CSS class names.** Fitting the
     reader to one app's Tailwind palette is what seven R7 red-team rounds
     punished, and it would make the next application's silence invisible again.
  2. **Form-scoped new-text-after-declined-submit** — text that appears inside
     the form after a submit the app declined, diffed against the pre-submit DOM.
  3. **Precedent to follow:** ACT-THEN-DIFF, `app/discovery.py:579-581` — commit a
     choice, re-read, and let the difference be the evidence. The mechanism
     already exists in this codebase for dependent-field discovery.
* **Non-author verification:** peer session `nexusqa-2d` has offered to verify
  this one fresh; it is large enough to deserve its own pass.
* **Appointed owner:** *pending — Phase-5 entry.*

### B2 · Constraint-aware repair

Generate values that satisfy the constraints the application declares or the
crawler discovers, rather than values that satisfy the widget.

* **Measured cause.** summit's `applicationSchema` demands `ssn`
  `/^\d{3}-?\d{2}-?\d{4}$/`, `zip` `/^\d{5}(-\d{4})?$/`, a real `email`,
  `faceAmount >= 10000`, and lowercase enums. The synthesizer's values satisfied
  every widget and failed the schema, and `react-hook-form` validated the whole
  schema before its handler ran — so nothing was submitted and nothing was said.
* **Depends on B1**: a repair loop driven by anything other than the
  application's own words is a guess wearing a retry's clothes.
* **Appointed owner:** *pending — Phase-5 entry.*

### B3 · List sub-action, allocation rule, and cross-step coherence (vkpower)

Three shapes behind vkpower's remaining two blockers:

* a **sub-action that commits a row** (`Add Beneficiary`) before the step's own
  submit will accept it;
* a **numeric rule across a list** — primary allocations must total exactly
  100%, enforced in `handleSubmit` and rendered as page text;
* **cross-step coherence** at `/apply/signature/`, never reached: five consent
  checkboxes plus a typed signature that must match a legal name entered seven
  steps earlier.
* **Appointed owner:** *pending — Phase-5 entry.*

### B4 · Seed targeting and ledger completeness

Operator seeds cannot reach the field instances that actually block a funnel.

* **Measured cause.** Seeding is keyed by a value-free signature, and
  near-duplicate labels hash apart: **`Face Amount ($)` ≠ `Face Amount`**,
  `Last Physical Exam` ≠ `Last Exam Date`. Both wizard copies stayed
  `synthesized` while the seeds landed, correctly, on same-named fields
  elsewhere in the app (`provenance: recalled`).
* **Worse:** five wizard fields — `First Name`, `Last Name`, `Date of Birth`,
  `Email Address`, `Gender` — are **absent from the field ledger entirely**, so
  they have no signature to seed against at all. `gender` is a required enum.
* **Consequence, and why this is a gap not a bug:** four seeded rounds converged
  `fields_needing_seed` 6 → 4 → 8 → 1 while the two values that actually failed
  never appeared on that list once. Candidate remedies: key seeds by
  `(label, url)` as well as signature, and make unfillable custom widgets
  ledger-visible.
* **Appointed owner:** *pending — Phase-5 entry.*

---

## 4 · What is not claimed

**The t3 admissibility gate is unchanged; the summit bundle remains refused; no
application was modified and no crossing was fabricated.**

Also not claimed: that the two remaining applications are close to crossing (they
need B1–B3); that a lexical path rule is complete (it is not — the residual is
read out separately at the ceremony); that any capability above exists today.

## 5 · Provenance of this record

Written at HEAD `f59e329`, working tree clean. Every verdict quoted here was read
from a committed bundle whose `produced_by` is stated beside it, and every gate
verdict was re-run rather than recalled.
