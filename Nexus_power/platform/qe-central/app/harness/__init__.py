"""QE-Central — Phase-0 REFUSE harness (design §3.1 REFUSE matrix).

The harness proves — before any explorer exists — that the UNCHANGED
VKPower factory chain (``/generate → /playwright → /auto-heal/run-config →
/verify``) REFUSES honestly when any evidence rule is broken, and that a
golden fixture passes the positive path (``PASS_BASELINE``).

Modules:
  * ``rules``  — the R1-R8 refusal rules as pure DATA (fixture mutation +
    chain step + refusal / green-wash predicates + verified contract pins)
    plus the verdict classifier.  Importable with zero service deps so the
    unit suite runs anywhere.
  * ``runner`` — drives the matrix against the REAL factory over HTTP with
    a minted service JWT, persists one ``qe_harness_runs`` row per rule
    (full HTTP evidence JSONB), and exposes a CLI deploy gate
    (``python -m app.harness.runner``) whose exit code is non-zero on any
    ``GREEN_WASH_DETECTED``.

This package deliberately keeps ``__init__`` import-free: ``rules`` is
pure, while ``runner`` pulls the substrate writer stack — consumers import
the module they need directly.
"""
