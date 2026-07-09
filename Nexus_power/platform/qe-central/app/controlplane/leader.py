"""QE-Central Phase-5.5 — daemon leader election (Postgres advisory lock).

WHY
===
The cycle-driver daemon (``app.controlplane.cycle.driver.cycle_driver_daemon``)
scans the WHOLE fleet each tick and fires cycles.  Run N replicas of qe-central
and N daemons each scan and each try to fire — the ``app_cycles`` partial unique
index stops a duplicate ACTIVE cycle, but N daemons still double-scan and race.
Leader election elects exactly ONE daemon across the fleet.

HOW
===
A Postgres SESSION-level ``pg_advisory_lock`` on a fixed 64-bit key derived from
a stable string.  Exactly one replica holds it at a time; the lock is bound to
the holding DB SESSION (connection), so on leader death — crash, network drop,
container kill — Postgres AUTO-RELEASES it when the connection closes and a
follower's next ``pg_try_advisory_lock`` succeeds.  No lease renewal, no clock,
no split-brain window beyond a TCP timeout.

BACKWARD-COMPATIBLE / OPT-IN
============================
``QEC_DAEMON_LEADER_ELECTION=none`` (the default) makes :attr:`is_leader` ALWAYS
true and :meth:`acquire` a no-op success — byte-for-byte today's single-daemon
behavior, no DB session held.  ``advisory_lock`` turns on real election.  The
engine is injected (defaults to the qecentral engine) so the pure election logic
is unit-testable with a fake connection; the real ``pg_advisory_lock`` round-trip
is proven by a DB-gated integration test.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Awaitable, Callable, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


# ── Modes ──────────────────────────────────────────────────────────────────
LEADER_MODE_NONE = "none"
LEADER_MODE_ADVISORY_LOCK = "advisory_lock"
LEADER_MODES = frozenset({LEADER_MODE_NONE, LEADER_MODE_ADVISORY_LOCK})


def lock_key_from_string(name: str) -> int:
    """Map a stable string to a deterministic signed 64-bit advisory-lock key.

    ``pg_advisory_lock(bigint)`` takes a signed 64-bit integer; we hash the name
    (blake2b, 8-byte digest) and read it as a signed int so the SAME string always
    maps to the SAME lock across every replica and process restart.
    """
    digest = hashlib.blake2b(str(name).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


class LeaderElection:
    """Single-leader election over a Postgres session-level advisory lock.

    In ``none`` mode this is inert (always leader).  In ``advisory_lock`` mode a
    dedicated DB connection is held open for the whole duration of leadership so
    the session lock persists; :meth:`release` (or the connection dropping on
    crash) frees it for a follower.
    """

    def __init__(
        self,
        *,
        mode: str = LEADER_MODE_NONE,
        lock_key_str: str = "qec-cycle-driver-leader",
        engine=None,
        retry_interval_seconds: float = 15.0,
    ) -> None:
        self._mode = str(mode or LEADER_MODE_NONE).strip().lower()
        if self._mode not in LEADER_MODES:
            logger.warning(
                "qec.leader.unknown_mode",
                extra={"mode": self._mode, "detail": "falling back to 'none'"},
            )
            self._mode = LEADER_MODE_NONE
        self._lock_key_str = str(lock_key_str or "qec-cycle-driver-leader")
        self._lock_key = lock_key_from_string(self._lock_key_str)
        self._engine = engine  # None ⇒ resolved lazily to the qecentral engine
        self._retry_interval = max(0.1, float(retry_interval_seconds))
        self._conn = None
        self._is_leader = False

    # ── introspection ───────────────────────────────────────────────────────
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def lock_key(self) -> int:
        return self._lock_key

    @property
    def is_leader(self) -> bool:
        """True when this instance may run leader-only work.

        Always True in ``none`` mode (today's behavior); in ``advisory_lock`` mode
        True only while this instance holds the lock.
        """
        if self._mode != LEADER_MODE_ADVISORY_LOCK:
            return True
        return self._is_leader

    def _resolve_engine(self):
        if self._engine is None:
            from ..db import qec_engine  # lazy: keep import-time side-effect-free
            self._engine = qec_engine
        return self._engine

    # ── election ─────────────────────────────────────────────────────────────
    async def acquire(self) -> bool:
        """Try to become leader (non-blocking ``pg_try_advisory_lock``).

        ``none`` mode always succeeds.  ``advisory_lock`` opens a dedicated
        connection and attempts the lock; on success the connection is retained
        (holding the session lock) and True is returned; on contention or any DB
        error the connection is closed and False is returned.  Idempotent: an
        instance that already holds leadership returns True without a round-trip.
        """
        if self._mode != LEADER_MODE_ADVISORY_LOCK:
            self._is_leader = True
            return True
        if self._is_leader:
            return True

        engine = self._resolve_engine()
        try:
            conn = await engine.connect()
        except Exception as exc:  # noqa: BLE001 - unreachable DB ⇒ not leader
            logger.warning("qec.leader.connect_failed", extra={"error": str(exc)[:300]})
            return False

        try:
            # AUTOCOMMIT so no transaction is left idle-in-transaction while we
            # hold the (session-level, txn-independent) advisory lock.
            try:
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            except Exception:  # noqa: BLE001 - optional; the lock works regardless
                pass
            got = (await conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": self._lock_key},
            )).scalar()
        except Exception as exc:  # noqa: BLE001 - DB error ⇒ close + not leader
            logger.warning("qec.leader.try_lock_failed", extra={"error": str(exc)[:300]})
            await self._safe_close(conn)
            return False

        if got:
            self._conn = conn
            self._is_leader = True
            logger.warning(
                "qec.leader.acquired",
                extra={"lock_key": self._lock_key, "key_str": self._lock_key_str},
            )
            return True

        await self._safe_close(conn)
        logger.info(
            "qec.leader.follower",
            extra={"lock_key": self._lock_key, "key_str": self._lock_key_str},
        )
        return False

    async def release(self) -> None:
        """Release leadership: unlock + close the held session (idempotent)."""
        if self._mode != LEADER_MODE_ADVISORY_LOCK:
            self._is_leader = False
            return
        conn = self._conn
        self._conn = None
        self._is_leader = False
        if conn is None:
            return
        try:
            await conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": self._lock_key},
            )
        except Exception as exc:  # noqa: BLE001 - close still frees the session lock
            logger.warning("qec.leader.unlock_failed", extra={"error": str(exc)[:300]})
        await self._safe_close(conn)
        logger.info("qec.leader.released", extra={"lock_key": self._lock_key})

    async def _safe_close(self, conn) -> None:
        try:
            await conn.close()
        except Exception:  # noqa: BLE001 - best-effort; a dropped conn frees the lock
            pass

    # ── the daemon wrapper ────────────────────────────────────────────────────
    async def run_as_leader(
        self,
        work: Callable[[], Awaitable[None]],
        *,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Run ``work`` (a coroutine factory) ONLY while this instance is leader.

        ``none`` mode runs ``work`` immediately (today's behavior).
        ``advisory_lock`` mode retries acquisition every
        ``retry_interval_seconds`` as a follower; once leader it runs ``work``
        (typically the daemon loop, which returns only on cancellation) and
        releases leadership when it returns or raises.  Pass ``stop_event`` for a
        clean, promptly-interruptible shutdown.
        """
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            if await self.acquire():
                try:
                    await work()
                finally:
                    await self.release()
                return
            # Follower — wait, but wake immediately if asked to stop.
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._retry_interval)
                    return
                except asyncio.TimeoutError:
                    continue
            await asyncio.sleep(self._retry_interval)


def build_leader_election(*, settings_obj=None) -> LeaderElection:
    """Construct a :class:`LeaderElection` from config (the wiring seam).

    ``main.lifespan`` can wrap the daemon with
    ``build_leader_election().run_as_leader(cycle_driver_daemon)`` — in the
    default ``none`` mode this is exactly today's ``asyncio.create_task`` of the
    daemon.
    """
    if settings_obj is None:
        from ..config import settings as settings_obj  # lazy: import-safe

    return LeaderElection(
        mode=getattr(settings_obj, "qec_daemon_leader_election", LEADER_MODE_NONE),
        lock_key_str=getattr(settings_obj, "qec_leader_lock_key", "qec-cycle-driver-leader"),
        retry_interval_seconds=float(
            getattr(settings_obj, "qec_leader_retry_interval_seconds", 15.0) or 15.0
        ),
    )


__all__ = [
    "LEADER_MODE_NONE",
    "LEADER_MODE_ADVISORY_LOCK",
    "LEADER_MODES",
    "LeaderElection",
    "build_leader_election",
    "lock_key_from_string",
]
