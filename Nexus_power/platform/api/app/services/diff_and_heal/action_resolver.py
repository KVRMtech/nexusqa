"""Re-anchor resolver — broadens TrueFix beyond control-kind to the renamed /
moved control class.

Given the RECORDED intent for a control (its accessible label + kind) and a
snapshot of the LIVE page's accessibility tree captured at the moment the step
FAILED, find the control the test should now target. Deterministic, $0, no LLM.

Safety doctrine (same as the rest of TrueFix): it REFUSES (returns None) unless a
single live node is a confident, unambiguous match. No confident match → a
genuine rename can't be proven → the caller falls toward needs_review, and the
real-regression lane is never overridden. It can therefore never silently
re-bind to the wrong (semantically-different) control — the one unacceptable
error for a self-heal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The ARIA roles a live node could plausibly have for a given recorded control
# kind. An empty/unknown kind accepts any role (name-similarity then carries the
# decision). Keeps a re-anchor from binding a textbox onto a button, etc.
_ROLE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "text": ("textbox", "searchbox", "combobox"),
    "select": ("combobox", "listbox", "menu"),
    "date": ("textbox", "combobox"),
    "button": ("button", "link"),
    "link": ("link", "button"),
    "toggle": ("checkbox", "radio", "switch"),
    "checkbox": ("checkbox", "switch"),
    "radio": ("radio",),
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _norm(s)))


def _similarity(a: str, b: str) -> float:
    """Cheap, dependency-free accessible-name similarity in [0,1]: token Jaccard,
    lifted when one name contains the other (a common rename shape, e.g.
    'Travel Class' → 'Trip Travel Class')."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    na, nb = _norm(a), _norm(b)
    substring = na in nb or nb in na
    return max(jac, 0.85 + 0.15 * jac) if substring else jac


@dataclass
class ReAnchor:
    name: str          # the live control's accessible name to re-bind to
    role: str          # its ARIA role (for the role rung of the ladder)
    confidence: float  # 0..1 name-similarity
    rationale: str


def flatten_aria(node, out: list | None = None) -> list[dict]:
    """Flatten a Playwright ``page.accessibility.snapshot()`` tree (nested
    {role,name,children}) into a flat ``[{role,name}]`` list of named nodes."""
    out = [] if out is None else out
    if isinstance(node, dict):
        role = node.get("role") or ""
        name = node.get("name") or ""
        if name:
            out.append({"role": role, "name": name})
        for ch in (node.get("children") or []):
            flatten_aria(ch, out)
    elif isinstance(node, list):
        for ch in node:
            flatten_aria(ch, out)
    return out


def resolve_reanchor(
    *,
    recorded_label: str,
    recorded_kind: str,
    live_nodes: list[dict],
    min_confidence: float = 0.62,
    ambiguity_margin: float = 0.12,
) -> ReAnchor | None:
    """Find the live node most likely to be the recorded control under a new name.

    Returns a ``ReAnchor`` only when ONE node is a confident, unambiguous match:
      * its role is plausible for the recorded kind (when the kind is known),
      * its name similarity to the recorded label is ≥ ``min_confidence``,
      * and the runner-up is at least ``ambiguity_margin`` behind (so two equally
        plausible controls → refuse, not a coin-flip re-bind).
    A node whose name is IDENTICAL to the recorded label is skipped — the ladder
    would already have found it, so it isn't the renamed target.

    Returns ``None`` (refuse) otherwise — the caller must then fall toward
    needs_review and never auto-heal.
    """
    rl = _norm(recorded_label)
    if not rl or not live_nodes:
        return None
    cand_roles = _ROLE_CANDIDATES.get(_norm(recorded_kind), ())

    scored: list[tuple[float, dict]] = []
    for n in live_nodes:
        nm = n.get("name") or ""
        if not nm or _norm(nm) == rl:  # identical name wouldn't have broken
            continue
        role = (n.get("role") or "").lower()
        if cand_roles and role not in cand_roles:
            continue
        sim = _similarity(recorded_label, nm)
        if sim > 0:
            scored.append((sim, n))

    if not scored:
        return None
    scored.sort(key=lambda t: t[0], reverse=True)
    best_sim, best = scored[0]
    if best_sim < min_confidence:
        return None
    # Ambiguity guard: a close runner-up means we can't tell them apart → refuse.
    if len(scored) > 1 and (best_sim - scored[1][0]) < ambiguity_margin:
        return None

    return ReAnchor(
        name=best["name"],
        role=(best.get("role") or ""),
        confidence=round(best_sim, 2),
        rationale=(
            f"The live page no longer has a control named '{recorded_label}', but "
            f"'{best['name']}' ({best.get('role') or 'control'}) is a close, "
            f"unambiguous match (similarity {round(best_sim, 2)}) — a likely "
            f"rename, not a removal."
        ),
    )


__all__ = ["ReAnchor", "flatten_aria", "resolve_reanchor"]
