# Crawl performance records

## `crawl_baseline.json` — the reference. **Does not exist yet.**

Written by `measure_crawl_performance.py --baseline`, and only ever on a QUIET
machine. It is the permanent reference future runs are compared against, so a
number recorded under load would silently raise the bar for every later
comparison and hide the regressions it exists to catch.

## `crawl_contended_2026-08-20.json` — NOT the baseline

Recorded during Gate 0 while **four other squads' pytest processes** were running
against the same machine. Kept because it proves the instrument produces every
metric the gate asks for, and because two of its findings survive the noise.

**Do not diff future runs against this file.** It is an artefact of a specific
bad afternoon.

### What the contention looks like, measured

`acme-life` did **identical work** in both repetitions — 6 states, 16
navigations, 685 port calls, byte for byte the same crawl — and took:

| rep | wall |
|---|---|
| 1 | 98 845 ms |
| 2 | 147 132 ms |

**A 49% spread with zero behavioural difference.** That single pair is the whole
argument for why the baseline needs a quiet machine: the work was constant and
only the environment moved. `vkpower-life`, measured minutes later, varied 0.6%
(75 055 / 75 470 ms) — so the noise is bursty, not a constant tax, which is worse.
An average over a contended machine would look plausible and be meaningless.

### A real finding that is NOT noise

`catalog-evidence` spends **61 seconds inside one `select_option` call**:

| rep | worst `select_option` | median | crawl wall | share |
|---|---|---|---|---|
| 1 | 61 637 ms | 1 272 ms | 101 531 ms | 61% |
| 2 | 61 117 ms | 1 164 ms | 98 778 ms | 62% |

It reproduces to within half a second across both reps, which contention does
not do — the acme-life spread above is what contention looks like. One call runs
~50× its own median and accounts for **more than half the entire crawl**.

The shape (≈61 s ≈ two 30 s Playwright waits) points at a locator that never
resolves and is retried once, rather than at slow work. Nothing measured this
before, because `measure.py` never touched a browser.

**Not fixed under Gate 0** — it is crawler/fill-engine behaviour, not durability
— but it is the first thing the baseline found, and it should be triaged before
anyone tunes anything else.
