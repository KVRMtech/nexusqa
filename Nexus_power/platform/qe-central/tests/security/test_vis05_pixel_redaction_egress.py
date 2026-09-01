"""M3.1 / T-VIS-05 — A SCREENSHOT MAY NOT LEAVE UNREDACTED.

WHAT WAS TRUE BEFORE THIS FILE
==============================
``guard_image`` validated the CONTAINER (real image magic, bounded size) and
scanned the METADATA (PNG tEXt / EXIF text runs), and then relayed the pixels.
Its own docstring said so: ``pixels_scanned`` is always ``False``.  The only
remaining control was ``QEC_VISION_PIXEL_EGRESS=1`` — an ACKNOWLEDGEMENT that
unreadable imagery of a client's screens reaches a third party.

An acknowledgement records that a risk was accepted.  It does not reduce it.  A
full-page screenshot of a filled life-insurance application renders the
applicant's name, date of birth, SSN and account number as pixels, and all of it
egressed.

WHAT IS TRUE NOW
================
The explorer masks the sensitive regions BEFORE encoding (it is the only process
holding both the pixels and the DOM that produced them) and sends a RECEIPT
bound by sha256 to the exact bytes.  This service refuses any screenshot whose
receipt is missing, malformed, of an unknown method, or describes different
bytes.

These tests assert the refusals, not the happy path — the happy path is already
covered by ``test_t_sec_12b_screenshot_egress``.  Every case here is a way the
protection could be lost, and each one must fail CLOSED.
"""
from __future__ import annotations

import base64
import hashlib
import zlib

import pytest

from app.services.pii_egress_guard import (
    REDACTION_METHOD,
    guard_image,
    verify_redaction_receipt,
)


# ── a structurally real PNG, so the container check is never what refuses ────

def _png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (len(payload).to_bytes(4, "big") + kind + payload
                + zlib.crc32(kind + payload).to_bytes(4, "big"))

    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
                 + bytes([8, 6, 0, 0, 0]))
    out += chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
    out += chunk(b"IEND", b"")
    return out


SHOT = base64.b64encode(_png()).decode("ascii")
OTHER = base64.b64encode(_png() + b"\x00").decode("ascii")


def _receipt(b64: str, **over) -> dict:
    out = {
        "applied": True,
        "method": REDACTION_METHOD,
        "regions": 4,
        "image_sha256": hashlib.sha256(base64.b64decode(b64)).hexdigest(),
    }
    out.update(over)
    return out


# ── 1. the refusals ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("receipt,why", [
    (None,                                   "no receipt at all"),
    ({},                                     "an empty receipt object"),
    ({"applied": False},                     "a receipt that says it did not run"),
    ({"applied": True},                      "a receipt with no method or digest"),
    ({"applied": True, "method": "blur-v9",
      "image_sha256": "0" * 64},             "an unknown redaction method"),
    ({"applied": True, "method": REDACTION_METHOD,
      "image_sha256": "short"},              "a malformed digest"),
])
def test_a_screenshot_without_a_valid_receipt_is_refused(receipt, why):
    verdict = guard_image(SHOT, site="vision:test", redaction=receipt)
    assert verdict["safe"] is False, why
    assert verdict["pixels_redacted"] is False
    assert "redaction" in verdict["reason"].lower()


def test_a_receipt_for_a_DIFFERENT_image_is_refused():
    """THE BYPASS THIS BINDING EXISTS FOR.

    Redact one screenshot, keep its receipt, then send the unmasked original
    beside it.  Without the sha256 binding the receipt is a boolean the caller
    sets, and a boolean the caller sets is not a control.
    """
    verdict = guard_image(OTHER, site="vision:test", redaction=_receipt(SHOT))
    assert verdict["safe"] is False
    assert "does not describe the image being sent" in verdict["reason"]


def test_a_valid_receipt_for_these_exact_bytes_passes():
    verdict = guard_image(SHOT, site="vision:test", redaction=_receipt(SHOT))
    assert verdict["safe"] is True
    assert verdict["pixels_redacted"] is True
    # …and never claims more than it did.
    assert verdict["pixels_scanned"] is False


def test_redaction_is_checked_BEFORE_the_consent_flag(monkeypatch):
    """Order is a property, not an implementation detail.

    An operator who set ``QEC_VISION_PIXEL_EGRESS=1`` consented to REDACTED
    imagery reaching a model.  If the consent flag were read first, that consent
    would silently cover the unredacted case it was never given for.
    """
    monkeypatch.setenv("QEC_VISION_PIXEL_EGRESS", "1")
    verdict = guard_image(SHOT, site="vision:test", nexus_env="production",
                          redaction=None)
    assert verdict["safe"] is False
    assert "redaction" in verdict["reason"].lower()
    # The same call WITH a receipt is the one the acknowledgement authorises.
    assert guard_image(SHOT, site="vision:test", nexus_env="production",
                       redaction=_receipt(SHOT))["safe"] is True


def test_a_deployed_environment_still_needs_the_acknowledgement(monkeypatch):
    """Redaction does not replace consent; it is the other half of it."""
    monkeypatch.delenv("QEC_VISION_PIXEL_EGRESS", raising=False)
    verdict = guard_image(SHOT, site="vision:test", nexus_env="production",
                          redaction=_receipt(SHOT))
    assert verdict["safe"] is False
    assert verdict["pixels_redacted"] is True      # it WAS masked…
    assert "QEC_VISION_PIXEL_EGRESS" in verdict["reason"]   # …and still unconsented


# ── 2. the receipt verifier in isolation ────────────────────────────────────

def test_verify_receipt_rejects_undecodable_payloads():
    ok, reason = verify_redaction_receipt(_receipt(SHOT), "!!not base64!!")
    assert ok is False and "base64" in reason


def test_verify_receipt_accepts_a_data_url_prefix():
    ok, _ = verify_redaction_receipt(_receipt(SHOT), "data:image/png;base64," + SHOT)
    assert ok is True


# ── 3. VISION CANNOT BYPASS THE GUARD ───────────────────────────────────────

def test_complete_vision_refuses_an_unredacted_screenshot(monkeypatch):
    """The wire chokepoint, exercised through the real ``complete_vision``.

    ``httpx.AsyncClient`` is replaced with one that RAISES if it is ever
    constructed, so the assertion is not "we returned ok=False" but "no HTTP
    client was built at all" — the image did not reach the network.
    """
    import asyncio

    from app.clients import platform_api

    class _Detonate:
        def __init__(self, *a, **kw):
            raise AssertionError(
                "an unredacted screenshot reached the HTTP layer")

    monkeypatch.setattr(platform_api.httpx, "AsyncClient", _Detonate)
    res = asyncio.run(platform_api.complete_vision(
        tenant_id="t", prompt="what is on screen?", screenshot_b64=SHOT,
        system="s", task="vision_perceive"))          # <- no redaction=
    assert res.ok is False
    assert "redaction" in (res.detail or "").lower()


def test_complete_vision_defaults_to_blocking_when_a_caller_forgets():
    """``redaction`` defaults to ``None``, and ``None`` BLOCKS.

    A future call site that forgets the parameter loses the capability, not the
    protection.  This is asserted on the signature so the default cannot drift
    to something permissive without this failing.
    """
    import inspect

    from app.clients import platform_api

    sig = inspect.signature(platform_api.complete_vision)
    assert sig.parameters["redaction"].default is None
    assert verify_redaction_receipt(None, SHOT)[0] is False


def test_no_other_function_in_this_service_posts_to_the_vision_endpoint():
    """The guard is at the WIRE, so it cannot be routed around.

    ``complete_vision`` must be the only place ``/api/v1/llm/vision`` is called.
    A second caller would be a second, unguarded path — which is exactly the
    shape T-SEC-12 was written to end, restated for the pixel half.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    hits = [p for p in root.rglob("*.py")
            if "/api/v1/llm/vision" in p.read_text(encoding="utf-8")]
    assert [p.name for p in hits] == ["platform_api.py"], hits


# ── 4. THE LOGS DO NOT RETAIN THE IMAGE ─────────────────────────────────────

def test_a_refusal_never_logs_the_screenshot(caplog, monkeypatch):
    """Blocking a payload must not copy it into our own logs on the way out.

    The whole purpose is to keep imagery out of a remote system; writing it to
    stdout instead is the same leak with a different destination.
    """
    import asyncio
    import logging

    from app.clients import platform_api

    class _Detonate:
        def __init__(self, *a, **kw):
            raise AssertionError("should not reach HTTP")

    monkeypatch.setattr(platform_api.httpx, "AsyncClient", _Detonate)
    with caplog.at_level(logging.DEBUG):
        guard_image(SHOT, site="vision:test", redaction=None)
        asyncio.run(platform_api.complete_vision(
            tenant_id="t", prompt="p", screenshot_b64=SHOT, system="s",
            task="vision_perceive"))
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert SHOT not in blob
    # …and not a recognisable fragment of it either.
    assert SHOT[:40] not in blob
