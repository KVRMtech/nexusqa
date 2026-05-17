"""Test exporter package (P5).

One canonical preparation pipeline (fetch graph + Heart + state-gate +
5-gate check + grouping) feeds N format adapters. Add a new format by
implementing :class:`TestExporter` and registering it in :data:`REGISTRY`.

Public surface:
    prepare_export_context(...)  → ExportContext
    REGISTRY: dict[str, type[TestExporter]]
"""
from .base import ExportContext, ExportBundle, TestExporter
from .preparation import prepare_export_context
from .playwright import PlaywrightExporter
from .cypress import CypressExporter
from .gherkin import GherkinExporter
from .json_plan import JsonExporter

REGISTRY: dict[str, type[TestExporter]] = {
    "playwright": PlaywrightExporter,
    "cypress": CypressExporter,
    "gherkin": GherkinExporter,
    "json": JsonExporter,
}

__all__ = [
    "ExportContext",
    "ExportBundle",
    "TestExporter",
    "prepare_export_context",
    "PlaywrightExporter",
    "CypressExporter",
    "GherkinExporter",
    "JsonExporter",
    "REGISTRY",
]
