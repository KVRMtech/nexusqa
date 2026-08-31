"""TEAM A / PHASE A — the worker ANNOUNCES itself and STAYS announced (G1).

Frozen wire shape: ``Nexus_power/contracts/fleet_heartbeat_v1.json`` — read it
first. This is the qe-explorer PRODUCER half; qe-central's consumer half lives
in ``platform/qe-central/app/routers/fleet.py``.

WHAT WAS MISSING. qe-central's worker registry (qec_022) judges liveness from
``last_heartbeat_at`` — but no route existed to write it and this service never
called one. The registry stayed empty on the VM, the queue drainer stayed
disabled, and dispatch fell back to the single static slot: two gate crawls
collided on it in one day. The registry's own docstring designed for exactly
this module; it just never existed.

SHAPE. One background task, started from the lifespan next to the sweeper:

    register (retry with backoff until qe-central answers)
        -> heartbeat every ``heartbeat_interval_s`` AS ADVERTISED BY THE
           RESPONSE (the interval lives in one place: qe-central's
           worker_registry.heartbeat_interval_seconds)
        -> a 404 means the registry no longer knows us (reset/restore):
           RE-REGISTER, declaring capacity and affinity again, rather than be
           resurrected with defaults nobody chose.

Every request carries the per-fleet ``X-QEC-Token`` AND the v2
``X-QEC-Signature`` envelope over the exact bytes, scope-bound to this worker
id — the same discipline as the completion callback, so a captured heartbeat
cannot be replayed as a different worker's. (Worker identity is the shared
fleet secret for now — the Team F seam; the contract says so plainly.)

FAIL-SAFE DIRECTION. This task can only ever ADD a worker to the fleet's view;
every failure path degrades to "not registered", which is exactly today's
static-pool behaviour. It never blocks startup, never raises out of its task,
and reports ``in_flight`` from the JobManager — the worker is the authority on
what it is actually running, which is what heals any slot-accounting drift in
qe-central after a lost release.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any, Optional

import httpx

from .config import settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
REGISTER_PATH = "/internal/fleet/workers/register"
TOKEN_HEADER = "X-QEC-Token"
SIGNATURE_HEADER = "X-QEC-Signature"

#: Lifecycle states the contract admits in a heartbeat.
STATUS_ACTIVE = "active"
STATUS_DRAINING = "draining"

_BACKOFF_START_S = 5.0
_BACKOFF_MAX_S = 60.0
#: Fallback beat interval until a response advertises one.
_DEFAULT_INTERVAL_S = 30.0


def heartbeat_path(worker_id: str) -> str:
    return f"/internal/fleet/workers/{worker_id}/heartbeat"


def register_scope(worker_id: str) -> str:
    return f"worker-register:{worker_id}"


def heartbeat_scope(worker_id: str) -> str:
    return f"worker-heartbeat:{worker_id}"


def default_worker_id() -> str:
    return socket.gethostname() or "qe-explorer"


def register_payload(*, worker_id: str, url: str, allowlist_path: str,
                     capacity: int, tenant_affinity: str = "",
                     meta: Optional[dict] = None) -> dict:
    """The register body, exactly as the contract freezes it."""
    return {
        "schema_version": SCHEMA_VERSION,
        "worker_id": worker_id,
        "url": url,
        "allowlist_path": allowlist_path,
        "capacity": int(capacity),
        "tenant_affinity": tenant_affinity or "",
        "meta": dict(meta or {}),
    }


def heartbeat_payload(*, worker_id: str, in_flight: int, capacity: int,
                      status: str = STATUS_ACTIVE) -> dict:
    """The heartbeat body, exactly as the contract freezes it."""
    return {
        "schema_version": SCHEMA_VERSION,
        "worker_id": worker_id,
        "in_flight": int(in_flight),
        "capacity": int(capacity),
        "status": status,
    }


def encode(body: dict) -> bytes:
    """Canonical bytes for signing — same recipe as the completion callback
    (sorted keys, compact separators), so the signature covers exactly what is
    sent."""
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


class FleetAnnouncer:
    """Register-then-heartbeat loop. One instance per process, one task.

    ``version`` is INJECTED by the caller (main.py owns EXPLORER_VERSION)
    rather than imported: importing main from here closes a runtime import
    cycle (heartbeat → main → heartbeat) that the explorer's import-cycle
    gate rightly refuses.
    """

    def __init__(self, *, http: httpx.AsyncClient, jobs: Any,
                 version: str = "unknown") -> None:
        self._http = http
        self._jobs = jobs
        self._version = str(version or "unknown")
        self.registered = False

    # ── configuration reads (resolved per call so tests can monkeypatch) ──

    @property
    def worker_id(self) -> str:
        return (settings.worker_id or "").strip() or default_worker_id()

    @property
    def worker_url(self) -> str:
        url = (settings.worker_url or "").strip()
        if url:
            return url
        return f"http://{default_worker_id()}:{settings.port}"

    def disabled_reason(self) -> str:
        """Why the announcer will NOT run — empty when it should.

        Fail-safe: an unconfigured announcer degrades to the static-pool
        behaviour, loudly. It never guesses a fence path: registering with an
        empty ``allowlist_path`` would hand qe-central a worker whose fence it
        cannot write, and the registry would rightly refuse it anyway.
        """
        if not settings.fleet_register:
            return "QEC_FLEET_REGISTER=0"
        if not (settings.explorer_token or "").strip():
            return "QEC_EXPLORER_TOKEN unset - nothing can be signed"
        if not (settings.callback_url or "").strip():
            return "QEC_CALLBACK_URL unset"
        if not (settings.worker_allowlist_path or "").strip():
            return ("QEC_WORKER_ALLOWLIST_PATH unset - qe-central could not "
                    "fence this worker, so it must not be offered work")
        return ""

    # ── one signed POST ───────────────────────────────────────────────────

    async def _post(self, path: str, body: dict, *, scope: str) -> httpx.Response:
        payload = encode(body)
        signature = settings.sign_payload(payload, scope=scope)
        url = settings.callback_url.rstrip("/") + path
        return await self._http.post(
            url, content=payload,
            headers={"Content-Type": "application/json",
                     SIGNATURE_HEADER: signature,
                     TOKEN_HEADER: settings.explorer_token})

    async def register_once(self) -> Optional[dict]:
        """One registration attempt. Returns the response body, or None."""
        wid = self.worker_id
        body = register_payload(
            worker_id=wid,
            url=self.worker_url,
            allowlist_path=settings.worker_allowlist_path,
            capacity=settings.explorer_capacity,
            tenant_affinity=settings.worker_tenant_affinity,
            meta={
                "explorer_version": self._version,
                "fence_mode": "per-crawl",
                "hostname": socket.gethostname(),
            },
        )
        try:
            resp = await self._post(REGISTER_PATH, body, scope=register_scope(wid))
        except Exception as exc:
            logger.warning("qec.explorer.register_unreachable worker_id=%s error=%s",
                           wid, str(exc)[:200])
            return None
        if resp.status_code // 100 != 2:
            logger.warning("qec.explorer.register_refused worker_id=%s status=%d body=%s",
                           wid, resp.status_code, resp.text[:200])
            return None
        try:
            out = resp.json()
        except Exception:
            out = {}
        self.registered = True
        logger.warning(
            "qec.explorer.registered worker_id=%s capacity=%d interval_s=%s",
            wid, settings.explorer_capacity, out.get("heartbeat_interval_s"))
        return out if isinstance(out, dict) else {}

    async def beat_once(self, *, status: str = STATUS_ACTIVE) -> tuple[bool, Optional[dict]]:
        """One heartbeat. ``(ok, body)``; ``(False, None)`` on 404 means the
        registry no longer knows us and the caller must re-register."""
        wid = self.worker_id
        body = heartbeat_payload(
            worker_id=wid,
            in_flight=int(getattr(self._jobs, "active_count", 0) or 0),
            capacity=settings.explorer_capacity,
            status=status,
        )
        try:
            resp = await self._post(heartbeat_path(wid), body,
                                    scope=heartbeat_scope(wid))
        except Exception as exc:
            logger.info("qec.explorer.heartbeat_unreachable worker_id=%s error=%s",
                        wid, str(exc)[:200])
            return True, {}       # transient: keep beating, do not re-register
        if resp.status_code == 404:
            logger.warning(
                "qec.explorer.heartbeat_unknown_worker worker_id=%s - the "
                "registry was reset under us; re-registering (declaring "
                "capacity and affinity again)", wid)
            self.registered = False
            return False, None
        if resp.status_code // 100 != 2:
            logger.warning("qec.explorer.heartbeat_refused worker_id=%s status=%d",
                           wid, resp.status_code)
            return True, {}
        try:
            out = resp.json()
        except Exception:
            out = {}
        return True, out if isinstance(out, dict) else {}

    @staticmethod
    def _interval_from(body: Optional[dict], current: float) -> float:
        try:
            v = float((body or {}).get("heartbeat_interval_s") or 0.0)
            return v if v > 0 else current
        except (TypeError, ValueError):
            return current

    async def run(self) -> None:
        """The announcer loop: register (with backoff), then beat forever."""
        reason = self.disabled_reason()
        if reason:
            logger.warning(
                "qec.explorer.fleet_register_disabled reason=%s - this worker "
                "stays OFF the registry; qe-central falls back to the static "
                "pool exactly as before the registry existed", reason)
            return
        interval = _DEFAULT_INTERVAL_S
        backoff = _BACKOFF_START_S
        while True:
            try:
                out = await self.register_once()
                if out is None:
                    await asyncio.sleep(backoff)
                    backoff = min(_BACKOFF_MAX_S, backoff * 2)
                    continue
                backoff = _BACKOFF_START_S
                interval = self._interval_from(out, interval)
                while True:
                    await asyncio.sleep(interval)
                    ok, body = await self.beat_once()
                    if not ok:
                        break             # 404 -> outer loop re-registers
                    interval = self._interval_from(body, interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - the loop survives anything
                logger.warning("qec.explorer.announcer_tick_failed", exc_info=True)
                await asyncio.sleep(_BACKOFF_START_S)
