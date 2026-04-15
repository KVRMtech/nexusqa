"""
Tests for nexus_sdk.security.headers — Security headers middleware.

Verifies:
- All OWASP-recommended headers are set
- HSTS can be disabled (for dev)
- CSP and Permissions-Policy are configurable
- Headers are set on every response
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus_sdk.security.headers import SecurityHeadersMiddleware


@pytest.fixture
def app():
    """FastAPI app with security headers middleware."""
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/test")
    async def test_endpoint():
        return {"status": "ok"}

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestSecurityHeadersMiddleware:
    """Tests for SecurityHeadersMiddleware."""

    def test_x_content_type_options(self, client):
        """X-Content-Type-Options: nosniff is set."""
        resp = client.get("/test")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"

    def test_x_frame_options(self, client):
        """X-Frame-Options: DENY is set."""
        resp = client.get("/test")
        assert resp.headers["X-Frame-Options"] == "DENY"

    def test_x_xss_protection(self, client):
        """X-XSS-Protection header is set."""
        resp = client.get("/test")
        assert resp.headers["X-XSS-Protection"] == "1; mode=block"

    def test_referrer_policy(self, client):
        """Referrer-Policy is set."""
        resp = client.get("/test")
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_cache_control(self, client):
        """Cache-Control prevents caching."""
        resp = client.get("/test")
        assert "no-store" in resp.headers["Cache-Control"]
        assert "no-cache" in resp.headers["Cache-Control"]

    def test_hsts(self, client):
        """Strict-Transport-Security is set by default."""
        resp = client.get("/test")
        hsts = resp.headers["Strict-Transport-Security"]
        assert "max-age=" in hsts
        assert "includeSubDomains" in hsts

    def test_csp(self, client):
        """Content-Security-Policy is set."""
        resp = client.get("/test")
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp

    def test_permissions_policy(self, client):
        """Permissions-Policy restricts browser features."""
        resp = client.get("/test")
        pp = resp.headers["Permissions-Policy"]
        assert "camera=()" in pp
        assert "microphone=()" in pp

    def test_hsts_disabled(self):
        """HSTS can be disabled for development."""
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware, include_hsts=False)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        resp = client.get("/test")
        assert "Strict-Transport-Security" not in resp.headers

    def test_custom_csp(self):
        """CSP can be customized."""
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware, csp="default-src 'self'")

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        resp = client.get("/test")
        assert resp.headers["Content-Security-Policy"] == "default-src 'self'"

    def test_csp_disabled(self):
        """CSP can be omitted by setting to None."""
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware, csp=None)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        resp = client.get("/test")
        assert "Content-Security-Policy" not in resp.headers
