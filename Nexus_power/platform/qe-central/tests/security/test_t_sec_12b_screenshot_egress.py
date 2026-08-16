"""T-SEC-12 (pixel half) — SCREENSHOT EGRESS.

ATTACK / GAP
============
The first M0.5 cut scanned the vision PROMPT and left the IMAGE beside it
untouched.  That is the wrong half: a screenshot of a filled insurance
application renders the SSN, the name and the account number as PIXELS, which no
text detector can see.  It is the highest-PII payload this system emits.

Worse, ``screenshot_b64`` was never validated to BE an image.  Any base64 string
in that field reached the model with no scan of any kind — an unguarded text
channel sitting inside the guarded one.

WHAT IS AND IS NOT CLAIMED
==========================
This closes what can honestly be closed:

  * the CONTAINER is validated — real image magic, bounded size;
  * the METADATA is scanned — PNG tEXt/iTXt/zTXt and EXIF carry readable text;
  * the PIXELS are NOT scanned, are never reported as scanned, and in a deployed
    environment their egress requires an explicit acknowledgement.

Reading the pixels would need OCR inside this service.  Until that exists, the
honest control is consent plus a refusal to pretend.
"""
from __future__ import annotations

import base64
import zlib

import pytest

from app.services.pii_egress_guard import (
    MAX_SCREENSHOT_BYTES,
    guard_image,
    pixel_egress_allowed,
)


# ── fixtures: real image containers ────────────────────────────────────────

def _png(text_chunks: list[tuple[bytes, bytes]] | None = None) -> bytes:
    """A minimal but structurally real PNG, optionally carrying tEXt metadata."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (len(payload).to_bytes(4, "big") + kind + payload
                + zlib.crc32(kind + payload).to_bytes(4, "big"))

    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
                 + bytes([8, 6, 0, 0, 0]))
    for key, value in (text_chunks or []):
        out += chunk(b"tEXt", key + b"\x00" + value)
    out += chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
    out += chunk(b"IEND", b"")
    return out


def _b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


CLEAN_PNG = _b64(_png())
JPEG = _b64(b"\xff\xd8\xff\xe0" + b"\x00" * 64)


# ── the smuggling channel ──────────────────────────────────────────────────

def test_a_non_image_payload_is_refused():
    """THE hole: any base64 string used to reach the model unscanned."""
    smuggled = _b64(b"the applicant SSN is 123-45-6789 " * 40)
    verdict = guard_image(smuggled, site="vision:test")
    assert verdict["safe"] is False
    assert "does not contain an image" in verdict["reason"]


def test_an_undecodable_payload_is_refused():
    assert guard_image("not base64 at all!!", site="vision:test")["safe"] is False


def test_an_empty_payload_is_refused():
    assert guard_image("", site="vision:test")["safe"] is False
    assert guard_image(None, site="vision:test")["safe"] is False


def test_an_oversized_image_is_refused():
    huge = _b64(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_SCREENSHOT_BYTES + 1))
    verdict = guard_image(huge, site="vision:test")
    assert verdict["safe"] is False and "exceeds" in verdict["reason"]


# ── metadata scanning ──────────────────────────────────────────────────────

def test_pii_in_png_text_metadata_is_detected():
    """Capture tooling stamps text chunks; they are readable and they egress."""
    tainted = _b64(_png([(b"Comment", b"applicant ssn 123-45-6789")]))
    verdict = guard_image(tainted, site="vision:test")
    assert verdict["safe"] is False
    assert "image metadata" in verdict["reason"]
    assert verdict["matches"]


def test_benign_metadata_is_not_blocked():
    ok = _b64(_png([(b"Software", b"qe-explorer capture")]))
    assert guard_image(ok, site="vision:test")["safe"] is True


# ── honesty about the pixels ───────────────────────────────────────────────

@pytest.mark.parametrize("payload", [CLEAN_PNG, JPEG])
def test_a_valid_image_passes_but_is_never_reported_as_pixel_scanned(payload):
    verdict = guard_image(payload, site="vision:test")
    assert verdict["safe"] is True
    assert verdict["pixels_scanned"] is False


def test_every_refusal_also_reports_pixels_unscanned():
    """No result shape may imply the image content was inspected."""
    for payload in ("", "!!", _b64(b"plain text payload here"), CLEAN_PNG):
        assert guard_image(payload, site="vision:test")["pixels_scanned"] is False


# ── the consent gate ───────────────────────────────────────────────────────

@pytest.mark.parametrize("env", ["development", "test", "", "local"])
def test_pixel_egress_is_allowed_in_a_non_deployed_environment(env, monkeypatch):
    monkeypatch.delenv("QEC_VISION_PIXEL_EGRESS", raising=False)
    allowed, _ = pixel_egress_allowed(env)
    assert allowed is True


@pytest.mark.parametrize("env", ["production", "staging"])
def test_pixel_egress_requires_acknowledgement_in_a_deployed_environment(
    env, monkeypatch,
):
    monkeypatch.delenv("QEC_VISION_PIXEL_EGRESS", raising=False)
    allowed, why = pixel_egress_allowed(env)
    assert allowed is False
    assert "cannot be PII-scanned" in why


@pytest.mark.parametrize("flag", ["1", "true", "yes", "on"])
def test_an_explicit_acknowledgement_permits_it(flag, monkeypatch):
    monkeypatch.setenv("QEC_VISION_PIXEL_EGRESS", flag)
    assert pixel_egress_allowed("production")[0] is True


def test_the_deployed_gate_blocks_a_valid_image_without_acknowledgement(monkeypatch):
    monkeypatch.delenv("QEC_VISION_PIXEL_EGRESS", raising=False)
    verdict = guard_image(CLEAN_PNG, site="vision:test", nexus_env="production")
    assert verdict["safe"] is False


# ── the chokepoint honours it ──────────────────────────────────────────────

def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_complete_vision_blocks_a_non_image_before_any_http(monkeypatch):
    import httpx

    from app.clients import platform_api

    def _explode(*a, **k):  # pragma: no cover — must not be reached
        raise AssertionError("an HTTP client was built for a blocked screenshot")

    monkeypatch.setattr(httpx, "AsyncClient", _explode)
    res = _run(platform_api.complete_vision(
        tenant_id="t", prompt="where do I click?",
        screenshot_b64=_b64(b"ssn 123-45-6789 " * 20), task="vision_medic"))
    assert res.ok is False
    assert "screenshot egress blocked" in res.detail


def test_complete_vision_blocks_tainted_image_metadata(monkeypatch):
    import httpx

    from app.clients import platform_api

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("HTTP client built")))
    res = _run(platform_api.complete_vision(
        tenant_id="t", prompt="where do I click?",
        screenshot_b64=_b64(_png([(b"Comment", b"ssn 123-45-6789")])),
        task="vision_medic"))
    assert res.ok is False and "screenshot egress blocked" in res.detail


def test_the_vision_chokepoint_gates_the_image_as_well_as_the_text():
    """Structural: both halves of the payload are checked, in that order."""
    import inspect

    from app.clients import platform_api

    src = inspect.getsource(platform_api.complete_vision)
    assert "_assert_egress_clean" in src
    assert "_assert_image_egress_clean" in src
    assert src.index("_assert_egress_clean") < src.index("_assert_image_egress_clean")
    # and both precede the token mint / request build
    assert src.index("_assert_image_egress_clean") < src.index("_mint_or_refuse")
