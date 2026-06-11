"""Nexus flywheel (Phase 2 foundation) — the consented failure→fix data substrate.

This package CAPTURES human/outcome corrections as de-identified labeled examples
(``ledger`` + ``featurize``). It does NOT train and does NOT export by itself —
cross-customer learning + consented export are separate, default-OFF, and
deferred to real data + infra. Everything here is additive and must never alter
the deterministic verdict/heal core.
"""
