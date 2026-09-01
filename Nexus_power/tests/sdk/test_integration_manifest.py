"""Unit tests for the integration manifest schema and loader.

Covers the production contract: manifests are strictly validated, every
declared capability must have a matching section, every section must
correspond to a declared capability, IDs are constrained, and unknown
keys are forbidden.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_sdk.integrations import (
    AuthMethod,
    Capability,
    IntegrationManifest,
    ManifestError,
    json_schema,
    load_manifest,
)


# ── Helper ─────────────────────────────────────────────────────


def _valid_manifest() -> dict:
    return {
        "id": "slack",
        "version": "1.0.0",
        "display_name": "Slack",
        "vendor": "nexus-core",
        "tier": "standard",
        "capabilities": ["source", "surface"],
        "auth": {
            "method": "oauth2",
            "scopes": [
                "channels:history",
                "chat:write",
                "users:read",
            ],
            "per_tenant": True,
        },
        "source": {
            "events": [
                {
                    "id": "message.channels",
                    "type": "message.channels",
                    "handler": "ingest_message",
                },
                {
                    "id": "file_shared",
                    "type": "file_shared",
                    "handler": "ingest_file_attachment",
                },
            ],
            "rate_limit": {"per_second": 50},
        },
        "surface": {
            "inbound": [
                {
                    "id": "app_mention",
                    "type": "app_mention",
                    "handler": "handle_question",
                }
            ],
            "outbound": [
                {
                    "id": "send_message",
                    "required_params": ["channel", "text"],
                    "optional_params": ["blocks"],
                    "retries": 3,
                },
                {
                    "id": "send_dm",
                    "required_params": ["user_id", "text"],
                    "retries": 3,
                },
            ],
        },
        "routing": {"tenant_resolver": "by_workspace_id"},
        "health": {
            "endpoint": "https://localhost/health",
            "interval_seconds": 60,
            "timeout_seconds": 5,
        },
    }


# ── Happy path ─────────────────────────────────────────────────


def test_valid_manifest_parses() -> None:
    manifest = IntegrationManifest.model_validate(_valid_manifest())
    assert manifest.id == "slack"
    assert Capability.SOURCE in manifest.capabilities
    assert Capability.SURFACE in manifest.capabilities
    assert manifest.auth.method == AuthMethod.OAUTH2


def test_load_manifest_from_json_file(tmp_path: Path) -> None:
    path = tmp_path / "plugin.json"
    path.write_text(json.dumps(_valid_manifest()), encoding="utf-8")
    manifest = load_manifest(path)
    assert manifest.id == "slack"


# ── ID / version validation ────────────────────────────────────


@pytest.mark.parametrize(
    "bad_id",
    ["Slack", "1slack", "slack/foo", "slack!", "", "x" * 200],
)
def test_invalid_id_rejected(bad_id: str) -> None:
    data = _valid_manifest()
    data["id"] = bad_id
    with pytest.raises(Exception):
        IntegrationManifest.model_validate(data)


@pytest.mark.parametrize(
    "bad_version", ["1.0", "v1.0.0", "1.0.0.1", "1.x.0"]
)
def test_invalid_version_rejected(bad_version: str) -> None:
    data = _valid_manifest()
    data["version"] = bad_version
    with pytest.raises(Exception):
        IntegrationManifest.model_validate(data)


# ── Capability section alignment ───────────────────────────────


def test_section_without_capability_rejected() -> None:
    data = _valid_manifest()
    data["capabilities"] = ["source"]  # remove surface from declared
    # surface section still present — must be rejected
    with pytest.raises(Exception) as exc:
        IntegrationManifest.model_validate(data)
    assert "surface" in str(exc.value)


def test_capability_without_section_rejected() -> None:
    data = _valid_manifest()
    data["capabilities"] = ["source", "surface", "sink"]
    # sink declared but no sink section
    with pytest.raises(Exception) as exc:
        IntegrationManifest.model_validate(data)
    assert "sink" in str(exc.value)


def test_empty_capabilities_rejected() -> None:
    data = _valid_manifest()
    data["capabilities"] = []
    with pytest.raises(Exception):
        IntegrationManifest.model_validate(data)


def test_duplicate_capabilities_rejected() -> None:
    data = _valid_manifest()
    data["capabilities"] = ["source", "source", "surface"]
    with pytest.raises(Exception):
        IntegrationManifest.model_validate(data)


# ── Strict mode ────────────────────────────────────────────────


def test_unknown_top_level_field_rejected() -> None:
    data = _valid_manifest()
    data["secret_kill_switch"] = True
    with pytest.raises(Exception):
        IntegrationManifest.model_validate(data)


def test_unknown_auth_field_rejected() -> None:
    data = _valid_manifest()
    data["auth"]["secret_field"] = "x"
    with pytest.raises(Exception):
        IntegrationManifest.model_validate(data)


# ── Surface / source duplicate IDs ─────────────────────────────


def test_duplicate_event_ids_rejected() -> None:
    data = _valid_manifest()
    data["source"]["events"].append(
        {"id": "message.channels", "type": "x", "handler": "y"}
    )
    with pytest.raises(Exception):
        IntegrationManifest.model_validate(data)


def test_duplicate_action_ids_rejected() -> None:
    data = _valid_manifest()
    data["surface"]["outbound"].append(
        {"id": "send_message", "required_params": ["a"]}
    )
    with pytest.raises(Exception):
        IntegrationManifest.model_validate(data)


# ── Surface requires at least one direction ────────────────────


def test_empty_surface_rejected() -> None:
    data = _valid_manifest()
    data["surface"] = {"inbound": [], "outbound": []}
    with pytest.raises(Exception):
        IntegrationManifest.model_validate(data)


# ── Rate limit must have at least one bound ────────────────────


def test_empty_rate_limit_rejected() -> None:
    data = _valid_manifest()
    data["source"]["rate_limit"] = {}
    with pytest.raises(Exception):
        IntegrationManifest.model_validate(data)


# ── Loader error cases ─────────────────────────────────────────


def test_load_manifest_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestError) as exc:
        load_manifest(tmp_path / "does-not-exist.yaml")
    assert "not found" in str(exc.value)


def test_load_manifest_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "plugin.txt"
    path.write_text("garbage", encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_load_manifest_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "plugin.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_load_manifest_root_not_mapping(tmp_path: Path) -> None:
    path = tmp_path / "plugin.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ManifestError) as exc:
        load_manifest(path)
    assert "mapping" in str(exc.value)


# ── JSON Schema export ─────────────────────────────────────────


def test_json_schema_includes_top_level_keys() -> None:
    schema = json_schema()
    assert schema["type"] == "object"
    required = set(schema.get("required", []))
    for key in {"id", "version", "display_name", "vendor", "auth", "routing", "health"}:
        assert key in required, f"missing required key {key!r} in schema"


def test_json_schema_is_serialisable() -> None:
    json.dumps(json_schema())  # must not raise
