"""M3.1 / T-VIS-05 — the explorer half: pixels are MASKED before they egress.

These tests read the actual PIXELS of the produced image.  A redaction test that
only checks a flag is the thing it is supposed to prevent.
"""
from __future__ import annotations

from io import BytesIO

import pytest

from app.pixel_redaction import (
    MAX_REGIONS,
    METHOD,
    RedactedScreenshot,
    redact_screenshot,
    verify_receipt,
)

PIL = pytest.importorskip("PIL", reason="Pillow is required to mask pixels")


def _png(w=200, h=100, colour=(255, 255, 255)) -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, format="PNG")
    return buf.getvalue()


def _pixel(png: bytes, x: int, y: int):
    from PIL import Image

    return Image.open(BytesIO(png)).convert("RGB").getpixel((x, y))


# ── 1. the mask is really applied to the image ──────────────────────────────

def test_the_masked_region_is_actually_black_and_the_rest_is_not():
    shot = redact_screenshot(_png(), [{"x": 20, "y": 20, "w": 40, "h": 30}],
                             page_w=200, page_h=100)
    assert shot is not None
    assert _pixel(shot.png, 40, 35) == (0, 0, 0)      # inside the region
    assert _pixel(shot.png, 150, 80) == (255, 255, 255)  # outside it
    assert shot.regions_masked == 1


def test_the_mask_over_covers_by_a_pixel():
    """Sub-pixel layout leaves a readable sliver of a glyph at an exact edge,
    and a sliver of an account number is an account number."""
    shot = redact_screenshot(_png(), [{"x": 50, "y": 50, "w": 20, "h": 20}],
                             page_w=200, page_h=100)
    assert _pixel(shot.png, 49, 50) == (0, 0, 0)      # one px left of the box
    assert _pixel(shot.png, 70, 60) == (0, 0, 0)      # one px right of it


def test_regions_are_scaled_when_the_capture_is_at_a_higher_device_ratio():
    """The scale is derived from the two widths actually held, not from a
    reported DPR — a reported DPR is one config change away from being wrong."""
    shot = redact_screenshot(_png(400, 200), [{"x": 10, "y": 10, "w": 20, "h": 20}],
                             page_w=200, page_h=100)      # image is 2x the page
    assert _pixel(shot.png, 40, 40) == (0, 0, 0)          # 20,20 CSS -> 40,40 px
    assert _pixel(shot.png, 100, 100) == (255, 255, 255)


def test_regions_outside_the_image_are_dropped_not_clamped_into_a_mask():
    shot = redact_screenshot(_png(), [{"x": 900, "y": 900, "w": 10, "h": 10}],
                             page_w=200, page_h=100)
    assert shot.regions_masked == 0
    assert _pixel(shot.png, 100, 50) == (255, 255, 255)


@pytest.mark.parametrize("bad", [
    {"x": 0, "y": 0, "w": 0, "h": 10},
    {"x": 0, "y": 0, "w": 10, "h": 0},
    {"x": "a", "y": 0, "w": 10, "h": 10},
    "not a mapping",
])
def test_degenerate_regions_are_skipped_without_failing_the_whole_mask(bad):
    shot = redact_screenshot(_png(), [bad, {"x": 5, "y": 5, "w": 10, "h": 10}],
                             page_w=200, page_h=100)
    assert shot is not None and shot.regions_masked == 1


# ── 2. FAIL-CLOSED: None means DO NOT SEND ──────────────────────────────────

def test_a_failed_region_read_refuses_rather_than_sending_the_original():
    """THE distinction the whole module turns on.

    An empty region list from a FAILED read is indistinguishable from an empty
    list from a page with nothing sensitive on it. Only the caller knows which
    it holds, so it passes ``regions_ok`` — and ``False`` refuses. Degrading to
    "send it unmasked" IS the leak.
    """
    assert redact_screenshot(_png(), [], page_w=200, page_h=100,
                             regions_ok=False) is None


def test_an_empty_page_with_a_SUCCESSFUL_read_still_produces_an_image():
    shot = redact_screenshot(_png(), [], page_w=200, page_h=100, regions_ok=True)
    assert shot is not None and shot.regions_masked == 0


def test_empty_bytes_refuse():
    assert redact_screenshot(b"", [{"x": 1, "y": 1, "w": 2, "h": 2}],
                             page_w=10, page_h=10) is None


def test_an_undecodable_image_refuses():
    assert redact_screenshot(b"not a png at all", [], page_w=10, page_h=10) is None


def test_too_many_regions_refuses_rather_than_masking_a_prefix():
    """A PARTIAL mask reads as a full one in every downstream report."""
    many = [{"x": i, "y": 0, "w": 1, "h": 1} for i in range(MAX_REGIONS + 1)]
    assert redact_screenshot(_png(), many, page_w=200, page_h=100) is None


def test_a_missing_pillow_refuses(monkeypatch):
    """No Pillow means no proof the mask ran, and no proof means no egress."""
    import builtins

    png = _png()                       # built BEFORE Pillow is taken away
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no pillow here")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert redact_screenshot(png, [{"x": 1, "y": 1, "w": 2, "h": 2}],
                             page_w=10, page_h=10) is None


# ── 3. the receipt binds the claim to the bytes ─────────────────────────────

def test_the_receipt_describes_the_MASKED_bytes():
    shot = redact_screenshot(_png(), [{"x": 1, "y": 1, "w": 5, "h": 5}],
                             page_w=200, page_h=100)
    receipt = shot.receipt()
    assert receipt["applied"] is True and receipt["method"] == METHOD
    ok, why = verify_receipt(receipt, shot.b64())
    assert ok is True, why


def test_a_receipt_does_not_verify_against_a_DIFFERENT_image():
    a = redact_screenshot(_png(colour=(255, 255, 255)), [], page_w=200, page_h=100)
    b = redact_screenshot(_png(colour=(10, 10, 10)), [], page_w=200, page_h=100)
    ok, why = verify_receipt(a.receipt(), b.b64())
    assert ok is False and "does not match" in why


@pytest.mark.parametrize("receipt,fragment", [
    (None,                                              "no redaction receipt"),
    ({},                                                "no redaction receipt"),
    ({"applied": True, "method": "blur"},               "unknown redaction method"),
    ({"applied": True, "method": METHOD},               "no image digest"),
])
def test_a_malformed_receipt_never_verifies(receipt, fragment):
    ok, why = verify_receipt(receipt, "")
    assert ok is False and fragment in why


# ── 4. the object cannot be logged by accident ──────────────────────────────

def test_a_screenshot_renders_as_a_digest_under_every_string_protocol():
    """A developer who logs the object writes a digest, never an image.

    ``%s``, f-strings and a bare ``repr`` in a traceback take three different
    routes to a string, so all three are covered.
    """
    shot = redact_screenshot(_png(), [], page_w=200, page_h=100)
    for rendered in (str(shot), repr(shot), "%s" % (shot,), f"{shot}"):
        assert "REDACTED" in rendered
        assert shot.b64()[:40] not in rendered
    assert "png=" not in repr(shot)


def test_nothing_on_the_object_holds_the_unmasked_original():
    """There is no attribute an accident could reach the original through."""
    original = _png(colour=(255, 255, 255))
    shot = redact_screenshot(original, [{"x": 0, "y": 0, "w": 200, "h": 100}],
                             page_w=200, page_h=100)
    assert isinstance(shot, RedactedScreenshot)
    for value in vars(shot).values():
        assert value != original
    # …and the fully-masked image really is fully masked.
    assert _pixel(shot.png, 100, 50) == (0, 0, 0)
