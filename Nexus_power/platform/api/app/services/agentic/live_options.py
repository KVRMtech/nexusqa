"""Eyes — Live-Option Capture (the grounding spine for Context).

Generic + additive. Context may only claim "value X is invalid for this control" if it
can SEE the control's real options. The failure-state accessibility capture
(diff_and_heal/heal_capture_store nodes) is name+role only, so we HARVEST the live
option set from the captured nodes by option-like role, and we collect the SIBLING
recorded field values (the controlling context) from the recording. If neither yields
a genuine live option set, Context stays INERT — no live options, no claim.

No domain vocabulary: works for any chooser on any form.
"""
from __future__ import annotations

# Roles whose captured nodes represent SELECTABLE OPTIONS of a chooser. A failing
# option-bearing control's valid values are the names of its sibling option nodes.
_OPTION_ROLES = frozenset({
    "option", "radio", "checkbox", "menuitemradio", "menuitemcheckbox",
    "listitem", "treeitem", "tab", "switch", "menuitem",
})


def _norm(s) -> str:
    return " ".join((s or "").strip().lower().split())


def _name(n) -> str:
    if isinstance(n, dict):
        return n.get("name") or n.get("label") or n.get("accessible_name") or ""
    return ""


def _role(n) -> str:
    if isinstance(n, dict):
        return (n.get("role") or n.get("kind") or "").strip().lower()
    return ""


def live_options(nodes, *, exclude=None) -> list[str]:
    """The set of live, selectable OPTION names harvested from the failure-state a11y
    capture — nodes whose role is option-like (radio / checkbox / option / ...).
    Generic: works for any chooser. Returns [] when the capture has no option-bearing
    nodes, which keeps Context inert (it cannot assert against options it cannot see)."""
    exn = _norm(exclude) if exclude else None
    out: list[str] = []
    seen: set = set()
    for n in (nodes or []):
        if _role(n) not in _OPTION_ROLES:
            continue
        nm = _name(n)
        if not nm:
            continue
        k = _norm(nm)
        if k in seen or (exn and k == exn):
            continue
        seen.add(k)
        out.append(nm)
    return out


def value_in_options(value, options) -> bool:
    """Is ``value`` present (verbatim, normalized) in the live ``options``? Used both to
    confirm an inconsistency (recorded value absent) and to ground a suggested fix
    (proposed value present)."""
    v = _norm(value)
    return bool(v) and any(_norm(o) == v for o in (options or []))


def sibling_field_values(recorded_steps, *, failing_step_number) -> list[dict]:
    """The OTHER recorded field values entered BEFORE the failing step — the controlling
    context (e.g. a value chosen earlier that constrains this control's valid options).
    Generic: any prior entry/select step with a label + value. Returns
    ``[{label, value, kind}]``."""
    out: list[dict] = []
    fsn = failing_step_number
    for s in (recorded_steps or []):
        if not isinstance(s, dict):
            continue
        n = s.get("step_number")
        if n is None or (fsn is not None and n >= fsn):
            continue
        label = (s.get("label") or "").strip()
        value = (s.get("value") or "").strip()
        verb = (s.get("verb") or "").strip().lower()
        if label and value and verb in ("type", "enter", "select", "fill", "choose", ""):
            out.append({"label": label, "value": value, "kind": s.get("kind") or ""})
    return out
