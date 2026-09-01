"""QE-Central Phase 5.5 — API-protection unit tests (in-process, no network).

Pins the three control-plane self-protection guarantees:

  * the global exception handler maps an arbitrary error to a clean ``500`` carrying
    the correlation id and NO leaked internal detail/stack, and does NOT hijack a
    deliberate ``HTTPException``;
  * the request-id middleware stamps a fresh correlation id (echoing an inbound one)
    onto ``request.state`` and the response header;
  * the per-principal rate limiter admits a burst then ``429``s over the cap (with
    ``Retry-After``) WHEN ENABLED, is fully inert (never throttles, never touches the
    response) when disabled, keys per principal, refills over time, and bounds memory
    via LRU eviction.

Time is injected for the limiter so the token-bucket behaviour is exact.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api_protect import (
    REQUEST_ID_HEADER,
    PrincipalRateLimiter,
    build_api_rate_limiter,
    make_principal_rate_limit_middleware,
    request_id_middleware,
    unhandled_exception_handler,
)

_LEAK = "super-secret-internal-detail-42"


class FakeClock:
    """A hand-cranked monotonic clock for deterministic token-bucket tests."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


# ── Global exception handler ────────────────────────────────────────────────
class TestExceptionHandler:
    def _app(self) -> FastAPI:
        app = FastAPI()
        app.middleware("http")(request_id_middleware)
        app.add_exception_handler(Exception, unhandled_exception_handler)

        @app.get("/api/v1/boom")
        async def boom():
            raise ValueError(_LEAK)

        @app.get("/api/v1/forbidden")
        async def forbidden():
            raise HTTPException(status_code=403, detail="nope")

        @app.get("/api/v1/ok")
        async def ok():
            return {"ok": True}

        return app

    def test_unhandled_error_maps_to_clean_500(self):
        client = TestClient(self._app(), raise_server_exceptions=False)
        r = client.get("/api/v1/boom")
        assert r.status_code == 500
        body = r.json()
        assert body["detail"] == "Internal Server Error"
        assert body["request_id"]
        assert REQUEST_ID_HEADER in r.headers

    def test_500_leaks_no_internal_detail(self):
        client = TestClient(self._app(), raise_server_exceptions=False)
        r = client.get("/api/v1/boom")
        assert _LEAK not in r.text
        assert "ValueError" not in r.text
        assert "Traceback" not in r.text

    def test_inbound_correlation_id_is_echoed(self):
        client = TestClient(self._app(), raise_server_exceptions=False)
        r = client.get("/api/v1/boom", headers={REQUEST_ID_HEADER: "corr-abc-123"})
        assert r.json()["request_id"] == "corr-abc-123"
        assert r.headers[REQUEST_ID_HEADER] == "corr-abc-123"

    def test_http_exception_is_not_hijacked(self):
        # A deliberate 403 must stay a 403 — the generic handler only catches 500s.
        client = TestClient(self._app(), raise_server_exceptions=False)
        r = client.get("/api/v1/forbidden")
        assert r.status_code == 403
        assert r.json()["detail"] == "nope"

    def test_healthy_route_still_gets_request_id(self):
        client = TestClient(self._app())
        r = client.get("/api/v1/ok")
        assert r.status_code == 200
        assert REQUEST_ID_HEADER in r.headers


# ── Rate limiter (unit: the bucket logic) ───────────────────────────────────
class TestPrincipalRateLimiterUnit:
    def test_disabled_by_default_env(self, monkeypatch):
        monkeypatch.delenv("QEC_API_RATE_LIMIT", raising=False)
        lim = build_api_rate_limiter()
        assert lim.enabled is False
        for _ in range(1000):
            allowed, retry = lim.allow("t:u")
            assert allowed is True and retry == 0.0

    def test_admits_burst_then_denies_over_cap(self):
        clock = FakeClock()
        lim = PrincipalRateLimiter(rate_per_sec=1.0, burst_factor=2.0, clock=clock)
        assert lim.enabled is True
        # Capacity = rate*burst = 2 and starts FULL ⇒ two immediate admits.
        assert lim.allow("A")[0] is True
        assert lim.allow("A")[0] is True
        allowed, retry = lim.allow("A")            # third exceeds the burst
        assert allowed is False
        assert retry > 0                            # a positive retry-after hint

    def test_refills_over_time(self):
        clock = FakeClock()
        lim = PrincipalRateLimiter(rate_per_sec=1.0, burst_factor=2.0, clock=clock)
        assert lim.allow("A")[0] and lim.allow("A")[0]
        assert lim.allow("A")[0] is False           # depleted
        clock.advance(1.0)                           # +1 token at 1 rps
        assert lim.allow("A")[0] is True
        assert lim.allow("A")[0] is False            # and depleted again

    def test_per_principal_isolation(self):
        clock = FakeClock()
        lim = PrincipalRateLimiter(rate_per_sec=1.0, burst_factor=2.0, clock=clock)
        # Drain principal A entirely.
        assert lim.allow("A")[0] and lim.allow("A")[0]
        assert lim.allow("A")[0] is False
        # Principal B is untouched — its own full bucket.
        assert lim.allow("B")[0] is True
        assert lim.allow("B")[0] is True
        assert lim.allow("B")[0] is False

    def test_lru_eviction_bounds_memory(self):
        clock = FakeClock()
        lim = PrincipalRateLimiter(
            rate_per_sec=1.0, burst_factor=2.0, max_principals=2, clock=clock,
        )
        lim.allow("p1")
        lim.allow("p2")
        lim.allow("p3")                              # evicts the LRU (p1)
        snap = lim.snapshot()
        assert snap["tracked_principals"] == 2
        assert snap["max_principals"] == 2

    def test_snapshot_shape(self):
        lim = PrincipalRateLimiter(rate_per_sec=5.0, burst_factor=3.0)
        snap = lim.snapshot()
        assert snap["enabled"] is True
        assert snap["rate_per_sec"] == 5.0
        assert snap["burst_factor"] == 3.0
        assert "tracked_principals" in snap


# ── Rate limiter (middleware: wired into an app) ────────────────────────────
class TestPrincipalRateLimitMiddleware:
    def _app(self, limiter: PrincipalRateLimiter) -> FastAPI:
        app = FastAPI()
        app.middleware("http")(make_principal_rate_limit_middleware(limiter))

        @app.get("/api/v1/thing")
        async def thing():
            return {"ok": True}

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        return app

    def test_enabled_admits_then_429s_over_cap(self):
        clock = FakeClock()
        lim = PrincipalRateLimiter(rate_per_sec=1.0, burst_factor=2.0, clock=clock)
        client = TestClient(self._app(lim))

        assert client.get("/api/v1/thing").status_code == 200   # token 2→1
        assert client.get("/api/v1/thing").status_code == 200   # token 1→0
        r = client.get("/api/v1/thing")                          # over cap
        assert r.status_code == 429
        assert r.json()["detail"] == "Rate limit exceeded"
        assert int(r.headers["Retry-After"]) >= 1
        assert REQUEST_ID_HEADER in r.headers

        clock.advance(1.0)                                       # refill one token
        assert client.get("/api/v1/thing").status_code == 200

    def test_disabled_is_inert(self):
        clock = FakeClock()
        lim = PrincipalRateLimiter(rate_per_sec=0.0, clock=clock)  # OFF
        client = TestClient(self._app(lim))
        for _ in range(50):
            assert client.get("/api/v1/thing").status_code == 200
        # Inert: no Retry-After ever emitted.
        assert "Retry-After" not in client.get("/api/v1/thing").headers

    def test_public_paths_are_never_limited(self):
        clock = FakeClock()
        lim = PrincipalRateLimiter(rate_per_sec=1.0, burst_factor=1.0, clock=clock)
        client = TestClient(self._app(lim))
        # /health is public — hammer well past any per-principal cap, never 429.
        for _ in range(20):
            assert client.get("/health").status_code == 200
