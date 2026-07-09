"""Deterministic seed-manifest builder.

Turns a universe's atoms into a ``seed-v1`` manifest: routes ranked by a
DETERMINISTIC criticality score (auth / payment / transaction keywords +
validator density + nav fan-in), an auth recipe (login route + field NAMES,
never credentials), and nav edges. No LLM, no randomness — the same atoms
always produce the same manifest (so it is diffable and CI-gradable).
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Tuple

from app.extract.registry import Atom, normalize_route_pattern
from app.manifest.schema import validate_seed_manifest

# Criticality keyword bands (path or endpoint substring → weight + label).
_CRITICALITY_SIGNALS: List[Tuple[re.Pattern, float, str]] = [
    (re.compile(r"pay|payment|checkout|billing|charge|invoice", re.I), 0.40, "payment-path"),
    (re.compile(r"transfer|withdraw|deposit|remit|disburse", re.I), 0.40, "money-movement"),
    (re.compile(r"login|signin|auth|logout|password|mfa|otp", re.I), 0.30, "auth-path"),
    (re.compile(r"order|purchase|cart|subscribe|bind|underwrit|claim|policy", re.I), 0.30, "transaction-path"),
    (re.compile(r"admin|settings|account|profile|delete|remove", re.I), 0.20, "account-admin"),
]

_LOGIN_ROUTE = re.compile(r"login|signin|auth", re.I)
_LOGIN_FIELD = re.compile(r"user|email|login|pass", re.I)


def _route_atoms(atoms: Iterable[Atom]) -> List[Atom]:
    return [a for a in atoms if a.kind in ("route", "api_endpoint")]


def _path_of(a: Atom) -> str:
    v = a.value
    return normalize_route_pattern(v.get("path_pattern") or v.get("path") or "")


def _validator_density(atoms: Iterable[Atom]) -> Dict[str, int]:
    """Count validator_rule atoms whose field name hints at a path segment —
    a proxy for 'this route carries constrained (important) input'."""
    density: Dict[str, int] = {}
    for a in atoms:
        if a.kind == "validator_rule":
            field = str(a.value.get("field", "")).lower()
            if field:
                density[field] = density.get(field, 0) + 1
    return density


def _score_route(path: str, nav_fan_in: int, validator_hits: int) -> Tuple[float, List[str]]:
    score = 0.0
    evidence: List[str] = []
    for pat, weight, label in _CRITICALITY_SIGNALS:
        if pat.search(path):
            score += weight
            evidence.append(label)
    if validator_hits:
        bump = min(0.15, 0.03 * validator_hits)
        score += bump
        evidence.append(f"validator-density:{validator_hits}")
    if nav_fan_in > 1:
        bump = min(0.15, 0.05 * nav_fan_in)
        score += bump
        evidence.append(f"nav-fan-in:{nav_fan_in}")
    return min(1.0, round(score, 4)), evidence


def build_seed_manifest(atoms: Iterable[Atom]) -> Dict:
    """Build (and validate) a ``seed-v1`` manifest from universe atoms."""
    atoms = list(atoms)
    routes = _route_atoms(atoms)
    validators = _validator_density(atoms)

    # nav fan-in: how many nav_edge atoms point at each normalized path.
    fan_in: Dict[str, int] = {}
    nav_edges: List[Dict] = []
    for a in atoms:
        if a.kind == "nav_edge":
            to = normalize_route_pattern(a.value.get("to", ""))
            fan_in[to] = fan_in.get(to, 0) + 1
            nav_edges.append({"from": a.value.get("from", ""), "to": a.value.get("to", "")})

    # Deduplicate routes by normalized pattern, keeping the richest evidence.
    ranked: Dict[str, Dict] = {}
    for a in routes:
        path = _path_of(a)
        if not path:
            continue
        vh = sum(1 for f in validators if f and f in path.lower())
        score, evidence = _score_route(path, fan_in.get(path, 0), vh)
        if path not in ranked or score > ranked[path]["criticality_score"]:
            ranked[path] = {
                "path_pattern": path,
                "criticality_score": score,
                "criticality_evidence": evidence,
                "expected_forms": _expected_forms(a),
            }

    ranked_routes = sorted(
        ranked.values(),
        key=lambda r: (-r["criticality_score"], r["path_pattern"]),
    )

    manifest = {
        "version": "seed-v1",
        "ranked_routes": ranked_routes,
        "auth_recipe": _auth_recipe(atoms),
        "nav_edges": nav_edges,
    }
    validate_seed_manifest(manifest)  # never emit an invalid / credential-bearing manifest
    return manifest


def _expected_forms(route_atom: Atom) -> List[Dict]:
    forms = route_atom.value.get("forms")
    return forms if isinstance(forms, list) else []


def _auth_recipe(atoms: Iterable[Atom]) -> Dict:
    """Derive the login route + field NAMES (never values) from auth_step or
    a login-looking route. Provenance-tagged; credential-free by construction."""
    login_route = ""
    provenance = ""
    field_names: List[str] = []
    for a in atoms:
        if a.kind == "auth_step":
            login_route = a.value.get("login_route", login_route)
            field_names = list(a.value.get("field_names", field_names))
            provenance = f"{a.provenance_path}:{a.provenance_line}"
            break
    if not login_route:
        for a in _route_atoms(atoms):
            path = _path_of(a)
            if _LOGIN_ROUTE.search(path):
                login_route = path
                provenance = f"{a.provenance_path}:{a.provenance_line}"
                break
    return {
        "login_route": login_route,
        "field_names": [f for f in field_names if _LOGIN_FIELD.search(f)] or field_names,
        "provenance": provenance,
    }
