# Crawl performance records

## `crawl_baseline.json` — the reference

Written by `measure_crawl_performance.py --reps 3 --baseline`. Records wall clock,
browser startup, artifact generation, throughput, per-phase attribution, peak RSS
and CPU across the process tree, with median / mean / P95 / worst per application.

**Reproduce it:**

```
python measure_crawl_performance.py --reps 3 --baseline
```

---

## The recorded baseline

`crawl_baseline.json`, three repetitions per application, on a quiet machine.

Windows 11 (10.0.26200) · 12 logical CPUs · 15.7 GB RAM · Python 3.10.11 ·
Playwright 1.49.0 · Chromium 131.0.6778.33.
Effective budget `{max_states: 40, max_depth: 6, max_actions_per_state: 30,
max_wall_ms: 1_800_000, max_requests: 4_000, rate_per_s: 1.0}`; rep cap 600 s.

| app | states | navs | port calls | median wall | P95 | spread |
|---|---|---|---|---|---|---|
| acme-life | 6 | 16 | 685 | 153 764 ms | 160 262 ms | 5.1% |
| catalog-evidence | 2 | 3 | 127 | 94 488 ms | 95 291 ms | 0.9% |
| vkpower-life | 5 | 11 | 318 | 68 838 ms | 70 443 ms | 2.5% |
| questionnaire-life | 2 | 3 | 114 | 20 497 ms | 20 888 ms | 3.4% |

`states_discovered`, `navigations` and `port_calls` were **identical across all
three repetitions of all four applications** — the summary marks each
`"stable": true`. No repetition hit the cap.

## How to read it — wall clock is context, not the signal

The same instrument, run earlier the same day while other work was on the machine,
produced 38–60% spreads for **byte-identical work**: `acme-life` did the same 6
states / 16 navigations / 685 port calls in 145 862, 163 172 and 232 798 ms.

That is the whole argument for what a regression check should compare:

* **Gate on** `states_discovered`, `navigations`, `interactions`, `port_calls` and
  `phases[*].n` — the deterministic counters, `"stable": true` in every run here —
  and on per-phase **medians**, which survive an outlier that destroys a mean.
* **Do not gate on** `crawl_wall_ms`. On a shared machine it moves 60% with no
  behavioural change; it would flag a regression most runs and hide real ones
  inside the noise.

Wall clock belongs in the record as context. The counters are the contract.

## Two defects this baseline found

### 1 · A crawl ran for 3 hours 8 minutes against a 30-minute budget

`catalog-evidence` rep 2 took **12 160 693 ms**. Same 2 states, same 3
navigations, same 127 port calls as the 83-second rep 1. The whole excess sits in
two calls:

| call | n | median | worst |
|---|---|---|---|
| `fill` | 7 | 656 ms | **11 317 974 ms** (3 h 08 m) |
| `click` | 3 | 745 ms | **751 334 ms** (12 m 31 s) |

`port_share_of_wall_pct` was **100.0%** — the crawler spent the entire run inside
the browser port.

**The budget did not fail; it was never consulted.**
`BudgetTracker.stop_reason()` (`app/budget.py:159`) compares `elapsed_ms` against
`max_wall_ms`, and the crawler polls it **between actions**. A port call that is
already blocked is never interrupted, so `max_wall_ms` bounds the *gaps* between
browser operations rather than the crawl. `_ACTION_TIMEOUT_MS = 5000` bounds one
locator action, but the port retries internally and nothing caps the aggregate.

This matters well beyond a benchmark: a crawl of a customer application can hang
indefinitely with a wall-clock budget configured and no stop reason recorded. A CI
job would hit its own timeout and produce no evidence at all.

**Not fixed here** — the right ceiling and what the manifest should record when it
trips are design decisions for the crawler's owner. The instrument now defends
itself with `QEC_PERF_REP_CAP_S` (default 900 s), which records a trip as data
instead of running away.

### 2 · `Budget.from_dict` silently ignores unknown keys

The budget passed to that crawl was:

```python
{"max_states": 40, "max_actions": 250, "max_requests": 4000, "max_duration_ms": 420_000}
```

`Budget.from_dict` reads `max_actions_per_state` and `max_wall_ms`. **Two of the
four keys were silently discarded**, so a crawl written to stop after 7 minutes
ran with the 30-minute default and 30 actions per state instead of 250:

```
effective: {'max_states': 40, 'max_depth': 6, 'max_actions_per_state': 30,
            'max_wall_ms': 1800000, 'max_requests': 4000, 'rate_per_s': 1.0}
```

This is **pre-existing**: `measure_boundary_crossing.py:121` carries the identical
typo and has been shipping with it. This instrument inherited it by being modelled
on that one — which is exactly how a silent-ignore defect spreads.

A budget is a safety control. A misspelled key should be an error, not a default.
`from_dict` rejecting unknown keys would have caught both instruments at their
first run.

Fixed here by using the names `Budget` reads, hoisting them to a single
`BUDGET_SPEC`, and reporting the **effective** budget read back off the `Budget`
object rather than a hand-written literal — the second copy is what let the report
claim 420 000 while the crawl ran 1 800 000, with both numbers in the same file.

### 3 · A reproducible ~61-second `select_option` stall

Steady across **four** measured repetitions of `catalog-evidence` — 60 779 /
60 695 / 62 029 / 61 282 ms worst, against a 1 039–1 257 ms median, and absent
from every other application (their worst `select_option` is ~1.1 s). Roughly two 30-second Playwright
waits, which points at a locator that never resolves and is retried once. Distinct
from defect 1 in the way that matters: defect 1 was seen **once** in twelve
crawls, so it is rare and catastrophic; this one is in **every** one, and alone
accounts for ~65% of a healthy 94-second `catalog-evidence` crawl.

---

## `crawl_contended_2026-08-20.json` — kept as a counter-example, NOT a reference

Recorded earlier the same day while four other squads' `pytest` processes were on
the machine. `acme-life` did identical work in two repetitions and took 98 845 ms
then 147 132 ms — a 49% spread with no behavioural difference at all.

Kept because it is the clearest single demonstration that wall clock on this
hardware measures the machine, not the crawler. **Do not diff future runs against
it.**
