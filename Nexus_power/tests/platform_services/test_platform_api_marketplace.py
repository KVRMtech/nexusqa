"""Marketplace router DTO + helper tests.

These tests exercise the validators, mappers, and SQL composition the
router relies on without standing up a live FastAPI app. The router's
write paths are covered indirectly by the DTO validation here plus the
manifest-validation test below.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ── DTO validators ────────────────────────────────────────────


def test_create_listing_validates_tier():
    from app.routers.marketplace import CreateListingRequest

    body = CreateListingRequest(
        plugin_id="acme_hr",
        display_name="Acme HR",
        vendor="acme",
        tier="enterprise",
    )
    assert body.tier == "enterprise"

    with pytest.raises(Exception):
        CreateListingRequest(
            plugin_id="x",
            display_name="X",
            vendor="acme",
            tier="bogus",
        )


def test_review_version_validates_decision():
    from app.routers.marketplace import ReviewVersionRequest

    assert ReviewVersionRequest(decision="approve").decision == "approve"
    assert ReviewVersionRequest(decision="reject").decision == "reject"
    with pytest.raises(Exception):
        ReviewVersionRequest(decision="maybe")


def test_decide_install_validates_decision():
    from app.routers.marketplace import DecideInstallRequest

    for ok in ("approve", "reject", "revoke"):
        assert DecideInstallRequest(decision=ok).decision == ok
    with pytest.raises(Exception):
        DecideInstallRequest(decision="purge")


def test_tenant_tier_validates_tier():
    from app.routers.marketplace import UpdateTenantTierRequest

    assert UpdateTenantTierRequest(tier=None).tier is None
    assert UpdateTenantTierRequest(tier="sovereign").tier == "sovereign"
    with pytest.raises(Exception):
        UpdateTenantTierRequest(tier="gold")


def test_relationship_validates_kind_and_scope():
    from app.routers.marketplace import UpsertRelationshipRequest

    body = UpsertRelationshipRequest(
        related_tenant_id="child-1",
        relationship_kind="child",
        share_scope="cards",
    )
    assert body.relationship_kind == "child"
    assert body.share_scope == "cards"

    with pytest.raises(Exception):
        UpsertRelationshipRequest(
            related_tenant_id="x",
            relationship_kind="sibling",
        )
    with pytest.raises(Exception):
        UpsertRelationshipRequest(
            related_tenant_id="x",
            relationship_kind="peer",
            share_scope="superuser",
        )


def test_create_export_dedupes_scopes():
    from app.routers.marketplace import CreateExportRequest

    body = CreateExportRequest(
        period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        scopes=["echoes", "echoes", "atlas", "", "plugin_events"],
    )
    assert body.scopes == ["echoes", "atlas", "plugin_events"]


# ── Manifest validation hook ──────────────────────────────────


def test_submit_version_validates_manifest_payload():
    """``_validate_manifest_text`` round-trips a YAML manifest through
    the Phase 0 schema and returns a parsed ``IntegrationManifest``."""
    from app.routers.marketplace import _validate_manifest_text

    yaml_text = """\
id: testplugin
version: 0.1.0
display_name: Test
vendor: nexus-core
tier: community
capabilities:
  - sink
auth:
  method: none
  per_tenant: false
sink:
  actions:
    - id: do_thing
      description: Stub.
      required_params:
        - x
routing:
  tenant_resolver: by_tenant
health:
  endpoint: file:///healthz
"""
    manifest = _validate_manifest_text(yaml_text)
    assert manifest.id == "testplugin"
    assert manifest.sink is not None
    assert [a.id for a in manifest.sink.actions] == ["do_thing"]


def test_submit_version_rejects_bad_manifest():
    from app.routers.marketplace import _validate_manifest_text
    from nexus_sdk.integrations import ManifestError

    with pytest.raises(ManifestError):
        _validate_manifest_text("not: a valid: manifest")


# ── Hash determinism ─────────────────────────────────────────


def test_hash_manifest_stable_across_calls():
    from app.routers.marketplace import _hash_manifest

    payload = "id: x\nversion: 1.0.0\n"
    assert _hash_manifest(payload) == _hash_manifest(payload)
    assert len(_hash_manifest(payload)) == 64


# ── Role gates ─────────────────────────────────────────────────


def test_require_operator_rejects_viewer():
    from fastapi import HTTPException

    from app.routers.marketplace import _require_operator

    with pytest.raises(HTTPException) as ex:
        _require_operator({"role": "viewer"})
    assert ex.value.status_code == 403


def test_require_priv_accepts_manager_rejects_viewer():
    from fastapi import HTTPException

    from app.routers.marketplace import _require_priv

    _require_priv({"role": "manager"})  # no raise
    with pytest.raises(HTTPException):
        _require_priv({"role": "viewer"})


# ── Mapper coverage ──────────────────────────────────────────


def test_listing_to_out_maps_row():
    from app.routers.marketplace import _listing_to_out

    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    row = {
        "listing_id": "L-1",
        "plugin_id": "acme_hr",
        "display_name": "Acme HR",
        "vendor": "acme",
        "tier": "enterprise",
        "description": None,
        "tags": ["hr", "compliance"],
        "documentation_url": None,
        "repository_url": None,
        "support_contact": None,
        "review_state": "approved",
        "is_core": False,
        "created_at": now,
        "updated_at": now,
    }
    out = _listing_to_out(row)
    assert out.listing_id == "L-1"
    assert out.tags == ["hr", "compliance"]
    assert out.review_state == "approved"


def test_export_to_out_handles_optional_timestamps():
    from app.routers.marketplace import _export_to_out

    row = {
        "export_id": "E-1",
        "requested_by": "u-1",
        "period_start": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "period_end": datetime(2026, 2, 1, tzinfo=timezone.utc),
        "scopes": ["echoes"],
        "manifest_sha256": "0" * 64,
        "status": "succeeded",
        "created_at": datetime(2026, 2, 2, tzinfo=timezone.utc),
        "completed_at": datetime(2026, 2, 3, tzinfo=timezone.utc),
        "storage_uri": "file:///tmp/bundle",
    }
    out = _export_to_out(row)
    assert out.completed_at is not None
    assert out.storage_uri == "file:///tmp/bundle"

    # absent completed_at falls back to None
    row2 = dict(row)
    row2["completed_at"] = None
    out2 = _export_to_out(row2)
    assert out2.completed_at is None
