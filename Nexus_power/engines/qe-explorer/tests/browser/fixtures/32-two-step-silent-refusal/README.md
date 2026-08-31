# Fixture 32 — two-step silent refusal

## Purpose

The smallest application with the failure shape B1-S exists for, wearing the
markup that makes it hardest: a TWO-STEP form whose submit is refused
**silently**, with the refusal rendered **where the field lives** rather than
where the commit lives.

Step 1 ("Contact") owns the `Phone Number` field **and** its message node — a
plain `<p>` with no ARIA role, no `aria-invalid`, no error-id convention.
Step 2 ("Review & Submit") owns the `Submit Application` button and **no
message node at all**. The submit handler validates the phone against
`(999) 999-9999`; on failure it writes the reason into step 1's message node —
which is `display:none` while step 2 is shown — and changes **nothing
visible** on the page the click happened on. A phone that satisfies the mask
replaces the form with an undecorated confirmation banner
(`Application received. Confirmation #TS-204.`).

This is the summit-life-carrier failure (zod refuses before the handler; every
`<FormMessage/>` lives on an unmounted earlier step; the review step renders
none) reduced to two steps and re-styled the way vkpower renders errors (bare
`<p>`, nothing the anchored attribution ladder can read). Between the two
proving grounds and this fixture, both halves of B1-S are covered — the
anchored read and the forward-walk text licence — and this fixture is the half
no fixture had.

Values persist across steps because the inputs never leave the DOM; they are
hidden with their step. That is also what hides the message node from a reader
standing on step 2: the capture's own visibility rule, not a special case.

## Expected controls

Four visible controls on load (step 1):

| control | role | state |
| --- | --- | --- |
| `Full Name` (`input#name`) | textbox | enabled, empty |
| `Phone Number` (`input#phone`) | textbox | enabled, empty |
| `Back` (`button#back`) | button | **disabled** on step 1 |
| `Continue` (`button#next`) | button | **disabled** until both fields hold text |

`Submit Application` (`button#submit`), step 2's texts, and the confirmation
banner are `display:none` on load and must be **excluded** by the inventory's
visibility rule — the same rule that makes the hidden message node unreadable
from step 2, which is the phenomenon under test.

## Expected manifest

A single-state characterization crawl (observe-only, `max_states=1`) captures
step 1 exactly as the table above: four controls, two textboxes with committed
value `""`, both buttons with their disabled state, no dialogs, no statuses,
no error texts (the message node is empty on load). No network events — the
fixture is synchronous and framework-free.

## Targeted defect

Regression guard for B1-S (the step-back rejection reader) and its
forward-walk text licence, and for the B2 closed loop's live shape. A crawler
that reads only the page it crossed from records `outcome=none` with zero
rejections named — the most misleading bundle shape there is. The live
companion module `tests/browser/test_two_step_silent_refusal_live.py` drives
the real Crawler over this fixture twice: loop OFF, the bundle must NAME the
refusal (`Phone Number`, the mask sentence, `steps_back=1`,
`anchored_by=text_names_control`) while reporting the journey honestly
incomplete; loop ON, one evidence-driven re-fill and one retry must complete
the journey with the application's own banner observed and both attempts on
the record.

## Running this fixture alone

    python -m pytest tests/browser/test_browser_characterization.py -k 32-two-step -p no:randomly
    python -m pytest tests/browser/test_two_step_silent_refusal_live.py -p no:randomly

The fixture is `playwright`-lane only: the live behaviour under test (a real
step transition, visibility-scoped capture, a real submit handler) is exactly
what jsdom does not render.
