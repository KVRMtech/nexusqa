"""QE-Central — S5 Control Plane package (design §3.5).

The thinnest orchestration that makes 1000 clients / 10,000 apps economical
and polite:

  * ``scheduling/`` — vendored HealScheduler-shaped admission control
    (global + per-tenant caps, round-robin fairness, per-host token bucket).
  * ``cycle/``      — change-triggered incremental regression: the fingerprint
    store, the fail-safe change detector, the incremental selector, and the
    lifespan-hosted cycle driver.
  * ``cost/``       — the append-only, under-count-only cost meter.

NOTHING here runs without a change event / schedule tick / human trigger, and
every skipped case carries a time-bounded, age-labelled verdict — never a
silent green (the S5 honesty contract).
"""
