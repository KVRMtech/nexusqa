# Track 1.1 — the step-5 arrival shape, as observed

Source: golden gate exploration `6961213d-1cfa-4f69-86d4-1723a774771a`
(Summit Life carrier admin, `86203785-…`), VM HEAD `c00b527`, 2026-08-15.

Read from the recorded crawl. Nothing here is inferred; A4.3 is specced against
this and not against the shape a five-step wizard is *assumed* to have.

## What the walk did

```
advances_by_tier   {"1": 4}          all four advances chosen by the label regex
flow.terminal      submit_boundary   the walk REACHED the boundary and stopped
flow.steps         5
step 5             0 filled / 0 unfilled · advance = NONE · decision_points = 1
forms_submitted    9                 none of them this wizard's submit
```

## The four facts that shape A4.3

**1. The terminal is already correct — `submit_boundary`, not `no_advance`.**
The walk is not stuck and not failing. It arrives at step 5 and stops at the
commit boundary by design. A4.3 is therefore *crossing* an existing boundary,
not fixing a broken walk.

**2. Step 5 has no fields, and that is the app being honest.**
`Review & Submit` renders summaries (`<p>Name: …`), not inputs — so `0 filled /
0 unfilled` is the correct reading of a read-only page, not a fill failure. A4.3
must not treat "nothing to fill" as "nothing happened": the outcome milestone
for this journey is a PAGE TRANSITION, not a committed field.

**3. `advance = NONE` at step 5 is correct and must stay correct.**
The application renders `Continue` only while `step < 4`, and `Submit
Application` only at `step === 4`. There is no forward control to find. A4.3
crosses via the boundary control, which is a different code path from the
advance picker — reusing the advance picker here would be looking for a control
the app deliberately does not render.

**4. `decision_points = 1` on step 5 is the commit fork.**
`_next_action_decisions` recorded the page's one real choice (submit vs back).
That is the fork A4.3's approval must attach to.

## The gap A4.3 has to close

`submit_candidates` for this crawl contains `Record FNOL`, `Calculate Premium`,
`Create Customer Profile`, `Continue`, `Back` … and **not** `Submit
Application`. The one control that ends the journey the product most wants to
prove is missing from the list an attested Phase-B submit would draw from.

**CORRECTION — the first reading of this was wrong, and the wrong version is
kept here because it is the more tempting one.** I recorded this as "a hook
wired to the outer path only, blind to what the walk reaches", by analogy with
the `_note_advance_blocked` defect. Reading `_note_boundary_controls` shows
otherwise:

```python
if not name or c.get("danger"):
    continue          # danger controls are skipped BY DESIGN
```

`Submit Application` is not missing because it was never seen. It is **excluded
deliberately**: "submit" matches an irreversible verb, the control is
danger-flagged, and a danger control is never offered as a submit candidate.
`Record FNOL` and `Calculate Premium` are on the list precisely because they are
*not* flagged. The mechanism is working as written.

An analogy to a defect fixed last week is not evidence. Program rule ③ applies
to diagnosis as much as to fixes.

## Spec for A4.3, written against the above

This changes what A4.3 is. It is **not** a capture fix. The terminal control is
seen and correctly refused; what is missing is the route from "refused
irreversible verb" to "approved, attested, crossed exactly once".

* Surface the walk-terminal DANGER control as an *approvable* boundary — a
  distinct list from `submit_candidates`, which must keep meaning "safe to
  cross without approval". Merging them would turn an approval gate into a
  list of things the crawl already does.
* Cross it exactly once, through the attested Phase-B path, on approval.
* Record the post-submit page as the journey's **outcome milestone** — the
  evidence is the transition and the resulting page, since step 5 commits no
  fields.
* Submit-once: a state whose approved submit already succeeded is never
  re-submitted on re-entry. Note `forms_submitted = 9` on a crawl that submitted
  no application — that counter counts something else, and A4.3 must not read it
  as "this journey was submitted".

## Not yet observed — do not spec against these

* Whether Phase-B fires here at all (it has never run on this journey).
* How the review page's navigation grounds after submit (no crossing has
  happened, so there is no recorded transition to read).
* Wall-clock of the app's `executeFlow` API sequence.

These need one crossing to observe. Per program rule ③, the parts of A4.3 that
depend on them wait for that observation rather than being written blind.
