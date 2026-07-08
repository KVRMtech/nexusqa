"""QE-Central Phase-0 unit tests: config parsing, fail-closed JWT auth,
service-token claims, and the frame-asset path guard mirror.

No database or network is touched — these pin the security-critical
mechanics (bad-signature/expired/missing token → 401; viewer → 403 on
privileged routes; ?token= rejected on state-changing methods; service
JWT claim set) exactly as the design requires.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import jwt_auth_middleware, require_auth, require_role
from app.config import Settings, settings
from app.db import safe_frame_asset_path
from app.service_token import (
    SERVICE_EMAIL,
    SERVICE_ROLE,
    SERVICE_SUBJECT,
    mint_service_jwt,
)

SECRET = settings.nexus_jwt_secret


# ─── helpers ───────────────────────────────────────────────────────────

def make_token(
    *,
    sub: str = "user-1",
    tenant_id: str | None = "tenant-a",
    email: str = "user@example.test",
    role: str = "viewer",
    secret: str = SECRET,
    exp_delta: timedelta = timedelta(minutes=10),
) -> str:
    claims: dict = {
        "sub": sub,
        "email": email,
        "role": role,
        "exp": datetime.now(timezone.utc) + exp_delta,
    }
    if tenant_id is not None:
        claims["tenant_id"] = tenant_id
    return pyjwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture()
def client() -> TestClient:
    """Minimal app wearing the real middleware + dependencies."""
    app = FastAPI()
    app.middleware("http")(jwt_auth_middleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/qec/whoami")
    async def whoami(user: dict = Depends(require_auth)):
        return user

    @app.post("/api/v1/qec/privileged")
    async def privileged(user: dict = Depends(require_role("admin", "manager"))):
        return {"ok": True, "role": user["role"]}

    return TestClient(app)


# ─── config parsing ────────────────────────────────────────────────────

class TestConfig:
    def test_env_overrides_are_parsed(self, monkeypatch):
        monkeypatch.setenv("QEC_DATABASE_URL", "postgresql+asyncpg://qec:pw@dbhost:5432/qecentral")
        monkeypatch.setenv(
            "NEXUS_DATABASE_URL_SUBSTRATE",
            "postgresql+asyncpg://qec_substrate:pw@dbhost:5432/nexus",
        )
        monkeypatch.setenv("PLATFORM_API_URL", "http://platform-api:9999")
        monkeypatch.setenv("NEXUS_STORAGE_BACKEND", "gcs")
        monkeypatch.setenv("NEXUS_FRAME_STORAGE_PATH", "/mnt/frames")
        monkeypatch.setenv("QE_HARNESS_ENABLED", "true")
        monkeypatch.setenv("QEC_SERVICE_NAME", "qe-central-test")
        monkeypatch.setenv("QEC_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("NEXUS_JWT_SECRET", "another-secret")

        s = Settings()
        assert s.qec_database_url.endswith("/qecentral")
        assert s.nexus_database_url_substrate.endswith("/nexus")
        assert s.platform_api_url == "http://platform-api:9999"
        assert s.nexus_storage_backend == "gcs"
        assert s.nexus_frame_storage_path == "/mnt/frames"
        assert s.qe_harness_enabled is True
        assert s.qec_service_name == "qe-central-test"
        assert s.qec_log_level == "DEBUG"
        assert s.nexus_jwt_secret == "another-secret"

    def test_harness_defaults_off(self, monkeypatch):
        monkeypatch.delenv("QE_HARNESS_ENABLED", raising=False)
        s = Settings()
        assert s.qe_harness_enabled is False

    def test_harness_boolean_parsing(self, monkeypatch):
        for raw, expected in (("1", True), ("true", True), ("0", False), ("false", False)):
            monkeypatch.setenv("QE_HARNESS_ENABLED", raw)
            assert Settings().qe_harness_enabled is expected

    def test_defaults(self, monkeypatch):
        for var in ("PLATFORM_API_URL", "QEC_SERVICE_NAME", "PORT"):
            monkeypatch.delenv(var, raising=False)
        s = Settings()
        assert s.platform_api_url == "http://platform-api:8091"
        assert s.qec_service_name == "qe-central"
        assert s.port == 8093
        assert s.jwt_algorithm == "HS256"


# ─── fail-closed JWT (middleware + dependency) ─────────────────────────

class TestJwtFailClosed:
    def test_missing_token_is_401(self, client):
        r = client.get("/api/v1/qec/whoami")
        assert r.status_code == 401

    def test_garbage_token_is_401(self, client):
        r = client.get(
            "/api/v1/qec/whoami",
            headers={"Authorization": "Bearer not.a.jwt"},
        )
        assert r.status_code == 401

    def test_bad_signature_is_401(self, client):
        token = make_token(secret="the-wrong-secret")
        r = client.get(
            "/api/v1/qec/whoami", headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401

    def test_expired_token_is_401(self, client):
        token = make_token(exp_delta=timedelta(minutes=-5))
        r = client.get(
            "/api/v1/qec/whoami", headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401

    def test_missing_tenant_claim_is_401(self, client):
        token = make_token(tenant_id=None)
        r = client.get(
            "/api/v1/qec/whoami", headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401

    def test_empty_tenant_claim_is_401(self, client):
        token = make_token(tenant_id="")
        r = client.get(
            "/api/v1/qec/whoami", headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401

    def test_valid_token_returns_conventions_keys(self, client):
        token = make_token(sub="u-9", tenant_id="t-9", email="e@x.test", role="manager")
        r = client.get(
            "/api/v1/qec/whoami", headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "sub": "u-9",
            "tenant_id": "t-9",
            "email": "e@x.test",
            "role": "manager",
        }

    def test_query_token_allowed_on_get(self, client):
        token = make_token()
        r = client.get(f"/api/v1/qec/whoami?token={token}")
        assert r.status_code == 200

    def test_query_token_rejected_on_post(self, client):
        # A token leaked into a URL must never mutate data.
        token = make_token(role="admin")
        r = client.post(f"/api/v1/qec/privileged?token={token}")
        assert r.status_code == 401

    def test_public_health_needs_no_token(self, client):
        r = client.get("/health")
        assert r.status_code == 200


class TestRbac:
    def test_viewer_is_403_on_privileged(self, client):
        token = make_token(role="viewer")
        r = client.post(
            "/api/v1/qec/privileged", headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    @pytest.mark.parametrize("role", ["admin", "manager"])
    def test_privileged_roles_pass(self, client, role):
        token = make_token(role=role)
        r = client.post(
            "/api/v1/qec/privileged", headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["role"] == role


# ─── service token ─────────────────────────────────────────────────────

class TestServiceToken:
    def test_claims_match_r13(self):
        token = mint_service_jwt("tenant-svc")
        payload = pyjwt.decode(token, SECRET, algorithms=["HS256"])
        assert payload["sub"] == SERVICE_SUBJECT == "svc-qe-central"
        assert payload["email"] == SERVICE_EMAIL == "qe-central@service"
        assert payload["role"] == SERVICE_ROLE == "manager"
        assert payload["tenant_id"] == "tenant-svc"
        now = datetime.now(timezone.utc).timestamp()
        assert payload["exp"] > now
        assert payload["exp"] <= now + settings.service_token_ttl_seconds + 5

    def test_service_token_passes_the_real_gate(self, client):
        token = mint_service_jwt("tenant-svc")
        r = client.post(
            "/api/v1/qec/privileged", headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

    def test_empty_tenant_refused(self):
        with pytest.raises(ValueError):
            mint_service_jwt("")
        with pytest.raises(ValueError):
            mint_service_jwt("   ")


# ─── frame-asset path guard (platform-api mirror) ──────────────────────

class TestSafeFrameAssetPath:
    def test_good_path_passes(self):
        p = "tenant-a/sess-1/crawl1_frames/frame_00001.png"
        assert safe_frame_asset_path("tenant-a", p) == p

    def test_traversal_rejected(self):
        assert safe_frame_asset_path("tenant-a", "tenant-a/../secrets/x.png") == ""

    def test_backslash_rejected(self):
        assert safe_frame_asset_path("tenant-a", "tenant-a\\sess\\f.png") == ""

    def test_cross_tenant_rejected(self):
        assert safe_frame_asset_path("tenant-a", "tenant-b/sess/f_frames/f.png") == ""

    def test_non_image_rejected(self):
        assert safe_frame_asset_path("tenant-a", "tenant-a/sess/f_frames/f.exe") == ""

    def test_empty_is_empty(self):
        assert safe_frame_asset_path("tenant-a", "") == ""
        assert safe_frame_asset_path("tenant-a", None) == ""
