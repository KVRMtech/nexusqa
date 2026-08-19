# Fixture 19 — native confirm dialog (M1.5 / T-ND-02)

## Purpose

Prove that the crawler **answers native browser dialogs with intent**, instead of
letting the browser automation library answer them for it.

`alert()`, `confirm()` and `prompt()` are not DOM. They live outside the document,
block the page until answered, and are completely invisible to the inventory
walker — capture of this page is byte-identical before and after the fix. So the
only way to hold the behaviour is a fixture that raises real dialogs and asserts
on where the journey ends up.

The fixture raises five dialogs behind five controls, and the correct answer is
different for each:

| control | dialog | correct answer | why |
|---|---|---|---|
| `Continue` | `confirm()` "…want to continue?" | **accept** | it gates the funnel step |
| `Return to dashboard` | `confirm()` "…leave this page?" | **dismiss** | accepting abandons the journey |
| `Show session notice` | `alert()` | **accept** | one button, and the page is blocked until answered |
| `Delete Application` | `confirm()` "…cannot be undone" | **dismiss** | a native confirm is not an approved crossing |
| `Add a reference code` | `prompt()` | **dismiss** | no grounded value; a fabricated one is invented input |

A crawler that accepts everything destroys the funnel it was sent to catalogue
(it leaves for the dashboard and deletes the application). A crawler that
dismisses everything never advances. Both failure modes are green-looking, which
is why both directions are asserted.

## Expected controls

Six interactive buttons, all enabled, all named from their text content:
`Continue`, `Return to dashboard`, `Show session notice`, `Delete Application`,
`Add a reference code`, `Print summary`. `Print summary` raises no dialog at all
and exists so the fixture can show the dialog path is not being taken
indiscriminately. The `role="status"` live region (`Awaiting review`) is read by
the status-text rung, not by the inventory.

## Expected manifest

The capture golden (`golden/manifest_19-native-confirm-dialog.json`) is recorded
under the standard characterization crawl: `max_states=1`, `observe_only=True`.
Observe-only is load-bearing here — under that posture the dialog policy dismisses
funnel confirms, so the characterization crawl cannot navigate off this page and
the golden stays about capture.

The behavioural expectations are asserted separately, by
`tests/browser/test_page_lifecycle_execution.py`, which drives the production
port with `observe_only=False` and checks that:

* clicking `Continue` lands on `step2.html`;
* clicking `Return to dashboard` stays on `index.html`;
* each decision appears in the drained browser events with `event="dialog"` and
  a populated `dialog_type` / `message` / `action` / `intent` / `reason`.

## Targeted defect

`BUG-M15-DIALOG-AUTODISMISS`. The crawler attached no `page.on("dialog")`
listener. Playwright's documented behaviour with no listener is to auto-dismiss
every dialog, so a confirm-gated `Continue` answered CANCEL on every crawl of
every application: `location.href` never ran, the action classified as outcome
`none`, and a perfectly healthy funnel was indistinguishable from a dead button.

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer
python -m pytest tests/browser -k 19-native-confirm-dialog
python -m pytest tests/browser/test_page_lifecycle_execution.py -k dialog
```
