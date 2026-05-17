"""Run the intra-scene step extractor against persisted frames from
artifact bbfbf73d and print the actions discovered per scene.

Reads frames + their scene assignment from stdin (a JSON object
``{"scenes": [{scene_index, scene_id, ...}], "frames": [...]}``) and
calls :func:`extract_intra_scene_steps` for each scene's frames.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

from nexus_sdk.evidence.step_extractor import extract_intra_scene_steps


def main() -> None:
    data = json.loads(sys.stdin.read())
    scenes = data["scenes"]
    frames = data["frames"]

    by_scene: dict[str, list[dict]] = defaultdict(list)
    for f in frames:
        sid = f.get("scene_id")
        if sid:
            by_scene[sid].append(f)

    total_actions = 0
    for s in sorted(scenes, key=lambda s: s.get("scene_index", 0)):
        sid = s.get("scene_id")
        scene_frames = by_scene.get(sid, [])
        actions = extract_intra_scene_steps(scene_frames)
        total_actions += len(actions)
        if not actions:
            continue
        idx = s.get("scene_index")
        label = (s.get("scene_state_summary") or {}).get("step_label") or s.get("screen_name") or ""
        print(f"\n── Scene {idx} ({len(scene_frames)} frames): {label[:60]} ──")
        for a in actions:
            print(
                f"  [{a['from_timestamp_s']:>5.1f}s → {a['to_timestamp_s']:>5.1f}s] "
                f"{a['action_kind']:<18}  conf={a['confidence']:.2f}  "
                f"target={a['target_label'][:35]!r:<35}  "
                f"delta={a['delta_text'][:50]!r}"
            )

    print(f"\nTotal in-scene actions extracted: {total_actions}")


if __name__ == "__main__":
    main()
