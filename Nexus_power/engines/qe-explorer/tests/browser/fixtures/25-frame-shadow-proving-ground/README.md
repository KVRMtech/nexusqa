# Fixture 25 — `frame-shadow-proving-ground`

## Purpose

The M3.2 proving ground: **one page carrying both previously-opaque surfaces at once**,
with ordinary DOM beside them.

* a **cross-origin payment embed** — card number, expiry, billing postcode, a security
  code and a Pay button, served from a genuinely foreign origin (the fixture server
  substitutes `__ALT_ORIGIN__` with a second `http://localhost:<port2>` at serve time), so
  `contentDocument` throws exactly as it does for Stripe Elements, Adyen or Braintree;
* a **closed shadow component** (`<secure-consent-panel>`) with a further **open shadow
  root nested inside it**, so "we read the closed root" cannot quietly mean "we read its
  first level";
* a **closed shadow component inside the foreign frame** (`<card-security-block>`, holding
  the CVC), which is only observable if the capture hook is installed on the browser
  **context** — a page-level install would never reach a frame on another origin;
* and ordinary labelled controls, which must go on being captured exactly as before.

The two halves fail in opposite directions, which is why they are fixtured together: frame
entry that fabricated controls and a shadow hook that silently stopped installing would
each leave a green suite if only the other surface were covered.

## Lane

**Playwright only.** The cross-origin half needs true origin isolation — jsdom has none, so
a second origin cannot be simulated faithfully — and frame entry goes through Playwright's
own frame APIs. The closed-shadow capability on its own is covered in *both* lanes by
fixture 02; this fixture is about the two together, at a real origin boundary.

Nothing is injected across that boundary. `content_frame()` asks the browser for the
frame's own execution context and the walker then runs inside it under that frame's origin,
exactly as the frame's own scripts do. Origin isolation is used, not circumvented.

## Expected controls

Eleven, and the mix is the point:

| control | where it lives | frame_selector |
|---|---|---|
| `input#holder-name`, `input#holder-email` | light DOM | `""` |
| `input#consent-initials`, `#accept-terms` | closed shadow root | `""` |
| `input#disclosure-ack` | open shadow root nested inside the closed one | `""` |
| `input#card-number`, `#card-expiry`, `#billing-postcode`, `Pay Now` | cross-origin frame | `iframe#payment-frame` |
| `input#card-cvc` | closed shadow root **inside** the cross-origin frame | `iframe#payment-frame` |
| `Review Order` | light DOM | `""` |

## Expected manifest

`tests/browser/golden/manifest_25-frame-shadow-proving-ground.json`, whose
`coverage.opaque` must carry a `cross_origin_iframe` row (the frame stays a *named* surface
even though it was entered — that row is what carries its selector) and a
`closed_shadow_entered` row (the closed root is reported as **observed**, not as a blind
spot; calling a surface we read a blind spot understates coverage exactly as badly as the
reverse overstates it), together with a `frame_entered` row from the port's own ledger.

`frame_selectors_must_resolve` additionally requires the payment frame's selector to be
handed back to the browser and to bind the fields captured through it — i.e. the
catalogued payment fields are **actionable**, not merely recorded.

## Targeted defect

None (regression guard) for M3.2 as a whole — see `expected.json`.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_playwright_execution.py -k 25-frame-shadow -v
python -m pytest tests/browser/test_capture_contract.py -k proving_ground -v
```
