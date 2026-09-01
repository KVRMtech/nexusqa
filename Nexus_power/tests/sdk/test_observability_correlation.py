"""
Tests for nexus_sdk.observability.correlation — Correlation ID middleware.

Verifies:
- X-Request-ID is generated when absent
- X-Request-ID is forwarded when present
- Tenant ID is extracted from X-Tenant-ID header
- Request state is populated
- Response headers include correlation ID
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from nexus_sdk.observability.correlation import (
    CorrelationIdMiddleware,
    REQUEST_ID_HEADER,
    TENANT_ID_HEADER,
)


@pytest.fixture
def app():
    """FastAPI app with correlation middleware."""
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware, service_name="test")

    @app.get("/test")
    async def test_endpoint(request: Request):
        return {
            "request_id": getattr(request.state, "request_id", None),
            "tenant_id": getattr(request.state, "tenant_id", None),
        }

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestCorrelationIdMiddleware:
    """Tests for CorrelationIdMiddleware."""

    def test_generates_request_id_when_absent(self, client):
        """A new X-Request-ID is generated if none is provided."""
        response = client.get("/test")
        assert response.status_code == 200
        assert REQUEST_ID_HEADER in response.headers
        request_id = response.headers[REQUEST_ID_HEADER]
        assert len(request_id) > 0

    def test_forwards_existing_request_id(self, client):
        """An existing X-Request-ID from upstream is preserved."""
        response = client.get("/test", headers={REQUEST_ID_HEADER: "upstream-req-123"})
        assert response.status_code == 200
        assert response.headers[REQUEST_ID_HEADER] == "upstream-req-123"

    def test_request_id_in_request_state(self, client):
        """Request ID is set on request.state for endpoint access."""
        response = client.get("/test", headers={REQUEST_ID_HEADER: "state-test-123"})
        data = response.json()
        assert data["request_id"] == "state-test-123"

    def test_tenant_id_from_header(self, client):
        """Tenant ID is extracted from X-Tenant-ID header."""
        response = client.get("/test", headers={TENANT_ID_HEADER: "tenant-abc"})
        data = response.json()
        assert data["tenant_id"] == "tenant-abc"

    def test_tenant_id_in_response(self, client):
        """X-Tenant-ID is echoed in response headers."""
        response = client.get("/test", headers={TENANT_ID_HEADER: "tenant-xyz"})
        assert response.headers.get(TENANT_ID_HEADER) == "tenant-xyz"

    def test_missing_tenant_id(self, client):
        """Missing tenant ID doesn't cause errors."""
        response = client.get("/test")
        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] is None
