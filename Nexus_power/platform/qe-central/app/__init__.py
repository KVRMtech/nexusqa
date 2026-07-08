"""QE-Central — crawler-evidence substrate writer + Phase-0 REFUSE harness.

The QE-Central service (design: QECentral/docs/IMPLEMENTATION_DESIGN.md §3.1)
turns an ``ExplorationBundle`` into the §2 substrate rows the UNCHANGED
VKPower factory reads, and proves — via the REFUSE harness — that the whole
chain refuses honestly when any evidence rule is broken.

Bounded-context rules (R-1/R-7):
  * QE-Central-owned tables live in the carved-out ``qecentral`` logical DB
    (role ``qec``); substrate rows are written into the ``nexus`` DB through
    a second least-privilege DSN (role ``qec_substrate``).
  * VKPower code is NEVER imported from here — the seam is the database and
    the audited HTTP factory endpoints.
"""
