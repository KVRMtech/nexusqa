"""Login-type fingerprint — the reuse-matching key for the recipe library (Phase 5).

Two apps should share a recorded login recipe when they share a LOGIN TYPE, not
merely a domain: one host (``usaa.com``) can serve a public **dotcom** login AND a
member **portal** login. So the reuse key is

    domain  +  login-page path  +  a fingerprint of the login FORM's shape

— sensitive to structural differences (dotcom vs portal: different path and/or
different fields), robust to cosmetic ones (case, field order, whitespace, dynamic
element ids). This is the deterministic core of the "record once, reuse fleet-wide"
proposal (§3 of RECORD_ONCE_RUN_ANYWHERE_PLAN.md). Pure functions — no DB, no I/O.

The caller supplies ``domain`` already reduced to its registrable host (the crawler
computes that via ``registrable_domain``); this module never guesses the domain.
"""
from __future__ import annotations

import hashlib
import re

# Drop volatile tokens (counters, uuids, hashes) so a fingerprint is stable across
# crawls/apps: 2+ digit runs and 8+ char hex-ish runs are removed, so a dynamic
# element suffix (member_number_9f3a1c22b7) matches its clean form (member_number).
_DYNAMIC = re.compile(r"\d{2,}|[0-9a-f]{8,}", re.IGNORECASE)
_SEP = re.compile(r"[\s_\-]+")


def _norm_token(value: object) -> str:
    """Lowercase, drop dynamic ids, normalize separators to a single space."""
    text = str(value or "").strip().lower()
    text = _DYNAMIC.sub("", text)
    text = _SEP.sub(" ", text).strip()
    return text


def _norm_path(path: object) -> str:
    """Path only (query/hash dropped), leading-slash normalized. '' -> '/'."""
    raw = str(path or "").strip()
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    raw = _norm_token(raw)
    raw = raw.strip("/ ")
    return "/" + raw if raw else "/"


def _field_signature(fields: list | None) -> list:
    """Order- and case-invariant list of ``identifier:kind`` for each login field.

    The identifier is the strongest stable handle available (name > label > slot >
    autocomplete); the kind is the input type. Fields with neither are dropped."""
    sig: list = []
    for raw in (fields or []):
        field = raw or {}
        ident = _norm_token(
            field.get("name") or field.get("label")
            or field.get("slot") or field.get("autocomplete") or "")
        kind = _norm_token(field.get("type") or "text")
        if ident or kind:
            sig.append(ident + ":" + kind)
    return sorted(sig)


def login_form_signature(*, fields: list | None, submit: object = "") -> str:
    """The structural signature of a login FORM (order/case/id-invariant)."""
    parts = _field_signature(fields)
    sub = _norm_token(submit)
    if sub:
        parts.append("submit:" + sub)
    return "|".join(parts)


def login_type_key(*, domain: object, login_path: object,
                   fields: list | None, submit: object = "") -> str:
    """Stable reuse key for a login type.

    Same login shape on the same host+path -> same key (safe reuse); a different
    form shape or a different login path (dotcom vs portal) -> a different key (no
    false reuse across distinct logins on one host)."""
    basis = "\n".join((
        _norm_token(domain),
        _norm_path(login_path),
        login_form_signature(fields=fields, submit=submit),
    ))
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return "lt_" + digest


def login_type_descriptor(*, domain: object, login_path: object,
                          fields: list | None, submit: object = "") -> dict:
    """The key plus human-readable parts, for the reuse-proposal UI
    ('portal login already recorded on usaa.com — just enter your member number')."""
    return {
        "key": login_type_key(domain=domain, login_path=login_path,
                              fields=fields, submit=submit),
        "domain": _norm_token(domain),
        "login_path": _norm_path(login_path),
        "form_signature": login_form_signature(fields=fields, submit=submit),
        "field_count": len(fields or []),
    }


__all__ = [
    "login_form_signature",
    "login_type_key",
    "login_type_descriptor",
]
