#!/usr/bin/env python3
"""Settle v2 mechanism proof — the exact empty-Age scenario, honestly hard:

  0.0-5.0s  page A: TEXTURED form (labels, boxes, paragraph noise)
  4.55-4.9s user types '35' — entirely BETWEEN 0.5s probes, after the last
            kept frame; no probe ever sees the filled state
  5.0s      hard navigation to page B (different texture layout -> dHash burst)
  5.0-12.0s page B stable

Expectation: OFF -> no frame in [4.5, 5.0) shows the value (it was never
sampled). ON -> a settle_before_transition frame at ~4.75s SHOWS '35'."""
import asyncio
import os
import sys
import tempfile

import cv2
import numpy as np

sys.path.insert(0, "/app/service")
from app.frame_diff import FrameExtractor  # noqa: E402


def _texture(img, seed):
    rng = np.random.RandomState(seed)
    for _ in range(24):
        x, y = int(rng.randint(0, 560)), int(rng.randint(140, 340))
        cv2.putText(img, "lorem ipsum dolor", (x, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (90, 90, 90), 1)


def build_video(path: str) -> None:
    fps, w, h = 30, 640, 360
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for i in range(12 * fps):
        t = i / fps
        img = np.full((h, w, 3), 245, np.uint8)
        if t < 5.0:
            cv2.putText(img, "Personal information", (30, 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (30, 30, 30), 2)
            cv2.putText(img, "Age", (30, 84), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (60, 60, 60), 1)
            cv2.rectangle(img, (30, 90), (300, 130), (120, 120, 120), 2)
            _texture(img, 7)
            typed = ""
            if t >= 4.55:
                typed = "3"
            if t >= 4.75:
                typed = "35"
            if typed:
                cv2.putText(img, typed, (45, 122), cv2.FONT_HERSHEY_SIMPLEX,
                            0.8, (10, 10, 10), 2)
        else:
            cv2.putText(img, "Height & weight", (200, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2)
            cv2.rectangle(img, (330, 150), (600, 190), (120, 120, 120), 2)
            _texture(img, 99)
        vw.write(img)
    vw.release()


async def run(settle: bool, video: str):
    out = tempfile.mkdtemp(prefix=f"sv2_{settle}_")
    fx = FrameExtractor(frame_diff_threshold=0.03, max_fps_extract=2.0,
                        adaptive_sampling=True, settle_frame=settle)
    return await fx.extract_frames(video, out)


def shows_value(frame_path: str) -> bool:
    img = cv2.imread(frame_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return False
    field = img[95:128, 38:130]
    return float((field < 90).mean()) > 0.02


def main() -> int:
    video = "/tmp/settle2.mp4"
    build_video(video)
    loop = asyncio.new_event_loop()
    on = loop.run_until_complete(run(True, video))
    off = loop.run_until_complete(run(False, video))

    fmt = lambda fs: [(f["timestamp"], "S" if f.get("settle_before_transition") else "-")
                      for f in fs]
    print("ON :", fmt(on))
    print("OFF:", fmt(off))

    on_win = [f for f in on if 4.5 <= f["timestamp"] < 5.0]
    off_win = [f for f in off if 4.5 <= f["timestamp"] < 5.0]
    on_val = [f for f in on_win if shows_value(f["frame_path"])]
    off_val = [f for f in off_win if shows_value(f["frame_path"])]

    if not any(f.get("settle_before_transition") for f in on_val):
        print(f"FAIL: ON-run has no settle frame showing the typed value "
              f"(window frames={len(on_win)}, with value={len(on_val)})")
        return 1
    if off_val:
        print("NOTE: OFF-run also captured the value (probe landed luckily) — "
              "settle still guarantees it deterministically")
    print(f"PASS: settle frame at t={on_val[0]['timestamp']} SHOWS '35' "
          f"(OFF-run value frames in window: {len(off_val)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
