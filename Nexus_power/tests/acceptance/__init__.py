"""End-to-end acceptance suite for the visual-evidence pipeline.

This package validates that, for a curated library of recording fixtures,
the canonical pipeline produces evidence_steps matching the human-annotated
ground truth above the configured accuracy thresholds.

Fixtures live in :mod:`tests.acceptance.fixtures`; each fixture is a YAML
file declaring a video / artifact reference and the list of expected user
actions.  See ``fixtures/README.md`` for the manifest schema.
"""
