#!/usr/bin/env python3
"""
Side-by-side harness: run ``build_scenes`` on the same frame payload twice —
with ``EYES_PHASE2_GUARDS=false`` (Phase 1 parity) vs ``true`` (Phase 2).

Use exported Eyes ``frames`` JSON (list of frame dicts) or any artifact-compatible
frame list. Prints quantitative deltas (scene counts, timing, coarse fingerprints)
so you can compare before enabling Phase 2 in production.

Examples::

    python scripts/compare_phase1_phase2_build_scenes.py --frames export.json

    # From platform API (fetch frames yourself) or DB export.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def _load_frames(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "frames" in data:
        data = data["frames"]
    if not isinstance(data, list):
        raise SystemExit("Input must be a JSON array of frame dicts, or an object with key 'frames'")
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            out.append(item)
    return out


def _scene_fingerprint(scene: dict[str, Any]) -> dict[str, Any]:
    return {
        "scene_index": scene.get("scene_index"),
        "start_ms": scene.get("start_ms"),
        "end_ms": scene.get("end_ms"),
        "detected_url": (scene.get("detected_url") or "")[:200],
        "ocr_len": len((scene.get("ocr_text") or "")),
        "has_keyframe_boundary": scene.get("has_keyframe_boundary"),
    }


def _run_build(
    frames: list[dict[str, Any]],
    artifact_id: str,
    session_id: str,
    tenant_id: str,
    phase2: bool,
) -> tuple[list[dict[str, Any]], float]:
    from nexus_sdk.evidence.build_scenes import build_scenes

    prev = os.environ.get("EYES_PHASE2_GUARDS")
    try:
        os.environ["EYES_PHASE2_GUARDS"] = "true" if phase2 else "false"
        t0 = time.perf_counter()
        scenes = build_scenes(
            frames=list(frames),
            artifact_id=artifact_id,
            session_id=session_id,
            tenant_id=tenant_id,
        )
        elapsed = time.perf_counter() - t0
        return scenes, elapsed
    finally:
        if prev is None:
            os.environ.pop("EYES_PHASE2_GUARDS", None)
        else:
            os.environ["EYES_PHASE2_GUARDS"] = prev


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare build_scenes Phase 1 (flag off) vs Phase 2 (flag on).",
    )
    parser.add_argument(
        "--frames",
        type=Path,
        required=True,
        help="JSON file: array of frame dicts, or object with 'frames' key",
    )
    parser.add_argument("--artifact-id", default="diff-harness-artifact", help="Deterministic scene_id scope")
    parser.add_argument("--session-id", default="diff-harness-session")
    parser.add_argument("--tenant-id", default="diff-harness-tenant")
    parser.add_argument("--json-out", type=Path, default=None, help="Write full report as JSON")
    args = parser.parse_args()

    if not args.frames.is_file():
        raise SystemExit(f"Not a file: {args.frames}")

    repo_root = Path(__file__).resolve().parents[1]
    sdk = repo_root / "sdk" / "nexus-sdk"
    if sdk.is_dir():
        sys.path.insert(0, str(sdk))

    frames = _load_frames(args.frames)
    if not frames:
        raise SystemExit("No frame dicts in input")

    s1, t1 = _run_build(frames, args.artifact_id, args.session_id, args.tenant_id, phase2=False)
    s2, t2 = _run_build(frames, args.artifact_id, args.session_id, args.tenant_id, phase2=True)

    fp1 = [_scene_fingerprint(s) for s in s1]
    fp2 = [_scene_fingerprint(s) for s in s2]

    report: dict[str, Any] = {
        "input_frame_count": len(frames),
        "phase1": {
            "EYES_PHASE2_GUARDS": "false",
            "scene_count": len(s1),
            "elapsed_seconds": round(t1, 4),
            "fingerprints": fp1,
        },
        "phase2": {
            "EYES_PHASE2_GUARDS": "true",
            "scene_count": len(s2),
            "elapsed_seconds": round(t2, 4),
            "fingerprints": fp2,
        },
        "delta": {
            "scene_count": len(s2) - len(s1),
            "timing_ratio": round(t2 / t1, 4) if t1 > 1e-9 else None,
        },
    }

    print(json.dumps(report, indent=2))
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
