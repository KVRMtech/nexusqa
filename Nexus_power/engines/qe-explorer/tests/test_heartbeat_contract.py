"""TEAM A / PHASE A — THE EXPLORER HALF of the fleet heartbeat contract.

qe-explorer and qe-central each ship a top-level ``app`` package and cannot be
imported into one interpreter, so the wire shape is frozen as DATA in
``contracts/fleet_heartbeat_v1.json`` and each side asserts against it in its
own process. qe-central's half lives in
``platform/qe-central/tests/fleet/test_a_worker_announces_itself_and_stays_announced.py``.
Together the two files are one proof: rename a field, a path, or a scope on
either side and that side's own suite fails.

Also proven here, against the REAL ``FleetAnnouncer`` over a mocked transport
(no sleeps, no server):

  * the register/heartbeat bodies carry exactly the frozen required fields;
  * both requests are SIGNED over the exact bytes sent, scope-bound to the
    worker id (the same v2 envelope the completion callback uses);
  * a 404 heartbeat means RE-REGISTER (the registry was reset), and the loop
    treats it that way;
  * the announcer refuses to run — loudly, with a named reason — when its
    configuration cannot describe a schedulable worker (fail-safe: the fleet
    degrades to the static pool, never to a half-registered worker).
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app import heartbeat as hb
from app.config import settings


def _contract() -> dict:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "fleet_heartbeat_v1.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError(
        "contracts/fleet_heartbeat_v1.json not found above %s — the frozen "
        "wire contract is the only thing tying this service's announcer to "
        "qe-central's registry routes, and it must not be deleted to make a "
        "test pass" % here
    )


CONTRACT = _contract()


# ── the frozen shapes ──────────────────────────────────────────────────────

def test_register_payload_carries_every_required_field():
    body = hb.register_payload(worker_id="w1", url="http://w1:8210",
                               allowlist_path="/eg/aw.txt", capacity=2)
    for field in CONTRACT["register_request"]["required_fields"]:
        assert field in body, f"register payload lost required field {field!r}"
    assert body["schema_version"] == CONTRACT["schema_version"]


def test_heartbeat_payload_carries_every_required_field():
    body = hb.heartbeat_payload(worker_id="w1", in_flight=1, capacity=2)
    for field in CONTRACT["heartbeat_request"]["required_fields"]:
        assert field in body, f"heartbeat payload lost required field {field!r}"
    assert body["status"] in CONTRACT["statuses"]


def test_routes_and_scopes_match_the_contract():
    assert hb.REGISTER_PATH == CONTRACT["routes"]["register"]["path"]
    assert hb.heartbeat_path("w1") == CONTRACT["routes"]["heartbeat"]["path"].format(
        worker_id="w1")
    assert hb.register_scope("w1") == CONTRACT["routes"]["register"]["scope"].format(
        worker_id="w1")
    assert hb.heartbeat_scope("w1") == CONTRACT["routes"]["heartbeat"]["scope"].format(
        worker_id="w1")
    assert hb.TOKEN_HEADER == CONTRACT["headers"]["token"]
    assert hb.SIGNATURE_HEADER == CONTRACT["headers"]["signature"]


def test_encode_is_canonical_so_the_signature_covers_what_is_sent():
    a = hb.encode({"b": 1, "a": 2})
    b = hb.encode({"a": 2, "b": 1})
    assert a == b == b'{"a":2,"b":1}'


# ── the announcer, over a mocked transport ─────────────────────────────────

class _Jobs:
    active_count = 1


def _announcer(handler) -> hb.FleetAnnouncer:
    transport = httpx.MockTransport(handler)
    return hb.FleetAnnouncer(http=httpx.AsyncClient(transport=transport),
                             jobs=_Jobs())


def _configured(monkeypatch, **over):
    values = {"fleet_register": True, "worker_id": "w1",
              "worker_url": "http://w1:8210",
              "worker_allowlist_path": "/eg/aw.txt",
              "worker_tenant_affinity": "", "explorer_capacity": 2,
              "callback_url": "http://qe-central:8093"}
    values.update(over)
    for k, v in values.items():
        monkeypatch.setattr(settings, k, v, raising=False)


@pytest.mark.asyncio
async def test_register_posts_the_contract_body_signed_and_scope_bound(monkeypatch):
    _configured(monkeypatch)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["sig"] = request.headers.get(hb.SIGNATURE_HEADER, "")
        seen["tok"] = request.headers.get(hb.TOKEN_HEADER, "")
        return httpx.Response(200, json={
            "worker_id": "w1", "registered": True, "capacity": 2,
            "heartbeat_interval_s": 7.5, "heartbeat_ttl_s": 90.0,
            "worker_identity": "fleet-secret"})

    a = _announcer(handler)
    out = await a.register_once()
    assert out is not None and a.registered

    assert seen["url"].endswith(CONTRACT["routes"]["register"]["path"])
    body = json.loads(seen["body"])
    for field in CONTRACT["register_request"]["required_fields"]:
        assert field in body
    assert seen["tok"] == settings.explorer_token
    # The signature verifies over the EXACT bytes sent, under the register
    # scope — and (control) fails verification under any other scope, so the
    # check would catch a signature that covered nothing.
    from app import hmac_auth
    fields = hmac_auth.verify(
        seen["body"], seen["sig"], keyring=settings.keyring(),
        nonces=hmac_auth.NonceStore(), scope=hb.register_scope("w1"))
    assert fields["kid"]
    with pytest.raises(hmac_auth.SignatureError):
        hmac_auth.verify(
            seen["body"], seen["sig"], keyring=settings.keyring(),
            nonces=hmac_auth.NonceStore(), scope=hb.heartbeat_scope("w1"))
    # The advertised interval is adopted — the interval lives in ONE place.
    assert a._interval_from(out, 30.0) == 7.5


@pytest.mark.asyncio
async def test_heartbeat_reports_what_the_worker_is_actually_running(monkeypatch):
    _configured(monkeypatch)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "worker_id": "w1", "acknowledged": True,
            "heartbeat_interval_s": 30.0, "heartbeat_ttl_s": 90.0})

    ok, body = await _announcer(handler).beat_once()
    assert ok and body["acknowledged"]
    assert seen["body"]["in_flight"] == 1, (
        "the heartbeat did not carry the JobManager's live count — qe-central "
        "could never heal slot-accounting drift from the worker's own truth")
    for field in CONTRACT["heartbeat_request"]["required_fields"]:
        assert field in seen["body"]


@pytest.mark.asyncio
async def test_a_404_heartbeat_means_re_register(monkeypatch):
    """The contract's unknown_worker rule: 404 ⇒ the registry was reset ⇒
    re-register declaring capacity and affinity, never resurrect-by-default."""
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unknown worker"})

    a = _announcer(handler)
    a.registered = True
    ok, body = await a.beat_once()
    assert ok is False and body is None
    assert a.registered is False, (
        "a 404 heartbeat left the announcer believing it is registered — it "
        "would beat into the void forever instead of re-registering")


@pytest.mark.asyncio
async def test_a_transient_transport_error_does_not_trigger_re_registration(monkeypatch):
    """FALSIFICATION CONTROL for the 404 rule: a network blip must KEEP the
    registration (re-registering resets in_flight to 0 on the server, which
    would wrongly zero a busy worker's accounting)."""
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    a = _announcer(handler)
    a.registered = True
    ok, body = await a.beat_once()
    assert ok is True and a.registered is True


def test_the_announcer_refuses_to_run_unconfigured(monkeypatch):
    """No allowlist path ⇒ no registration: qe-central could not fence the
    worker, so it must never be offered work. The reason NAMES the variable."""
    _configured(monkeypatch, worker_allowlist_path="")
    reason = hb.FleetAnnouncer(http=None, jobs=_Jobs()).disabled_reason()
    assert "QEC_WORKER_ALLOWLIST_PATH" in reason

    _configured(monkeypatch, fleet_register=False)
    assert "QEC_FLEET_REGISTER" in hb.FleetAnnouncer(
        http=None, jobs=_Jobs()).disabled_reason()

    # CONTROL: fully configured ⇒ no refusal, or the two above prove nothing.
    _configured(monkeypatch)
    assert hb.FleetAnnouncer(http=None, jobs=_Jobs()).disabled_reason() == ""
