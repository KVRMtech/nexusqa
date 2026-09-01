# Fixture 12 — `download-step`

## Purpose

Isolate ONE capability: **link destination capture**. `href` is a diagnostic field that
drives the crawler's href-follow traversal, which is how it reaches routes on a pushState
SPA where a click produces no observable URL change.

## Expected controls

Seven captured, one deliberately absent.

| Subject | `href` in markup | Expected capture |
|---|---|---|
| A | `policy.pdf` (relative) | absolute, ends `/12-download-step/policy.pdf` |
| B | `/documents/schedule.pdf` | absolute, ends `/documents/schedule.pdf` |
| C | `#/claims` (SPA route) | absolute, keeps `#/claims` |
| D | `#faq` (scroll anchor) | absolute, keeps `#faq` |
| E | *(no href)* | **absent** — `SELECTOR` matches `a[href]` only |
| F | `javascript:window.print()` | captured verbatim, not resolved |
| G | *(a `<button>`)* | `href: ""` |
| H | `https://help.example.com/…` | unchanged |

A cross-cutting `href_absoluteness` assertion covers the whole set: every captured anchor
href except a `javascript:` scheme must be absolute.

## Why C and D are both here

`_norm_url()` in `browser.py` keeps a hash **route** (`#/…`, `#!/…`) and drops a pure
**scroll anchor** (`#faq`), because collapsing every SPA route to one URL makes
route-to-route navigation invisible and the crawler never leaves its entry page. The walker
captures both identically; the distinction is made downstream. Having both in one fixture
keeps the capture side honest about that division of labour.

## Why G matters

`Export to PDF` is a `<button>`: there is no href, so capture alone cannot tell a download
from any other action. The fixture records that limit explicitly (`href: ""`) rather than
implying the walker can classify downloads.

## Expected manifest

`tests/browser/golden/manifest_12-download-step.json`.

## Targeted defect

None — **regression guard** for `hrefOf()` (`inventory_js.py:378-384`), which reads the IDL
property `el.href` (auto-resolving) and only falls back to the raw attribute. Reading the
attribute first would hand the crawler `"policy.pdf"` with no origin to resolve it against.

## Note on `policy.pdf`

The target file does not exist; the fixture asserts on the **captured href string**, never
on fetching it. Nothing in this fixture performs a download, so nothing here is
timing-dependent.

## Running this fixture alone

```bash
python -m pytest tests/browser/test_jsdom_execution.py -k 12-download-step -v
python -m pytest tests/browser/test_playwright_execution.py -k 12-download-step -v
```
