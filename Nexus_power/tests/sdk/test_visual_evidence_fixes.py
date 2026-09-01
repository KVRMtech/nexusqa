"""
Targeted regression tests for Visual E2E architect review findings (April 2026).

Covers:
  1. Interleaved return-visit gaps — flow.scene_ids are non-contiguous in
     scene_index when other apps were active in between.
  2. App-switch edge destination — switchEdge.to_scene_id belongs to the
     correct target flow, not just the next rendered group.
  3. Reprocessing safety — ControlExtractor works independently of any prior
     delete; the extraction never raises on minimal/empty input.
  4. Same-domain path-family separation — web_ui return visit to a DIFFERENT
     path family on the same domain starts a new flow (not merged).
  4b. Desktop app no-URL merge — non-web app with no URL still merges
      return visits on (app_type, domain) via the tier-2 fallback.
"""

import uuid
import pytest

from nexus_sdk.evidence.flow_segmenter import FlowSegmenter, FlowGroup
from nexus_sdk.evidence.flow_builder import FlowBuilder
from nexus_sdk.evidence.control_extractor import ControlExtractor, _clean_control_label


# ── helpers ───────────────────────────────────────────────────────────────────

ARTIFACT_ID = "art-fix-test-001"
SESSION_ID  = "sess-fix-test-001"
TENANT_ID   = "test-tenant"


def _scene(
    index: int,
    app_type: str = "web_ui",
    domain: str = "example.com",
    url: str = "",
    ocr: str = "",
    scene_id: str = "",
) -> dict:
    sid = scene_id or str(uuid.uuid4())
    return {
        "scene_id": sid,
        "scene_index": index,
        "app_type": app_type,
        "detected_url": url or f"https://{domain}/",
        "screen_name": f"Scene {index}",
        "start_ms": index * 2000,
        "end_ms": index * 2000 + 1999,
        "ocr_text": ocr,
        "llava_description": "",
        "perceptual_hash": f"hash{index:04d}",
        "completeness_confidence": 0.85,
        "representative_frame_id": str(uuid.uuid4()),
    }


def _segment(scenes: list[dict]) -> list[FlowGroup]:
    """Run FlowSegmenter with consistent test IDs."""
    return FlowSegmenter().segment(
        scenes,
        artifact_id=ARTIFACT_ID,
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
    )


def _indices(flow: FlowGroup, scenes: list[dict]) -> list[int]:
    """Sorted scene_index values for every scene_id in the given flow."""
    sid_to_idx = {s["scene_id"]: s["scene_index"] for s in scenes}
    return sorted(sid_to_idx[sid] for sid in flow.scene_ids if sid in sid_to_idx)


# ── Finding 1: interleaved return-visit gaps ──────────────────────────────────
#
# Timeline: app-a(0,1) → app-b(2,3) → app-a(4)
# Domain change triggers a boundary (domain + web_ui same-type bonus ≈ 0.60).
# The segmenter should merge scene 4 back into the app-a flow.
# Resulting scene_ids for app-a flow = [0, 1, 4] — non-contiguous.

class TestInterleavedReturnVisit:

    def _scenes(self) -> list[dict]:
        return [
            _scene(0, domain="app-a.com", url="https://app-a.com/home",      ocr="Life Insurance Quote"),
            _scene(1, domain="app-a.com", url="https://app-a.com/home",      ocr="Life Insurance Quote"),
            _scene(2, domain="app-b.com", url="https://app-b.com/dashboard", ocr="Inbox Compose Send"),
            _scene(3, domain="app-b.com", url="https://app-b.com/dashboard", ocr="Inbox Compose Send"),
            _scene(4, domain="app-a.com", url="https://app-a.com/home",      ocr="Life Insurance Quote"),
        ]

    def test_produces_two_flows(self):
        flows = _segment(self._scenes())
        assert len(flows) == 2, f"Expected 2 flows, got {len(flows)}: {[f.flow_label for f in flows]}"

    def test_app_a_has_non_contiguous_indices(self):
        scenes = self._scenes()
        flows = _segment(scenes)

        # App-A flow is the one containing scene_index 4 (return visit)
        flow_a = next((f for f in flows if 4 in _indices(f, scenes)), None)
        assert flow_a is not None, "App-A flow (with scene 4) not found"

        idx = _indices(flow_a, scenes)
        assert idx == [0, 1, 4], f"App-A indices should be [0,1,4]; got {idx}"

        gaps = [idx[i + 1] - idx[i] for i in range(len(idx) - 1)]
        assert max(gaps) > 1, f"No chronological gap detected; gaps={gaps}"

    def test_app_a_flagged_as_interleaved(self):
        scenes = self._scenes()
        flows = _segment(scenes)
        flow_a = next((f for f in flows if 4 in _indices(f, scenes)), None)
        assert flow_a is not None
        assert flow_a.is_interleaved is True,    "App-A flow must be marked is_interleaved"
        assert flow_a.visit_count    >= 2,       "App-A flow visit_count must be >= 2"


# ── Finding 2: app-switch edge destination ───────────────────────────────────
#
# flow_builder must emit app_switch edges whose to_scene_id belongs to a
# different flow than from_scene_id — i.e. the edge truly crosses a flow
# boundary and the destination flow is unambiguous.

class TestAppSwitchEdgeDestination:

    def test_app_switch_edges_cross_flow_boundaries(self):
        flow_a_id = str(uuid.uuid4())
        flow_b_id = str(uuid.uuid4())

        scenes = [
            {"scene_id": str(uuid.uuid4()), "scene_index": 0, "flow_id": flow_a_id, "start_ms": 0,    "end_ms": 1000},
            {"scene_id": str(uuid.uuid4()), "scene_index": 1, "flow_id": flow_a_id, "start_ms": 1000, "end_ms": 2000},
            {"scene_id": str(uuid.uuid4()), "scene_index": 2, "flow_id": flow_b_id, "start_ms": 2000, "end_ms": 3000},
            {"scene_id": str(uuid.uuid4()), "scene_index": 3, "flow_id": flow_b_id, "start_ms": 3000, "end_ms": 4000},
            {"scene_id": str(uuid.uuid4()), "scene_index": 4, "flow_id": flow_a_id, "start_ms": 4000, "end_ms": 5000},
        ]
        scene_flow = {s["scene_id"]: s["flow_id"] for s in scenes}

        edges = FlowBuilder().build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)
        switch_edges = [e for e in edges if e["edge_type"] == "app_switch"]

        assert len(switch_edges) >= 1, "Expected at least one app_switch edge"
        for e in switch_edges:
            frm = scene_flow.get(e["from_scene_id"])
            to  = scene_flow.get(e["to_scene_id"])
            assert frm is not None and to is not None, "app_switch scene ids must be known"
            assert frm != to, f"app_switch edge must cross flows: from={frm} to={to}"

    def test_app_switch_to_scene_resolves_to_known_flow(self):
        """to_scene_id on every app_switch edge must map to a real flow."""
        flow_x = str(uuid.uuid4())
        flow_y = str(uuid.uuid4())
        scenes = [
            {"scene_id": str(uuid.uuid4()), "scene_index": i,
             "flow_id": flow_x if i < 3 else flow_y,
             "start_ms": i * 1000, "end_ms": i * 1000 + 999}
            for i in range(6)
        ]
        known_flows = {flow_x, flow_y}
        scene_flow = {s["scene_id"]: s["flow_id"] for s in scenes}

        edges = FlowBuilder().build_observed_transitions(scenes, ARTIFACT_ID, TENANT_ID)
        for e in edges:
            if e["edge_type"] == "app_switch":
                assert scene_flow.get(e["to_scene_id"]) in known_flows, (
                    f"to_scene_id resolves to unknown flow: {e['to_scene_id']}"
                )


# ── Finding 3: reprocessing safety ───────────────────────────────────────────
#
# The ControlExtractor must produce results regardless of whether a prior
# delete ran successfully.  It must not raise on empty/minimal input.

class TestControlReprocessingSafety:

    def test_extractor_returns_list_on_typical_input(self):
        extractor = ControlExtractor()
        scene = {
            "scene_id": str(uuid.uuid4()),
            "scene_index": 0,
            "app_type": "web_ui",
            "detected_url": "https://usaa.com/insurance/life",
            "ocr_text": "Life Insurance Get Quote Back Continue",
            "screen_name": "Life Insurance",
            "llava_description": "Web page showing life insurance options with Get Quote button",
        }
        frame = {
            "frame_id": str(uuid.uuid4()),
            "ocr_text": scene["ocr_text"],
            "bounding_boxes": [],
        }
        result = extractor.extract(scene=scene, frame=frame,
                                   artifact_id=ARTIFACT_ID, tenant_id=TENANT_ID)
        assert isinstance(result, list)

    def test_extractor_does_not_raise_on_empty_frame(self):
        """Empty frame dict → returns list (possibly empty), does not raise."""
        extractor = ControlExtractor()
        scene = {
            "scene_id": str(uuid.uuid4()),
            "scene_index": 0,
            "app_type": "web_ui",
            "detected_url": "https://example.com/",
            "ocr_text": "",
            "screen_name": "Scene 0",
            "llava_description": "",
        }
        result = extractor.extract(scene=scene, frame={},
                                   artifact_id=ARTIFACT_ID, tenant_id=TENANT_ID)
        assert isinstance(result, list)

    def test_extractor_is_independent_of_delete_step(self):
        """
        Controls are built in-memory first; the DB delete is a separate step.
        Verify that even if we simulate 'no prior DB' state, extraction still
        returns a result list.
        """
        extractor = ControlExtractor()
        for i in range(3):
            scene = {
                "scene_id": str(uuid.uuid4()),
                "scene_index": i,
                "app_type": "web_ui",
                "detected_url": f"https://example.com/page/{i}",
                "ocr_text": f"Page {i} content Submit Cancel",
                "screen_name": f"Page {i}",
                "llava_description": "",
            }
            frame = {"frame_id": str(uuid.uuid4()), "ocr_text": scene["ocr_text"], "bounding_boxes": []}
            controls = extractor.extract(scene=scene, frame=frame,
                                         artifact_id=ARTIFACT_ID, tenant_id=TENANT_ID)
            assert isinstance(controls, list), f"Scene {i}: expected list, got {type(controls)}"


# ── Finding 4: same-domain path-family separation ────────────────────────────
#
# The path_family key prevents CROSS-DOMAIN RETURN VISITS from being wrongly
# merged when the user revisits the same domain at a different path.
#
# Scenario (boundary triggered by domain change):
#   usaa.com/insurance → gmail.com → usaa.com/banking
#   OLD behavior: usaa.com/banking matched the usaa.com/insurance flow → merged (wrong)
#   NEW behavior: different path_family → new flow (correct)
#
# Finding 4b:
#   mainframe → usaa.com → mainframe
#   Non-web (path_family="") → tier-2 fallback allowed → merged (correct)

class TestSameDomainPathSeparation:

    def test_different_path_family_after_interruption_is_new_flow(self):
        """
        usaa.com/insurance → gmail.com → usaa.com/banking
        should produce 3 flows (not 2), because banking ≠ insurance path family.
        """
        scenes = [
            _scene(0, domain="usaa.com",  url="https://usaa.com/insurance/life/",    ocr="Life Insurance Premium"),
            _scene(1, domain="gmail.com", url="https://gmail.com/mail",               ocr="Inbox Compose Reply"),
            _scene(2, domain="usaa.com",  url="https://usaa.com/banking/checking/",   ocr="Checking Balance Deposit"),
        ]
        flows = _segment(scenes)
        assert len(flows) == 3, (
            f"usaa/insurance + gmail + usaa/banking should be 3 separate flows; got {len(flows)}: "
            f"{[f.flow_label for f in flows]}"
        )

    def test_same_path_family_after_interruption_merges(self):
        """
        usaa.com/insurance → gmail.com → usaa.com/insurance (same path)
        should produce 2 flows — insurance merges as a return visit.
        """
        scenes = [
            _scene(0, domain="usaa.com",  url="https://usaa.com/insurance/life/",    ocr="Life Insurance Premium"),
            _scene(1, domain="gmail.com", url="https://gmail.com/mail",               ocr="Inbox Compose Reply"),
            _scene(2, domain="usaa.com",  url="https://usaa.com/insurance/life/quote", ocr="Life Insurance Quote"),
        ]
        flows = _segment(scenes)
        assert len(flows) == 2, (
            f"usaa/insurance return visit should merge to 2 flows; got {len(flows)}: "
            f"{[f.flow_label for f in flows]}"
        )
        # Scene 2 (return visit) must be in the same flow as scene 0
        usaa_flow = next((f for f in flows if 0 in _indices(f, scenes)), None)
        assert usaa_flow is not None
        assert 2 in _indices(usaa_flow, scenes), (
            "Scene 2 (same path family return) must merge into the usaa/insurance flow"
        )

    def test_desktop_app_no_url_merges_via_tier2_fallback(self):
        """
        mainframe_terminal (no URL) → usaa.com → mainframe_terminal (no URL)
        Desktop/non-web app: tier-2 fallback (app_type + domain) is allowed,
        so the return visit at scene 3 merges into the original mainframe flow.
        """
        sid0 = str(uuid.uuid4())
        sid1 = str(uuid.uuid4())
        sid2 = str(uuid.uuid4())
        sid3 = str(uuid.uuid4())
        scenes = [
            dict(_scene(0, app_type="mainframe_terminal", domain="mainframe", ocr="Account Balance Transfer"),
                 scene_id=sid0, detected_url=""),
            dict(_scene(1, app_type="mainframe_terminal", domain="mainframe", ocr="Account Balance Transfer"),
                 scene_id=sid1, detected_url=""),
            _scene(2, domain="usaa.com", url="https://usaa.com/", ocr="USAA Banking Insurance"),
            dict(_scene(3, app_type="mainframe_terminal", domain="mainframe", ocr="Account Balance Transfer"),
                 scene_id=sid3, detected_url=""),
        ]
        flows = _segment(scenes)
        assert len(flows) == 2, (
            f"Desktop app return visit should merge to 2 flows; got {len(flows)}"
        )
        mf_flow = next((f for f in flows if 3 in _indices(f, scenes)), None)
        assert mf_flow is not None, "Mainframe flow must include scene 3 (return visit)"

    def test_web_ui_no_url_tier2_not_applied(self):
        """
        A web_ui scene with no URL should NOT merge into a different-path web_ui
        flow via tier-2.  Tier-2 is disabled for web_ui (no path → ambiguous).
        The no-URL scene may start a new group or stay in the current one, but
        must not falsely collapse into a different path-family flow.
        """
        sid0 = str(uuid.uuid4())
        sid1 = str(uuid.uuid4())
        sid2 = str(uuid.uuid4())
        sid_nourl = str(uuid.uuid4())

        scenes = [
            _scene(0, domain="usaa.com", url="https://usaa.com/insurance/life/",    ocr="Life Insurance",   scene_id=sid0),
            _scene(1, domain="usaa.com", url="https://usaa.com/insurance/life/",    ocr="Life Insurance",   scene_id=sid1),
            _scene(2, domain="gmail.com", url="https://gmail.com/",                 ocr="Inbox Compose",    scene_id=sid2),
            # web_ui scene on usaa.com but no URL → should NOT merge into insurance flow via tier-2
            dict(_scene(3, app_type="web_ui", domain="usaa.com", ocr="USAA Home",   scene_id=sid_nourl),
                 detected_url=""),
        ]
        flows = _segment(scenes)

        insurance_flow = next((f for f in flows if 0 in _indices(f, scenes)), None)
        assert insurance_flow is not None
        assert 3 not in _indices(insurance_flow, scenes), (
            "web_ui no-URL scene must NOT merge into a different-path insurance flow via tier-2"
        )


# ── OCR noise cleaning — regression guard ────────────────────────────────────

class TestCleanControlLabel:
    """Guard _clean_control_label against regressions."""

    @pytest.mark.parametrize("raw,expected", [
        # Separator chop: ": " at position 14 → "Life Insurance"
        ("Life Insurance: Get Policy Quc Ask Gemini usaa com", "Life Insurance"),
        # Separator chop: ": " at position 5 → "years"
        ("years: Get an auto Checking Home Credit cards Get a renters quote accounts insurance quote", "years"),
        # No separator → returned as-is (under 60 chars)
        ("Short Label", "Short Label"),
        # Separator " | " → take first clause
        ("Home | Example Site", "Home"),
        # Separator " › " → take first clause
        ("Dashboard \u203a Analytics", "Dashboard"),
        # Truncation at 60 chars
        ("A" * 80, "A" * 60),
    ])
    def test_clean_label(self, raw: str, expected: str):
        result = _clean_control_label(raw)
        assert result == expected, f"Input {raw!r}: expected {expected!r}, got {result!r}"

    @pytest.mark.parametrize("raw", [
        "Ask Gemini",
        "Customize Chrome",
    ])
    def test_pure_chrome_keyword_stripped_or_preserved(self, raw: str):
        """
        When the raw label IS a chrome keyword (found at position 0), the
        function strips from position 0 onward → empty string.
        The CALLER falls back to raw[:60] in that case.
        We assert the result is either '' or the original — no partial garbage.
        """
        result = _clean_control_label(raw)
        assert result == "" or result == raw, (
            f"Chrome keyword {raw!r}: expected '' or original, got {result!r}"
        )
