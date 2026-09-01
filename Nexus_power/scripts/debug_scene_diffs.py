"""Inspect raw frame-to-frame OCR diffs inside a single scene to see why
the step extractor is or is not classifying changes as actions."""
from __future__ import annotations

import json
import sys

from nexus_sdk.evidence.build_scenes import _structural_fingerprint
from nexus_sdk.evidence.step_extractor import (
    _classify_action,
    _filter_meaningful_tokens,
    _ui_element_set,
)


def main() -> None:
    data = json.loads(sys.stdin.read())
    frames = sorted(data["frames"], key=lambda f: f["frame_index"])

    for s in sorted(data["scenes"], key=lambda s: s["scene_index"]):
        sid = s["scene_id"]
        sf = [f for f in frames if f.get("scene_id") == sid]
        if len(sf) < 2:
            continue
        label = (s.get("scene_state_summary") or {}).get("step_label") or s.get("screen_name") or ""
        print(f"\n── Scene {s['scene_index']} ({len(sf)} frames): {label[:60]} ──")
        for i in range(1, len(sf)):
            prev, curr = sf[i - 1], sf[i]
            p_struct = _structural_fingerprint(prev.get("extracted_text", ""))
            c_struct = _structural_fingerprint(curr.get("extracted_text", ""))
            added = _filter_meaningful_tokens(c_struct - p_struct)
            removed = _filter_meaningful_tokens(p_struct - c_struct)
            conf = curr.get("ocr_confidence") or 0
            kind, delta, conf_out, ev = _classify_action(
                prev.get("extracted_text", ""),
                curr.get("extracted_text", ""),
                _ui_element_set(prev),
                _ui_element_set(curr),
                float(conf),
            )
            ts_p = prev.get("timestamp_seconds") or 0
            ts_c = curr.get("timestamp_seconds") or 0
            print(
                f"  f{prev['frame_index']:>2}->f{curr['frame_index']:<2} "
                f"({ts_p:>5.1f}s->{ts_c:>5.1f}s) "
                f"ocr_conf={conf:.2f}  +{len(added):>2}  -{len(removed):>2}  "
                f"kind={kind!r:<22} ev={ev}"
            )
            if added:
                print(f"      added: {sorted(added, key=len, reverse=True)[:6]}")
            if removed:
                print(f"      removed: {sorted(removed, key=len, reverse=True)[:6]}")


if __name__ == "__main__":
    main()
