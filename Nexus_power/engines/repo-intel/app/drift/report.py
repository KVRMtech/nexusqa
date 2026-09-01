"""Live-vs-repo drift report.

Compares the route/endpoint atoms of an App Model universe (code side) with
the ``page_visits`` a crawl actually reached (live side) and emits the exact
four drift item kinds the design names. Pattern-normalized so ``/orders/123``
and ``/orders/{id}`` compare equal.

This is a PURE function over already-fetched inputs — the DB read of
``page_visits`` (read-only nexus DSN) happens in the router and is passed in,
so the drift logic is unit-testable without a database.
"""
from __future__ import annotations

from typing import Dict, Iterable, List

from app.extract.registry import Atom, normalize_reached_path, normalize_route_pattern

# Drift item kinds (design §3.3).
ROUTE_IN_CODE_UNREACHABLE = "route_in_code_unreachable"
ROUTE_LIVE_NOT_IN_CODE = "route_live_not_in_code"
FORM_FIELD_MISMATCH = "form_field_mismatch"
VALIDATOR_UNTESTED = "validator_untested"


def build_drift_report(
    code_atoms: Iterable[Atom],
    live_paths: Iterable[str],
    *,
    live_form_fields: Dict[str, List[str]] | None = None,
    code_form_fields: Dict[str, List[str]] | None = None,
) -> Dict:
    """Return ``{summary, items}``.

    ``code_atoms`` — route/api_endpoint/validator_rule atoms from the universe.
    ``live_paths`` — normalized-or-raw url_path values from page_visits.
    ``*_form_fields`` — optional {path: [field,...]} maps for field-mismatch.
    """
    code_atoms = list(code_atoms)
    live_norm = {normalize_reached_path(p) for p in live_paths if p}
    code_routes = {
        normalize_route_pattern(a.value.get("path_pattern") or a.value.get("path") or "")
        for a in code_atoms if a.kind in ("route", "api_endpoint")
    }
    code_routes.discard("")

    items: List[Dict] = []

    # 1) In code but never reached by the crawl.
    for r in sorted(code_routes - live_norm):
        items.append({"kind": ROUTE_IN_CODE_UNREACHABLE, "code_side": r, "live_side": None})

    # 2) Reached live but absent from the code model (repo-intel recall gap or
    #    a route built dynamically / outside the analyzed repo).
    for r in sorted(live_norm - code_routes):
        items.append({"kind": ROUTE_LIVE_NOT_IN_CODE, "code_side": None, "live_side": r})

    # 3) Form-field mismatches on shared paths.
    lf = live_form_fields or {}
    cf = code_form_fields or {}
    for path in sorted(set(lf) & set(cf)):
        live_set = {f.lower() for f in lf[path]}
        code_set = {f.lower() for f in cf[path]}
        if live_set != code_set:
            items.append({
                "kind": FORM_FIELD_MISMATCH,
                "code_side": {"path": path, "fields": sorted(code_set)},
                "live_side": {"path": path, "fields": sorted(live_set)},
            })

    # 4) Validators declared in code whose owning route was never reached
    #    (so the constraint is currently UNTESTED by the live suite).
    reached_fields = set()
    for fields in lf.values():
        reached_fields |= {f.lower() for f in fields}
    for a in code_atoms:
        if a.kind == "validator_rule":
            field = str(a.value.get("field", "")).lower()
            if field and field not in reached_fields:
                items.append({
                    "kind": VALIDATOR_UNTESTED,
                    "code_side": {"field": field, "rule": a.value.get("rule"),
                                  "at": f"{a.provenance_path}:{a.provenance_line}"},
                    "live_side": None,
                })

    summary = {
        "code_routes": len(code_routes),
        "live_routes": len(live_norm),
        "reachable": len(code_routes & live_norm),
        "counts": _counts(items),
    }
    return {"summary": summary, "items": items}


def _counts(items: List[Dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items:
        out[it["kind"]] = out.get(it["kind"], 0) + 1
    return out
