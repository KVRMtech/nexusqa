# 22 — collapsed disclosure

## Purpose

Does capture report *which controls are shut doors* — the evidence the crawler's
pre-capture expansion pass (M2.6 / T-CAP-03) decides from?

`isVisible()` is right to refuse a control inside a collapsed accordion: it is not
on the page, and cataloguing it would be a capture-says-covered /
replay-cannot-bind claim of exactly the kind this harness was built to catch. But
the consequence was that a question the application asks was never recorded by any
crawl of it. The fix is for the crawl to *open the section and read again* — and
for that to be a deliberate act rather than blind clicking, capture has to say
which controls are doors and which of those are shut.

## Targeted defect

**BUG-DISCLOSURE-BLIND** — capture emitted no signal that a `<details>` was shut,
so no deliberate expansion pass could be written and every field inside a
collapsed section went uncatalogued.

Two of the three declarations were already emitted raw (`aria-expanded`, and
`aria-selected` on a `role=tab`). The third was not emittable at all:

```html
<details>              <!-- open is a live PROPERTY, not an attribute -->
  <summary>Medical history</summary>
```

`getAttribute("open")` does not track the state, so nothing already captured
distinguished an open `<details>` from a shut one — and the only way to find out
by clicking is to *close every one that was already open*. `disclosureState()`
normalises all three declarations into one field, `disclosure`.

## Expected controls

| control | declares | the pass must |
|---|---|---|
| `#acc-beneficiary` | `aria-expanded="false"` | **open it** — the acceptance criterion: `Beneficiary full name` is uncatalogued until it does |
| `summary` (`#det-medical`) | closed `<details>` | **open it** — the case that required a capture change |
| `#acc-contact` | `aria-expanded="true"` | **leave it alone** — clicking would close it and catalogue *fewer* fields than doing nothing |
| `#tab-term` | `role=tab aria-selected="false"` | **click, see the loss, undo, skip** — selecting it hides the Whole-life panel, so merging both is a page that never existed |
| `#submit-app` | `aria-expanded="false"` + a commit word | **refuse** — this is the control the pass would submit an application with |
| `#nav-more` | `aria-haspopup="menu"` | **refuse** — a nav fly-out belongs to `_menu_reveal`, not to this form's control set |
| `#full-name` | nothing (`disclosure: ""`) | control group: present whether or not any of the above works |

## Expected manifest

The recorded goldens are of the **unexpanded** page, because the characterization
crawl records what capture returns before any crawler pass runs. So the manifest
and inventory goldens show the seven controls above plus `Full name`, `Contact
email` and `Target cash value` — and **none** of `Beneficiary full name`,
`Beneficiary share percent`, `Existing conditions`, `Term length years`,
`Confirmation code` or `Help centre`.

`forbid_controls` pins exactly that pre-expansion truth. It is what makes the
crawl-level test meaningful: `tests/browser/test_capture_expansion.py` asserts the
same fields **are** present after the pass, so the two together measure the
difference the pass makes rather than the state of the page.

## Running this fixture alone

```bash
python -m pytest tests/browser -k 22-collapsed-disclosure -v
python -m pytest tests/browser/test_capture_expansion.py -v   # the crawl-level proof
```
