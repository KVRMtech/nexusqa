"""QE-Central S4 — scenario-governance services ("the 1%").

Deterministic, ZERO-LLM services that turn crawl substrate into a governed
regression contract:

  * :mod:`app.services.approval` — append-only, hash-chained approval events +
    e-signed universe baselines (Stage-1).
  * :mod:`app.services.coverage` — atoms-vs-invariants coverage model, the
    universe-shrinkage guard, and the waiver lifecycle (Stage-1).

Other S4 services (criticality, synthesis, tier_label, touch_meter) land in
their own modules and share the ORM in :mod:`app.db.gov_models`.
"""
