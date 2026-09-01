"""QE-Central — S5 cycle package (design §3.5).

Change-triggered incremental regression, built fail-safe:

  * :mod:`.fingerprints`    — the fingerprint store (journey-graph seam,
    FAIL-SAFE to ``uncomputable`` when the endpoint / ``journey_graph.py`` is
    unavailable) + pure snapshot-diff helpers.
  * :mod:`.change_detector` — ``detect(...)`` = UNION of the repo-SHA diff and
    the journey-graph fingerprint diff; FAIL-SAFE-TO-CHANGED.
  * :mod:`.selector`        — ``select(...)`` = incremental selection with a P0
    criticality floor and age-labelled carry-forward (never a silent green).
  * :mod:`.driver`          — the lifespan-hosted cycle daemon (Stage-2).
"""
