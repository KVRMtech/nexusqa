"""QE-Central Phase-5.5 — observability unit tests (metrics + correlation id).

Pins the observability contract other agents wire against:

  * **metrics are import-guarded** — every ``record_*`` helper is a hard NO-OP
    (never raises) when ``prometheus_client`` is absent OR disabled, and records
    correctly when present.  BOTH branches are asserted so the file passes with
    or without prometheus installed (the local checkout has no prometheus; CI /
    the container does — that is the whole point of the guard);
  * **the registry exposes the expected metric names** — a scrape carries every
    high-value control-plane family;
  * **the correlation-id middleware mints an id when none is supplied, preserves
    a well-formed inbound id, and SANITISES a hostile one** (never echoing
    attacker bytes), binding it into the context so handlers + the propagation
    helper see it.

No DB / no network.  The middleware is driven through a real ASGI app with
Starlette's ``TestClient``.
"""
from __future__ import annotations

import re

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from app.observability import metrics as m
from app.observability.middleware import (
    DEFAULT_REQUEST_ID_HEADER,
    MAX_REQUEST_ID_LEN,
    CorrelationIdMiddleware,
    _sanitize_request_id,
    current_request_id,
    mint_request_id,
    outbound_request_id_headers,
)

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


# ══════════════════════ metrics: helpers ═══════════════════════════════════

def _record_all_helpers() -> None:
    """Call every ``record_*`` helper once (normal + Decimal-ish inputs).

    Used by BOTH branches: when prometheus is absent these MUST be no-ops that
    never raise; when present they must not raise either (they record).
    """
    m.record_cycle_started(trigger="manual", mode="auto")
    m.record_cycle_completed(terminal_state="done", duration_seconds=12.5)
    m.record_cycle_completed(terminal_state="failed")  # no duration
    m.record_admission_decision(admitted=True)
    m.record_admission_decision(admitted=False, reason="host_busy")
    m.inc_admission_in_flight()
    m.dec_admission_in_flight()
    m.set_admission_in_flight(3)
    m.record_factory_call(endpoint="generate", method="post", status=200, duration_seconds=0.42)
    m.record_factory_call(endpoint="rtm", method="GET", status="transport_error", duration_seconds=1.0)
    m.record_substrate_rows(table="page_snapshots", count=7)
    m.record_substrate_rows(table="page_snapshots", count=0)  # <=0 ignored
    m.record_harness_outcome(outcome="refused")
    m.record_cost_units(unit="browser_seconds", quantity=1.5)
    m.record_cost_units(unit="browser_seconds", quantity=0)  # <=0 ignored


def _counter_total(registry, family: str, labels: dict):
    """Read a counter's ``_total`` sample value for ``labels`` (version-robust)."""
    for mf in registry.collect():
        if mf.name != family:
            continue
        for s in mf.samples:
            if s.name.endswith("_total") and dict(s.labels) == labels:
                return s.value
    return None


def _gauge_value(registry, family: str):
    for mf in registry.collect():
        if mf.name == family:
            for s in mf.samples:
                if s.name == family:
                    return s.value
    return None


def _hist_count(registry, family: str, labels: dict):
    for mf in registry.collect():
        if mf.name == family:
            for s in mf.samples:
                if s.name == family + "_count" and dict(s.labels) == labels:
                    return s.value
    return None


# ══════════════════════ metrics: unconditional ═════════════════════════════

def test_record_helpers_never_raise() -> None:
    """Recording must NEVER raise into a caller — in either environment."""
    _record_all_helpers()  # would raise if any helper propagated


def test_render_latest_shape() -> None:
    """``render_latest`` always returns ``(bytes, content_type_str)``."""
    payload, content_type = m.render_latest()
    assert isinstance(payload, (bytes, bytearray))
    assert isinstance(content_type, str) and content_type


def test_metrics_endpoint_serves_200() -> None:
    """The provided ``/metrics`` route mounts + serves in either environment."""
    app = FastAPI()
    app.include_router(m.build_metrics_router())
    with TestClient(app) as client:
        resp = client.get(m.DEFAULT_METRICS_PATH)
    assert resp.status_code == 200
    assert resp.text  # exposition text or a disabled-comment line


def test_metrics_path_is_env_configurable(monkeypatch) -> None:
    """``QEC_METRICS_PATH`` re-homes the exposition route (config-driven)."""
    monkeypatch.setenv(m.ENV_METRICS_PATH, "/custom-metrics")
    app = FastAPI()
    app.include_router(m.build_metrics_router())
    with TestClient(app) as client:
        assert client.get("/custom-metrics").status_code == 200
        assert client.get(m.DEFAULT_METRICS_PATH).status_code == 404


# ══════════════════════ metrics: prometheus ABSENT branch ══════════════════

@pytest.mark.skipif(m.PROM_AVAILABLE, reason="prometheus_client is installed")
def test_absent_helpers_are_noops() -> None:
    """With prometheus absent: registry is None, disabled, exposition = comment."""
    assert m.get_metrics_registry() is None
    assert m.is_enabled() is False
    _record_all_helpers()  # pure no-ops
    payload, content_type = m.render_latest()
    assert b"disabled" in payload
    assert content_type.startswith("text/plain")


# ══════════════════════ metrics: prometheus PRESENT branch ═════════════════

@pytest.mark.skipif(not m.PROM_AVAILABLE, reason="prometheus_client not installed")
def test_present_registry_exposes_expected_names() -> None:
    """With prometheus present: the scrape exposes every expected family."""
    registry = m.get_metrics_registry()
    assert registry is not None
    assert m.is_enabled() is True

    families = {mf.name for mf in registry.collect()}
    assert m.EXPECTED_METRIC_NAMES <= families

    text = m.render_latest()[0].decode()
    for name in m.EXPECTED_METRIC_NAMES:
        assert name in text  # HELP/TYPE lines carry the family name


@pytest.mark.skipif(not m.PROM_AVAILABLE, reason="prometheus_client not installed")
def test_present_recording_increments() -> None:
    """With prometheus present: helpers record against the right series."""
    registry = m.get_metrics_registry()

    before = _counter_total(registry, m.METRIC_CYCLES_STARTED,
                            {"trigger": "manual", "mode": "auto"}) or 0.0
    m.record_cycle_started(trigger="manual", mode="auto")
    after = _counter_total(registry, m.METRIC_CYCLES_STARTED,
                           {"trigger": "manual", "mode": "auto"})
    assert after == pytest.approx(before + 1.0)

    # admission decision labels (admitted → reason normalised to 'none')
    d_before = _counter_total(registry, m.METRIC_ADMISSION_DECISIONS,
                              {"decision": "admitted", "reason": "none"}) or 0.0
    m.record_admission_decision(admitted=True)
    d_after = _counter_total(registry, m.METRIC_ADMISSION_DECISIONS,
                             {"decision": "admitted", "reason": "none"})
    assert d_after == pytest.approx(d_before + 1.0)

    # in-flight gauge set/inc/dec
    m.set_admission_in_flight(5)
    assert _gauge_value(registry, m.METRIC_ADMISSION_IN_FLIGHT) == pytest.approx(5.0)
    m.inc_admission_in_flight()
    assert _gauge_value(registry, m.METRIC_ADMISSION_IN_FLIGHT) == pytest.approx(6.0)
    m.dec_admission_in_flight(2)
    assert _gauge_value(registry, m.METRIC_ADMISSION_IN_FLIGHT) == pytest.approx(4.0)

    # factory histogram observes (count increments; method upper-normalised)
    h_before = _hist_count(registry, m.METRIC_FACTORY_CALL_DURATION,
                           {"endpoint": "generate", "method": "POST"}) or 0.0
    m.record_factory_call(endpoint="generate", method="post", status=200, duration_seconds=0.3)
    h_after = _hist_count(registry, m.METRIC_FACTORY_CALL_DURATION,
                          {"endpoint": "generate", "method": "POST"})
    assert h_after == pytest.approx(h_before + 1.0)

    # cost counter sums the quantity
    c_before = _counter_total(registry, m.METRIC_COST_UNITS, {"unit": "browser_seconds"}) or 0.0
    m.record_cost_units(unit="browser_seconds", quantity=2.5)
    c_after = _counter_total(registry, m.METRIC_COST_UNITS, {"unit": "browser_seconds"})
    assert c_after == pytest.approx(c_before + 2.5)


@pytest.mark.skipif(not m.PROM_AVAILABLE, reason="prometheus_client not installed")
def test_present_content_type_is_prometheus() -> None:
    """The exposition advertises the prometheus content type."""
    _, content_type = m.render_latest()
    assert "text/plain" in content_type
    assert "version=" in content_type  # CONTENT_TYPE_LATEST carries the format version


# ══════════════════════ correlation-id middleware ══════════════════════════

def _probe_app() -> FastAPI:
    """A minimal app whose one route reports the bound correlation id."""
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/probe")
    async def probe(request: Request) -> dict:
        return {
            "ctx_request_id": current_request_id(),
            "state_request_id": getattr(request.state, "request_id", None),
            "outbound": outbound_request_id_headers({"X-Existing": "keep"}),
        }

    return app


def test_middleware_mints_id_when_absent() -> None:
    """No inbound header ⇒ a fresh uuid4-hex id, echoed + bound everywhere."""
    with TestClient(_probe_app()) as client:
        resp = client.get("/probe")
    assert resp.status_code == 200
    echoed = resp.headers.get(DEFAULT_REQUEST_ID_HEADER)
    assert echoed and _HEX32.match(echoed)
    body = resp.json()
    assert body["ctx_request_id"] == echoed
    assert body["state_request_id"] == echoed
    # propagation helper carries it downstream, preserving pre-existing headers.
    assert body["outbound"][DEFAULT_REQUEST_ID_HEADER] == echoed
    assert body["outbound"]["X-Existing"] == "keep"


def test_middleware_preserves_valid_inbound_id() -> None:
    """A well-formed inbound id is preserved verbatim (not re-minted)."""
    supplied = "trace.ABC-123_def"
    with TestClient(_probe_app()) as client:
        resp = client.get("/probe", headers={DEFAULT_REQUEST_ID_HEADER: supplied})
    assert resp.headers.get(DEFAULT_REQUEST_ID_HEADER) == supplied
    assert resp.json()["ctx_request_id"] == supplied


@pytest.mark.parametrize(
    "hostile",
    [
        "bad id with spaces",
        "inject\r\nSet-Cookie: x=1",  # CRLF header-injection attempt
        "unicode-☠-id",
        "a" * (MAX_REQUEST_ID_LEN + 1),  # over the length cap
        "semi;colon",
        "path/traversal",
        "",
        "   ",
    ],
)
def test_sanitizer_rejects_hostile_ids(hostile: str) -> None:
    """The security-critical sanitizer rejects EVERY hostile id (→ re-mint).

    Driven at the unit level because the HTTP client itself refuses to transmit
    some of these (non-ascii / CRLF) — the defence must hold regardless.
    """
    assert _sanitize_request_id(hostile) == ""


@pytest.mark.parametrize(
    "hostile",
    [
        "bad id with spaces",
        "a" * (MAX_REQUEST_ID_LEN + 1),  # over the length cap
        "semi;colon",
        "path/traversal",
    ],
)
def test_middleware_sanitises_hostile_id(hostile: str) -> None:
    """End-to-end: a transport-sendable hostile id is replaced with a mint."""
    with TestClient(_probe_app()) as client:
        resp = client.get("/probe", headers={DEFAULT_REQUEST_ID_HEADER: hostile})
    echoed = resp.headers.get(DEFAULT_REQUEST_ID_HEADER)
    assert echoed and _HEX32.match(echoed)  # minted, not the hostile value
    assert echoed != hostile
    assert resp.json()["ctx_request_id"] == echoed


def test_current_request_id_empty_outside_request() -> None:
    """Outside any request context there is no bound id (opt-in, inert)."""
    assert current_request_id() == ""
    # propagation adds NO fabricated header when nothing is bound.
    assert outbound_request_id_headers({"a": "b"}) == {"a": "b"}
    assert outbound_request_id_headers() == {}


def test_mint_request_id_is_hex32() -> None:
    assert _HEX32.match(mint_request_id())


def test_no_id_leak_between_requests() -> None:
    """Each request gets its own id; nothing leaks into the next context."""
    with TestClient(_probe_app()) as client:
        first = client.get("/probe").json()["ctx_request_id"]
        second = client.get("/probe").json()["ctx_request_id"]
    assert first != second
    assert current_request_id() == ""  # unbound after the requests complete
