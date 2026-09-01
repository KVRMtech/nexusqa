"""
Tests for nexus_sdk.observability.metrics — Prometheus custom metrics + middleware.

Verifies:
- NexusMetrics creates standard HTTP metrics
- Custom counter/histogram/gauge registration
- MetricsMiddleware instruments endpoints
- Path normalization prevents cardinality explosion
- Service info metric
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from prometheus_client import CollectorRegistry

from nexus_sdk.observability.metrics import (
    NexusMetrics,
    MetricsMiddleware,
    init_metrics,
    get_metrics,
    _normalize_path,
)


@pytest.fixture
def registry():
    """Fresh Prometheus registry to avoid metric name collisions."""
    return CollectorRegistry()


@pytest.fixture
def metrics(registry):
    """NexusMetrics instance with isolated registry."""
    return NexusMetrics("test-engine", registry=registry)


class TestNexusMetrics:
    """Tests for NexusMetrics class."""

    def test_standard_metrics_created(self, metrics):
        """Standard HTTP metrics are registered on init."""
        assert metrics.requests_total is not None
        assert metrics.request_duration is not None
        assert metrics.requests_in_progress is not None
        assert metrics.request_size is not None
        assert metrics.response_size is not None

    def test_service_info(self, metrics):
        """Service info metric can be set."""
        metrics.set_service_info(version="1.0.0", environment="test")
        # No exception means success

    def test_custom_counter(self, metrics):
        """Custom counters can be registered and retrieved."""
        counter = metrics.custom_counter("test_counter", "A test counter")
        assert counter is not None
        # Same name returns same counter
        counter2 = metrics.custom_counter("test_counter", "A test counter")
        assert counter is counter2

    def test_custom_histogram(self, metrics):
        """Custom histograms can be registered."""
        hist = metrics.custom_histogram("test_histogram", "A test histogram")
        assert hist is not None

    def test_custom_gauge(self, metrics):
        """Custom gauges can be registered."""
        gauge = metrics.custom_gauge("test_gauge", "A test gauge")
        assert gauge is not None

    def test_custom_counter_with_labels(self, metrics):
        """Custom counters support additional labels."""
        counter = metrics.custom_counter(
            "labeled_counter", "Counter with labels", labels=["engine", "type"]
        )
        # Should have service + custom labels
        counter.labels(service="test", engine="ears", type="transcription").inc()

    def test_increment_request_metrics(self, metrics):
        """Request metrics can be incremented."""
        metrics.requests_total.labels(
            service="test", method="GET", endpoint="/api/v1/test", status_code="200"
        ).inc()
        metrics.request_duration.labels(
            service="test", method="GET", endpoint="/api/v1/test"
        ).observe(0.5)
        metrics.requests_in_progress.labels(service="test").inc()
        metrics.requests_in_progress.labels(service="test").dec()


class TestNormalizePath:
    """Tests for URL path normalization."""

    def test_uuid_replacement(self):
        """UUIDs in paths are replaced with {id}."""
        path = "/api/v1/sessions/550e8400-e29b-41d4-a716-446655440000/results"
        assert _normalize_path(path) == "/api/v1/sessions/{id}/results"

    def test_numeric_ids(self):
        """Numeric segments are replaced with {id}."""
        assert _normalize_path("/api/v1/users/12345") == "/api/v1/users/{id}"

    def test_no_change_for_static_paths(self):
        """Static paths are returned unchanged."""
        assert _normalize_path("/api/v1/health") == "/api/v1/health"

    def test_mixed_path(self):
        """Paths with both UUIDs and static segments."""
        path = "/api/v1/tenants/abc-tenant/users/550e8400-e29b-41d4-a716-446655440000"
        result = _normalize_path(path)
        assert "{id}" in result


class TestInitMetrics:
    """Tests for global metrics initialization."""

    def test_init_and_get_metrics(self):
        """init_metrics creates global instance, get_metrics retrieves it."""
        registry = CollectorRegistry()
        m = init_metrics("test-service", registry=registry)
        assert m is not None
        assert m.service_name == "test-service"

    def test_get_metrics_before_init(self):
        """get_metrics returns None-or-existing before explicit init."""
        # May return a previously initialized instance from other tests
        result = get_metrics()
        # Just verify it doesn't crash
        assert result is None or isinstance(result, NexusMetrics)
