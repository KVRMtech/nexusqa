"""Environment parameter substitution (Phase 4 — record-once environments).

An environment can be a parameterized recipe rather than a fixed URL: a CIT box is
``http://{box}.usaa.com`` and a runway is a cookie value ``{runway}``. At resolve
time the ``{param}`` placeholders are filled with concrete run-time values (from the
environment's saved defaults or passed at launch), so a box or runway is "recorded
once, re-pointed later" — turn a dial, not re-record.

Honest by construction: an UNKNOWN placeholder is left intact and surfaced via
``unresolved_params`` — never silently blanked into a wrong URL. Pure — no I/O.
"""
from __future__ import annotations

import json
import re

# {name} where name starts with a letter — avoids matching JSON braces / CSS.
_PARAM = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")


def find_params(value: object) -> list:
    """The distinct ``{param}`` names referenced anywhere in a str/list/dict value."""
    try:
        blob = value if isinstance(value, str) else json.dumps(value, default=str)
    except (TypeError, ValueError):
        blob = str(value)
    return sorted(set(_PARAM.findall(blob)))


def _sub_str(text: str, params: dict) -> str:
    def repl(m: "re.Match") -> str:
        name = m.group(1)
        val = params.get(name)
        return str(val) if val is not None else m.group(0)  # leave unknown intact
    return _PARAM.sub(repl, str(text))


def substitute(value: object, params: dict) -> object:
    """Recursively substitute ``{param}`` in a str / list / dict env value."""
    if isinstance(value, str):
        return _sub_str(value, params)
    if isinstance(value, list):
        return [substitute(v, params) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, params) for k, v in value.items()}
    return value


def resolve_env_params(env_context: dict | None, params: dict | None) -> tuple:
    """Apply run-time ``params`` to an env_context's routing fields (base_url,
    cookies, headers, data_overrides). Returns ``(resolved_context, missing)`` where
    ``missing`` is the list of placeholders still unfilled (empty on full resolve)."""
    clean = {str(k): v for k, v in (params or {}).items() if v is not None}
    src = dict(env_context or {})
    resolved = dict(src)
    routed = {}
    for key in ("base_url", "cookies", "headers", "data_overrides"):
        if key in resolved:
            resolved[key] = substitute(resolved[key], clean)
            routed[key] = resolved[key]
    missing = find_params(routed)
    return resolved, missing


__all__ = ["find_params", "substitute", "resolve_env_params"]
