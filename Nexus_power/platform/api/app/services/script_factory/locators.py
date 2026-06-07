"""Deterministic locator + action derivation from a step's OBSERVED evidence.

Pure functions, no I/O, no LLM: the same observed signal always yields the same
Playwright locator. User-facing locators in priority order
(getByRole(name) -> getByLabel -> getByText), anchor-scoped to disambiguate
repeated controls. Never absolute/index XPath.
"""

from __future__ import annotations

import re

# Container role used to scope an element by its captured spatial anchor.
# The anchor is stored as visible text; 'row' is the dominant repeating-control
# container (results tables / lists). Generation-time best-effort; flagged so a
# reviewer can adjust if the container is not a row.
_DEFAULT_ANCHOR_ROLE = "row"

# kind (captured) -> ARIA role for getByRole.
_KIND_ROLE = {
    "button": "button",
    "link": "link",
    "tab": "tab",
    "checkbox": "checkbox",
    "radio": "radio",
    "toggle": "radio",
    "menuitem": "menuitem",
}

_RE_META = re.compile(r"([.*+?^${}()|\[\]\\/])")


def js_str(value: str) -> str:
    """Escape a Python string for a single-quoted JS string literal."""
    s = "" if value is None else str(value)
    s = s.replace("\\", "\\\\").replace("'", "\\'")
    s = s.replace("\r", " ").replace("\n", " ").strip()
    return s


def js_regex_literal(path: str) -> str:
    """A JS regex literal that matches the given URL path (metachars escaped)."""
    escaped = _RE_META.sub(r"\\\1", path or "")
    return f"/{escaped}/"


def element_locator(observed: dict) -> str:
    """User-facing locator for one element, anchor-scoped when an anchor exists.

    Priority: getByRole(role, {name}) -> getByLabel -> getByText.
    """
    label = js_str(observed.get("label", ""))
    kind = (observed.get("kind") or "").strip().lower()
    anchor = js_str(observed.get("anchor", ""))

    if kind == "field":
        el = f"getByLabel('{label}')"
    else:
        role = _KIND_ROLE.get(kind)
        if role and label:
            el = f"getByRole('{role}', {{ name: '{label}' }})"
        elif label:
            el = f"getByText('{label}')"
        else:
            el = "getByText('')"

    if anchor:
        return f"page.getByRole('{_DEFAULT_ANCHOR_ROLE}', {{ name: '{anchor}' }}).{el}"
    return f"page.{el}"


def url_path(url: str) -> str:
    """Path component of a captured URL (drops scheme/host/query — stable part)."""
    u = (url or "").strip()
    if not u:
        return ""
    # already a path
    if u.startswith("/"):
        return u.split("?", 1)[0]
    # strip scheme://host
    without_scheme = u.split("://", 1)[-1]
    slash = without_scheme.find("/")
    if slash == -1:
        return ""
    return without_scheme[slash:].split("?", 1)[0]
