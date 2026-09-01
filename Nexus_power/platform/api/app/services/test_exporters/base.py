"""Test exporter abstractions (P5).

ExportContext bundles everything an exporter needs after the canonical
preparation pipeline runs:

  * test_cases — filtered to exportable lifecycle states + automation-gate-passed
  * controls_by_id — every cited control with its selector + automation_ready flag
  * tc_by_inst — test cases grouped by app_instance_id
  * inst_name_map — instance_id → human label
  * scenes_by_id, edges_by_id — for evidence resolution
  * artifact_id, tenant_id — for filenames + audit

ExportBundle is the universal output shape:
  bytes + content_type + filename

TestExporter is the abstract base; every format implements ``render``.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


def safe_name(text: str, max_len: int = 60) -> str:
    """Slug for filenames (alpha-num + _ -)."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", text)[:max_len] or "scenario"


@dataclass
class ExportContext:
    """Everything an exporter needs to produce an export bundle."""
    artifact_id: str
    tenant_id: str

    # Filtered test cases (post lifecycle + automation gates)
    test_cases: list[dict]

    # Lookup indexes
    controls_by_id: dict[str, dict]
    scenes_by_id: dict[str, dict]
    edges_by_id: dict[str, dict]

    # App-instance grouping
    tc_by_inst: dict[str, list[dict]]
    inst_name_map: dict[str, str]

    # Diagnostics for the response body
    scenarios_skipped_by_state: dict[str, int] = field(default_factory=dict)
    total_scenarios_before_filter: int = 0

    # Phase 2 — URL-keyed surfaces.  Each is empty when the page_visit
    # / page_action / form_snapshot extractors have not yet run.
    # Exporters append a URL-coverage appendix when these are non-empty,
    # giving QA engineers a per-URL view of the test plan independent
    # of how the canonical pipeline grouped frames into scenes.
    #
    # ``page_visits``    — list of {page_visit_id, sequence_index,
    #                       location, canonical_host, url_path, ...,
    #                       form_snapshot, form_snapshot_signals}
    # ``page_actions``   — {page_visit_id: [{subaction_index, verb,
    #                       target_label, target_kind, value, ...}]}
    # ``form_snapshots`` — {page_visit_id: {fields, field_kinds,
    #                       visible_field_count, overall_confidence,
    #                       page_intent, ...}}
    page_visits: list[dict] = field(default_factory=list)
    page_actions: dict[str, list[dict]] = field(default_factory=dict)
    form_snapshots: dict[str, dict] = field(default_factory=dict)


@dataclass
class ExportBundle:
    """Universal output of an exporter."""
    payload: bytes
    content_type: str
    filename: str


class TestExporter(ABC):
    """Abstract base for every test format exporter.

    Implementations should:
      * be stateless apart from the ExportContext passed in
      * never throw — return a bundle even when test_cases is empty
        (the preparation pipeline already validated non-emptiness)
    """

    #: Short identifier used in URLs (?format=playwright)
    format_id: str = ""
    #: User-visible label for the export dropdown
    label: str = ""
    #: Extension of an individual generated file (no dot)
    file_extension: str = ""

    def __init__(self, ctx: ExportContext) -> None:
        self.ctx = ctx

    @abstractmethod
    def render(self) -> ExportBundle:
        """Produce the export bundle. MUST set sensible filename + content_type."""
