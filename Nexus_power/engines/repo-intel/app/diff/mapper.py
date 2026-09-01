"""Changed-files → changed-atoms mapper (CODE P4 — the incremental-cycle core).

Given the files that changed between two deployed SHAs and the App Model's atoms
(each tagged with the ``provenance_path`` it was extracted from), compute WHICH
atoms — and therefore which routes/endpoints — a change actually touched, so the
control plane can run ONLY the affected tests instead of the full suite every
push.  This is the scale-efficiency lever for 1000+ apps/day.

NEVER-GREEN-WASH (the whole point of a verifier):
  * A change we cannot map to a known atom is NOT silently "no change".  A changed
    file that yields no atom is reported in ``unmapped_files`` and sets
    ``fail_safe_to_full=True`` — the caller MUST run the full suite (a config /
    template / dependency edit can change behaviour with no atom of its own).
  * An EMPTY diff (no files changed) is the only case that legitimately narrows to
    zero — and even then the caller keeps its own SHA-vs-baseline union.

Pure + dependency-free (operates over dicts / plain objects), so the mapping is
unit-testable without a repo, a clone, or the DB.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: Atom kinds that correspond to a runnable surface (a route / API the suite hits).
_SURFACE_KINDS = frozenset({"route", "api_endpoint"})


def _norm_path(path: Any) -> str:
    """Normalize a repo-relative path for robust comparison (``./a\\b`` → ``a/b``)."""
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def _atom_get(atom: Any, key: str) -> Any:
    """Read a field from an atom that may be an ORM row, a dataclass, or a dict."""
    if isinstance(atom, Mapping):
        return atom.get(key)
    return getattr(atom, key, None)


def _atom_route(atom: Any) -> str:
    """The route/endpoint path an atom represents, if any (for affected-surface list)."""
    value = _atom_get(atom, "value")
    if isinstance(value, Mapping):
        return str(value.get("path_pattern") or value.get("path") or "").strip()
    return ""


def map_changed_atoms(
    changed_files: Sequence[str],
    atoms: Iterable[Any],
) -> dict:
    """Map a set of changed files onto the atoms they touched.

    Returns a fail-safe honest report::

        {
          "changed_files":   [<normalized repo paths>],
          "changed_atoms":   [<atom kind/value/provenance for each touched atom>],
          "affected_routes": [<route/api path patterns among the changed atoms>],
          "unmapped_files":  [<changed files that matched NO atom>],
          "fail_safe_to_full": <bool>,   # True ⇒ caller must run the FULL suite
          "stack_supported": True,
        }
    """
    changed = {_norm_path(f) for f in (changed_files or ()) if _norm_path(f)}
    matched_files: set[str] = set()
    changed_atoms: list[dict] = []
    affected_routes: set[str] = set()

    for atom in atoms or ():
        prov = _norm_path(_atom_get(atom, "provenance_path"))
        if prov and prov in changed:
            matched_files.add(prov)
            kind = str(_atom_get(atom, "kind") or "")
            changed_atoms.append({
                "atom_id": _atom_get(atom, "atom_id"),
                "kind": kind,
                "value": _atom_get(atom, "value"),
                "provenance_path": prov,
            })
            if kind in _SURFACE_KINDS:
                route = _atom_route(atom)
                if route:
                    affected_routes.add(route)

    unmapped = sorted(changed - matched_files)
    return {
        "changed_files": sorted(changed),
        "changed_atoms": changed_atoms,
        "affected_routes": sorted(affected_routes),
        "unmapped_files": unmapped,
        # Any changed file we could not tie to an atom ⇒ run the full suite. An
        # empty diff (nothing changed) is NOT fail-safe — there is genuinely nothing.
        "fail_safe_to_full": bool(unmapped),
        "stack_supported": True,
    }


__all__ = ["map_changed_atoms"]
