"""Deterministic, model-free perceptual diff of two screenshots — an ADVISORY
signal for the triage two-up card (baseline-vs-actual).

WHY THIS IS SAFE / ADDITIVE
---------------------------
This module is PURE and read-only. It produces an *advisory* "X% changed"
number + a changed-region bbox for display next to the baseline-vs-actual
cards. It NEVER touches the frozen deterministic verdict/heal core
(``app/services/diff_and_heal/*``, which diffs the *visual graph*, not pixels)
and it MUST NOT be used to flip a pass/fail. No model, no I/O, no globals,
deterministic, $0.

THE LOAD-BEARING DETAIL — LETTERBOX, DON'T NAIVE-DIFF
-----------------------------------------------------
The baseline screenshot is captured at *recording* resolution; the actual is
captured at the *runner viewport* resolution. These almost never match. A naive
same-size ``ImageChops.difference`` on mismatched sizes is impossible (PIL
raises) and even after a resize-to-fit it smears every pixel → reports ~100%
changed, which is useless. Instead we LETTERBOX both grayscale images onto a
common canvas (max width x max height, padded with black) so identical content
lands on identical coordinates and only genuine UI change shows up.

Pillow is already a hard dependency (requirements.txt: ``Pillow>=10.0,<12.0``,
drives the frame_annotator). We reuse it; we add nothing.

Robust to junk: ``None`` / empty / truncated / non-image bytes never raise —
they yield ``identical=True, changed_ratio=0.0`` (the conservative "no change
to report" answer, so a missing/corrupt capture can never manufacture a
visual-change signal).
"""

from __future__ import annotations

import io
from typing import Optional

from PIL import Image, ImageChops

__all__ = ["diff_screenshots", "PIXEL_THRESHOLD"]

# Per-pixel grayscale delta (0..255) below which a pixel counts as "unchanged".
# ~24/255 (~9.4%) absorbs JPEG/anti-aliasing/sub-pixel-render noise while still
# catching real UI change. Compression artefacts and faint anti-alias fringes
# sit comfortably under this; a moved/recoloured control sits well above it.
PIXEL_THRESHOLD = 24

# A canvas larger than this (width x height) gets proportionally downscaled
# before diffing. Caps work + memory on a pathological upload; sub-pixel
# precision is irrelevant for an advisory badge. Aspect ratio preserved, so the
# bbox stays meaningful (it is reported in the *diffed* canvas coordinate space).
_MAX_CANVAS_EDGE = 2000


def _load_gray(data: object) -> Optional[Image.Image]:
    """Decode arbitrary bytes to a grayscale ('L') PIL image, or None.

    Never raises: bad / empty / non-bytes / un-decodable input -> None. We force
    a full ``.load()`` so a *truncated* PNG (which opens lazily then explodes on
    first access) is caught here rather than later.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(data)
    if not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # force decode now so truncation/corruption surfaces here
        return img.convert("L")
    except Exception:
        return None


def _letterbox_onto(img: Image.Image, canvas_w: int, canvas_h: int) -> Image.Image:
    """Paste ``img`` (already 'L') top-left onto a black canvas_w x canvas_h
    canvas. Top-left anchoring matches how a viewport renders content from the
    origin, so shared content lands on shared coordinates."""
    if img.width == canvas_w and img.height == canvas_h:
        return img
    canvas = Image.new("L", (canvas_w, canvas_h), color=0)
    canvas.paste(img, (0, 0))
    return canvas


def diff_screenshots(baseline_bytes: bytes, actual_bytes: bytes) -> dict:
    """Deterministic perceptual diff of two screenshots.

    Returns a dict::

        {
            "changed_ratio": float,   # 0.0 .. 1.0, fraction of canvas pixels changed
            "changed_bbox":  [x, y, w, h] | None,  # changed region in canvas coords
            "identical":     bool,    # True iff changed_ratio == 0.0
        }

    ADVISORY ONLY — feeds a display badge / geometric overlay. Never raises;
    un-loadable input yields ``identical=True, changed_ratio=0.0, bbox=None``.
    """
    base = _load_gray(baseline_bytes)
    actual = _load_gray(actual_bytes)

    # Either side missing / corrupt -> conservative "no change to report".
    if base is None or actual is None:
        return {"changed_ratio": 0.0, "changed_bbox": None, "identical": True}

    # Common letterbox canvas = max of each dimension. This is the fix: baseline
    # (recording res) and actual (viewport res) differ, so a same-size diff is
    # either impossible or reports ~100%. Padded black so shared content aligns.
    canvas_w = max(base.width, actual.width)
    canvas_h = max(base.height, actual.height)
    if canvas_w <= 0 or canvas_h <= 0:
        return {"changed_ratio": 0.0, "changed_bbox": None, "identical": True}

    # Cap pathological canvases (keep aspect ratio so bbox stays proportional).
    longest = max(canvas_w, canvas_h)
    if longest > _MAX_CANVAS_EDGE:
        scale = _MAX_CANVAS_EDGE / float(longest)
        canvas_w = max(1, int(round(canvas_w * scale)))
        canvas_h = max(1, int(round(canvas_h * scale)))
        # Scale each source proportionally into the (downscaled) canvas.
        base = base.resize(
            (max(1, int(round(base.width * scale))), max(1, int(round(base.height * scale))))
        )
        actual = actual.resize(
            (max(1, int(round(actual.width * scale))), max(1, int(round(actual.height * scale))))
        )

    base_lb = _letterbox_onto(base, canvas_w, canvas_h)
    actual_lb = _letterbox_onto(actual, canvas_w, canvas_h)

    # Absolute per-pixel grayscale delta, then threshold to a 0/255 mask:
    # anything <= PIXEL_THRESHOLD becomes 0 (unchanged), everything else 255.
    delta = ImageChops.difference(base_lb, actual_lb)
    mask = delta.point(lambda p: 255 if p > PIXEL_THRESHOLD else 0)

    changed_bbox_raw = mask.getbbox()  # tight box around non-zero pixels, or None

    total_pixels = canvas_w * canvas_h
    if changed_bbox_raw is None or total_pixels <= 0:
        return {"changed_ratio": 0.0, "changed_bbox": None, "identical": True}

    # Count actually-changed pixels (the histogram of the binary mask: bucket 255
    # holds the changed count). Deterministic and exact.
    changed_pixels = mask.histogram()[255]
    changed_ratio = changed_pixels / float(total_pixels)
    if changed_ratio < 0.0:
        changed_ratio = 0.0
    elif changed_ratio > 1.0:
        changed_ratio = 1.0

    # getbbox() returns (left, upper, right, lower); convert to [x, y, w, h].
    left, upper, right, lower = changed_bbox_raw
    changed_bbox = [left, upper, right - left, lower - upper]

    return {
        "changed_ratio": changed_ratio,
        "changed_bbox": changed_bbox,
        "identical": changed_ratio == 0.0,
    }
