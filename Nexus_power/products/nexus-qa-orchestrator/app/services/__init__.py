"""Orchestrator services — pure-Python algorithmic modules for evidence processing.

These modules define the canonical algorithms for scene grouping, app segmentation,
control extraction, and flow graph construction.  They have no dependencies on
FastAPI, SQLAlchemy, or any network I/O — pure computation over dict payloads.

The Spine engine implements the same algorithms inside its HTTP endpoints using
this code as the authoritative reference.
"""
