"""Vision perception helpers (any-UI U2 — G1/G2/G3). PURE + value-free.

The DOM-opaque layer needs three things beyond "click a coordinate":
  * G1 — a COARSE perceptual hash of the screenshot so a canvas app's screens are
    distinct fingerprints (``average_hash`` / ``perceptual_hash_png``);
  * G3 — a JITTER-TOLERANT signature for a perceived control so catalog question
    ids don't churn on OCR/render wobble (``vision_control_signature``);
  * G2/G3 — turning a Perceiver's ``controls[]`` / ``displayed_values[]`` into the
    same shapes the walk's inventory + outcome paths already consume
    (``synthesize_vision_controls`` / ``synthesize_vision_outcomes``).

No I/O except the optional Pillow decode in ``perceptual_hash_png``; everything
else is deterministic on plain inputs, so it unit-tests without a browser.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Sequence

CAPTURE_VISION = "vision"


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


# ── G1: coarse perceptual hash ──────────────────────────────────────────────────

def average_hash(gray_rows: Iterable[Iterable[int]]) -> str:
    """aHash: one bit per cell, set when the cell is ≥ the frame mean — coarse and
    animation-tolerant (a blinking cursor / minor repaint keeps the same hash; a
    different screen changes it). ``gray_rows`` = rows of grayscale ints (0-255).
    Empty input → "".
    """
    flat = [int(v) for row in gray_rows for v in row]
    if not flat:
        return ""
    mean = sum(flat) / len(flat)
    bits = 0
    for v in flat:
        bits = (bits << 1) | (1 if v >= mean else 0)
    width = (len(flat) + 3) // 4
    return format(bits, "0%dx" % width)


def perceptual_hash_png(png_bytes: bytes, size: int = 8) -> str:
    """Coarse perceptual hash of a PNG screenshot (G1): downscale to ``size×size``
    grayscale and aHash. Returns "" if Pillow is unavailable or the bytes don't
    decode — the caller then simply supplies no hash and the fingerprint stays
    DOM-only (honest degradation, never a crash).
    """
    if not png_bytes:
        return ""
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:
        return ""
    try:
        img = Image.open(BytesIO(png_bytes)).convert("L").resize((size, size))
        rows = [[img.getpixel((x, y)) for x in range(size)] for y in range(size)]
    except Exception:
        return ""
    return average_hash(rows)


# ── G3: jitter-tolerant identity for a vision-perceived control ──────────────────

def bbox_bucket(bbox: Sequence[float], grid: int = 12) -> str:
    """The coarse grid cell of a bbox CENTER, so small render/OCR jitter maps to
    the same cell. ``bbox`` = ``[x, y, w, h]`` as fractions of the page (0..1).
    Returns ``"r{row}c{col}"``.
    """
    try:
        x, y, w, h = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    except (TypeError, ValueError, IndexError):
        return "r0c0"
    cx, cy = x + w / 2.0, y + h / 2.0
    col = max(0, min(grid - 1, int(cx * grid)))
    row = max(0, min(grid - 1, int(cy * grid)))
    return "r%dc%d" % (row, col)


def vision_control_signature(
    label: Any, role: Any, bbox: Sequence[float], grid: int = 12
) -> str:
    """G3: a stable, value-free signature for a vision-perceived control —
    normalized label + role + coarse position bucket. Survives small OCR/label
    wobble and sub-cell jitter, so ``question_id_for`` stays stable across
    re-crawls and ``catalog_diff`` does not cry wolf.
    """
    basis = "%s|%s|%s" % (_norm(role) or "button", _norm(label), bbox_bucket(bbox, grid))
    return "vis:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# ── G2/G3: Perceiver output → walk-consumable shapes ─────────────────────────────

def _norm_bbox(bbox: Any, page_w: float, page_h: float) -> list[float]:
    """A pixel bbox → page fractions [x, y, w, h] in 0..1 (clamped)."""
    try:
        x, y, w, h = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
    except (TypeError, ValueError, IndexError):
        return [0.0, 0.0, 0.0, 0.0]
    pw, ph = max(1.0, float(page_w or 1)), max(1.0, float(page_h or 1))
    return [max(0.0, min(1.0, x / pw)), max(0.0, min(1.0, y / ph)),
            max(0.0, min(1.0, w / pw)), max(0.0, min(1.0, h / ph))]


def synthesize_vision_controls(
    perceived: Iterable[Mapping[str, Any]], page_w: float, page_h: float,
) -> list[dict[str, Any]]:
    """G2/G3: turn a Perceiver's ``controls[]`` into ControlRecord-shaped dicts the
    walk's inventory→act path can consume. Each perceived control carries a
    ``label``, ``role``, pixel ``bbox`` and a page-pixel click point
    (``click_x``/``click_y``). The synthetic record marks ``capture_mode:"vision"``
    and a low name confidence, carries the G3 signature and the click coordinates,
    so the action is a coordinate click that R0 must still verify.
    """
    out: list[dict[str, Any]] = []
    for c in perceived:
        if not isinstance(c, Mapping):
            continue
        label = str(c.get("label") or c.get("name") or "").strip()
        role = _norm(c.get("role")) or "button"
        bbox_px = c.get("bbox") or [0, 0, 0, 0]
        norm = _norm_bbox(bbox_px, page_w, page_h)
        sig = vision_control_signature(label, role, norm)
        out.append({
            "name": label,
            "name_source": "vision",
            "role": role,
            "kind": "button",
            "tag": "canvas",
            "signature": sig,
            "qec": {
                "signature": sig,
                "capture_mode": CAPTURE_VISION,
                "name_confidence": "medium" if label else "low",
                "click_x": c.get("click_x"),
                "click_y": c.get("click_y"),
                "bbox": [round(v, 4) for v in norm],
            },
        })
    return out


def route_opaque_surfaces(
    surfaces: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Route each detected opaque surface to its handler (U1/U2).

    ``OPAQUE_JS`` emits ``{kind, label, reason}`` for the surfaces the DOM walker
    cannot read. A **cross-origin iframe** is ENTERABLE — Playwright's
    ``frame_locator`` crosses origins, so the walk re-observes INSIDE it (vendor
    payment / e-sign / captcha). A **canvas** or **closed-shadow** surface has no
    DOM to enter → the vision Perceiver (U2). Returns
    ``{enter_frames: [...], vision: [...]}``. Pure.
    """
    enter: list[dict[str, Any]] = []
    vision: list[dict[str, Any]] = []
    for s in surfaces:
        if not isinstance(s, Mapping):
            continue
        kind = str(s.get("kind") or "").strip().lower()
        if kind == "cross_origin_iframe":
            enter.append(dict(s))
        elif kind in ("canvas", "closed_shadow"):
            vision.append(dict(s))
    return {"enter_frames": enter, "vision": vision}


def synthesize_vision_outcomes(
    values: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """G2: turn a Perceiver's ``displayed_values[]`` (read from pixels) into the
    ``{label, selector, text}`` outcome shape the flow-ledger already captures —
    with ``selector:""`` (coordinate, not DOM) and vision provenance, so a canvas
    journey carries its premium/decision/policy-number PROOF, not just navigation.
    """
    out: list[dict[str, Any]] = []
    for v in values:
        if not isinstance(v, Mapping):
            continue
        label = str(v.get("label") or "").strip()
        text = str(v.get("text") or v.get("value") or "").strip()
        if not (label or text):
            continue
        out.append({"label": label, "selector": "", "text": text,
                    "source": CAPTURE_VISION})
    return out
