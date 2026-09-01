# Fixture 16 — `iframe-special-chars`

## Purpose

The **anti-overfit variation** for `BUG-IFRAME-SELECTOR`. Fixture 03 covers a dot in an `id`
and a double quote in a `name`. `frameSelectorFor()` has **four** branches — `id`, `name`,
`title`, `src` — and every one of them interpolated raw, so escaping only the two characters
fixture 03 happens to use would leave the rest broken.

This fixture widens the character set to what a real `title` attribute actually contains, and
exercises the `title` branch, which had never been escaped at all.

| Frame | Attribute used | Characters exercised |
|---|---|---|
| `title='customer&apos;s [account] "primary"'` | `title` | apostrophe, brackets, double quote, spaces |
| `title="back\slash and (parens)"` | `title` | backslash (the CSS escape character itself), parentheses |
| `id="step[2].section"` | `id` | brackets + dot — the *identifier* branch, which needs `CSS.escape` |
| `name='a b "c" \d'` | `name` | quotes + backslash + spaces — the *attribute-value* branch |

Frames (a) and (b) deliberately carry no `id` and no `name`, because selector precedence is
`id > name > title > src` and the title branch is otherwise unreachable.

## Expected controls

Five: one in the main frame plus one per child frame (`input#field-a` … `input#field-d`), each
in its own child document so a control can be traced back to the exact frame it came from.

## Expected manifest

`tests/browser/golden/inventory_16-iframe-special-chars.json`. The four emitted selectors are:

```text
iframe[title="customer's [account] \"primary\""]
iframe[title="back\\slash and (parens)"]
iframe#step\[2\]\.section
iframe[name="a b \"c\" \\d"]
```

Note the two escaping *positions*: `CSS.escape` for the identifier after `#`, and CSS string
escaping (backslash and double quote only) inside a quoted attribute value. Spaces, brackets,
apostrophes and parentheses are already legal inside a CSS string and are left alone.

## Targeted defect — BUG-IFRAME-SELECTOR (variation)

Measured against the pre-fix walker, all four were broken:

```text
iframe[title="customer's [account] "primary""]   ← unparseable (inner quotes terminate)
iframe[title="back\slash and (parens)"]          ← the backslash eats the "s"
iframe#step[2].section                           ← VALID CSS meaning something else
iframe[name="a b "c" \d"]                        ← unparseable
```

The acceptance is deliberately **not** a string comparison. `test_capture_contract.py` hands
each emitted selector back to the browser and requires it to resolve to exactly one frame —
the operation a generated script performs at replay. A selector can be beautifully escaped and
still wrong, and "fixing" the escaping by broadening to `iframe` would satisfy a string test
while silently binding every control to the first frame on the page.

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer
python -m pytest tests/browser -q -k 16-iframe-special-chars
python -m pytest tests/browser/test_capture_contract.py -q -k frame_selector
```
