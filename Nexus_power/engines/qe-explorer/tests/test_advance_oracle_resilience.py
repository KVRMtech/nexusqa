"""The per-crawl advance-oracle callable: three-state contract, circuit
breaker, call cap, and timeout — the resilience that makes fast failure safe
(the honest ``oracle_unavailable`` terminal does the rest).
"""
from __future__ import annotations

import asyncio
import json

from app import main as main_mod
from app.config import settings
from app.main import _make_advance_oracle


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _Client:
    """Scripted http client: replays responses (or exceptions) and records
    every request's URL, payload and timeout."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.requests = []

    async def post(self, url, content=b"", headers=None, timeout=None):
        self.requests.append({
            "url": url, "payload": json.loads(content or b"{}"),
            "headers": dict(headers or {}), "timeout": timeout,
        })
        outcome = self._outcomes[min(len(self.requests) - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


_CONTROLS = [{"name": "See My Quote", "kind": "button",
              "disabled": False, "danger": False}]


def _consult(oracle):
    return asyncio.run(oracle(_CONTROLS, "Step", "https://a.example/q"))


def test_picked_passes_through_with_signature():
    client = _Client([_Resp(200, {"control_index": 0, "status": "picked",
                                  "signature": "sig-1"})])
    oracle = _make_advance_oracle(client, "t1", "c1")
    out = _consult(oracle)
    assert out == {"index": 0, "status": "picked", "signature": "sig-1"}


def test_none_passes_through():
    client = _Client([_Resp(200, {"control_index": None, "status": "none",
                                  "signature": "sig-2"})])
    oracle = _make_advance_oracle(client, "t1", "c1")
    assert _consult(oracle)["status"] == "none"


def test_legacy_body_without_status_is_unavailable():
    """A body with no ``status`` (the pre-three-state contract) is a decision
    NOT made — never silently treated as none."""
    client = _Client([_Resp(200, {"control_index": 0})])
    oracle = _make_advance_oracle(client, "t1", "c1")
    assert _consult(oracle)["status"] == "unavailable"


def test_non_200_is_unavailable():
    client = _Client([_Resp(503, {})])
    oracle = _make_advance_oracle(client, "t1", "c1")
    assert _consult(oracle)["status"] == "unavailable"


def test_transport_error_is_unavailable():
    client = _Client([RuntimeError("connection refused")])
    oracle = _make_advance_oracle(client, "t1", "c1")
    assert _consult(oracle)["status"] == "unavailable"


def test_breaker_opens_after_consecutive_failures_and_stops_http():
    threshold = settings.advance_oracle_breaker_threshold
    client = _Client([RuntimeError("down")])
    oracle = _make_advance_oracle(client, "t1", "c1")
    for _ in range(threshold):
        assert _consult(oracle)["status"] == "unavailable"
    assert len(client.requests) == threshold
    # Circuit is open: further consultations make NO http attempts.
    for _ in range(5):
        assert _consult(oracle)["status"] == "unavailable"
    assert len(client.requests) == threshold


def test_success_resets_the_consecutive_counter():
    threshold = settings.advance_oracle_breaker_threshold
    outcomes = []
    for _ in range(threshold - 1):
        outcomes.append(RuntimeError("down"))
    outcomes.append(_Resp(200, {"control_index": None, "status": "none",
                                "signature": ""}))
    outcomes.append(RuntimeError("down"))
    client = _Client(outcomes)
    oracle = _make_advance_oracle(client, "t1", "c1")
    for _ in range(threshold + 1):
        _consult(oracle)
    # threshold-1 failures, then a success, then 1 failure — never threshold
    # consecutive, so the circuit stays closed and http continues.
    assert _consult(oracle)["status"] == "unavailable"
    assert len(client.requests) == threshold + 2


def test_call_cap_is_honored(monkeypatch):
    monkeypatch.setattr(settings, "advance_oracle_max_calls", 2)
    client = _Client([_Resp(200, {"control_index": 0, "status": "picked",
                                  "signature": "s"})])
    oracle = _make_advance_oracle(client, "t1", "c1")
    assert _consult(oracle)["status"] == "picked"
    assert _consult(oracle)["status"] == "picked"
    # Cap reached: unavailable, no further http.
    assert _consult(oracle)["status"] == "unavailable"
    assert len(client.requests) == 2


def test_timeout_comes_from_settings():
    client = _Client([_Resp(200, {"control_index": None, "status": "none",
                                  "signature": ""})])
    oracle = _make_advance_oracle(client, "t1", "c1")
    _consult(oracle)
    assert client.requests[0]["timeout"] == settings.advance_oracle_timeout_s


def test_default_timeout_is_seconds_not_half_a_minute():
    """The 30 s stall this replaces is pinned out: default must be ≤ 10 s."""
    assert 0 < settings.advance_oracle_timeout_s <= 10.0


def test_payload_is_hmac_signed_and_shape_only():
    client = _Client([_Resp(200, {"control_index": None, "status": "none",
                                  "signature": ""})])
    oracle = _make_advance_oracle(client, "t1", "c1")
    _consult(oracle)
    req = client.requests[0]
    assert req["url"].endswith("/internal/pick-advance")
    assert req["headers"].get("X-QEC-Signature")
    assert req["headers"].get("X-QEC-Token")
    sent = req["payload"]["controls"][0]
    assert set(sent) == {"name", "kind", "disabled", "danger"}


def test_breaker_and_cap_state_is_per_oracle_instance():
    threshold = settings.advance_oracle_breaker_threshold
    down = _Client([RuntimeError("down")])
    o1 = _make_advance_oracle(down, "t1", "c1")
    for _ in range(threshold):
        _consult(o1)
    healthy = _Client([_Resp(200, {"control_index": 0, "status": "picked",
                                   "signature": "s"})])
    o2 = _make_advance_oracle(healthy, "t1", "c2")
    assert _consult(o2)["status"] == "picked"
