"""The ``seed-v1`` JSON schema + a dependency-light validator.

The seed manifest is the ONLY artifact repo-intel hands the crawler. It is
advisory (the crawler runs identically without it) and it carries NO
credentials — only route rankings and field NAMES. The schema is enforced so
a credential-shaped value can never slip into the manifest.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

SEED_V1_SCHEMA: Dict = {
    "type": "object",
    "required": ["version", "ranked_routes", "auth_recipe", "nav_edges"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": "seed-v1"},
        "ranked_routes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path_pattern", "criticality_score", "criticality_evidence", "expected_forms"],
                "additionalProperties": False,
                "properties": {
                    "path_pattern": {"type": "string"},
                    "criticality_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "criticality_evidence": {"type": "array", "items": {"type": "string"}},
                    "expected_forms": {"type": "array"},
                },
            },
        },
        "auth_recipe": {
            "type": "object",
            "required": ["login_route", "field_names", "provenance"],
            "additionalProperties": False,
            "properties": {
                "login_route": {"type": "string"},
                # Field NAMES only — never a value.
                "field_names": {"type": "array", "items": {"type": "string"}},
                "provenance": {"type": "string"},
            },
        },
        "nav_edges": {"type": "array"},
    },
}

# Keys whose presence in the manifest would indicate a leaked credential.
_CREDENTIAL_KEYS = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"client[_-]?secret|auth[_-]?token|private[_-]?key|credential)\b"
)


class SeedValidationError(ValueError):
    """Raised when a seed manifest violates the schema or the no-credential rule."""


def validate_seed_manifest(manifest: Dict) -> None:
    """Validate against ``seed-v1``. Uses ``jsonschema`` when importable,
    else a self-contained structural check. ALWAYS enforces the
    no-credential-key rule regardless of the jsonschema availability."""
    _no_credentials(manifest)
    try:
        import jsonschema  # type: ignore
        jsonschema.validate(manifest, SEED_V1_SCHEMA)
        return
    except ImportError:
        _structural_check(manifest)


def _no_credentials(manifest: Dict) -> None:
    """Refuse any credential-shaped KEY or an auth_recipe carrying values."""
    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if _CREDENTIAL_KEYS.search(str(k)):
                    raise SeedValidationError(
                        f"credential-shaped key '{k}' at {path} — the seed "
                        "manifest must never carry secrets"
                    )
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")
    walk(manifest, "$")
    # auth_recipe must expose field NAMES only, never a values map.
    recipe = manifest.get("auth_recipe", {})
    if isinstance(recipe, dict):
        for forbidden in ("values", "credentials", "secrets"):
            if forbidden in recipe:
                raise SeedValidationError(
                    f"auth_recipe.{forbidden} is forbidden — names only"
                )


def _structural_check(manifest: Dict) -> None:
    if manifest.get("version") != "seed-v1":
        raise SeedValidationError("version must be 'seed-v1'")
    for req in ("ranked_routes", "auth_recipe", "nav_edges"):
        if req not in manifest:
            raise SeedValidationError(f"missing required key '{req}'")
    if not isinstance(manifest["ranked_routes"], list):
        raise SeedValidationError("ranked_routes must be a list")
    for r in manifest["ranked_routes"]:
        for req in ("path_pattern", "criticality_score", "criticality_evidence", "expected_forms"):
            if req not in r:
                raise SeedValidationError(f"ranked_route missing '{req}'")
        score = r["criticality_score"]
        if not (isinstance(score, (int, float)) and 0.0 <= score <= 1.0):
            raise SeedValidationError("criticality_score out of [0,1]")
    recipe = manifest["auth_recipe"]
    for req in ("login_route", "field_names", "provenance"):
        if req not in recipe:
            raise SeedValidationError(f"auth_recipe missing '{req}'")
