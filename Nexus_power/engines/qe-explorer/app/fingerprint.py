"""STATE FINGERPRINT — an SPA-state-aware, cosmetic-invariant page identity.

The crawler's priority frontier and its "have I seen this state?" set both key
on :func:`state_fingerprint`.  A URL alone is too coarse (a single-page app
serves many states from one URL) and too fine (``/orders/123`` vs
``/orders/456`` are the SAME state shape); a full DOM hash is far too noisy
(any cosmetic text edit would mint a "new" state and the crawl would never
converge).

The fingerprint is therefore a sha256 over exactly two grounded signals:

  * the URL TEMPLATE — host + id-normalised path (+ an SPA hash-route when
    present), query DROPPED (session/tracking params are cosmetic; a genuine
    state difference shows up in the controls below);
  * the sorted set of INTERACTIVE ``(role, name, disabled)`` triples — the
    controls a user can actually operate.  Static/decorative text is excluded,
    so a copy tweak never moves the fingerprint, while a newly-revealed
    required field, an opened menu, or a disabled→enabled Submit all do.

Plus any caller-supplied ``dialog_flags`` (e.g. ``"modal:confirm-delete"``) so
an overlay/dialog state is distinct from the same page with nothing open.

Determinism: everything is normalised (whitespace-collapsed, lower-cased) and
sorted, so two crawls of the identical state on different runs hash to the same
hex — the property the manifest golden-stability test depends on.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

# ─── URL-template normalisation ────────────────────────────────────────────────

#: A pure-integer path/route segment (``/orders/123``).
_INT_SEG = re.compile(r"^\d+$")
#: A canonical UUID segment.
_UUID_SEG = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
#: A long hex/hash segment (object ids, content hashes).
_HEX_SEG = re.compile(r"^[0-9a-fA-F]{12,}$")
#: A run of 3+ digits anywhere in a segment (``order-00042``, ``v2024`` → id-ish).
_DIGIT_RUN = re.compile(r"\d{3,}")

#: Placeholder every id-shaped segment collapses to.
ID_PLACEHOLDER = "*"


def _is_id_segment(seg: str) -> bool:
    """True when a path/route segment is a volatile identifier, not a route name."""
    if not seg:
        return False
    return bool(
        _INT_SEG.match(seg)
        or _UUID_SEG.match(seg)
        or _HEX_SEG.match(seg)
        or _DIGIT_RUN.search(seg)
    )


def _template_path(path: str) -> str:
    """Collapse id-shaped segments to :data:`ID_PLACEHOLDER` (``/orders/123`` →
    ``/orders/*``).  Empty segments (leading slash / doubles) are preserved so
    ``/`` stays ``/``."""
    if not path:
        return ""
    return "/".join(ID_PLACEHOLDER if _is_id_segment(seg) else seg
                     for seg in path.split("/"))


def _template_fragment(fragment: str) -> str:
    """Normalise an SPA hash-route fragment; drop a cosmetic in-page anchor.

    A fragment is treated as a client route only when it looks like one
    (starts with ``/`` or ``!`` or contains a ``/`` — e.g. ``#/orders/123`` or
    ``#!/dashboard``).  A bare ``#section-2`` scroll anchor is cosmetic and is
    dropped so it never moves the fingerprint.
    """
    frag = (fragment or "").strip()
    if not frag:
        return ""
    routeish = frag.startswith("/") or frag.startswith("!") or "/" in frag
    if not routeish:
        return ""
    lead = ""
    body = frag
    if body.startswith("!"):
        lead, body = "!", body[1:]
    return lead + _template_path(body)


def url_template(url: str) -> str:
    """Return the cosmetic-invariant URL template for ``url``.

    ``scheme://host/path?query#frag`` → ``host + id-normalised-path
    (+ '#' + normalised-route)``.  Scheme, port and query are dropped; the host
    is lower-cased.  Deterministic and safe on malformed input.
    """
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    host = (parts.hostname or "").lower()
    template = host + _template_path(parts.path or "")
    route = _template_fragment(parts.fragment or "")
    if route:
        template += "#" + route
    return template


# ─── Interactive-control signature ─────────────────────────────────────────────

# Roles that count toward state identity.  Kept in sync with
# app.inventory.INTERACTIVE_ROLES; duplicated here so fingerprint has NO import
# dependency on inventory (either module may be used standalone).
_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "listbox",
    "checkbox", "radio", "switch", "menuitem", "menuitemcheckbox",
    "menuitemradio", "tab", "option", "slider", "spinbutton", "gridcell",
})
_CONTROL_KINDS = frozenset(
    {"text", "date", "select", "link", "toggle", "button", "checkbox", "radio"}
)


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def _is_interactive(control: Mapping[str, Any]) -> bool:
    """A control counts when its role is interactive OR it carries a refined
    control ``kind`` (i.e. it came out of :func:`app.inventory.build_inventory`)."""
    if _norm(control.get("role")) in _INTERACTIVE_ROLES:
        return True
    return _norm(control.get("kind")) in _CONTROL_KINDS


def interactive_signature(controls: Iterable[Mapping[str, Any]]) -> list[list[str]]:
    """The sorted, de-duplicated ``[role, name, disabled]`` set of interactive
    controls — the state-bearing half of the fingerprint.

    ``name`` is whitespace-collapsed + lower-cased so a purely cosmetic
    re-render can never move it; ``disabled`` is normalised to ``"1"``/``"0"``
    so a Submit flipping enabled↔disabled DOES move it.
    """
    seen: set[tuple[str, str, str]] = set()
    for control in controls or []:
        if not isinstance(control, Mapping) or not _is_interactive(control):
            continue
        triple = (
            _norm(control.get("role")),
            _norm(control.get("name")),
            "1" if (control.get("disabled") is True
                    or _norm(control.get("disabled")) in ("true", "1", "yes", "disabled")) else "0",
        )
        seen.add(triple)
    return [list(t) for t in sorted(seen)]


def _normalise_flags(dialog_flags: Any) -> list[str]:
    if not dialog_flags:
        return []
    if isinstance(dialog_flags, (str, bytes)):
        dialog_flags = [dialog_flags]
    out = {_norm(f) for f in dialog_flags if _norm(f)}
    return sorted(out)


# ─── Public entry point ────────────────────────────────────────────────────────


def state_fingerprint(
    url: str,
    controls: Iterable[Mapping[str, Any]],
    dialog_flags: Any = (),
) -> str:
    """A sha256 hex identifying a page STATE (design §3.2 fingerprint contract).

    Args:
        url: the state's location; only its :func:`url_template` is hashed.
        controls: inventoried controls (or any dicts carrying ``role``/``name``/
            ``disabled``); only the interactive ``(role, name, disabled)`` set is
            hashed.
        dialog_flags: optional extra SPA-state flags (open modal/dialog markers);
            normalised and hashed so an overlay state is distinct.

    Returns:
        A 64-char lower-case sha256 hex.  Identical states across runs hash
        identically; a cosmetic text change does not move it; a new required
        field / opened dialog / enabled-state change does.
    """
    payload = {
        "url": url_template(url),
        "controls": interactive_signature(controls),
        "dialogs": _normalise_flags(dialog_flags),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
