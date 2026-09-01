"""M3.1 / T-VIS-05 — PIXEL PII REDACTION BEFORE VISION EGRESS.

THE HOLE
========
``qe-central``'s :func:`pii_egress_guard.guard_image` is honest about what it
does and does not do, and what it does NOT do is the problem::

    ``pixels_scanned`` is ALWAYS ``False`` — it exists so no caller, log line or
    report can imply the image content was inspected when it was not.

So the screenshot half of the guard checks the CONTAINER (is it really a PNG,
is it under 8 MiB) and the METADATA (tEXt/EXIF text runs), and then relays a
full-page image of a filled insurance application — name, date of birth, SSN,
account number, all rendered as pixels — to a third-party model.  The remaining
control was ``QEC_VISION_PIXEL_EGRESS=1``, an *acknowledgement* that this
happens, not a defence against it.

THE FIX, AND WHY IT LIVES HERE
==============================
Redaction cannot live in qe-central: by the time the image arrives there it is a
flat PNG and nothing knows where the sensitive regions were.  The explorer is
the ONLY process that holds both the pixels and the DOM that produced them, so
it is the only place the two can be joined.  The screenshot is therefore masked
HERE, before it is base64-encoded, before it is signed, before it leaves the
container.

WHAT IS MASKED, AND WHY THE RULE IS SHAPE AND NOT CONTENT
=========================================================
Every VALUE-BEARING control is masked whether or not it currently holds
anything, plus every visible text run that matches a PII pattern, plus anything
the page itself declares sensitive (``type=password``, an ``autocomplete`` token
naming a person or an instrument, ``data-pii``).

Masking on shape rather than on content is deliberate and is the fail-closed
choice.  Deciding per-field would mean READING the value in order to judge it —
and a value read for that purpose is a value one bug away from being logged,
hashed into a fingerprint, or attached to a diagnostic.  The whole system is
built on never carrying a committed value across a boundary; this keeps that
true.  The cost is nil where it matters: vision escalates only on DOM-OPAQUE
pages, whose readable inputs are by definition few.

FAIL-CLOSED, IN THE ONLY WAY THAT COUNTS
========================================
:func:`redact_screenshot` returns ``None`` when it cannot prove the mask was
applied — Pillow missing, PNG undecodable, region read failed, size mismatch —
and the vision path treats ``None`` as "do not send".  A redaction that cannot
run must never degrade to sending the original; that degradation IS the leak.

LOGS
====
:class:`RedactedScreenshot` renders as ``<screenshot 41kB sha256:ab12cd34
regions=7 REDACTED>`` under ``str``, ``repr`` and ``%s``.  A developer who logs
the object — the accident this is written to survive — writes a digest, never an
image.  The raw bytes are reachable only through an explicitly named attribute.

Pure except for the optional Pillow decode.  No HTTP, no browser.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

#: The fill used for a mask.  Opaque black: a blur or a pixelate is reversible
#: enough to have been broken in public, and a solid block is not.
MASK_RGB = (0, 0, 0)

#: Redaction method recorded on the wire receipt.  Version it: a future change
#: to the masking rule must be distinguishable in the evidence of old crawls.
METHOD = "dom-region-blackout-v1"

#: Regions are clamped to this many per screenshot.  A page with more sensitive
#: regions than this is masked for the first N *and refused* (see
#: :func:`redact_screenshot`) rather than partially masked — a partial mask is
#: the worst of both worlds.
MAX_REGIONS = 400


class RedactionUnavailable(Exception):
    """Redaction could not be proven to have happened.  Egress must not occur."""


@dataclass(frozen=True)
class RedactedScreenshot:
    """A screenshot that has been masked, and the receipt proving it.

    ``png`` is the masked image.  Nothing in this object is the original: the
    unmasked bytes are dropped by :func:`redact_screenshot` and never stored, so
    there is no attribute an accident could reach them through.
    """

    png: bytes
    regions_masked: int
    page_w: int
    page_h: int
    method: str = METHOD

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.png).hexdigest()

    def b64(self) -> str:
        """The wire encoding.  A method rather than a field so a dataclass
        ``repr``/``asdict`` can never carry the image by accident."""
        return base64.b64encode(self.png).decode("ascii")

    def receipt(self) -> dict[str, Any]:
        """The claim that travels BESIDE the image so the server can enforce it.

        ``image_sha256`` binds the receipt to these exact bytes: a receipt
        detached from its image would be a checkbox, and a checkbox is not a
        control.
        """
        return {
            "applied": True,
            "method": self.method,
            "regions": int(self.regions_masked),
            "page_w": int(self.page_w),
            "page_h": int(self.page_h),
            "image_sha256": self.digest,
        }

    # A screenshot must be unloggable by accident. All three string protocols
    # are covered because logging reaches for %s, f-strings reach for __format__
    # (which defaults to __str__), and a bare repr() in a traceback reaches for
    # __repr__.
    def __repr__(self) -> str:
        return ("<screenshot %dB sha256:%s regions=%d REDACTED>"
                % (len(self.png), self.digest[:8], self.regions_masked))

    __str__ = __repr__


def _rects(regions: Iterable[Mapping[str, Any]], *, scale: float,
           img_w: int, img_h: int) -> list[tuple[int, int, int, int]]:
    """Region dicts → integer pixel boxes, scaled, clamped, degenerates dropped.

    Each box is inflated by one pixel on every side.  Sub-pixel layout means an
    exact box can leave a readable sliver of a glyph at the boundary, and a
    sliver of an account number is an account number.
    """
    out: list[tuple[int, int, int, int]] = []
    for r in regions or ():
        if not isinstance(r, Mapping):
            continue
        try:
            x = float(r.get("x", 0)) * scale
            y = float(r.get("y", 0)) * scale
            w = float(r.get("w", 0)) * scale
            h = float(r.get("h", 0)) * scale
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        left = max(0, int(x) - 1)
        top = max(0, int(y) - 1)
        right = min(img_w, int(x + w) + 1)
        bottom = min(img_h, int(y + h) + 1)
        if right <= left or bottom <= top:
            continue        # entirely outside the captured image
        out.append((left, top, right, bottom))
    return out


def redact_screenshot(
    png_bytes: bytes,
    regions: Sequence[Mapping[str, Any]],
    *,
    page_w: float = 0,
    page_h: float = 0,
    regions_ok: bool = True,
) -> Optional[RedactedScreenshot]:
    """Mask ``regions`` out of ``png_bytes``.  ``None`` means DO NOT SEND.

    ``regions_ok=False`` is how a caller says "the region read itself failed" —
    an empty region list from a failed read is indistinguishable from an empty
    region list from a page with nothing sensitive on it, and only the caller
    knows which it holds.  Passing ``False`` refuses, which is the whole point:
    a page whose sensitive regions could not be located must not be photographed
    and sent.

    Refuses (returns ``None``) on: a failed region read, empty bytes, Pillow
    absent, an undecodable image, more regions than :data:`MAX_REGIONS`, or any
    error while drawing.  Succeeds — with ``regions_masked=0`` — only when the
    read SUCCEEDED and genuinely found nothing to mask.
    """
    if not regions_ok:
        logger.warning("qec.vision.redaction_refused reason=region_read_failed")
        return None
    if not png_bytes:
        logger.warning("qec.vision.redaction_refused reason=empty_image")
        return None
    if len(regions or ()) > MAX_REGIONS:
        # Not "mask the first 400": a partial mask reads as a full one in every
        # downstream report, which is the failure mode this whole module exists
        # to prevent.
        logger.warning("qec.vision.redaction_refused reason=too_many_regions n=%d",
                       len(regions or ()))
        return None
    try:
        from io import BytesIO

        from PIL import Image, ImageDraw
    except Exception as exc:
        logger.warning("qec.vision.redaction_refused reason=pillow_unavailable err=%s",
                       str(exc)[:120])
        return None
    try:
        img = Image.open(BytesIO(png_bytes))
        img.load()
        img = img.convert("RGB")
    except Exception as exc:
        logger.warning("qec.vision.redaction_refused reason=undecodable err=%s",
                       str(exc)[:120])
        return None

    img_w, img_h = img.size
    # The screenshot is full-page at the context's device scale factor, while the
    # regions are CSS pixels. Deriving the scale from the two widths we actually
    # hold is safer than trusting a reported DPR, which is one config change from
    # being wrong.
    scale = (float(img_w) / float(page_w)) if page_w and float(page_w) > 0 else 1.0
    boxes = _rects(regions, scale=scale, img_w=img_w, img_h=img_h)
    try:
        draw = ImageDraw.Draw(img)
        for box in boxes:
            draw.rectangle(box, fill=MASK_RGB)
        buf = BytesIO()
        img.save(buf, format="PNG")
        masked = buf.getvalue()
    except Exception as exc:
        logger.warning("qec.vision.redaction_refused reason=draw_failed err=%s",
                       str(exc)[:120])
        return None
    if not masked:
        return None
    logger.info("qec.vision.redaction_applied regions=%d image_w=%d image_h=%d",
                len(boxes), img_w, img_h)
    return RedactedScreenshot(png=masked, regions_masked=len(boxes),
                              page_w=int(page_w or img_w),
                              page_h=int(page_h or img_h))


def verify_receipt(receipt: Mapping[str, Any], screenshot_b64: str) -> tuple[bool, str]:
    """Server-side half: does this receipt actually describe THESE bytes?

    Lives in the explorer beside the producer so both ends of the wire read one
    implementation, and is imported by ``qe-central``'s guard through the frozen
    contract file rather than by import (the two services cannot import each
    other — see ``contracts/``).  Returns ``(ok, reason)``.
    """
    if not isinstance(receipt, Mapping) or not receipt.get("applied"):
        return False, "no redaction receipt"
    if str(receipt.get("method") or "") != METHOD:
        return False, "unknown redaction method %r" % (receipt.get("method"),)
    claimed = str(receipt.get("image_sha256") or "")
    if len(claimed) != 64:
        return False, "receipt carries no image digest"
    raw = (screenshot_b64 or "").strip()
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception:
        return False, "screenshot is not valid base64"
    if hashlib.sha256(blob).hexdigest() != claimed:
        # The receipt describes a DIFFERENT image than the one being sent —
        # which is what a redaction bypass looks like from the server side.
        return False, "redaction receipt does not match the image being sent"
    return True, ""


__all__ = ["MASK_RGB", "METHOD", "MAX_REGIONS", "RedactionUnavailable",
           "RedactedScreenshot", "redact_screenshot", "verify_receipt"]
