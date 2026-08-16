"""Fail-closed PII egress guard for the Data Agent (Phase 3).

Source redaction (substrate/redact) runs at PERSISTENCE and is fail-OPEN. Sending
field metadata to a (possibly cloud) LLM tier is a DIFFERENT egress point that needs a
FAIL-CLOSED guard: if PII is detected in the payload — or if detection cannot run at
all — the guard refuses to send, and the Data Agent falls back to the deterministic
floor. A regulated buyer's SSN/account# must never egress to an LLM.

Two defences:
  1. VALUE-FREE payload — only labels/types/options are ever assembled for the LLM,
     never a filled or observed value. So a real secret a user typed cannot leave.
  2. FAIL-CLOSED scan — the value-free payload is still scanned (a captured option or
     label could itself contain a real identifier). Any match, or any failure of the
     detector to run, marks the payload UNSAFE — the caller then skips the LLM.

Reuses the shipped ``nexus_sdk.llm.pii_guard`` detector (SSN / Luhn-valid card / etc.).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)


class EgressBlocked(Exception):
    """Raised when the payload cannot be proven free of PII before an LLM call."""


def _detector():
    # Imported lazily so a missing SDK is a fail-CLOSED block, never an import crash.
    from nexus_sdk.llm.pii_guard import check_text
    return check_text


def scan(text: str) -> list:
    """Detect PII in a string. FAIL-CLOSED: if the detector cannot run, RAISE
    ``EgressBlocked`` so the caller blocks egress rather than send unscanned text."""
    try:
        check_text = _detector()
    except Exception as exc:  # SDK absent/broken → never send unscanned
        raise EgressBlocked(f"PII detector unavailable ({exc}); refusing to send to the LLM") from exc
    try:
        return list(check_text(text or ""))
    except Exception as exc:  # a scan error is also fail-closed
        raise EgressBlocked(f"PII scan failed ({exc}); refusing to send to the LLM") from exc


def value_free_payload(inventory: Iterable[Mapping]) -> str:
    """Assemble ONLY labels/types/options for the LLM — never a value the user typed."""
    parts: list[str] = []
    for f in inventory:
        parts.append(str(f.get("label") or ""))
        parts.append(str(f.get("type") or ""))
        for o in (f.get("options") or ()):
            parts.append(str(o))
    return "\n".join(p for p in parts if p)


#: Set False (``QEC_PII_EGRESS_GUARD=0``) ONLY to diagnose a false positive.  It
#: is read at call time, never cached, and every disabled call is logged — so a
#: deployment running unguarded is visible in its own logs rather than silent.
def _guard_enabled() -> bool:
    import os

    return (os.environ.get("QEC_PII_EGRESS_GUARD", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def guard_text(text: str, *, site: str) -> dict:
    """THE egress gate for one outbound payload (M0.5 T-SEC-12).

    ``site`` names the call site (``complete``/``vision``/…) so a block is
    attributable.  Returns ``{"safe": bool, "reason": str, "matches": [...]}``.

    FAIL-CLOSED in both directions that matter: a positive detection blocks, and
    a detector that cannot run blocks.  The scan is over the payload we are
    about to SEND, so there is no way for a caller to be "already checked" and
    skip it — the check lives at the wire, not at the caller.

    NEVER logs the matched text, only the pattern NAMES: the whole point is to
    keep an identifier out of a remote system, and copying it into our own logs
    on the way to refusing would defeat that.
    """
    if not _guard_enabled():
        logger.warning("qec.egress.pii_guard_disabled site=%s", site)
        return {"safe": True, "reason": "guard disabled", "matches": []}
    try:
        matches = scan(text or "")
    except EgressBlocked as exc:
        logger.warning("qec.egress.blocked site=%s reason=%s", site, str(exc)[:200])
        return {"safe": False, "reason": str(exc), "matches": []}
    if matches:
        names = sorted({str(getattr(m, "pattern_name", "pii")) for m in matches})
        logger.warning("qec.egress.pii_detected site=%s patterns=%s", site, names)
        return {
            "safe": False,
            "reason": (
                "PII detected in the outbound payload "
                f"({', '.join(names)}) — refusing to send it to an external model"
            ),
            "matches": names,
        }
    return {"safe": True, "reason": "", "matches": []}


# ── Screenshot (pixel) egress ───────────────────────────────────────────────
#
# A page screenshot is the HIGHEST-PII payload this system sends anywhere: a
# filled insurance application renders the SSN, the name and the account number
# as PIXELS. No text detector can see them, so a text scan of the prompt says
# nothing about the image travelling beside it.
#
# Three things are therefore true at once, and the guard states all three rather
# than pretending the first one covers the rest:
#
#   1. the payload CONTAINER is checked — it must really be an image, within a
#      size bound. Without this the ``image`` field is an unscanned text channel:
#      any base64 string reached the model, and no prompt scan ever looked at it;
#   2. the image METADATA is scanned — PNG tEXt/iTXt/zTXt chunks and EXIF hold
#      readable text that tooling routinely stamps in (URLs, usernames, paths);
#   3. the PIXELS are NOT scanned, and are never claimed to be. Reading them
#      would need OCR inside this service. So pixel egress is a CONSENT
#      decision, not a scanning one — see :func:`pixel_egress_allowed`.

#: 8 MiB — far above a full-page PNG, far below anything that could be an
#: exfiltration channel dressed as a screenshot.
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024

#: Magic prefixes for the image types the vision path legitimately produces.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"RIFF", "webp"),           # RIFF....WEBP
)

#: Printable ASCII runs of at least this length are pulled out of the container
#: for scanning. Shorter runs are noise in compressed data.
_MIN_TEXT_RUN = 6


def _printable_runs(blob: bytes, *, limit: int = 200_000) -> str:
    """Extract readable ASCII runs from binary data (metadata, not pixels).

    Bounded so a pathological payload cannot turn the scan into the denial of
    service it exists to prevent.
    """
    out: list[bytes] = []
    current = bytearray()
    for byte in blob[:limit]:
        if 32 <= byte < 127:
            current.append(byte)
        else:
            if len(current) >= _MIN_TEXT_RUN:
                out.append(bytes(current))
            current = bytearray()
    if len(current) >= _MIN_TEXT_RUN:
        out.append(bytes(current))
    return "\n".join(r.decode("ascii", "replace") for r in out)


def pixel_egress_allowed(nexus_env: str) -> tuple[bool, str]:
    """May raw screenshot PIXELS leave for an external model? (pure)

    The pixels cannot be scanned, so this is an ACKNOWLEDGEMENT, not a check.
    In a DEPLOYED environment — where the screenshots are of a real client's
    real application — it must be granted explicitly with
    ``QEC_VISION_PIXEL_EGRESS=1``. In development/test it is allowed, so local
    work and the suite are unaffected.

    Note this is the SECOND gate, not the first: vision is already off unless
    ``QEC_CRAWL_VISION_ENABLED`` is set. An operator turning vision on in
    production is asked to say separately that unredactable imagery of their
    client's screens may go to a third party.
    """
    import os

    from ..config import DEPLOYED_ENVS

    if (nexus_env or "").strip().lower() not in DEPLOYED_ENVS:
        return True, ""
    flag = (os.environ.get("QEC_VISION_PIXEL_EGRESS", "") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True, ""
    return False, (
        "screenshot pixels cannot be PII-scanned and this is a deployed "
        "environment - set QEC_VISION_PIXEL_EGRESS=1 to acknowledge that "
        "unredactable images of the client's screens may reach an external model"
    )


def guard_image(screenshot_b64: str, *, site: str, nexus_env: str = "") -> dict:
    """Decide whether ONE screenshot may egress (M0.5 T-SEC-12, pixel half).

    Returns ``{"safe", "reason", "matches", "pixels_scanned"}``.
    ``pixels_scanned`` is ALWAYS ``False`` — it exists so no caller, log line or
    report can imply the image content was inspected when it was not.

    Fail-closed on: an undecodable payload, a payload that is not an image, an
    oversized payload, PII in the image metadata, or unacknowledged pixel egress
    in a deployed environment.
    """
    import base64

    result = {"safe": False, "reason": "", "matches": [], "pixels_scanned": False}
    if not _guard_enabled():
        logger.warning("qec.egress.pii_guard_disabled site=%s (image)", site)
        return {**result, "safe": True, "reason": "guard disabled"}

    raw = (screenshot_b64 or "").strip()
    if not raw:
        return {**result, "reason": "empty screenshot payload"}
    # Some callers prefix a data URL; accept it, then judge the bytes.
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception as exc:
        logger.warning("qec.egress.screenshot_undecodable site=%s error=%s",
                       site, str(exc)[:120])
        return {**result, "reason": "screenshot is not valid base64"}

    if len(blob) > MAX_SCREENSHOT_BYTES:
        return {**result,
                "reason": f"screenshot exceeds {MAX_SCREENSHOT_BYTES} bytes"}
    if not any(blob.startswith(magic) for magic, _ in _IMAGE_MAGIC):
        # THE smuggling hole: the ``image`` field used to accept any string and
        # relay it to the model without a single scan touching it.
        logger.warning("qec.egress.screenshot_not_an_image site=%s", site)
        return {**result,
                "reason": "the screenshot field does not contain an image "
                          "(PNG/JPEG/WebP) - refusing to relay it unscanned"}

    verdict = guard_text(_printable_runs(blob), site=f"{site}:image-metadata")
    if not verdict["safe"]:
        return {**result, "reason": f"image metadata: {verdict['reason']}",
                "matches": verdict["matches"]}

    allowed, why = pixel_egress_allowed(nexus_env)
    if not allowed:
        logger.warning("qec.egress.pixel_egress_unacknowledged site=%s", site)
        return {**result, "reason": why}
    return {**result, "safe": True}


def guard_inventory(inventory: Iterable[Mapping]) -> dict:
    """Decide whether the field inventory is SAFE to send to an LLM tier.

    Returns ``{safe, reason, matches, payload}``. ``safe`` is False — and the caller
    MUST skip the LLM and use the deterministic floor — when PII is detected in the
    (value-free) metadata OR the detector could not run (fail-closed).
    """
    inv = list(inventory)
    payload = value_free_payload(inv)
    try:
        matches = scan(payload)
    except EgressBlocked as exc:
        logger.warning("qec.data_agent.egress_blocked", extra={"reason": str(exc)[:200]})
        return {"safe": False, "reason": str(exc), "matches": [], "payload": payload}
    if matches:
        names = [getattr(m, "pattern_name", "pii") for m in matches]
        logger.warning("qec.data_agent.egress_pii_detected", extra={"patterns": names})
        return {"safe": False, "reason": "PII detected in field metadata", "matches": names, "payload": payload}
    return {"safe": True, "reason": "", "matches": [], "payload": payload}


__all__ = [
    "MAX_SCREENSHOT_BYTES",
    "EgressBlocked",
    "guard_image",
    "guard_inventory",
    "guard_text",
    "pixel_egress_allowed",
    "scan",
    "value_free_payload",
]
