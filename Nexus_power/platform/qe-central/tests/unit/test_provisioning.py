"""QE-Central Phase-7 — tenant provisioning + lifecycle tests.

Pure-logic where possible (no DB, no network):
  * the tenant's first-admin token carries the RIGHT claims (role admin, Verdict
    audience, the tenant scope) and NEVER the platform-admin marker;
  * a PLATFORM SUPER-ADMIN token is required to manage tenants — a plain tenant
    admin gets 403 (it can NOT provision/suspend/offboard other tenants);
  * the fail-closed lifecycle gate: an ACTIVE / un-provisioned tenant passes, a
    SUSPENDED tenant is REFUSED (this is "suspend blocks the crawl/cycle" at the
    gate the driver + crawl dispatch call);
  * the lifecycle transition matrix + idempotency;
  * provisioning input validation + the deployed-env fail-closed safety gate.

DB-backed behavior (full provision → idempotent re-provision → suspend-blocks →
resume → offboard-retains-evidence) is exercised against a DISPOSABLE Postgres
(``QEC_TEST_DATABASE_URL``); the qec-owned tables are materialised in-test via
``QecBase.metadata.create_all`` and the ``tenants`` registry table is created
minimally, so no live nexus substrate is needed.  Unset ⇒ skips (never a false
green).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.fleet import lifecycle
from app.fleet.lifecycle import (
    STATUS_ACTIVE,
    STATUS_OFFBOARDING,
    STATUS_SUSPENDED,
    TenantLifecycleError,
    TenantNotOperational,
    TenantProvisioningRecord,
    assert_tenant_operational,
    resolve_transition,
)
from app.fleet.rbac import (
    PLATFORM_ADMIN_CLAIM,
    is_platform_admin,
    mint_platform_admin_jwt,
    mint_tenant_principal_jwt,
    require_platform_admin,
)

SECRET = settings.nexus_jwt_secret
AUD = settings.qec_jwt_audience


def _decode(token: str) -> dict:
    return pyjwt.decode(token, SECRET, algorithms=["HS256"], audience=AUD)


# ─── token claims (pure) ────────────────────────────────────────────────────

class TestPrincipalTokens:
    def test_tenant_admin_token_claims(self):
        token, exp = mint_tenant_principal_jwt("tenant-42", "admin@client.test")
        claims = _decode(token)
        assert claims["role"] == "admin"
        assert claims["tenant_id"] == "tenant-42"
        assert claims["email"] == "admin@client.test"
        assert claims["aud"] == AUD
        # A tenant admin is NOT a platform super-admin — the marker is ABSENT.
        assert PLATFORM_ADMIN_CLAIM not in claims
        assert is_platform_admin(claims) is False
        assert exp.timestamp() > 0

    def test_platform_admin_token_claims(self):
        token, _ = mint_platform_admin_jwt("ops@vkpower.test")
        claims = _decode(token)
        assert claims["role"] == "admin"
        assert claims[PLATFORM_ADMIN_CLAIM] is True
        assert claims["aud"] == AUD
        # Carries the reserved operator tenant scope (mandatory-tenant gate).
        assert claims["tenant_id"] == settings.qec_platform_tenant_id
        assert is_platform_admin(claims) is True

    def test_missing_tenant_is_rejected(self):
        with pytest.raises(ValueError):
            mint_tenant_principal_jwt("", "x@y.test")

    def test_is_platform_admin_rejects_marker_without_admin_role(self):
        # A viewer with a stray platform_admin marker is NOT a platform admin.
        assert is_platform_admin({"role": "viewer", PLATFORM_ADMIN_CLAIM: True}) is False
        # A string "false" marker must not read truthy.
        assert is_platform_admin({"role": "admin", PLATFORM_ADMIN_CLAIM: "false"}) is False


# ─── super-admin RBAC (a tenant admin can NOT manage other tenants) ─────────

@pytest.fixture()
def rbac_client() -> TestClient:
    app = FastAPI()

    @app.post("/api/v1/qec/tenants")
    async def _provision(user: dict = Depends(require_platform_admin)):
        return {"ok": True, "platform_admin": user.get(PLATFORM_ADMIN_CLAIM)}

    return TestClient(app)


class TestSuperAdminScope:
    def test_no_token_is_401(self, rbac_client):
        assert rbac_client.post("/api/v1/qec/tenants").status_code == 401

    def test_tenant_admin_cannot_provision(self, rbac_client):
        # role=admin but NO platform_admin marker → 403 (the security property:
        # a tenant admin can NOT provision/suspend/offboard OTHER tenants).
        token, _ = mint_tenant_principal_jwt("tenant-a", "admin@a.test")
        r = rbac_client.post(
            "/api/v1/qec/tenants", headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    def test_viewer_cannot_provision(self, rbac_client):
        token, _ = mint_tenant_principal_jwt("tenant-a", "v@a.test", role="viewer")
        r = rbac_client.post(
            "/api/v1/qec/tenants", headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    def test_platform_admin_passes(self, rbac_client):
        token, _ = mint_platform_admin_jwt("ops@vkpower.test")
        r = rbac_client.post(
            "/api/v1/qec/tenants", headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["platform_admin"] is True


# ─── the fail-closed lifecycle gate (suspend blocks crawl/cycle) ────────────

class TestLifecycleGate:
    def test_active_record_passes(self):
        rec = TenantProvisioningRecord(tenant_id="t", status=STATUS_ACTIVE)
        assert_tenant_operational(rec)  # no raise
        assert lifecycle.is_operational(rec) is True

    def test_none_record_passes(self):
        # Backward-compat: an un-provisioned tenant is operational.
        assert_tenant_operational(None)  # no raise
        assert lifecycle.is_operational(None) is True

    def test_suspended_record_is_refused(self):
        rec = TenantProvisioningRecord(tenant_id="t", status=STATUS_SUSPENDED)
        with pytest.raises(TenantNotOperational) as exc:
            assert_tenant_operational(rec, operation="cycle")
        assert exc.value.status_code == 403
        assert exc.value.status == STATUS_SUSPENDED
        assert exc.value.as_http_detail()["reason"] == "tenant_not_operational"

    def test_offboarding_record_is_refused(self):
        rec = TenantProvisioningRecord(tenant_id="t", status=STATUS_OFFBOARDING)
        with pytest.raises(TenantNotOperational):
            assert_tenant_operational(rec)

    def test_gate_accepts_status_string_and_mapping(self):
        assert_tenant_operational(STATUS_ACTIVE)
        assert_tenant_operational({"status": STATUS_ACTIVE})
        with pytest.raises(TenantNotOperational):
            assert_tenant_operational(STATUS_SUSPENDED)
        with pytest.raises(TenantNotOperational):
            assert_tenant_operational({"status": STATUS_SUSPENDED})

    def test_record_from_namespace_row(self):
        row = SimpleNamespace(
            tenant_id="t", plan="starter", status=STATUS_SUSPENDED,
            admin_email="a@b.test", display_name="Acme", quota_overrides={},
            provisioned_at=None, suspended_at=None, offboarding_started_at=None,
            retention_until=None, tokens_revoked_at=None, purge_requested=False,
            actor="op", reason="",
        )
        rec = TenantProvisioningRecord.from_row(row)
        assert rec.status == STATUS_SUSPENDED
        assert rec.is_operational is False


# ─── the transition matrix (pure) ──────────────────────────────────────────

class TestTransitions:
    def test_active_to_suspended(self):
        target, changed = resolve_transition(STATUS_ACTIVE, lifecycle.ACTION_SUSPEND)
        assert (target, changed) == (STATUS_SUSPENDED, True)

    def test_suspend_is_idempotent(self):
        target, changed = resolve_transition(STATUS_SUSPENDED, lifecycle.ACTION_SUSPEND)
        assert (target, changed) == (STATUS_SUSPENDED, False)

    def test_resume_from_suspended(self):
        target, changed = resolve_transition(STATUS_SUSPENDED, lifecycle.ACTION_RESUME)
        assert (target, changed) == (STATUS_ACTIVE, True)

    def test_resume_active_is_noop(self):
        target, changed = resolve_transition(STATUS_ACTIVE, lifecycle.ACTION_RESUME)
        assert (target, changed) == (STATUS_ACTIVE, False)

    def test_no_record_provisions_implicit_active(self):
        # A tenant with no record (None) treated as active for the transition base.
        target, changed = resolve_transition(None, lifecycle.ACTION_SUSPEND)
        assert (target, changed) == (STATUS_SUSPENDED, True)

    def test_offboard_from_active(self):
        target, changed = resolve_transition(STATUS_ACTIVE, lifecycle.ACTION_OFFBOARD)
        assert (target, changed) == (STATUS_OFFBOARDING, True)

    def test_illegal_resume_from_offboarding(self):
        with pytest.raises(TenantLifecycleError) as exc:
            resolve_transition(STATUS_OFFBOARDING, lifecycle.ACTION_RESUME)
        assert exc.value.status_code == 409

    def test_unknown_action_is_422(self):
        with pytest.raises(TenantLifecycleError) as exc:
            resolve_transition(STATUS_ACTIVE, "teleport")
        assert exc.value.status_code == 422


# ─── provisioning input validation + deploy-safety (pure, no DB) ────────────

class TestProvisionValidation:
    def test_bad_email_is_422(self):
        from app.fleet.provisioning import ProvisioningError, provision_tenant

        with pytest.raises(ProvisioningError) as exc:
            asyncio.run(provision_tenant("Acme", "starter", "not-an-email"))
        assert exc.value.status_code == 422

    def test_empty_name_is_422(self):
        from app.fleet.provisioning import ProvisioningError, provision_tenant

        with pytest.raises(ProvisioningError) as exc:
            asyncio.run(provision_tenant("   ", "starter", "a@b.test"))
        assert exc.value.status_code == 422

    def test_deployed_env_with_dev_defaults_fails_closed(self, monkeypatch):
        # In a DEPLOYED env still wearing dev defaults (the test JWT secret is a
        # known dev default), provisioning refuses BEFORE any DB work (403).
        from app.fleet.provisioning import ProvisioningError, provision_tenant

        monkeypatch.setattr(settings, "nexus_env", "production")
        with pytest.raises(ProvisioningError) as exc:
            asyncio.run(provision_tenant("Acme", "starter", "a@b.test"))
        assert exc.value.status_code == 403

    def test_dev_env_is_not_fail_closed_here(self, monkeypatch):
        # Sanity: the safety gate is INERT in development/test (no raise from it).
        from app.fleet.provisioning import ensure_deploy_safe

        monkeypatch.setattr(settings, "nexus_env", "test")
        ensure_deploy_safe("provision_tenant")  # no raise


# ─── DB-backed lifecycle (skipif-gated on a disposable Postgres) ────────────

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")

needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — provisioning persistence needs a "
           "disposable Postgres (QecBase tables + a minimal tenants table are "
           "created in-test; no live nexus substrate required)",
)

_CREATE_TENANTS_SQL = text(
    "CREATE TABLE IF NOT EXISTS tenants ("
    " tenant_id varchar(64) PRIMARY KEY,"
    " name varchar(200) NOT NULL DEFAULT '',"
    " domain varchar(200) UNIQUE,"
    " plan varchar(50) NOT NULL DEFAULT 'starter',"
    " status varchar(20) NOT NULL DEFAULT 'active',"
    " created_at timestamptz NOT NULL DEFAULT now(),"
    " updated_at timestamptz NOT NULL DEFAULT now())"
)


@needs_db
def test_provisioning_lifecycle_db(monkeypatch):
    """provision → idempotent re-provision → suspend-BLOCKS → resume → offboard."""
    import app.db as db_mod
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # Redirect BOTH app session factories at the disposable test DB (the qec
    # control record + the substrate tenants row both land here).
    monkeypatch.setattr(db_mod, "_qec_session_factory", factory)
    monkeypatch.setattr(db_mod, "_substrate_session_factory", factory)
    try:
        asyncio.run(_run_lifecycle_db(engine, factory, QecBase))
    finally:
        asyncio.run(engine.dispose())


async def _run_lifecycle_db(engine, factory, QecBase):
    from app.db import tenant_scoped_qec_session
    from app.fleet.lifecycle import TenantNotOperational
    from app.fleet.provisioning import (
        assert_tenant_operational_db,
        get_tenant_provisioning,
        offboard_tenant,
        provision_tenant,
        resume_tenant,
        suspend_tenant,
    )

    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
        await conn.execute(_CREATE_TENANTS_SQL)

    tid = f"prov-{uuid.uuid4().hex[:12]}"

    # ── provision creates a scoped tenant + a right-claimed token ───────────
    handle = await provision_tenant("Acme Insurance", "starter", "admin@acme.test", tenant_id=tid)
    assert handle.created is True
    assert handle.tenant_id == tid
    assert handle.status == STATUS_ACTIVE
    claims = _decode(handle.admin_token)
    assert claims["role"] == "admin" and claims["tenant_id"] == tid
    assert PLATFORM_ADMIN_CLAIM not in claims

    # ── idempotent re-provision (same tenant_id) → created False ────────────
    again = await provision_tenant("Acme Insurance", "starter", "admin@acme.test", tenant_id=tid)
    assert again.created is False and again.tenant_id == tid

    # ── an ACTIVE tenant passes the DB gate ─────────────────────────────────
    async with tenant_scoped_qec_session(tid) as s:
        await assert_tenant_operational_db(s, tid)  # no raise

    # ── suspend BLOCKS the tenant's crawl/cycle (the gate now refuses) ──────
    res = await suspend_tenant(tid, actor="ops", reason="fraud review")
    assert res.status == STATUS_SUSPENDED and res.changed is True
    async with tenant_scoped_qec_session(tid) as s:
        with pytest.raises(TenantNotOperational):
            await assert_tenant_operational_db(s, tid, operation="cycle")

    # suspend is idempotent
    res2 = await suspend_tenant(tid, actor="ops")
    assert res2.status == STATUS_SUSPENDED and res2.changed is False

    # ── resume restores operation ───────────────────────────────────────────
    res3 = await resume_tenant(tid, actor="ops")
    assert res3.status == STATUS_ACTIVE and res3.changed is True
    async with tenant_scoped_qec_session(tid) as s:
        await assert_tenant_operational_db(s, tid)  # no raise again

    # ── offboard revokes tokens + schedules retention, RETAINS evidence ─────
    off = await offboard_tenant(tid, actor="ops", retention_days=45, purge=False)
    assert off.status == STATUS_OFFBOARDING
    assert off.evidence_retained is True
    assert off.purge_requested is False
    assert off.retention_until is not None
    assert off.tokens_revoked_at is not None
    # the offboarded tenant is non-operational
    async with tenant_scoped_qec_session(tid) as s:
        with pytest.raises(TenantNotOperational):
            await assert_tenant_operational_db(s, tid)

    # ── an idempotent re-offboard can ADD a purge request (evidence still kept) ─
    off2 = await offboard_tenant(tid, actor="ops", purge=True)
    assert off2.purge_requested is True and off2.evidence_retained is True

    rec = await get_tenant_provisioning(tid)
    assert rec is not None and rec.status == STATUS_OFFBOARDING and rec.purge_requested is True
