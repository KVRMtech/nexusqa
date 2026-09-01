"""
Phase 5-A parity-test scaffold for the eyes split.

When the per-step refactor lands, these tests verify behavioral parity
between the old monolithic `_process_video_single` and the new 6-step
pipeline. Today (foundation-only commit) the parity tests are marked
`xfail` so the file is checkable into CI without blocking.

The intent:
  - As each commit lands (stage methods, then handlers, then plan flip),
    flip the `xfail` to `pass` on the tests that now hold.
  - At Phase 5-A acceptance, all tests are passing.

The scaffold also exercises the manifest schemas — those ARE under test
today; the manifest round-trip tests are real, not xfail.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


def _import_eyes_manifests():
    """Resolve `app.manifests` to the eyes-engine package WITHOUT
    polluting sys.path globally — the ears tests inject their own
    `app/` and we mustn't shadow it. Each test function calls this
    helper and uses the returned module.
    """
    engine_root = (
        Path(__file__).resolve().parents[2] / "engines" / "eyes-engine"
    )
    # Stash + restore sys.path so the import is locally scoped.
    saved = list(sys.path)
    sys.path.insert(0, str(engine_root))
    try:
        # Force a fresh import every call so we don't grab a cached
        # ears `app.manifests` if those tests ran first.
        for mod_name in list(sys.modules):
            if mod_name == "app" or mod_name.startswith("app."):
                del sys.modules[mod_name]
        return importlib.import_module("app.manifests")
    finally:
        sys.path[:] = saved
        # Drop the imported `app.*` modules so subsequent ears tests
        # re-resolve `app` against their own sys.path injection.
        for mod_name in list(sys.modules):
            if mod_name == "app" or mod_name.startswith("app."):
                del sys.modules[mod_name]


# ─── Manifest schema tests (active today) ──────────────────────


def test_frames_manifest_roundtrip():
    """JSON round-trip preserves every field — schemas are wire-stable."""
    m = _import_eyes_manifests()
    FramesManifest, FrameRecord = m.FramesManifest, m.FrameRecord

    fm = FramesManifest(
        job_id="job-1",
        tenant_id="t",
        session_id="s",
        workflow_id="wf-1",
        source_video_artifact_key="eyes/t/s/wf-1/video.mp4",
        duration_seconds=600.0,
        fps=30.0,
        frames=[
            FrameRecord(
                index=i,
                timestamp_ms=i * 1000,
                artifact_key=f"eyes/t/s/wf-1/frame-{i:05d}.png",
                hash=f"{i:032x}",
                source_frame_idx=i,
                width=1280,
                height=720,
                size_bytes=85000,
            )
            for i in range(50)
        ],
    )
    serialized = fm.model_dump_json()
    restored = FramesManifest.model_validate_json(serialized)
    assert restored.job_id == "job-1"
    assert len(restored.frames) == 50
    assert restored.frames[0].artifact_key == "eyes/t/s/wf-1/frame-00000.png"


def test_scenes_manifest_roundtrip_without_ocr():
    """A ScenesManifest produced by detect_scenes has no OCR fields
    populated — those are filled in by the downstream OCR step."""
    m = _import_eyes_manifests()
    ScenesManifest, SceneRecord = m.ScenesManifest, m.SceneRecord

    sm = ScenesManifest(
        job_id="job-1",
        workflow_id="wf-1",
        frames_manifest_key="eyes/t/s/wf-1/frames_manifest.json",
        scenes=[
            SceneRecord(
                scene_id=f"sc-{i}",
                representative_frame_idx=i * 10,
                frame_indices=list(range(i * 10, (i + 1) * 10)),
                start_ms=i * 10_000,
                end_ms=(i + 1) * 10_000,
            )
            for i in range(5)
        ],
        enrichment_scene_ids=["sc-0", "sc-2", "sc-4"],
    )
    restored = ScenesManifest.model_validate_json(sm.model_dump_json())
    assert len(restored.scenes) == 5
    # Critical: OCR fields are None until the ocr_frames step writes them.
    assert restored.scenes[0].representative_ocr_text is None
    assert restored.scenes[0].merged_ocr_text is None
    assert len(restored.enrichment_scene_ids) == 3


def test_ocr_manifest_keyed_by_frame_idx():
    """OCRManifest is sparse — only frames actually OCR'd. The join
    with scenes happens in analyze_scenes at read time."""
    m = _import_eyes_manifests()
    OCRManifest, OCRResult = m.OCRManifest, m.OCRResult

    om = OCRManifest(
        job_id="job-1",
        workflow_id="wf-1",
        scenes_manifest_key="eyes/t/s/wf-1/scenes_manifest.json",
        profile="fast",
        results=[
            OCRResult(
                frame_idx=0, text="Login\nUsername", lines=["Login", "Username"], confidence=0.9,
            ),
            OCRResult(
                frame_idx=10, text="Password", lines=["Password"], confidence=0.85,
            ),
        ],
        skipped_frame_count=8,
    )
    restored = OCRManifest.model_validate_json(om.model_dump_json())
    by_idx = {r.frame_idx: r for r in restored.results}
    assert by_idx[0].text == "Login\nUsername"
    assert 5 not in by_idx, "non-OCR'd frames are absent, not zero-filled"
    assert restored.skipped_frame_count == 8


def test_enriched_scenes_manifest_carries_ui_elements():
    """EnrichedScenesManifest is the GPU-pass output. UIElement objects
    have entity_id slots populated later by build_evidence's
    ElementTracker."""
    m = _import_eyes_manifests()
    EnrichedScenesManifest = m.EnrichedScenesManifest
    EnrichedScene = m.EnrichedScene
    UIElement = m.UIElement

    em = EnrichedScenesManifest(
        job_id="job-1",
        workflow_id="wf-1",
        ocr_manifest_key="eyes/t/s/wf-1/ocr_manifest.json",
        enriched=[
            EnrichedScene(
                scene_id="sc-0",
                representative_frame_idx=0,
                description="Login screen with username and password fields",
                ui_elements=[
                    UIElement(element_type="textbox", text="Username", bbox=[10, 20, 200, 50]),
                    UIElement(element_type="textbox", text="Password", bbox=[10, 80, 200, 110]),
                    UIElement(element_type="button", text="Sign in", bbox=[10, 140, 100, 170]),
                ],
                application_type="web",
                enrichment_model="moondream",
            ),
        ],
    )
    restored = EnrichedScenesManifest.model_validate_json(em.model_dump_json())
    assert len(restored.enriched[0].ui_elements) == 3
    # entity_id is added in build_evidence's element-tracker pass, not here.
    assert restored.enriched[0].ui_elements[0].entity_id is None


def test_manifest_ref_used_in_checkpoint():
    """The checkpoint carries ManifestRef pointers, not the manifests
    themselves — this is what keeps workflow_state.checkpoint small."""
    m = _import_eyes_manifests()
    ManifestRef = m.ManifestRef
    import json

    ref = ManifestRef(
        kind="frames",
        artifact_key="eyes/t/s/wf-1/frames_manifest.json",
        schema_version=1,
        size_bytes=512_000,
    )
    # Simulate the checkpoint: a dict of named refs.
    checkpoint = {
        "frames_ref": ref.model_dump(mode="json"),
        "scene_count": 12,
        "current_eyes_step": "eyes.extract_frames",
    }
    payload = json.dumps(checkpoint)
    # Even for a 1000-frame video (~512 KB manifest), the checkpoint is
    # tiny because only the key + size is carried.
    assert len(payload) < 1000, (
        f"checkpoint payload should stay <1KB; got {len(payload)}B"
    )


# ─── Static structural assertions (active now that 5-A code is in) ──


def test_video_plan_has_6_eyes_steps():
    """The new video plan dispatches 6 distinct eyes steps, not 3."""
    from nexus_sdk.workflows.plans import build_plan, WorkflowKind

    plan = build_plan(WorkflowKind.VIDEO, tenant_id="t", session_id="s")
    eyes_steps = [s.name for s in plan.steps if s.engine == "eyes"]
    assert eyes_steps == [
        "eyes.extract_frames",
        "eyes.detect_scenes",
        "eyes.ocr_frames",
        "eyes.analyze_scenes",
        "eyes.analyze_transitions",
        "eyes.build_evidence",
    ], (
        f"Expected 6 distinct eyes steps; got {eyes_steps}. "
        "Phase 5-A regression?"
    )


def test_multimodal_plan_uses_dag_parallel_branches():
    """Multimodal plan's video and audio branches are independent —
    both rooted at shield, both joining at backbone."""
    from nexus_sdk.workflows.plans import build_plan, WorkflowKind
    from nexus_sdk.workflows.dag import validate_dag

    plan = build_plan(WorkflowKind.MULTIMODAL, tenant_id="t", session_id="s")
    validate_dag(plan)

    by_name = {s.name: s for s in plan.steps}
    # The first eyes step depends only on shield.redact_video
    assert by_name["eyes.extract_frames"].depends_on == ["shield.redact_video"]
    # The first ears step depends only on shield.redact_audio
    assert by_name["ears.preprocess"].depends_on == ["shield.redact_audio"]
    # backbone joins both branches
    backbone_deps = set(by_name["backbone.canonicalize_multimodal"].depends_on)
    assert backbone_deps == {"eyes.build_evidence", "ears.align"}


def test_plan_version_is_v2():
    """Plan version field is wired and defaults to 2 (DAG-aware)."""
    from nexus_sdk.workflows.plans import build_plan, WorkflowKind

    for kind in (
        WorkflowKind.AUDIO, WorkflowKind.VIDEO,
        WorkflowKind.MULTIMODAL, WorkflowKind.DOCUMENT,
    ):
        plan = build_plan(kind, tenant_id="t", session_id="s")
        assert plan.version == 2, f"{kind.value} plan version is {plan.version}"


def test_eyes_handlers_module_exports_6_step_names():
    """The Phase 5-A handler module registers 6 step names. Verified at
    import time — failure here means the rewrite regressed."""
    m = _import_eyes_workflow_handlers()
    expected = {
        "eyes.extract_frames",
        "eyes.detect_scenes",
        "eyes.ocr_frames",
        "eyes.analyze_scenes",
        "eyes.analyze_transitions",
        "eyes.build_evidence",
    }
    actual = {
        getattr(m, name) for name in dir(m)
        if name.startswith("_STEP_")
    }
    assert actual == expected, f"Expected {expected}, got {actual}"


# ─── Integration tests (need real video fixture — skip in CI without one) ──


@pytest.mark.skip(
    reason="Needs tests/load/corpus/video-5min.mp4 fixture + GPU; "
           "run manually with `NEXUS_EYES_FIXTURE_VIDEO=path pytest -k parity`"
)
def test_workflow_produces_6_distinct_step_history_rows():
    """A 10-min video through the new pipeline produces 6 step-history rows."""
    pass


@pytest.mark.skip(reason="Needs running orchestrator + real fixture")
def test_ocr_failure_retries_only_ocr_step():
    """Injecting a one-shot exception in OCR stage should cause only
    that step to retry."""
    pass


@pytest.mark.skip(reason="Needs running orchestrator + real fixture")
def test_transition_lm_failure_retries_only_that_step():
    pass


@pytest.mark.skip(reason="Needs real fixture + monolith side-by-side comparison")
def test_new_pipeline_structurally_matches_monolith_on_fixture():
    pass


@pytest.mark.skip(reason="Needs multi-pod orchestrator")
def test_pod_hop_no_local_filesystem_dependency():
    pass


def _import_eyes_workflow_handlers():
    """Same sys.path dance as _import_eyes_manifests."""
    engine_root = (
        Path(__file__).resolve().parents[2] / "engines" / "eyes-engine"
    )
    saved = list(sys.path)
    sys.path.insert(0, str(engine_root))
    try:
        for mod_name in list(sys.modules):
            if mod_name == "app" or mod_name.startswith("app."):
                del sys.modules[mod_name]
        return importlib.import_module("app.workflow_handlers")
    finally:
        sys.path[:] = saved
        for mod_name in list(sys.modules):
            if mod_name == "app" or mod_name.startswith("app."):
                del sys.modules[mod_name]
