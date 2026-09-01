"""JSON test plan exporter (P5).

Single ``test-plan.json`` document with the full machine-readable test
plan: every scenario, every step, every evidence citation, every selector,
plus a top-level metadata block describing the source artifact.

Designed for downstream consumers (custom CI pipelines, test management
systems, audit logs) that want structured data instead of executable code.

The JSON schema is versioned (``version: "1.0"``) so consumers can detect
breaking changes.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

from .base import ExportBundle, TestExporter


_JSON_SCHEMA_VERSION = "1.0"


def _serialize_step(step: dict, controls_by_id: dict[str, dict]) -> dict:
    ctrl_id = step.get("evidence_control_id") or ""
    ctrl = controls_by_id.get(ctrl_id) or {}
    return {
        "step_number": step.get("step_number"),
        "action": step.get("action") or "",
        "target_element": step.get("target_element") or "",
        "input_data": step.get("input_data") or "",
        "expected_output": step.get("expected_output") or "",
        "selector": ctrl.get("playwright_selector") or "",
        "selector_source": ctrl.get("selector_source") or "",
        "selector_confidence": ctrl.get("selector_confidence"),
        "automation_ready": bool(ctrl.get("automation_ready", False)),
        "evidence": {
            "scene_id": step.get("evidence_scene_id") or "",
            "control_id": ctrl_id,
            "edge_id": step.get("evidence_edge_id") or "",
            "proof_confidence": step.get("proof_confidence", 0.0) or 0.0,
        },
    }


def _serialize_scenario(
    tc: dict, controls_by_id: dict[str, dict],
) -> dict:
    return {
        "scenario_id": tc.get("scenario_id"),
        "title": tc.get("title") or "",
        "strategy": tc.get("strategy") or "happy_path",
        "category": tc.get("category") or "observed",
        "priority": tc.get("priority") or "P1_high",
        "rationale": tc.get("rationale") or "",
        "preconditions": list(tc.get("preconditions") or []),
        "expected_outcome": tc.get("expected_outcome") or "",
        "workflow_steps_covered": list(tc.get("workflow_steps_covered") or []),
        "risk_areas_addressed": list(tc.get("risk_areas_addressed") or []),
        "data_matrix": list(tc.get("data_matrix") or []),
        "steps": [
            _serialize_step(s, controls_by_id)
            for s in (tc.get("steps") or [])
        ],
        "visual_proven_steps": tc.get("visual_proven_steps"),
        "visual_total_steps": tc.get("visual_total_steps"),
        "lifecycle_state": tc.get("state") or "approved",
    }


class JsonExporter(TestExporter):
    format_id = "json"
    label = "JSON test plan (.json)"
    file_extension = "json"

    def render(self) -> ExportBundle:
        scenarios = [
            _serialize_scenario(tc, self.ctx.controls_by_id)
            for tc in self.ctx.test_cases
        ]

        plan = {
            "version": _JSON_SCHEMA_VERSION,
            "generator": "nexus-qa-visual-strict",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "artifact_id": self.ctx.artifact_id,
            "tenant_id": self.ctx.tenant_id,
            "summary": {
                "total_scenarios": len(scenarios),
                "scenarios_skipped_by_state": dict(self.ctx.scenarios_skipped_by_state),
                "total_scenarios_before_filter": self.ctx.total_scenarios_before_filter,
                "app_instances": [
                    {
                        "instance_id": inst_id,
                        "name": self.ctx.inst_name_map.get(inst_id, inst_id),
                        "scenario_count": len(tcs),
                    }
                    for inst_id, tcs in self.ctx.tc_by_inst.items()
                ],
            },
            "scenarios": scenarios,
        }

        body = json.dumps(plan, indent=2, ensure_ascii=False).encode("utf-8")

        # JSON exports as a single .json wrapped in a ZIP for download
        # consistency with other formats.  Frontend treats the ZIP uniformly.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"test-plan-{self.ctx.artifact_id[:8]}.json", body)
        buf.seek(0)
        return ExportBundle(
            payload=buf.getvalue(),
            content_type="application/zip",
            filename=f"test-plan-json-{self.ctx.artifact_id[:8]}.zip",
        )
