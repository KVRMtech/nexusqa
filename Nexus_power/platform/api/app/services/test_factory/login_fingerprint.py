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
    """Order- and case-invariant list of field IDENTIFIERS for each login field.

    The identifier is the strongest stable handle available (name > label > slot >
    autocomplete). Field TYPE is deliberately NOT part of the signature: types are
    often unknown when a recipe is RECORDED (from steps) yet known when a form is
    MATCHED at onboarding — including type would make those two keys differ and
    reuse would silently never match. Field names already separate login types
    (email vs member_number vs …). Fields with no identifier are dropped."""
    sig: list = []
    for raw in (fields or []):
        field = raw or {}
        ident = _norm_token(
            field.get("name") or field.get("label")
            or field.get("slot") or field.get("autocomplete") or "")
        if ident:
            sig.append(ident)
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


def match_by_key(candidate_key: str, library: list | None) -> dict | None:
    """The single library entry whose ``key`` equals ``candidate_key``, else None.
    ``library`` is an iterable of entries each carrying at least a ``key``."""
    for entry in (library or []):
        if entry and entry.get("key") == candidate_key:
            return entry
    return None


def candidates_by_domain(domain: object, library: list | None) -> list:
    """Login types already recorded on a domain — for the 'dotcom or portal?'
    prompt when a new app is onboarded on a known host before its form is seen."""
    dom = _norm_token(domain)
    return [e for e in (library or [])
            if e and _norm_token(e.get("domain")) == dom]


def propose_reuse(*, domain: object, login_path: object = None,
                  fields: list | None = None, submit: object = "",
                  library: list | None = None) -> dict:
    """The reuse decision for a newly-onboarding app — the heart of 'don't make
    the 1000th tester re-record':

    - a login FORM was observed (``fields`` given) -> compute its key and either
      ``reuse`` the exact match or ``record`` fresh;
    - only the domain is known (no form yet) and it already has recorded logins ->
      ``reuse`` when there is exactly one, else ``disambiguate`` (dotcom vs portal);
    - nothing matches -> ``record`` fresh.

    Never guesses: a form is matched only by its exact key; a bare domain with
    multiple types asks rather than picking one."""
    lib = list(library or [])
    if fields is not None:
        key = login_type_key(domain=domain, login_path=login_path or "/",
                             fields=fields, submit=submit)
        hit = match_by_key(key, lib)
        if hit:
            return {"action": "reuse", "key": key, "recipe": hit}
        return {"action": "record", "key": key}
    doms = candidates_by_domain(domain, lib)
    if len(doms) == 1:
        return {"action": "reuse", "key": doms[0].get("key"), "recipe": doms[0]}
    if len(doms) > 1:
        return {"action": "disambiguate", "options": doms}
    return {"action": "record"}


__all__ = [
    "login_form_signature",
    "login_type_key",
    "login_type_descriptor",
    "match_by_key",
    "candidates_by_domain",
    "propose_reuse",
]
