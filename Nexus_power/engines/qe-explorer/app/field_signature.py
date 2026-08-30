"""FIELD SIGNATURE — the value-free fingerprint a field can be learned by.

A crawl that asks a client for a value, gets it, and then asks the SAME question
on the next crawl has learned nothing.  Remembering the answer needs a key, and
the key decides whether the learning is worth anything at scale:

  * keyed by URL, learning is per-application — what one client teaches about
    their Social Security field teaches nothing about anyone else's;
  * keyed by the field's own SEMANTICS, learning transfers.  "A field labelled
    Social Security Number, ``maxlength=11``, ``autocomplete`` unset, matching
    ``\\d{3}-\\d{2}-\\d{4}``" is the same field in every application on earth.

So a signature is built ONLY from what the application itself declares about the
field: its accessible name, its input type, its own validation constraints, and
the SHAPE of its options.  Never from a value, never from a URL, never from a
tenant.  That is what makes it safe to share a signature across clients when the
value it once held could never be.

PURE + DETERMINISTIC.  No I/O, no clock, no randomness — the same control always
produces the same signature, on any machine, so a signature recorded in evidence
months ago still resolves today.

WHAT IS DELIBERATELY EXCLUDED
    the field's current or committed value, the page URL, the tenant, the app,
    any id/class/name attribute that carries a build hash (``css-1x2y3z``), and
    anything that would make the same logical field signature differently after
    a cosmetic redeploy.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

__all__ = ["compute", "signature_of", "SIGNATURE_VERSION"]

#: Bumped only when the feature set changes in a way that must invalidate every
#: previously stored signature. Stored alongside the hash so old rows are
#: identifiable rather than silently mismatched.
SIGNATURE_VERSION = 1

#: Build-hash-looking tokens (``css-1x2y3z``, ``sc-bdVaJa``, ``jsx-1234567``) —
#: they change on a redeploy that did not change the field, so including them
#: would make a signature expire for no reason.
_HASHY_RE = re.compile(r"^(?:css|sc|jsx|emotion|styled|mui|chakra)[-_][a-z0-9]{4,}$", re.I)
#: A token that is mostly digits/hex carries no semantics (``field_38271``).
_OPAQUE_RE = re.compile(r"^[a-f0-9]{6,}$|^\d{3,}$", re.I)

#: Separators an authoring convention might use inside one logical word.
_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _tokens(text: Any) -> list[str]:
    """Normalise free text into stable, meaning-bearing tokens.

    Case, punctuation, camelCase and snake_case all collapse, so ``firstName``,
    ``first_name`` and ``First Name`` are ONE signature — the same field authored
    three ways must not become three things to learn."""
    raw = "" if text is None else str(text)
    # split camelCase before lowering so the boundary survives
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)
    out: list[str] = []
    for tok in _SPLIT_RE.split(raw.lower()):
        if not tok or _HASHY_RE.match(tok) or _OPAQUE_RE.match(tok):
            continue
        out.append(tok)
    return out


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def _qec(control: Mapping[str, Any]) -> Mapping[str, Any]:
    q = control.get("qec")
    return q if isinstance(q, Mapping) else {}


def _first(control: Mapping[str, Any], *keys: str) -> str:
    """First non-empty of several attribute spellings, checking the nested ``qec``
    envelope too — the inventory populates one or the other depending on how the
    control was discovered."""
    q = _qec(control)
    for k in keys:
        v = control.get(k)
        if v is None or v == "":
            v = q.get(k)
        if v not in (None, ""):
            return _norm(v)
    return ""


def _option_shape(control: Mapping[str, Any]) -> str:
    """The SHAPE of an option set, never its contents.

    Two country dropdowns are the same field even though one lists 195 countries
    and the other 12. But a two-option yes/no is NOT the same field as a
    fifty-option state picker, so the bucketed size carries real signal."""
    opts = control.get("options")
    if not isinstance(opts, (list, tuple)):
        return ""
    n = len([o for o in opts if str(o).strip()])
    if n == 0:
        return ""
    if n <= 2:
        return "binary"
    if n <= 6:
        return "few"
    if n <= 20:
        return "many"
    return "large"


def _constraints(control: Mapping[str, Any]) -> str:
    """The application's OWN declared validation, normalised.

    This is the strongest non-linguistic signal there is: an app that declares
    ``pattern="\\d{3}-\\d{2}-\\d{4}"`` has told us exactly what it wants, in a
    language that is identical across every app that wants the same thing."""
    parts: list[str] = []
    pattern = _first(control, "pattern")
    if pattern:
        # the pattern is a declaration, not free text — keep it verbatim but bounded
        parts.append("p=" + pattern[:120])
    for attr, prefix in (("maxlength", "max"), ("minlength", "min"),
                         ("max", "hi"), ("min", "lo"), ("step", "st")):
        v = _first(control, attr)
        if v:
            parts.append(f"{prefix}={v[:24]}")
    if control.get("required") or _qec(control).get("required"):
        parts.append("req")
    if control.get("multiple") or _qec(control).get("multiple"):
        parts.append("multi")
    return "|".join(sorted(parts))


def compute(control: Mapping[str, Any], *, kind: str = "",
            page_role: str = "") -> dict[str, Any]:
    """Fingerprint one control for learning. Returns the signature plus the
    value-free features it was built from, so a stored signature can always be
    explained rather than being an opaque hash nobody can audit.

    ``page_role`` is an OPTIONAL coarse context token (the section of the app the
    field appeared in). It is deliberately NOT part of the hash: the same field
    must resolve to the same signature wherever it appears, or a value learned on
    one page would have to be re-learned on the next."""
    q = _qec(control)
    # RAW, not normalised: `_tokens` splits camelCase, and lowering first would
    # destroy the boundary that makes `firstName` and `first_name` one signature.
    # THE QUESTION IS THE FIELD; ITS ANSWERS ARE NOT SEPARATE FIELDS.
    #
    # MEASURED (underwriting fixture, 2026-08-29): a page of 63 questions
    # produced a field ledger with THREE entries. The ledger dedups on this
    # signature, and this line began with `control["name"]` -- for a radio, the
    # OPTION. All 126 radio controls on the page therefore hashed to TWO
    # signatures ("Yes", "No") and the selects to a third; everything else was
    # discarded as a duplicate of something it had nothing to do with.
    #
    # `question_label` is the application's own wording, taken from a declared
    # accessible-name rung only and left "" when the page declared none -- so a
    # control with no declared question keeps exactly the identity it had, which
    # is pinned as a control test. Preferring it here also satisfies this
    # function's own contract: "the same field must resolve to the same signature
    # wherever it appears". "Do you smoke?" is that field; "Yes" never was.
    name_raw = (control.get("question_label") or q.get("question_label")
                or control.get("name") or q.get("name")
                or control.get("label") or q.get("label")
                or control.get("accessible_name") or q.get("accessible_name") or "")
    input_type = _first(control, "input_type", "type")
    # `autocomplete` is a W3C-standard vocabulary — when an app sets it, the app has
    # NAMED the field's semantics itself. That is a declaration, not a heuristic,
    # which is why it is carried separately and weighted first by the classifier.
    autocomplete = _first(control, "autocomplete", "auto_complete")
    placeholder = _first(control, "placeholder")
    inputmode = _first(control, "inputmode", "input_mode")

    tokens = _tokens(name_raw)
    if not tokens:
        # A nameless field still has semantics if the app declared them elsewhere.
        tokens = _tokens(control.get("placeholder") or q.get("placeholder"))             or _tokens(control.get("id") or q.get("id"))

    kind_n = _norm(kind) or _norm(control.get("kind") or q.get("kind"))
    shape = _option_shape(control)
    constraints = _constraints(control)

    material = "".join((
        f"v{SIGNATURE_VERSION}",
        " ".join(tokens),
        kind_n,
        input_type,
        autocomplete,
        inputmode,
        shape,
        constraints,
    ))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    return {
        "signature": digest,
        "version": SIGNATURE_VERSION,
        "tokens": tokens,
        "kind": kind_n,
        "input_type": input_type,
        "autocomplete": autocomplete,
        "inputmode": inputmode,
        "placeholder_tokens": _tokens(control.get("placeholder") or q.get("placeholder")),
        "option_shape": shape,
        "constraints": constraints,
        "page_role": _norm(page_role),
    }


def signature_of(control: Mapping[str, Any], *, kind: str = "") -> str:
    """Just the hash, for callers that only need the key."""
    return compute(control, kind=kind)["signature"]
