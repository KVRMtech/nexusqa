"""Apply the new _merge_by_state_key pass to the persisted scenes from
artifact bbfbf73d and report the before/after picture.

Run inside the spine container so nexus_sdk imports cleanly:
    docker exec -i nexus-spine python /app/scripts/verify_state_key_merge.py < scenes.json

The script reads scene rows on stdin (JSON array), applies the new merge,
and prints the resulting merged steps with their visit counts.
"""
from __future__ import annotations

import json
import sys

from nexus_sdk.evidence.build_scenes import _merge_by_state_key


def main() -> None:
    raw = sys.stdin.read().strip()
    scenes = json.loads(raw)
    print(f"Loaded {len(scenes)} scenes")

    merged = _merge_by_state_key(scenes, artifact_id="bbfbf73d-dc01-43c5-9350-045e74cad1ed")
    print(f"After state_key merge: {len(merged)} scenes")
    print()
    print(f"{'idx':>4} {'visits':>6} {'quality':>8}  step_label")
    print("-" * 80)
    for s in merged:
        summary = s.get("scene_state_summary") or {}
        idx = s.get("scene_index", "?")
        visits = summary.get("visit_count", 1)
        quality = summary.get("state_quality", "weak")
        label = (summary.get("step_label") or s.get("screen_name") or "")[:60]
        print(f"{idx:>4} {visits:>6} {quality:>8}  {label}")


if __name__ == "__main__":
    main()
