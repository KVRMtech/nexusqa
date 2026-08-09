"""eSignature widget recognition (P4) — deterministic, value-free.

The walk treats "sign" as a commit/danger word and crosses it as a boundary. But
a policy application ends at an eSignature step that is a WIDGET to operate, not a
button to cross: a canvas signature pad, an "I agree / electronically sign" attest
control, or an embedded vendor signer (DocuSign/Adobe Sign). This module names
which one a control is, so the walk can decide to operate it (or, gated, cross it)
instead of mis-handling it.

Value-free: the decision is made from labels/attributes (UI shape), never from any
user value. Pure predicate — no I/O.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Optional

KIND_CANVAS_SIGNATURE = "canvas_signature"
KIND_ATTEST = "attest"
KIND_VENDOR_IFRAME = "vendor_iframe"

#: "Sign in / out / on" is authentication, never an eSignature — mirror the
#: crawler's auth guard so it is excluded before any signature match.
_AUTH_RE = re.compile(r"\b(sign|log)\s*(in|out|on)\b", re.IGNORECASE)

#: Embedded third-party signers.
_VENDOR_RE = re.compile(
    r"\b(docu\s*sign|adobe\s*sign|echo\s*sign|hello\s*sign|sign\s*now|pandadoc|"
    r"eversign|signeasy|onespan)\b", re.IGNORECASE)

#: Attest / consent to sign electronically (a checkbox or button).
_ATTEST_RE = re.compile(
    r"\b(e[-\s]?sign(ature|ing)?|electronic(ally)?\s+sign(ature|ed|ing)?|"
    r"i\s+(agree|consent|authoriz\w*|acknowledg\w*)|"
    r"sign\s+(and\s+)?(submit|here|below|document|electronically)|"
    r"adopt\s+(and\s+sign|your\s+signature)|apply\s+signature)\b",
    re.IGNORECASE)

#: A canvas/field that is a place to draw or type a signature.
_SIGNATURE_WORD_RE = re.compile(r"signatur", re.IGNORECASE)
_DRAW_RE = re.compile(
    r"\b(draw|sign\s+here|signature\s+(pad|box|field|canvas)|"
    r"type\s+your\s+(full\s+)?name|your\s+signature)\b", re.IGNORECASE)


def classify_esign_widget(control: Mapping[str, Any]) -> Optional[str]:
    """Recognize an eSignature widget from a control's UI shape.

    Returns one of ``KIND_CANVAS_SIGNATURE`` / ``KIND_ATTEST`` /
    ``KIND_VENDOR_IFRAME``, or ``None`` if this is not an eSignature widget.
    Precedence: auth is excluded first; then vendor, canvas, attest.
    """
    if not isinstance(control, Mapping):
        return None
    name = str(control.get("name") or "").strip()
    nl = name.lower()
    kind = str(control.get("kind") or "").strip().lower()
    tag = str(control.get("tag") or "").strip().lower()
    src = str(control.get("src") or control.get("url") or "")

    if _AUTH_RE.search(name):                       # "Sign in/out" — auth, not e-sign
        return None

    if _VENDOR_RE.search(src) or _VENDOR_RE.search(nl):
        return KIND_VENDOR_IFRAME

    is_canvas = tag == "canvas" or kind == "canvas"
    if is_canvas and (not nl or _SIGNATURE_WORD_RE.search(nl) or _DRAW_RE.search(nl)):
        return KIND_CANVAS_SIGNATURE
    if _SIGNATURE_WORD_RE.search(nl) and _DRAW_RE.search(nl):
        return KIND_CANVAS_SIGNATURE

    if _ATTEST_RE.search(name):
        return KIND_ATTEST

    return None


def is_esign_widget(control: Mapping[str, Any]) -> bool:
    """True when the control is any kind of eSignature widget."""
    return classify_esign_widget(control) is not None
