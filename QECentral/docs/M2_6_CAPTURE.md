# M2.6 — Capture: verification, deterministic advance memory, expansion before capture

**Status:** implemented and measured on this branch (`feat/qec-dynamic-catalog-p0-p6`).
Not committed, not deployed, not run against a live tenant.

Three tasks. One was a verification of work Phase 0 had already done; two were gaps
Phase 0 had not touched. Along the way the work surfaced one pre-existing binding
defect, which is documented here because it was blocking T-CAP-03 and is fixed.

---

## T-CAP-01 — verify the Phase-0 capture fixes

**Nothing was rewritten.** Phase 0's fixes were already covered by executing tests in
the browser harness; the task was to run them against the current implementation and
report what they measure. All three areas hold.

| area | fixture | what actually runs |
|---|---|---|
| 250-option select | `07-native-select-250`, `18-select-edge-cases` | `test_capture_contract.py::test_250_option_select_survives_every_layer`, `::test_clipping_is_deterministic_and_reports_the_true_size`, `::test_there_is_exactly_one_option_ceiling`, `::test_no_layer_reapplies_a_private_option_ceiling` |
| autocomplete-driven classification | `14-capture-attributes` | `::test_classifier_attributes_are_captured_in_chromium` / `_in_jsdom`, `::test_rung_one_fires_on_a_captured_control`, `::test_nameless_fields_classify_from_placeholder_and_id`, `::test_absent_empty_and_off_never_fabricate_a_verdict` |
| shadow / `aria-labelledby` naming | `01-shadow-open`, `05-aria-labelledby-multiblock`, `15-shadow-nested`, `17-shadow-group-collision` | `test_known_bugs.py::test_shadow_dom_names_resolve_against_the_owning_root`, `::test_aria_labelledby_uses_rendered_text`, `::test_aria_labelledby_name_is_bindable_by_playwright` |

Two of these are worth naming because they are *not* string assertions:

* the 250-option contract is checked **through every layer** — the browser snippet, the
  Python refiner, and the catalogue ceiling — and separately pins that no layer
  re-applies a private ceiling of its own. That is the shape the original defect took
  (JS 300, refiner 60, catalogue 48), and a test of any single layer would have missed it.
* `test_aria_labelledby_name_is_bindable_by_playwright` does not compare the captured
  name to an expected string. It hands the captured name back to `get_by_role` and
  requires it to resolve to exactly one element. A name that is *right* but not
  *bindable* fails.

---

## T-CAP-02 — persist tier-1 advance memory

### Why only LLM-tier advances were remembered

Two independent gates, either of which alone was sufficient:

1. **The explorer produced no key.** `AdvanceDecision.signature` — the value-free
   decision-point signature a memory is stored under — was set only on the tier-3 path,
   because it arrived in the oracle's reply. A tier-1/2 pick carried `signature=""`.
2. **qe-central filtered on provenance.** `advance_memory._proven_oracle_advances`
   skipped any step whose `advance.oracle` was falsy.

The consequence is the opposite of what a learning layer should do: on an ordinary
application, whose forward controls are named "Next" and "Continue", the crawl proved an
advance at *every step of every wizard* and stored **none of them** — then paid an LLM at
the first label a regex could not read. The system only ever learned from the case that
was most expensive to learn and rarest to re-encounter.

### The fix

`app/advance_signature.py` (qe-explorer) computes the identical signature locally, and
`Walker._deterministic_signature` attaches it to tier-1 and tier-2 decisions.
qe-central's harvest now folds in **every** proven advance and reports the
`oracle` / `deterministic` split.

Two constraints shaped it:

* **The key must be the one qe-central would have computed**, or the memory is written
  where nothing will look for it. qe-central signs the eligible set it *receives*, which
  is exactly `Walker._tier3_candidates` — so that is the set signed locally. The two
  services share no library, so the hash is a deliberate mirror pinned by a frozen vector
  in **both** suites (`test_signature_parity_vector`), the same doctrine the commit
  vocabulary already lives under. A cross-process contract cannot be proven inside one
  process; it can be frozen as data that both processes assert against.
* **A control the oracle may never pick is never remembered.** If the advancing control
  is outside the eligible set the signature is deliberately left empty. Every tier-2 pick
  is in this position by construction — tier 1 already takes any destination-shaped label
  without a commit word, so tier 2 fires *only* on the shape tier 1 vetoed (advance word
  + destination + commit word). Remembering "continue to payment" would put an answer
  into memory that recall is structurally forbidden to hand back, and — with consent on —
  contribute a commit word to the shared label pool. `test_every_tier2_pick_is_structurally_unrecallable`
  states this as a law rather than leaving it as an accident of one label.

**No LLM is required on the deterministic path.** Storing needs none (the explorer
computes the key itself). Recall needs none (`advance_agent.pick_advance` answers from
memory before it builds a prompt). `test_a_deterministic_advance_is_recalled_at_the_key_the_explorer_wrote`
runs both halves across the seam with the LLM client monkeypatched to count calls, and
asserts zero.

---

## T-CAP-03 — expansion before capture

### The gap

`isVisible()` in the capture snippet is *right* to refuse a control inside a collapsed
accordion: it is not on the page, and cataloguing it would be a capture-says-covered /
replay-cannot-bind claim of exactly the kind the browser harness was built to catch. The
consequence, though, is that the catalogue was quietly a catalogue of the **open parts**
of an application while being reported as the application.

### Capture had to answer one question first

For the pass to be deliberate rather than blind clicking, the crawler needs to know which
controls are doors and which of those are shut. Two of the three declarations were
already emitted raw (`aria-expanded`; `aria-selected` on a `role=tab`). The third was not
emittable at all:

```html
<details>              <!-- open is a live PROPERTY, not an attribute -->
  <summary>Medical history</summary>
```

`getAttribute("open")` does not track the state, and the only way to find out by clicking
is to **close every `<details>` that was already open**. So capture now emits
`disclosure` — `"collapsed" | "expanded" | ""` — normalised across all three. Nothing is
inferred from a class name or a chevron; a page that declares nothing is left completely
alone, which is also why a page with no collapsed UI pays **zero** browser round trips
(`test_a_page_with_nothing_collapsed_pays_nothing`).

### Two mechanisms, because there are two kinds of collapsed UI

**Additive disclosures** — accordions, `<details>`, `aria-expanded="false"` toggles —
are opened *in place*, before the state is fingerprinted, screenshotted or recorded
(`Discovery._expand_disclosures`). The acceptance test for each click is evidence, not
intent: after every open the page is re-read and the new control set must be a strict
**superset** of the one before. A click that revealed nothing is not recorded; a click
that left the page restarts the visit and abandons the pass; a click that gained and lost
is undone.

Bounded by `_MAX_EXPANSIONS = 12` in document order — a stable prefix, not a sample —
and what was left shut is logged rather than implied.

The opens are recorded as actions **on the state they produced, ahead of everything
else**. A field that only exists once a section is open is unbindable at replay unless
the run that binds it opens the section first; recording the field without recording the
open would be the very green-wash this milestone is meant to remove.

**Mutually exclusive views** — tab strips — are *not* opened in place, and the pass
refuses them from their ARIA declaration rather than discovering the problem by clicking.
Selecting one panel deselects another: merging both into one catalogued state would
describe a page no user has ever seen, and a script bound to it could not run. Unlike a
disclosure, a tab does not toggle, so clicking it again does not put the page back.

Refusing is only honest if the panel is recorded *somewhere*, so `Discovery._tab_views`
gives each unselected tab a state of its own: entered from a fresh load, reached by a
grounded recorded click, linked by an edge, fingerprint-deduped, and expanded again
inside the panel (a collapsed section inside a tab is still a shut door). Bounded by
`_MAX_TAB_VIEWS = 6` and by the crawl's own budget.

### Counters

`expansions_opened`, `expansions_skipped` and `tab_views_recorded` ride in the coverage
payload. A crawl that opened nothing on an accordion-heavy application and a crawl that
had nothing to open are otherwise indistinguishable in a manifest.

---

## A pre-existing defect found on the way, and fixed

`PlaywrightBrowserPort._locator` walked the compiler ladder and took the first rung that
did not **raise**. `get_by_role(role, name=...)` does not raise when it matches nothing —
it returns a locator over zero elements. So for every control carrying both a role and a
name (nearly all of them) the rungs below it were **unreachable**, including the css rung
capture had already recorded.

Measured, not reasoned: a `<summary>` inside a `<details>` matches `get_by_role` for **no
role at all** in Chromium — not button, not group, not generic — while capture calls it a
button, which is what the crawler needs behaviourally. Every click the crawl ever aimed at
a native disclosure therefore spent a full action timeout and returned `action_error`, and
no `<details>` on any application was ever opened. Its css rung, `summary`, was one line
further down the ladder.

`_bound_locator` now takes the first rung that actually matches an element, falling back
to the old behaviour when nothing matches so `locator_unresolved` still means what it
meant. Used on the acting path.

**Known remaining gap, not fixed here:** the *recorded* locator for a `<summary>` is still
`{strategy: accessible_name, role: button, bindable: true}`, which a generated script
cannot bind. The crawl can now open the section; a generated replay of it still cannot.
That is a compiler/locator-record question rather than a capture one, and it deserves its
own fixture and its own decision about what role a `<summary>` should be reported as.

---

## Evidence

| claim | how it was measured |
|---|---|
| the three Phase-0 fixes hold | `tests/browser/test_capture_contract.py`, `tests/browser/test_known_bugs.py` — both lanes, real Chromium + jsdom over the production snippet |
| a deterministic advance is stored | `qe-explorer/tests/test_advance_signature.py` (11 tests), `qe-central/tests/test_advance_memory.py` |
| …and reused with no LLM | `test_a_deterministic_advance_is_recalled_at_the_key_the_explorer_wrote` — LLM client counted, asserted zero |
| …under a key both services agree on | `test_signature_parity_vector`, frozen in both suites |
| a field behind a collapsed accordion is catalogued | `tests/browser/test_capture_expansion.py` — a real crawl through the production `Crawler` + `PlaywrightBrowserPort`, reading the manifest the production emitter wrote |
| …on a real application | `proving-grounds/acme-life` now asks three questions behind an accordion and a `<details>`, all optional and all off the submit path |
| the pass never submits or opens a menu | `test_the_expansion_pass_never_submits_an_application`, `test_a_menu_opener_is_not_folded_into_the_form` |
| tab panels are never merged | `test_two_tab_panels_are_never_merged_into_one_state` + `test_the_unselected_tab_panel_gets_a_state_of_its_own` |
| the pass costs nothing on a normal page | `test_a_page_with_nothing_collapsed_pays_nothing` |
