# M2.1 — Real question wording, one question per group, stable identity

**Status:** code complete, proven by a real crawl of a real application.
**Branch:** `feat/qec-dynamic-catalog-p0-p6`. **Not committed, not deployed.**

---

## What was actually wrong

Grounded by reading the tree, not the roadmap. The line numbers the roadmap cited
had moved; the defects had not.

### 1. The catalogue published wording no application ever contained

`walker._answer_questionnaire` labelled every bare-button questionnaire question
`"Question %d" % (ordinal + 1)`. A twenty-question health questionnaire reached
the client as `Question 1 … Question 20`. Nothing on the page says that. A reader
could not tell which row asked about tobacco, and a regression diff could not
tell a **reworded** question from a **reordered** one.

The wording was there the whole time — `<legend>`, `aria-label`, a heading inside
a `role=group` — and nothing read it.

### 2. `form_snapshot_signals` is keyed by a control's ACCESSIBLE NAME

…and on a member of a choice group that is the name of an **answer**. So:

```
Gender → ( ) Male   ( ) Female
```

crossed to the catalogue as two questions, `"Male"` and `"Female"`, each offering
**no answers at all** — the question itself absent, and every answer it accepts
absent. Worse: fixture 09's forty radios are all named `Yes` or `No`, and a dict
keyed by name holds exactly two of those, so twenty questions came out as two.

### 3. Three id spaces for one question

| space | who mints it | what the catalogue did with it |
|---|---|---|
| `field_signature` | `forms.py` per ELEMENT | one catalogue question **per radio** |
| `group_id` | `inventory.py` per QUESTION | branch rows → **a second** catalogue question, named after whichever answer was seen first |
| `kind:accessible-name` | `flow_ledger.activated_signatures` | reveal identities that resolve to an answer, or to nothing |

Measured on a page asking **3** questions: **7** catalogue rows.

### 4. The reveal named an answer, so reconciliation could not work

`activated_signatures` identified a revealed control as `radio:yes` — a name every
other question on a health questionnaire also has. That resolves to whichever
question was indexed first: not "no rule", which would be honest, but the **wrong**
rule.

---

## Before / after, same input

`extract_controls` → `build_master_catalog` → `rules_from_branches`, on one page
asking *Gender*, *Do you use tobacco?* and *Cigarettes per day*:

```
BEFORE — 7 questions                        AFTER — 3 questions
  'Cigarettes per day'  text  []              'Cigarettes per day'   text  []
  'Female'              radio []              'Do you use tobacco?'  radio ['Yes','No']
  'Male'                radio []                  members: Yes, No
  'No'                  radio []              'Gender'               radio ['Male','Female']
  'Yes'                 radio []                  members: Male, Female
  'male'   choice ['male','female']
  'yes'    choice ['yes','no']
```

---

## The design

**One canonical question id.** `inventory.question_identity(record)` is the single
accessor: `group_id` (HTML's own radio/checkbox grouping) **or**
`question_group_id` (2+ controls sharing a declared `fieldset` / `role=group` /
`role=radiogroup` — the bare-button questionnaire). They never overlap. Every
consumer goes through it, and `catalog.group_question_id(gid)` turns it into the
catalogue's `question_id`. Identity is **DOM structure**, so re-reading a
reworded question does not re-key it.

`group_id` is deliberately **not** overloaded onto buttons: its hashes key
remembered branch-walk overrides, and `state_identity` reads it as per-control
identity when fingerprinting a page. Stamping it on buttons would re-key both.

**Wording is captured or admitted, never invented.** `inventory_js.questionOf()`
walks to the nearest DECLARED question container and reads its label from an
accessible-name rung only — `aria-labelledby` → `aria-label` → `<legend>` →
a heading inside the container. Never proximity, never "the text just above". A
question the application words nowhere gets `name = ""` and
`name_status = "unverified"`: catalogued, answerable, stably identified, and not
given text it never had. `name_source` says which rung produced the wording.

**The members are not discarded.** They become `options` + `members` on the
question, so a planned walk can still force one answer and a per-radio locator is
still reachable — metadata of the question, not questions of their own.

**Carried alongside, never instead of.** `coverage.states[].question_groups` is a
new, additive key. `form_snapshot_signals` is untouched, so the frozen compiler
keeps reading exactly what it always has.

---

## Two defects found while building this

1. **A planned walk of a questionnaire question would have silently answered the
   default.** `journey_fold` stores a branch under `group_id or
   control_signature`, and `branch_planner` hands that value back as the
   `choice_overrides` key — but the walker looked up `"q:" + group_id`. The two
   are now the same string, asserted by a test.
2. **A revealed question never reached the catalogue.** A reveal is transient: a
   step is recorded once, at its end, and by then the follow-up may be hidden
   again. Measured — the walk saw all three answers of *"How many cigarettes per
   day?"*, recorded the reveal as a branch rule, and recorded a state that did
   not contain the question. The rule pointed at a catalogue question that did
   not exist. The walk now notes the state the reveal produced, on the
   fingerprint the identity layer had already declared distinct.

---

## Proof — `proving-grounds/questionnaire-life`

Two REAL Chromium crawls through the production port and the production
`Crawler`: a base crawl, then a **planned re-crawl** with `choice_overrides`
forcing the tobacco question to `Yes` — exactly as `branch_planner` dispatches
one. The re-crawl is keyed on the question id the FIRST crawl recorded, so
identity surviving a re-crawl is not asserted separately: **the second crawl
cannot be aimed without it.**

```
=== questionnaire-life: MASTER CATALOG (12 questions) ===
  q_945e9ec334743376  unverified button    ''                       ['Yes','No']
  q_78b768530eb38975  observed   text      'Age at last birthday'
  q_4329ac0030aa6e46  observed   button    'Do you consume more than 14 units of alcohol per week?'
  q_d0cdd02f91879c11  observed   button    'Do you take part in scuba diving, motorsport or private aviation?'
  q_2d31734eabf5315c  observed   radio     'Gender'                 ['Female','Male','Prefer not to say']
  q_1186a78707942e49  observed   button    'Have you used tobacco or nicotine products in the last 12 months?'
  q_ce73b1bff7db27f8  observed   text      'Height in centimetres'
  q_b5ad2b53d0562a7c  observed   radio     'How many cigarettes per day do you smoke?'  ['1 to 10','11 to 20','More than 20']
  q_6eb77afb642ae390  observed   checkbox  'I consent to a medical records check'
  q_dfbb924f2b129f4e  observed   select    'State of residence'
  q_57898110895fe013  observed   checkbox  'Which of these have you been diagnosed with?'
                                            ['Diabetes','Heart disease','Cancer','None of these']
  q_3ab79f6adde16c7b  observed   radio     'Which product are you applying for?'
  summary: worded=11 unverified=1

=== TRIGGER RULES ===
[{ "question_id": "q_1186a78707942e49",        # Have you used tobacco…
   "option": "yes",
   "reveals_question_ids": ["q_b5ad2b53d0562a7c"] }]   # How many cigarettes per day…
```

The recorded reveal identity is `group:cc204bd532b2a59f1c4ae86e995f9357` — the
question, not one of its answers.

`tests/browser/test_questionnaire_catalog_e2e.py`, 14 assertions, all green.

---

## Architectural concerns discovered

1. **A page whose only questions are bare buttons is never walked.**
   `discovery.py`'s wizard gate requires `fill.filled or
   fill.has_unanswered_decisions`, and a step made of nothing but `<button>`
   answers commits nothing — so `_answer_questionnaire` never runs on it. The
   proving ground carries one plain field to sidestep this; the gap is real and
   is not M2.1's.
2. **A single declared question is invisible to the questionnaire path.**
   `_is_option` requires an answer label to repeat page-wide (≥2), which is the
   only honest signal on a page that declares nothing — but a `<fieldset>`
   holding one `Yes`/`No` pair IS a declaration, and it is now captured. The rule
   could safely relax to "declared container OR repeated label". Deliberately not
   changed here: it alters what a crawl clicks.
3. **A `<select>`'s placeholder is catalogued as an answer.**
   `"Select a state…"` appears in `options`. `_enumerable_options` strips
   placeholders for branch rows; `extract_controls` takes the signal's option
   list verbatim. Two rules for one thing.
4. **Reveals are only computed on the bare-button path.** A radio-group trigger
   records no reveals at all, because the fill answers every field at once and
   attributing the delta to one of them would be a fabrication. Making radio
   triggers work needs a per-answer probe, not a wider diff.
5. **Concurrent milestones are writing the same files.** M2.2/M2.3/M2.5 landed in
   `catalog.py`, `journey_fold.py`, `inventory.py`, `forms.py` and the
   characterization goldens during this work. The goldens had to be regenerated;
   of the 240 keys that moved, exactly one is M2.1's
   (`coverage.states[].question_groups`) and **nothing was removed**.
