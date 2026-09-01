# Fixture 24 — `iframe-ambiguous-attrs`

## Purpose

Isolate ONE capability: emitting a frame selector that identifies **exactly one frame**.

Fixture 03 and fixture 16 prove the selector is *escaped* — that it parses, and that a
metacharacter in an id, name, title or src does not silently turn it into a different
selector. Escaping is not identification. This fixture covers the two ways a
perfectly-parseable selector still names the wrong frame:

1. **Ambiguity.** Two embeds whose only identifying attribute is the same `title` (and a
   title full of quotes and brackets, so escaping and uniqueness are exercised together).
   One selector for two frames means `frameLocator` takes the first and says nothing — so
   every control captured in the second is recorded against the first. That reads as
   coverage, which is why it is worse than capturing nothing.
2. **Page-global indexing.** The positional rung emits `iframe >> nth=N`, which Playwright
   resolves against the whole document with a shadow-piercing selector engine. The ordinal
   used to be the loop index inside whichever subtree the walker happened to be standing
   in, so a frame nested in a shadow root was numbered among its shadow siblings — and the
   emitted selector bound to a completely unrelated frame.

## Lane

**Playwright only.** The acceptance is that each emitted selector resolves to exactly one
frame *and binds the control captured through it*, in a real engine with a real
shadow-piercing selector implementation. jsdom has neither a frame locator nor shadow
piercing, so it could only compare selector strings — which is precisely the test this
ticket must not be reduced to. The `lanes` key in `expected.json` states this, and the
jsdom suite skips this fixture by name rather than silently not asserting.

## Expected controls

Seven. `input#claim-ref` from the main frame, and one field per embed:

| control | frame_selector | rung |
|---|---|---|
| `input#step-one-field` | `iframe[title="payment \"step\" [1]"] >> nth=0` | title, disambiguated |
| `input#step-two-field` | `iframe[title="payment \"step\" [1]"] >> nth=1` | title, disambiguated |
| `input#shadow-frame-field` | `div#shadow-host >> iframe >> nth=0` | host addressed by **id** |
| `input#aside-frame-field` | `aside >> iframe >> nth=0` | host addressed by its **unique tag** |
| `input#section-frame-field` | `section >> nth=1 >> iframe >> nth=0` | host addressed **positionally** |
| `input#light-frame-field` | `iframe >> nth=2` | light-DOM positional |

Three host shapes, because addressing the frame is only half of it: the **host** has to be
addressable too, and the id rung is the easy one. A component library that never expected
to be addressed from outside gives its hosts neither ids nor unique tags reliably, and
before these two the id rung was the only one any fixture executed.

The shadow-nested frame is addressed **through its host** rather than by a document-wide
ordinal: Playwright scopes the right-hand side of `>>` to the left-hand element's subtree
and pierces open shadow roots on the way in, so the selector never depends on how two
different roots interleave. The light-DOM frame keeps a positional rung, and its `2` is
correct only if the ordinal counts the same frame set, in the same order, that Playwright
counts — this root's own tree first, then the shadow roots hanging off it. A depth-first
list, interleaving the shadow frame at its host's position, would make it `3`; that looks
more natural and is wrong.

## Expected manifest

`tests/browser/golden/manifest_24-iframe-ambiguous-attrs.json`, plus the resolution
contract in `frame_selectors_must_resolve`, asserted in `test_capture_contract.py` by
handing every emitted selector back to the browser. Resolving to one frame is not enough:
the control must be findable *inside* the frame the selector resolves to, because the whole
defect class here is a selector that confidently resolves to the wrong one.

## Targeted defect

`BUG-FRAME-AMBIGUOUS` (M3.2 / T-FR-03) — the two branches above, both in
`frameSelectorFor()`.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_playwright_execution.py -k 24-iframe-ambiguous-attrs -v
python -m pytest tests/browser/test_capture_contract.py -k ambiguous -v
```
