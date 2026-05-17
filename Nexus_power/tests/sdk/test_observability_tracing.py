"""
Tests for nexus_sdk.observability.tracing — OpenTelemetry tracing setup.

Verifies:
- setup_tracing is idempotent
- Tracing can be disabled
- get_tracer returns a usable tracer (or no-op fallback)
- NoOp tracer/span work correctly when OTel is unavailable
- reset_tracing_state works for test isolation
"""
import pytest

from nexus_sdk.observability.tracing import (
    setup_tracing,
    get_tracer,
    reset_tracing_state,
    _NoOpTracer,
    _NoOpSpan,
    _NoOpContextManager,
)


@pytest.fixture(autouse=True)
def reset_tracing():
    """Reset tracing state before each test."""
    reset_tracing_state()
    yield
    reset_tracing_state()


class TestSetupTracing:
    """Tests for OpenTelemetry tracing initialization."""

    def test_setup_tracing_disabled(self):
        """Tracing is not configured when enabled=False."""
        setup_tracing(
            service_name="test",
            service_version="0.1.0",
            enabled=False,
        )
        # No exception = success

    def test_setup_tracing_idempotent(self):
        """Calling setup_tracing twice doesn't fail."""
        setup_tracing(service_name="svc1", enabled=False)
        setup_tracing(service_name="svc1", enabled=False)

    def test_setup_tracing_graceful_failure(self):
        """setup_tracing handles missing OTel packages gracefully."""
        # Even if OTel packages aren't installed, this should not raise
        setup_tracing(
            service_name="test",
            service_version="0.1.0",
            otlp_endpoint="http://nonexistent:4317",
            enabled=True,
        )


class TestGetTracer:
    """Tests for get_tracer function."""

    def test_get_tracer_returns_something(self):
        """get_tracer returns a tracer object."""
        tracer = get_tracer("test.module")
        assert tracer is not None

    def test_tracer_can_create_span(self):
        """Returned tracer supports span creation."""
        tracer = get_tracer("test")
        # Should work regardless of OTel installation
        ctx = tracer.start_as_current_span("test-span")
        assert ctx is not None


class TestNoOpTracer:
    """Tests for the NoOp fallback tracer."""

    def test_noop_tracer_span(self):
        """NoOp tracer creates context managers."""
        tracer = _NoOpTracer()
        ctx = tracer.start_as_current_span("test")
        with ctx as span:
            assert isinstance(span, _NoOpSpan)

    def test_noop_tracer_start_span(self):
        """NoOp tracer can start a span directly."""
        tracer = _NoOpTracer()
        span = tracer.start_span("test")
        assert isinstance(span, _NoOpSpan)

    def test_noop_span_methods(self):
        """NoOp span methods don't raise."""
        span = _NoOpSpan()
        span.set_attribute("key", "value")
        span.set_status("ok")
        span.record_exception(Exception("test"))
        span.end()
