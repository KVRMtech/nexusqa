# Browser Test Harness (M0.2)

The verification foundation for every future Capture, Crawl, Inventory and Manifest
change to the QE Explorer.

## Why it exists

Before this milestone the browser layer was, in effect, untested. `inventory_js.py` is
~800 lines of JavaScript that runs inside every crawled page, and the tests covering it
asserted things like:

```python
assert 'lc(attr(el, "aria-checked"))' in INVENTORY_JS      # tests/test_custom_toggle_state.py
```

That assertion passes whether or not the expression is ever reached, whether or not it sits
in dead code, and whether or not the value it produces is correct. It is a test of the
source text, not of the behaviour.

**Five real defects were found within hours of executing that code instead of reading it** —
each one had been passing the string-assertion suite cleanly. All five are now fixed and
guarded:

| ID | Defect | Consequence |
|---|---|---|
| BUG-SHADOW-NAME | `walk()` forwarded the outer `doc` into a shadow root | every shadow-scoped control captured unnamed → dropped by the compiler |
| BUG-IFRAME-SELECTOR | `frameSelectorFor()` interpolated unescaped | `iframe#pay.frame` matched 0 elements; `iframe[name="quote"frame"]` was a parse error |
| BUG-ARIA-LABELLEDBY-TEXTCONTENT | `idText()` used `textContent`, not rendered text | captured name matched **0** elements under `get_by_role` |
| BUG-CATALOG-TRUNCATION-60 | `inventory.py` re-truncated options to 60 | the JS ceiling of 300 was nullified one layer down |
| BUG-CAPTURE-ATTRS | `describe()` emitted no `autocomplete`/`inputmode`/`placeholder`/`id` | `field_semantics.classify()` rung 1 was unreachable code |

Four of the five are *capture-says-covered / replay-cannot-bind* green-washes: the manifest
records the control as captured, and the generated script cannot bind it.

## Layout

```
browser_harness/
  package.json          jsdom dependency
  jsdom_runner.js       executes a PRODUCTION snippet inside jsdom
  README.md             this file

tests/browser/
  _harness.py                     fixture server, lanes, normalisation, comparison
  conftest.py                     session-scoped lanes + fixture parametrisation
  fixtures/                       the fixture application library  (see its README)
  golden/                         recorded characterization snapshots
  test_jsdom_execution.py         T-HN-01
  test_playwright_execution.py    T-HN-02
  test_browser_characterization.py T-HN-03
  test_fixture_library.py         T-HN-04
  test_known_bugs.py              T-HN-05
  test_capture_contract.py        T-HN-05 (capture-completeness family)
  test_proving_grounds.py         T-HN-06
  test_coverage.py                execution-coverage gate
```

## The two lanes

Both execute the **real** `app.inventory_js` constants. The harness ships no JavaScript of
its own and duplicates no production logic —
`test_jsdom_execution.py::test_production_snippet_is_read_not_copied` and
`test_playwright_execution.py::test_the_lane_uses_the_production_adapter` enforce that.

| | jsdom | Playwright |
|---|---|---|
| engine | jsdom 24 on Node | real headless Chromium |
| how the JS is delivered | `window.eval(<the constant>)` | `PlaywrightBrowserPort.collect_controls()` |
| injection path | equivalent to `page.evaluate(<expression>)` | **the production one** |
| speed | ~1s per fixture | ~5s per fixture |
| authoritative for | structure, grouping, options, attributes | rendered names, layout, cross-origin, contenteditable |

The Playwright lane goes through `app.main.PlaywrightBrowserPort` — the same class
`_run_job` constructs for a live crawl — so `goto()` carries the production 429 backoff,
hash-router nudge and `_settle()` quiescence wait.

### jsdom limitations, probed rather than assumed

`test_jsdom_capability_probe` pins what jsdom actually provides. Three APIs are missing:

| API | Handling | Why |
|---|---|---|
| `CSS.escape` | **supplied** (CSSOM spec algorithm, on the environment) | jsdom 24 has no `CSS` object at all, so the walker's first accessible-name rung throws into its own `try/catch` and *every* labelled control comes back unnamed. Without it the lane measures the runtime, not the walker. |
| `innerText` | **not supplied** | faking it would fabricate the exact behaviour fixture 05 exists to adjudicate. Those assertions run in Chromium. |
| `isContentEditable` | **not supplied** | same reason. |

The shim is applied to the *environment*, never to the snippet, and is reported as
`capabilities.css_escape_polyfilled`. If a jsdom upgrade ever provides these natively, the
probe **fails** — an instruction to move assertions back, not a silent drift.

## Characterization

```
Fixture App → real Crawler → manifest.jsonl → normalize → golden → byte-compare
```

Two goldens per fixture, because the manifest is a lossy projection of the capture:

* `manifest_<fixture>.json` — the normalised crawl manifest (the production interface)
* `inventory_<fixture>.json` — the complete `RawControl` array from the production port

The manifest-only golden was blind to BUG-CATALOG-TRUNCATION-60; the inventory golden is
what closes that class of gap.

**Normalisation touches only run-varying values** — minted ids, clock *readings*, per-run
paths, the ephemeral port. It is an explicit allowlist, not a `*_ms` suffix rule, because
`max_wall_ms` is a declared *budget* and normalising it would let a silently reconfigured
crawl pass its own golden. `test_normalisation_does_not_erase_behaviour` and
`test_declared_bounds_are_not_normalised` pin both directions.

`test_a_behavioural_change_breaks_the_golden` proves the net catches things: it mutates the
production constant in memory, re-runs the crawl, requires a diff, reverts, and requires
green again.

## Coverage

Measured with **V8 precise coverage** over a raw CDP session while the whole fixture
library runs through the production injection path. No instrumentation, no `sourceURL`
comment, no wrapper — the script is identified by matching its *source*, fetched back from
the debugger, against the production constant.

Two measurement bugs were found and fixed while building this, both caught because the
number contradicted a passing test:

1. a single harvest at the end only saw the **last** fixture (a `goto` discards the
   previous execution context) — harvest per fixture instead;
2. uncovered ranges from one page erased bytes another page covered, because V8 emits
   different range boundaries per instance — resolve bytes **per instance**, then union.

## Running

```bash
cd Nexus_power/engines/qe-explorer
npm ci --prefix browser_harness
python -m playwright install chromium

python -m pytest tests/browser -v                              # everything
python -m pytest tests/browser -m jsdom -v                     # fast lane only
python -m pytest tests/browser -k 07-native-select -v          # one fixture
QEC_UPDATE_GOLDENS=1 python -m pytest tests/browser -k golden  # re-record (review the diff!)
QEC_BUG_REPRO_STRICT=1 python -m pytest tests/browser/test_known_bugs.py  # open bugs fail hard
```

Markers: `browser`, `jsdom`, `playwright`, `characterization`, `bug_repro`,
`proving_ground`.

## Adding a defect reproduction

1. Add a fixture (or a control to an existing one) that exhibits the markup.
2. Describe the **correct** behaviour in `expected.json` → `describes_correct_behaviour`.
   Never write what the code currently does.
3. That is all. `test_described_correct_behaviour` picks it up, xfails while the defect is
   open, and hard-fails under `QEC_BUG_REPRO_STRICT=1`.
4. When Capture is fixed, the strict marker turns the silent fix into an `XPASS(strict)` CI
   failure. Remove the marker — the assertions stay **unchanged** — and the reproduction
   becomes a permanent regression guard. This cycle has already run once, for all five
   defects above.
