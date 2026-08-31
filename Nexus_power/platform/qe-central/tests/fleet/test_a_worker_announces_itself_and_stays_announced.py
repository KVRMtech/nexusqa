"""TEAM A / PHASE A (A1) — a worker ANNOUNCES itself and STAYS announced.

THE QE-CENTRAL HALF of the frozen fleet heartbeat contract
(``contracts/fleet_heartbeat_v1.json``). The explorer's producer half is
``engines/qe-explorer/tests/test_heartbeat_contract.py``; the two services
cannot share an interpreter, so each asserts the contract in its own process
and together the files are one proof.

WHAT THIS FILE PROVES, against the REAL routes and the REAL store:

  * the routes exist at exactly the contract paths, behind the scope-bound
    signature (an unsigned caller is refused BEFORE any validation leaks);
  * a registration the fleet cannot fence (no absolute allowlist_path, bad id,
    absurd capacity) is refused 422 with a named reason — fail-closed toward
    the static pool, never a half-registered worker;
  * the A1 lifecycle end to end: register → the row EXISTS and is schedulable;
    heartbeat → liveness + the worker's own in_flight; silence past the TTL →
    the sweeper RETIRES the row (status='stale', unschedulable, visible to an
    operator's SELECT); silence past retention → the row is GONE — which is
    the "drops to 0 within one TTL of stopping it" measurement, in miniature;
  * a heartbeat for an unknown worker answers 404 telling it to RE-REGISTER —
    the contract's registry-was-reset rule.

Route-level tests build a minimal FastAPI app around the REAL router. That is
the correct boundary here: the /internal prefix token middleware is main.py's
and is already pinned by the T-SEC-02 suite; what THIS seam owns is the
signature + validation + store write, and that is what is driven.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")
if QEC_DB_URL:
    os.environ["QEC_DATABASE_URL"] = QEC_DB_URL
    os.environ["QEC_TEST_DB_NULLPOOL"] = "1"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.clients.config import SIGNATURE_HEADER, phase1_settings  # noqa: E402
from app.controlplane.scheduling import worker_registry as wr  # noqa: E402
from app.routers.fleet import worker_router  # noqa: E402

needs_db = pytest.mark.skipif(
    not QEC_DB_URL,
    reason=("QEC_TEST_QEC_DATABASE_URL not set — the A1 lifecycle proof needs "
            "the migrated qecentral DB"),
)


def _contract() -> dict:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "fleet_heartbeat_v1.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError(
        "contracts/fleet_heartbeat_v1.json not found above %s — the frozen "
        "wire contract must not be deleted to make a test pass" % here)


CONTRACT = _contract()

_app = FastAPI()
_app.include_router(worker_router)
client = TestClient(_app, raise_server_exceptions=False)


def _signed(path: str, body: dict, *, scope: str) -> "tuple[bytes, dict]":
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return payload, {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: phase1_settings.sign_payload(payload, scope=scope),
    }


def _register_body(wid: str, **over) -> dict:
    body = {
        "schema_version": 1, "worker_id": wid, "url": "http://w:8210",
        "allowlist_path": "/eg/w/aw.txt", "capacity": 2,
        "tenant_affinity": "", "meta": {"fence_mode": "per-crawl"},
    }
    body.update(over)
    return body


# ══════════════════════════════════════════════════════════════════════════
# ROUTE EXISTENCE + AUTH, at the contract paths (no DB needed)
# ══════════════════════════════════════════════════════════════════════════

def test_the_routes_exist_at_exactly_the_contract_paths():
    """A 404 here would be PATH DRIFT — the explorer would announce into a
    void and the registry would stay empty with everything 'green'. (401 is
    the right answer for an unsigned probe: the route exists and refused.)"""
    r = client.post(CONTRACT["routes"]["register"]["path"], json={})
    assert r.status_code == 401, (r.status_code, r.text)
    r = client.post(CONTRACT["routes"]["heartbeat"]["path"].format(worker_id="w1"),
                    json={})
    assert r.status_code == 401, (r.status_code, r.text)


def test_an_unsigned_register_is_refused_before_validation():
    """No signature ⇒ 401, never 422: refusing AFTER validation would let an
    unauthenticated caller enumerate what a valid registration looks like."""
    r = client.post(CONTRACT["routes"]["register"]["path"],
                    json=_register_body("w1"))
    assert r.status_code == 401


def test_a_signature_for_another_worker_does_not_authenticate_this_one():
    """The SCOPE binds the signature to one worker id (contract): a captured
    register for w-other cannot be replayed as w-mine."""
    body = _register_body("w-mine")
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: phase1_settings.sign_payload(
            payload, scope="worker-register:w-other"),
    }
    r = client.post(CONTRACT["routes"]["register"]["path"],
                    content=payload, headers=headers)
    assert r.status_code == 401


def test_a_heartbeat_body_for_a_different_worker_is_refused():
    path = CONTRACT["routes"]["heartbeat"]["path"].format(worker_id="w-path")
    payload, headers = _signed(
        path, {"schema_version": 1, "worker_id": "w-body", "in_flight": 0,
               "capacity": 1, "status": "active"},
        scope="worker-heartbeat:w-path")
    r = client.post(path, content=payload, headers=headers)
    assert r.status_code == 400


def test_a_registration_the_fleet_cannot_fence_is_refused_422():
    for field, bad in [
        ("allowlist_path", ""),            # no fence path ⇒ cannot be fenced
        ("allowlist_path", "relative/aw"),
        ("worker_id", "has spaces"),
        ("url", "ftp://w:21"),
        ("capacity", 0),
        ("capacity", 9999),
    ]:
        body = _register_body("w1", **{field: bad})
        payload, headers = _signed(
            CONTRACT["routes"]["register"]["path"], body,
            scope=f"worker-register:{body['worker_id']}")
        r = client.post(CONTRACT["routes"]["register"]["path"],
                        content=payload, headers=headers)
        assert r.status_code == 422, (
            f"{field}={bad!r} registered anyway ({r.status_code}) — a worker "
            "the fleet cannot fence or address must never be schedulable")


# ══════════════════════════════════════════════════════════════════════════
# THE A1 LIFECYCLE, end to end against the real store
# ══════════════════════════════════════════════════════════════════════════

@needs_db
def test_a_worker_announces_itself_and_stays_announced(monkeypatch):
    """register → schedulable; heartbeat → alive + own in_flight; silence →
    retired at TTL, deleted at retention; 404 → re-register works."""
    wid = "wa1_" + uuid.uuid4().hex[:10]

    # ── ANNOUNCE ──────────────────────────────────────────────────────────
    body = _register_body(wid)
    payload, headers = _signed(CONTRACT["routes"]["register"]["path"], body,
                               scope=f"worker-register:{wid}")
    r = client.post(CONTRACT["routes"]["register"]["path"],
                    content=payload, headers=headers)
    assert r.status_code == 200, r.text
    out = r.json()
    for field in CONTRACT["register_response"]["required_fields"]:
        assert field in out, f"register response lost {field!r}"
    assert out["registered"] is True
    assert out["worker_identity"] == CONTRACT["worker_identity"]

    async def _rows():
        return {w["worker_id"]: w for w in await wr.list_workers()}

    import asyncio
    rows = asyncio.run(_rows())
    assert wid in rows, "the registry does not hold the announced worker"
    assert rows[wid]["capacity"] == 2
    assert rows[wid]["allowlist_path"] == "/eg/w/aw.txt"
    assert wr.eligible_workers([rows[wid]], tenant_id="t",
                               now=wr.utc_now(),
                               ttl_s=wr.heartbeat_ttl_seconds()), (
        "a freshly announced worker is not schedulable")

    # ── STAY ANNOUNCED (heartbeat carries the worker's own truth) ─────────
    hb_path = CONTRACT["routes"]["heartbeat"]["path"].format(worker_id=wid)
    payload, headers = _signed(
        hb_path, {"schema_version": 1, "worker_id": wid, "in_flight": 1,
                  "capacity": 2, "status": "active"},
        scope=f"worker-heartbeat:{wid}")
    r = client.post(hb_path, content=payload, headers=headers)
    assert r.status_code == 200, r.text
    out = r.json()
    for field in CONTRACT["heartbeat_response"]["required_fields"]:
        assert field in out, f"heartbeat response lost {field!r}"
    rows = asyncio.run(_rows())
    assert rows[wid]["in_flight"] == 1, (
        "the worker's own in_flight report did not land — qe-central cannot "
        "heal slot drift from the worker's truth")

    # ── SILENCE PAST THE TTL ⇒ RETIRED (visible in the table) ─────────────
    from sqlalchemy import text as _text

    from app.db import qec_engine

    async def _age(seconds: float):
        async with qec_engine.begin() as conn:
            await conn.execute(_text(
                "UPDATE explorer_workers SET last_heartbeat_at = :p "
                "WHERE worker_id = :w"),
                {"p": wr.utc_now() - timedelta(seconds=seconds), "w": wid})

    asyncio.run(_age(wr.heartbeat_ttl_seconds() + 5))
    retired = asyncio.run(wr.retire_stale_workers())
    assert retired >= 1
    rows = asyncio.run(_rows())
    assert rows[wid]["status"] == wr.STATUS_STALE, (
        "a silent worker was not RETIRED — an operator SELECTing the table "
        "would still read it as active")
    assert not wr.eligible_workers([rows[wid]], tenant_id="t",
                                   now=rows[wid]["last_heartbeat_at"],
                                   ttl_s=1e9), (
        "a retired row was still schedulable even with a generous TTL — the "
        "status is not honoured")

    # ── A HEARTBEAT BRINGS IT BACK (its own status says active) ──────────
    payload, headers = _signed(
        hb_path, {"schema_version": 1, "worker_id": wid, "in_flight": 0,
                  "capacity": 2, "status": "active"},
        scope=f"worker-heartbeat:{wid}")
    r = client.post(hb_path, content=payload, headers=headers)
    assert r.status_code == 200
    rows = asyncio.run(_rows())
    assert rows[wid]["status"] == wr.STATUS_ACTIVE

    # ── SILENCE PAST RETENTION ⇒ GONE (stops being announced) ────────────
    asyncio.run(_age(wr.heartbeat_ttl_seconds() + 5))
    monkeypatch.setenv(wr.ENV_WORKER_RETENTION, "1")
    asyncio.run(wr.retire_stale_workers())
    asyncio.run(wr.reap_stale_workers())
    rows = asyncio.run(_rows())
    assert wid not in rows, (
        "a worker silent past the retention window still has a row — "
        "`SELECT count(*) FROM explorer_workers` would never drop to 0")

    # ── AND THE CONTRACT'S RESET RULE: unknown ⇒ 404 ⇒ re-register ───────
    payload, headers = _signed(
        hb_path, {"schema_version": 1, "worker_id": wid, "in_flight": 0,
                  "capacity": 2, "status": "active"},
        scope=f"worker-heartbeat:{wid}")
    r = client.post(hb_path, content=payload, headers=headers)
    assert r.status_code == 404
    assert "re-register" in r.json()["detail"], (
        "the 404 does not tell the worker WHAT to do — the contract's "
        "registry-was-reset rule")
    body = _register_body(wid)
    payload, headers = _signed(CONTRACT["routes"]["register"]["path"], body,
                               scope=f"worker-register:{wid}")
    assert client.post(CONTRACT["routes"]["register"]["path"],
                       content=payload, headers=headers).status_code == 200
    rows = asyncio.run(_rows())
    assert wid in rows and rows[wid]["in_flight"] == 0, (
        "re-registration did not reset in_flight — a restarted worker runs "
        "nothing, and carrying the old count strands capacity forever")


@needs_db
def test_a_draining_worker_is_not_retired_by_the_sweep():
    """The sweep is guarded on status='active': an operator's chosen state
    (draining for a rolling deploy) must survive staleness — retiring it would
    erase the operator's intent the moment the pod stopped beating."""
    import asyncio
    wid = "wdr_" + uuid.uuid4().hex[:10]
    asyncio.run(wr.register_worker(worker_id=wid, url="http://x:1",
                                   allowlist_path="/eg/x/aw.txt"))
    asyncio.run(wr.heartbeat(worker_id=wid, status=wr.STATUS_DRAINING))

    from sqlalchemy import text as _text

    from app.db import qec_engine

    async def _age():
        async with qec_engine.begin() as conn:
            await conn.execute(_text(
                "UPDATE explorer_workers SET last_heartbeat_at = :p "
                "WHERE worker_id = :w"),
                {"p": wr.utc_now() - timedelta(
                    seconds=wr.heartbeat_ttl_seconds() + 60), "w": wid})
    asyncio.run(_age())
    asyncio.run(wr.retire_stale_workers())
    rows = {w["worker_id"]: w for w in asyncio.run(wr.list_workers())}
    assert rows[wid]["status"] == wr.STATUS_DRAINING
