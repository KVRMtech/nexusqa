# Fixture Application Library (T-HN-04)

Deterministic HTML applications, one per browser capability, used by **both**
execution lanes and by the characterization suite.

Each fixture is a self-contained directory served over HTTP:

```
NN-capability/
  index.html      the application            (required)
  expected.json   the declared contract      (required)
  README.md       purpose / defect / how to run it alone
  child-*.html    additional documents, for iframe fixtures
```

A directory missing `index.html` **or** `expected.json` is **not discovered** — a
fixture caught mid-creation must not crash every parametrised test in the suite. It is
not silently ignored either: `test_fixture_library.py::test_no_fixture_is_half_written`
reports it as one named failure.

## The library

| # | Fixture | Capability isolated | Lanes | Targeted defect |
|---|---|---|---|---|
| 01 | `shadow-open` | open shadow root, names resolved against the owning root | both | BUG-SHADOW-NAME ✅ fixed |
| 02 | `shadow-closed` | honest blindness + named opaque surface | both | guard |
| 03 | `iframe-same-origin` | same-origin descent, all 5 `frameSelectorFor` rungs | both | BUG-IFRAME-SELECTOR ✅ fixed |
| 04 | `iframe-cross-origin` | failing honestly at an origin boundary | playwright | guard |
| 05 | `aria-labelledby-multiblock` | rendered accessible name over block children | playwright | BUG-ARIA-LABELLEDBY-TEXTCONTENT ✅ fixed |
| 06 | `custom-listbox` | custom ARIA choice control + placeholder-is-not-a-value | both | guard |
| 07 | `native-select-250` | complete answer set + honest clipping | both | BUG-CATALOG-TRUNCATION-60 ✅ fixed |
| 08 | `radio-groups` | `group_key` — which question a control answers | both | guard |
| 09 | `questionnaire-20-samefingerprint` | 20 questions indistinguishable by `(role, name)` | both | guard |
| 10 | `save-draft-wizard` | declared validation + four distinct actuators | both | guard |
| 11 | `confirm-gated-step` | disabled/visibility — never offer a dead control | both | guard |
| 12 | `download-step` | link destination resolution | both | guard |
| 13 | `canvas` | DOM-opaque surface + three-snippet separation | both | guard |
| 14 | `capture-attributes` | the four attributes the field classifier reads | both | BUG-CAPTURE-ATTRS ✅ fixed |
| 15 | `shadow-nested` | nested roots + id collision across the shadow boundary | both | BUG-SHADOW-NAME (variation) ✅ fixed |
| 16 | `iframe-special-chars` | all four `frameSelectorFor` branches, escaped | playwright | BUG-IFRAME-SELECTOR (variation) ✅ fixed |
| 17 | `shadow-group-collision` | `group_key` uniqueness across shadow roots | both | guard (regression introduced by the CAP-02 fix) |
| 18 | `select-edge-cases` | option-ceiling BOUNDARIES + select read-back | both | guard |

Fixtures numbered 14 and above were added after the milestone against the same contract
and are picked up automatically — no harness change is needed to add one.

**14–17 (M0.x capture completeness).** 14 is a primary reproduction; 15 and 16 are the
*anti-overfit variations* for defects whose primary fixtures are 01 and 03 — each is
constructed so that a plausible-but-wrong fix passes the primary and fails the variation.
17 guards a collision the CAP-02 fix itself introduced: scoping name resolution to the
owning shadow root also scopes `groupContainerKey`'s positional lookup, so two anonymous
radiogroups in separate roots both keyed `ix:0` and merged into one question. 18 pins the
option-ceiling BOUNDARIES that 07 (which tests the ceiling at scale) leaves uncovered —
0/1/60 options, a selection at the END of the list, duplicate labels, an over-long label
and disabled options.

## `expected.json` contract

```jsonc
{
  "purpose":          "what ONE capability this isolates",
  "targeted_defect":  "BUG-… : diagnosis + file:line   |   None (regression guard)",
  "lanes":            ["jsdom", "playwright"],
  "lane_note":        "REQUIRED when only one lane is listed — why the other cannot adjudicate",
  "snippet":          "INVENTORY_JS | OPAQUE_JS | DISPLAYED_VALUES_JS",
  "min_controls":     5,
  "exact_control_count": 41,          // optional, when the count itself is the assertion

  "expect_controls": [{
    "where":  {"css_hint": "input#pin"},        // must identify EXACTLY one control
    "lanes":  ["playwright"],                   // optional per-expectation narrowing
    "fields": {"name": "Security PIN"},         // structured equality
    "list_lengths": {"options": 250},
    "list_edges":   {"options": {"first": "…", "last": "…"}},
    "href_suffix":  "/policy.pdf"
  }],

  "forbid_controls":            [{"where": {"name": "Card Number"}}],
  "describes_correct_behaviour":[ /* CORRECT behaviour Capture does not implement yet */ ],
  "already_correct":            [ /* a sibling rung that proves the defect is localised */ ],

  "group_key_partition":  {"expect_distinct_nonempty": 20, "expect_group_sizes": 2},
  "href_absoluteness":    {"exempt_schemes": ["javascript:"]},
  "opaque":               {"lanes": ["playwright"], "expect": [{"kind": "canvas"}]},
  "displayed_values":     {"lanes": ["jsdom","playwright"], "expect": [{"selector": "#x"}]}
}
```

`describes_correct_behaviour` is the bug-reproduction channel. Anything listed there is
executed by `test_known_bugs.py::test_described_correct_behaviour`, which xfails while the
defect is open and hard-fails under `QEC_BUG_REPRO_STRICT=1`. **A new defect needs a
fixture entry, not new test code.**

## Rules

1. **One capability per fixture.** If two things can break independently, they are two
   fixtures.
2. **Include a control group.** Something on the page that must work whether or not the
   subject does — it is what tells "the walker is broken" from "this branch is broken".
3. **Deterministic.** No clock, no randomness, no network beyond the fixture server. The
   two large fixtures (07, 09) are generated by a fixed script and committed as static
   files.
4. **Structured assertions only.** Never whitespace, serialized HTML, or source strings.
5. **Declare lane restrictions with a reason.** A restriction with no `lane_note` reads as
   an oversight and someone will widen it.
6. **Formatting can be load-bearing.** Fixture 05 has no whitespace between its block
   children *on purpose* — pretty-printing it makes `norm()` collapse the indentation into
   the separator the defect is about, and the fixture silently stops testing anything.
   Where this applies, the file says so.

## Origin tokens

The fixture server substitutes two tokens in any served HTML:

| Token | Becomes |
|---|---|
| `__SELF_ORIGIN__` | the primary origin, `http://127.0.0.1:<portA>` |
| `__ALT_ORIGIN__` | a genuinely foreign origin, `http://localhost:<portB>` |

Fixture 04 uses `__ALT_ORIGIN__` so its embed is really cross-origin rather than a
simulation. Substitution is server-side, so the file on disk stays a plain static page a
developer can open directly.

## Running

```bash
cd Nexus_power/engines/qe-explorer

python -m pytest tests/browser -k 07-native-select -v     # one fixture, every lane
python -m pytest tests/browser/test_jsdom_execution.py -v # the whole library, jsdom
python -m pytest tests/browser/test_fixture_library.py -v # validate the library itself
```

## Regenerating fixtures 07 and 09

Both are generated deterministically — 07 from `range(1, 251)` and `range(1700, 2020)`,
09 from a fixed 20-question list. Re-running the generator must produce byte-identical
output; it uses no clock and no randomness. The generated files are committed so the
fixtures are identical on every machine and in CI.
