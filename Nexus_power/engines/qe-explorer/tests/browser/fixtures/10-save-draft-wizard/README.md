# Fixture 10 — `save-draft-wizard`

## Purpose

Isolate ONE capability: capturing a **wizard step** completely enough for the layers above
to act on it — the declared validation rules on the fields, and four actuators whose
meanings differ.

## Expected controls

Seven: three fields and four buttons.

**Fields — the declared rule the application states about itself:**

| Control | Constraints captured |
|---|---|
| `#face-amount` | `required`, `min=25000`, `max=2000000`, `step=1000`, `value_committed=250000` |
| `#term` | `required`, 3 options, `value_committed="20"` |
| `#notes` | `maxlength=500`, `best_effort=false` (named by `label[for]`, **not** its placeholder) |

**Actuators — four different meanings for the traversal layer:**

| Button | Meaning |
|---|---|
| `Back` | reverses the walk |
| `Save Draft` | a **non-advancing mutation** — writes server state, does not move the funnel |
| `Continue` | the advance |
| `Cancel Application` | **irreversible** — a never-click leaf for the refuse pack |

## Expected manifest

`tests/browser/golden/manifest_10-save-draft-wizard.json`.

## Targeted defect

None — **regression guard** for the constraint block (`inventory_js.py:600-613`):

> THE REST OF THE RULE THE APPLICATION DECLARED ABOUT ITSELF. A catalogue question with no
> validation justifies no negative and no boundary case, so a crawl that reads only
> min/max/step leaves the scenario deriver nothing to work with on every text field in the
> fleet.

`#notes` additionally pins the `best_effort` flag: it has both a `label[for]` and a
`placeholder`, and the ladder must take the label (rung 1) and set `best_effort=false`. A
regression that let the placeholder win would flip `best_effort` to `true` and mark a
perfectly good name as an a11y weakness.

`Save Draft` next to `Continue` is the pair that matters most: a walk that treats
`Save Draft` as an advance records a funnel step that never happened. This fixture makes
both available under distinct accessible names so the layers above have something to
distinguish.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_jsdom_execution.py -k 10-save-draft -v
python -m pytest tests/browser/test_playwright_execution.py -k 10-save-draft -v
```
