"""Explorer HTTP boundary — owner scoping (T-SEC-07) and forced observation
(T-SEC-05), asserted through the real endpoints.

ATTACK
======
The per-fleet ``X-QEC-Token`` proves the caller is qe-central.  It proves
NOTHING about which tenant qe-central is acting for.  So on a shared worker,
``GET /api/v1/explore/{crawl_id}`` returned another tenant's live crawl progress
— page urls, control names, phase — and ``POST .../cancel`` stopped their crawl,
to anyone holding the fleet token or able to induce a call.

And the mutation posture arrived as a boolean the caller chose, so a manipulated
or stale dispatch could ask for a mutating crawl of a production environment.

EXPECTED
========
Status and cancel are refused without an owning tenant; the resolved
observe-only posture is decided here and can only be raised.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.guard import Attestation
from app.main import JobManager, app, resolve_observe_only

VICTIM, ATTACKER = "tenant-victim", "tenant-attacker"
CRAWL = "d" * 32


def _job(crawl_id: str):
    import types

    from app.main import _Job

    return _Job(types.SimpleNamespace(
        crawl_id=crawl_id,
        progress=lambda: {"crawl_id": crawl_id, "running": True, "phase": "explore",
                          "current_url": "https://victim.example/secret"},
        cancel=lambda: cancelled.append(crawl_id),
    ))


cancelled: list[str] = []


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def running(client):
    """A crawl owned by VICTIM, live on this worker."""
    cancelled.clear()
    jobs = JobManager()
    app.state.jobs = jobs
    jobs.reserve(CRAWL, VICTIM)
    jobs.activate(_job(CRAWL))
    yield jobs
    app.state.jobs = JobManager()


AUTH = {"X-QEC-Token": settings.explorer_token}


# ── T-SEC-07: owner scoping ────────────────────────────────────────────────

def test_status_without_a_tenant_is_refused(client, running):
    r = client.get(f"/api/v1/explore/{CRAWL}", headers=AUTH)
    assert r.status_code == 400


def test_status_for_another_tenants_crawl_is_refused(client, running):
    r = client.get(f"/api/v1/explore/{CRAWL}?tenant_id={ATTACKER}", headers=AUTH)
    assert r.status_code == 403
    assert "secret" not in r.text


def test_status_for_the_owner_still_works(client, running):
    """POSITIVE half — the legitimate poll is unaffected."""
    r = client.get(f"/api/v1/explore/{CRAWL}?tenant_id={VICTIM}", headers=AUTH)
    assert r.status_code == 200 and r.json()["running"] is True


def test_cancel_by_another_tenant_is_refused_and_stops_nothing(client, running):
    r = client.post(f"/api/v1/explore/{CRAWL}/cancel?tenant_id={ATTACKER}",
                    headers=AUTH)
    assert r.status_code == 403
    assert cancelled == []


def test_cancel_by_the_owner_still_works(client, running):
    r = client.post(f"/api/v1/explore/{CRAWL}/cancel?tenant_id={VICTIM}",
                    headers=AUTH)
    assert r.status_code == 200 and cancelled == [CRAWL]


def test_reservation_endpoints_require_the_fleet_token(client):
    assert client.post("/api/v1/reserve",
                       json={"crawl_id": CRAWL, "tenant_id": VICTIM}).status_code == 401
    assert client.get(f"/api/v1/explore/{CRAWL}?tenant_id={VICTIM}").status_code == 401


def test_reserve_then_a_second_tenant_gets_409(client):
    app.state.jobs = JobManager()
    first = client.post("/api/v1/reserve", headers=AUTH,
                        json={"crawl_id": CRAWL, "tenant_id": VICTIM})
    assert first.status_code == 200 and first.json()["status"] == "reserved"
    second = client.post("/api/v1/reserve", headers=AUTH,
                         json={"crawl_id": "e" * 32, "tenant_id": ATTACKER})
    assert second.status_code == 409
    app.state.jobs = JobManager()


def test_release_by_a_non_owner_is_refused(client):
    app.state.jobs = JobManager()
    client.post("/api/v1/reserve", headers=AUTH,
                json={"crawl_id": CRAWL, "tenant_id": VICTIM})
    r = client.post(f"/api/v1/reserve/{CRAWL}/release", headers=AUTH,
                    json={"crawl_id": CRAWL, "tenant_id": ATTACKER})
    assert r.status_code == 403
    assert app.state.jobs.busy is True         # the slot was NOT freed
    app.state.jobs = JobManager()


def test_the_explore_endpoint_binds_the_slot_to_the_requesting_tenant():
    """A dispatch for a crawl id reserved by someone else is refused.

    Runs inside the lifespan (``with``) because /explore needs the refuse pack —
    a fail-closed load the container performs at startup."""
    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.jobs.reserve(CRAWL, VICTIM)
        r = c.post("/api/v1/explore", headers=AUTH, json={
            "crawl_id": CRAWL, "tenant_id": ATTACKER,
            "target_url": "https://attacker.example/"})
        assert r.status_code == 403
        # the victim's reservation survives the attempt
        assert app.state.jobs.owner(CRAWL) == VICTIM


def test_a_second_tenant_dispatching_a_fresh_crawl_id_gets_409(client):
    """Contention, through the real endpoint, with no reservation to inherit."""
    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.jobs.reserve(CRAWL, VICTIM)
        r = c.post("/api/v1/explore", headers=AUTH, json={
            "crawl_id": "f" * 32, "tenant_id": ATTACKER,
            "target_url": "https://attacker.example/"})
        assert r.status_code == 409


# ── T-SEC-05: the explorer decides its own posture ─────────────────────────

class _Req:
    def __init__(self, observe_only=False, env_kind=""):
        self.observe_only = observe_only
        self.env_kind = env_kind


@pytest.mark.parametrize("env_kind", [
    "prod", "production", "staging", "uat", "production_test", "", "  ",
    "PROD", "unknown",
])
def test_a_non_disposable_environment_is_forced_to_observe(env_kind):
    assert resolve_observe_only(_Req(env_kind=env_kind), None) is True


def test_a_disposable_environment_may_mutate():
    assert resolve_observe_only(_Req(env_kind="disposable"), None) is False


def test_an_explicit_observe_only_is_never_lowered():
    assert resolve_observe_only(_Req(observe_only=True, env_kind="disposable"),
                                None) is True


def test_the_signed_attestation_beats_a_disagreeing_dispatch():
    """THE bypass case: a manipulated dispatch claims ``disposable`` while the
    signed attestation says ``prod``.  The attestation wins."""
    att = Attestation(attested_by="qa", env_kind="prod", expires_at_ms=None)
    assert resolve_observe_only(_Req(env_kind="disposable"), att) is True


def test_a_disposable_attestation_agreeing_with_the_dispatch_permits_mutation():
    att = Attestation(attested_by="qa", env_kind="disposable", expires_at_ms=None)
    assert resolve_observe_only(_Req(env_kind="disposable"), att) is False
