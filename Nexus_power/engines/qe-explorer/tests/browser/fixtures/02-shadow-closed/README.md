# Fixture 02 — `shadow-closed`

## Purpose

Isolate ONE capability: **observing a closed shadow root, by being there when it is
created**.

`attachShadow({mode:"closed"})` hands its root to the component and to nobody else.
`el.shadowRoot` stays `null` forever and no API recovers the root afterwards — so a
retrofit cannot work, and for as long as capture only tried to read the DOM it already
had, every control in this widget was uncatalogued. That is the shape of a whole class of
design systems, and of the "quote widget" pattern in particular: a page that looks like a
ZIP field and nothing else.

The one moment at which the root IS observable is the moment it is created. The capture
init script (`app.inventory_js.CAPTURE_HOOKS_JS`) is installed on the **browser context**,
so it is evaluated in every page and every frame before a single line of application
script runs, wraps `Element.prototype.attachShadow`, and keeps the root it returns in a
`WeakMap` the page cannot reach. `el.shadowRoot` still reads `null` for the application,
so nothing about the page's own behaviour changes.

**This is not a browser security boundary.** A closed shadow root is an encapsulation
convention — the HTML spec says so in as many words — and the DOM it hides is same-origin,
same-process content the page already rendered to the user. Contrast fixture 04, where the
boundary IS real and the walker refuses to inject across it, using Playwright's frame APIs
instead.

## What this fixture used to assert

That the walker was honestly blind here: zero controls from inside the widget, and a
`closed_shadow` row in the opaque ledger so the blind spot was named rather than silent.
That anti-green-wash contract is unchanged and still binding everywhere it applies — what
changed is that this surface is no longer unreadable. A future change that installed the
hook later (on the page, after navigation, on demand) would silently restore the blind
spot while every other test stayed green; this fixture is what catches it.

## Lane

**Both.** jsdom implements `attachShadow`, and the runner installs the production hooks in
`beforeParse` — jsdom's equivalent of `addInitScript`, running on a window whose document
has not been parsed yet. The ordering is the behaviour under test, so the lane reproduces
it rather than approximating it, and reports `capabilities.capture_hooks_installed` with
every run so a result from a run where the hooks did not install can never be mistaken for
one where they did.

## Expected controls

Three: `input#zip` from the light DOM (the control group — the ordinary page must read
exactly as it did before the hook existed), plus `Coverage Amount` and `Get Quote` from
inside the closed root. `frame_selector` stays `""` for all three: a shadow root is the
same frame, and the compiler's `getByRole`/`getByLabel` pierce shadow DOM.

`Coverage Amount` resolves its name via `label[for]` against the **shadow root**, not the
host document, because an id reference does not cross a shadow boundary.

## Expected manifest

`tests/browser/golden/manifest_02-shadow-closed.json`, whose `coverage.opaque` must carry
one `closed_shadow_entered` row naming `closed-quote-widget` and how many controls were
read from inside it. **Observed, not opaque**: reporting a surface we read as a blind spot
understates coverage exactly as badly as the reverse overstates it.

## Targeted defect

Regression guard, re-aimed by M3.2 / T-FR-02 — see `expected.json`.

## Running this fixture alone

```bash
python -m pytest tests/browser -k 02-shadow-closed -v
```
