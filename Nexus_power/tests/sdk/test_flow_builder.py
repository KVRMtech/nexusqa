"""
Targeted tests for FlowBuilder and flow edge isolation.

Covers:
  - Observed transitions for consecutive scenes in the same flow
  - Cross-flow pairs emitted as ``app_switch`` (context-switch arcs)
  - Transition-pair LLM enrichment (Wave B1)
  - Edge enrichment does not change edge count
  - Single-flow artifacts produce a linear chain
  - Empty / single-scene inputs are handled gracefully
"""
import uuid

import pytest

from nexus_sdk.evidence.flow_builder import FlowBuilder


def _scene(index: int, flow_id: str = "", scene_id: str = "", **kw) -> dict:
    """Build a minimal scene dict for testing."""
    sid = scene_id or str(uuid.uuid4())
    return {
        "scene_id": sid,
        "scene_index": index,
        "flow_id": flow_id,
        "start_ms": index * 1000,
        "end_ms": index * 1000 + 999,
        "app_instance_id": kw.get("app_instance_id"),
        "ocr_text": kw.get("ocr_text", ""),
        "detected_url": kw.get("detected_url", ""),
    }


ARTIFACT_ID = "test-artifact-001"
TENANT_ID = "test-tenant"

FLOW_A = str(uuid.uuid4())
FLOW_B = str(uuid.uuid4())


class TestObservedTransitions:
    """Layer 1 edge generation."""

    def test_single_flow_produces_linear_chain(self):
        scenes = [_scene(i, flow_id=FLOW_A) for i in range(5)]
        builder = FlowBuilder()
        edges = builder.build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)

        assert len(edges) == 4
        for e in edges:
            assert e["edge_type"] == "observed_transition"
            assert e["evidence_confidence"] == 1.0

    def test_cross_flow_boundary_is_app_switch(self):
        """Boundary between flows becomes an ``app_switch`` edge (not dropped)."""
        scenes = [
            _scene(0, flow_id=FLOW_A),
            _scene(1, flow_id=FLOW_A),
            _scene(2, flow_id=FLOW_A),
            _scene(3, flow_id=FLOW_B),  # flow boundary
            _scene(4, flow_id=FLOW_B),
            _scene(5, flow_id=FLOW_B),
        ]
        builder = FlowBuilder()
        edges = builder.build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)

        # 2 intra-A + 1 cross + 2 intra-B = 5
        assert len(edges) == 5
        cross = [e for e in edges if e["edge_type"] == "app_switch"]
        assert len(cross) == 1
        scene_to_flow = {s["scene_id"]: s["flow_id"] for s in scenes}
        cf = cross[0]
        assert scene_to_flow[cf["from_scene_id"]] != scene_to_flow[cf["to_scene_id"]]

        intra = [e for e in edges if e["edge_type"] == "observed_transition"]
        assert len(intra) == 4
        for e in intra:
            assert scene_to_flow[e["from_scene_id"]] == scene_to_flow[e["to_scene_id"]]

    def test_interleaved_flows_use_app_switch_each_step(self):
        """When flow_id alternates every scene, every consecutive pair switches app."""
        scenes = [
            _scene(0, flow_id=FLOW_A),
            _scene(1, flow_id=FLOW_B),
            _scene(2, flow_id=FLOW_A),
            _scene(3, flow_id=FLOW_B),
        ]
        builder = FlowBuilder()
        edges = builder.build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)

        assert len(edges) == 3
        assert all(e["edge_type"] == "app_switch" for e in edges)

    def test_no_flow_id_falls_through(self):
        """When scenes lack flow_id, edges are still created (backwards compat)."""
        scenes = [_scene(i) for i in range(3)]  # no flow_id
        builder = FlowBuilder()
        edges = builder.build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)

        assert len(edges) == 2

    def test_mixed_flow_and_no_flow(self):
        """Scenes without flow_id adjacent to ones with flow_id → edge created."""
        scenes = [
            _scene(0, flow_id=FLOW_A),
            _scene(1),  # no flow_id
            _scene(2, flow_id=FLOW_A),
        ]
        builder = FlowBuilder()
        edges = builder.build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)

        # No flow_id on scene 1 → both boundary edges pass (no filtering)
        assert len(edges) == 2

    def test_empty_scenes(self):
        builder = FlowBuilder()
        assert builder.build_observed_transitions([], ARTIFACT_ID, TENANT_ID) == []

    def test_single_scene(self):
        builder = FlowBuilder()
        edges = builder.build_observed_transitions(
            [_scene(0, flow_id=FLOW_A)], ARTIFACT_ID, TENANT_ID
        )
        assert edges == []

    def test_edge_ids_are_deterministic(self):
        scenes = [_scene(i, flow_id=FLOW_A, scene_id=f"scene-{i}") for i in range(3)]
        builder = FlowBuilder()
        edges_a = builder.build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)
        edges_b = builder.build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)

        assert [e["edge_id"] for e in edges_a] == [e["edge_id"] for e in edges_b]


class TestEnrichWithActions:
    """Layer 2 edge enrichment preserves flow isolation."""

    def test_enrichment_does_not_add_edges(self):
        """Enrichment promotes edges in place; count stays fixed."""
        scenes = [
            _scene(0, flow_id=FLOW_A, detected_url="https://example.com/page1"),
            _scene(1, flow_id=FLOW_A, detected_url="https://example.com/page2"),
            _scene(2, flow_id=FLOW_B, detected_url="https://other.com/"),
        ]
        builder = FlowBuilder()
        layer1 = builder.build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)

        assert len(layer1) == 2

        frames = [
            {"scene_id": scenes[0]["scene_id"], "frame_index": 0, "description": ""},
        ]
        enriched = builder.enrich_with_actions(layer1, frames, [], scenes)

        assert len(enriched) == 2
        scene_to_flow = {s["scene_id"]: s["flow_id"] for s in scenes}
        for e in enriched:
            if e["edge_type"] == "app_switch":
                continue
            assert scene_to_flow[e["from_scene_id"]] == scene_to_flow[e["to_scene_id"]]

    def test_transition_llm_pairs_enrich_edge(self):
        s_a = _scene(0, flow_id=FLOW_A, scene_id="scene-a", ocr_text="before")
        s_b = _scene(1, flow_id=FLOW_A, scene_id="scene-b", ocr_text="after")
        scenes = [s_a, s_b]
        builder = FlowBuilder()
        layer1 = builder.build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)
        f_exit, f_entry = "frame-exit-1", "frame-entry-0"
        frames = [
            {"frame_id": f_exit, "scene_id": "scene-a", "frame_index": 1, "description": ""},
            {"frame_id": f_entry, "scene_id": "scene-b", "frame_index": 0, "description": ""},
        ]
        st = [
            {
                "from_frame_id": f_exit,
                "to_frame_id": f_entry,
                "action_kind": "click_cta",
                "action_label": "Clicked Continue",
                "target_element_label": "",
                "observed_value": "",
                "confidence": 0.88,
                "evidence_text": "Button state changed",
            }
        ]
        enriched = builder.enrich_with_actions(
            layer1, frames, [], scenes, scene_transitions=st
        )
        assert len(enriched) == 1
        e0 = enriched[0]
        assert e0["edge_type"] == "action_confirmed_transition"
        assert e0["action_type"] == "click"
        assert "Continue" in (e0.get("primary_action_summary") or {}).get("action_label", "")

    def test_transition_does_not_override_stronger_url_signal(self):
        s_a = _scene(
            0,
            flow_id=FLOW_A,
            scene_id="scene-a",
            ocr_text="x",
            detected_url="https://example.com/page1",
        )
        s_b = _scene(
            1,
            flow_id=FLOW_A,
            scene_id="scene-b",
            ocr_text="y",
            detected_url="https://example.com/page2",
        )
        scenes = [s_a, s_b]
        builder = FlowBuilder()
        layer1 = builder.build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)
        f_exit, f_entry = "fx", "fz"
        frames = [
            {"frame_id": f_exit, "scene_id": "scene-a", "frame_index": 0, "description": ""},
            {"frame_id": f_entry, "scene_id": "scene-b", "frame_index": 0, "description": ""},
        ]
        st = [
            {
                "from_frame_id": f_exit,
                "to_frame_id": f_entry,
                "action_kind": "click_cta",
                "action_label": "Clicked elsewhere",
                "confidence": 0.84,
                "target_element_label": "",
                "observed_value": "",
                "evidence_text": "",
            }
        ]
        enriched = builder.enrich_with_actions(
            layer1, frames, [], scenes, scene_transitions=st
        )
        assert enriched[0]["action_type"] == "navigate"
        assert "example.com/page2" in (enriched[0].get("action_value") or "")

    def test_transition_overrides_url_when_higher_confidence(self):
        s_a = _scene(
            0,
            flow_id=FLOW_A,
            scene_id="scene-a",
            detected_url="https://example.com/page1",
        )
        s_b = _scene(
            1,
            flow_id=FLOW_A,
            scene_id="scene-b",
            detected_url="https://example.com/page2",
        )
        scenes = [s_a, s_b]
        builder = FlowBuilder()
        layer1 = builder.build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)
        f_exit, f_entry = "fx2", "fz2"
        frames = [
            {"frame_id": f_exit, "scene_id": "scene-a", "frame_index": 0, "description": ""},
            {"frame_id": f_entry, "scene_id": "scene-b", "frame_index": 0, "description": ""},
        ]
        st = [
            {
                "from_frame_id": f_exit,
                "to_frame_id": f_entry,
                "action_kind": "submit_form",
                "action_label": "Submitted application",
                "confidence": 0.91,
                "target_element_label": "",
                "observed_value": "",
                "evidence_text": "",
            }
        ]
        enriched = builder.enrich_with_actions(
            layer1, frames, [], scenes, scene_transitions=st
        )
        assert enriched[0]["action_type"] == "submit"
        assert "Submitted" in (enriched[0].get("primary_action_summary") or {}).get(
            "action_label", ""
        )
