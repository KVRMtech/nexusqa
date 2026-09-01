"""
Nexus OpenTelemetry Tracing — Distributed tracing across all services.

Provides:
- TracerProvider initialization with OTLP exporter
- FastAPI auto-instrumentation
- HTTPX auto-instrumentation (for inter-service calls)
- Trace/span context propagation via W3C TraceContext headers
- Integration with the existing NexusRequest/NexusResponse trace_id field

The tracing setup is optional and gracefully degrades if the OTel Collector
is not available. In development, tracing can be disabled entirely.

Usage:
    # Automatically called by NexusEngine. For standalone services:
    from nexus_sdk.observability.tracing import setup_tracing

    setup_tracing(
        service_name="gateway",
        service_version="0.2.0",
        otlp_endpoint="http://otel-collector:4317",
    )
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_tracing_configured = False


def setup_tracing(
    service_name: str,
    service_version: str = "0.1.0",
    otlp_endpoint: str = "http://localhost:4317",
    enabled: bool = True,
    environment: str = "production",
    sample_rate: float = 1.0,
    app=None,
) -> None:
    """
    Initialize OpenTelemetry distributed tracing.

    Args:
        service_name: Name of this service (e.g., "ears", "gateway").
        service_version: Semantic version of the service.
        otlp_endpoint: OTel Collector OTLP gRPC endpoint.
        enabled: Set to False to disable tracing entirely.
        environment: Deployment environment (production, staging, dev).
        sample_rate: Fraction of requests to trace (0.0-1.0).
        app: Optional FastAPI app to instrument.
    """
    global _tracing_configured
    if _tracing_configured or not enabled:
        return
    _tracing_configured = True

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

        # Resource identifies this service in traces
        resource = Resource.create({
            SERVICE_NAME: service_name,
            SERVICE_VERSION: service_version,
            "deployment.environment": environment,
        })

        # Sampler controls what fraction of traces are exported
        sampler = TraceIdRatioBased(sample_rate)

        # Create and set the global TracerProvider
        provider = TracerProvider(resource=resource, sampler=sampler)

        # OTLP exporter sends traces to the collector
        exporter = OTLPSpanExporter(
            endpoint=otlp_endpoint,
            insecure=True,  # Use TLS in production via env var OTEL_EXPORTER_OTLP_INSECURE
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)

        # Auto-instrument FastAPI if app is provided
        if app is not None:
            _instrument_fastapi(app)

        # Auto-instrument HTTPX for outbound calls
        _instrument_httpx()

        logger.info(
            "OpenTelemetry tracing initialized: service=%s endpoint=%s sample_rate=%.2f",
            service_name,
            otlp_endpoint,
            sample_rate,
        )

    except ImportError as e:
        logger.warning(
            "OpenTelemetry packages not installed, tracing disabled: %s", str(e)
        )
    except Exception as e:
        logger.warning(
            "Failed to initialize OpenTelemetry tracing (non-fatal): %s", str(e)
        )


def _instrument_fastapi(app) -> None:
    """Auto-instrument FastAPI with OpenTelemetry."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="health,health/ready,health/detail,metrics",
        )
    except ImportError:
        logger.debug("opentelemetry-instrumentation-fastapi not installed")
    except Exception as e:
        logger.warning("FastAPI OTel instrumentation failed: %s", str(e))


def _instrument_httpx() -> None:
    """Auto-instrument HTTPX for outbound HTTP calls."""
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except ImportError:
        logger.debug("opentelemetry-instrumentation-httpx not installed")
    except Exception as e:
        logger.warning("HTTPX OTel instrumentation failed: %s", str(e))


def get_tracer(name: str = __name__):
    """
    Get an OpenTelemetry tracer for manual span creation.

    Returns a no-op tracer if OTel is not configured.

    Usage:
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("my_operation") as span:
            span.set_attribute("key", "value")
            # ... do work ...
    """
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        # Return a no-op tracer
        return _NoOpTracer()


class _NoOpTracer:
    """Fallback tracer when OpenTelemetry is not installed."""

    def start_as_current_span(self, name: str, **kwargs):
        return _NoOpContextManager()

    def start_span(self, name: str, **kwargs):
        return _NoOpSpan()


class _NoOpSpan:
    """No-op span for when tracing is disabled."""

    def set_attribute(self, key: str, value) -> None:
        pass

    def set_status(self, status) -> None:
        pass

    def record_exception(self, exception) -> None:
        pass

    def end(self) -> None:
        pass


class _NoOpContextManager:
    """No-op context manager for _NoOpTracer."""

    def __enter__(self):
        return _NoOpSpan()

    def __exit__(self, *args):
        pass

    def __await__(self):
        yield
        return _NoOpSpan()


def reset_tracing_state() -> None:
    """Reset tracing configuration state. Used in tests."""
    global _tracing_configured
    _tracing_configured = False
