"""QE-Central Phase-5.5 — daemon leader-election tests.

Proves the election contract WITHOUT a real database (a fake engine that models
Postgres session-level ``pg_advisory_lock`` semantics: exactly one connection
holds the lock; a connection close — graceful OR a crash — frees it):

  * **``none`` mode is inert** — always leader, no DB session, ``acquire`` is a
    no-op success (byte-for-byte today's single-daemon behavior);
  * **``advisory_lock`` elects ONE leader** — a second replica contends and
    becomes a follower;
  * **failover** — releasing (or the holder's session dropping on crash) lets the
    next replica acquire;
  * **``run_as_leader``** — runs work only while leader, retries as a follower,
    and releases when the work returns;
  * **stable key** — a string maps to the SAME signed 64-bit key deterministically.

An OPTIONAL integration test (skipif ``QEC_TEST_DATABASE_URL``) contends for the
REAL ``pg_advisory_lock`` on a live Postgres.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.controlplane.leader import (
    LEADER_MODE_ADVISORY_LOCK,
    LEADER_MODE_NONE,
    LeaderElection,
    build_leader_election,
    lock_key_from_string,
)


def run(coro):
    return asyncio.run(coro)


# ══════════════════════ fake advisory-lock engine ══════════════════════════
class _FakeAdvisoryServer:
    """Models one Postgres server's advisory-lock table: ONE holder connection."""

    def __init__(self) -> None:
        self.holder = None
        self.unlock_calls = 0


class _FakeResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    """A DB session bound to one connection id.  Holds a session-level advisory
    lock until unlocked OR the connection closes (graceful/crash)."""

    def __init__(self, server: _FakeAdvisoryServer, cid: int) -> None:
        self._server = server
        self._cid = cid
        self._holds = False
        self.closed = False

    def execution_options(self, **_kw):
        return self  # AUTOCOMMIT no-op

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            if self._server.holder in (None, self._cid):
                self._server.holder = self._cid
                self._holds = True
                return _FakeResult(True)
            return _FakeResult(False)
        if "pg_advisory_unlock" in sql:
            self._server.unlock_calls += 1
            if self._server.holder == self._cid:
                self._server.holder = None
                self._holds = False
                return _FakeResult(True)
            return _FakeResult(False)
        return _FakeResult(None)

    async def close(self):
        self.closed = True
        # A closing/crashing session frees a held session-level lock.
        if self._holds and self._server.holder == self._cid:
            self._server.holder = None
            self._holds = False


class _FakeEngine:
    """Hands out fresh connections (distinct sessions) sharing one lock server."""

    def __init__(self, server: _FakeAdvisoryServer) -> None:
        self._server = server
        self._n = 0

    async def connect(self):
        self._n += 1
        return _FakeConn(self._server, self._n)


def _election(engine, **kw) -> LeaderElection:
    kw.setdefault("mode", LEADER_MODE_ADVISORY_LOCK)
    kw.setdefault("lock_key_str", "qec-test-leader")
    kw.setdefault("retry_interval_seconds", 0.02)
    return LeaderElection(engine=engine, **kw)


# ══════════════════════ none mode (default = today) ════════════════════════
class TestNoneMode:
    def test_always_leader_without_engine(self):
        e = LeaderElection(mode=LEADER_MODE_NONE)  # no engine needed
        assert e.is_leader is True

        async def body():
            assert await e.acquire() is True
            assert e.is_leader is True
            await e.release()  # no-op; the property is still True in none mode
            assert e.is_leader is True

        run(body())

    def test_unknown_mode_falls_back_to_none(self):
        e = LeaderElection(mode="banana")
        assert e.mode == LEADER_MODE_NONE
        assert e.is_leader is True

    def test_run_as_leader_runs_immediately(self):
        e = LeaderElection(mode=LEADER_MODE_NONE)
        ran = []

        async def work():
            ran.append(1)

        run(e.run_as_leader(work))
        assert ran == [1]


# ══════════════════════ advisory-lock election ═════════════════════════════
class TestAdvisoryLock:
    def test_single_leader_and_follower(self):
        server = _FakeAdvisoryServer()
        engine = _FakeEngine(server)
        e1, e2 = _election(engine), _election(engine)

        async def body():
            assert await e1.acquire() is True
            assert e1.is_leader is True
            # Second replica contends and becomes a follower.
            assert await e2.acquire() is False
            assert e2.is_leader is False
            # Idempotent: the holder re-acquiring is a no-op success.
            assert await e1.acquire() is True

        run(body())

    def test_release_enables_failover(self):
        server = _FakeAdvisoryServer()
        engine = _FakeEngine(server)
        e1, e2 = _election(engine), _election(engine)

        async def body():
            assert await e1.acquire() is True
            assert await e2.acquire() is False
            await e1.release()
            assert e1.is_leader is False
            assert server.holder is None
            # The follower can now take over.
            assert await e2.acquire() is True
            assert e2.is_leader is True

        run(body())

    def test_crash_frees_lock_for_next_replica(self):
        server = _FakeAdvisoryServer()
        engine = _FakeEngine(server)
        e1, e2 = _election(engine), _election(engine)

        async def body():
            assert await e1.acquire() is True
            # Simulate a crash: the holding session's connection drops WITHOUT a
            # graceful release() — Postgres auto-frees the session lock.
            await e1._conn.close()
            assert server.holder is None
            assert await e2.acquire() is True
            assert e2.is_leader is True

        run(body())

    def test_run_as_leader_runs_then_releases(self):
        server = _FakeAdvisoryServer()
        engine = _FakeEngine(server)
        e = _election(engine)
        ran = []

        async def work():
            ran.append(1)

        run(e.run_as_leader(work))
        assert ran == [1]
        assert e.is_leader is False
        assert server.holder is None  # released after work returned

    def test_run_as_leader_waits_as_follower_and_never_runs_blocked_work(self):
        server = _FakeAdvisoryServer()
        engine = _FakeEngine(server)
        blocker = _election(engine)
        follower = _election(engine, retry_interval_seconds=0.02)
        ran = []

        async def work():
            ran.append(1)

        async def body():
            assert await blocker.acquire() is True  # hold the lock
            stop = asyncio.Event()
            task = asyncio.create_task(follower.run_as_leader(work, stop_event=stop))
            await asyncio.sleep(0.06)   # let it try + wait as a follower a few times
            assert ran == []            # follower NEVER runs leader-only work
            assert follower.is_leader is False
            stop.set()                  # ask it to stop
            await asyncio.wait_for(task, timeout=1.0)
            assert ran == []

        run(body())


# ══════════════════════ lock key derivation ════════════════════════════════
class TestLockKey:
    def test_stable_and_signed_64bit(self):
        k = lock_key_from_string("qec-cycle-driver-leader")
        assert k == lock_key_from_string("qec-cycle-driver-leader")  # deterministic
        assert lock_key_from_string("a") != lock_key_from_string("b")
        assert -(2 ** 63) <= k < 2 ** 63  # fits a Postgres bigint

    def test_election_exposes_its_key(self):
        e = LeaderElection(mode=LEADER_MODE_ADVISORY_LOCK, lock_key_str="xyz")
        assert e.lock_key == lock_key_from_string("xyz")


# ══════════════════════ factory ════════════════════════════════════════════
class TestBuildLeaderElection:
    def test_defaults_to_none_mode(self):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            qec_daemon_leader_election="none",
            qec_leader_lock_key="qec-cycle-driver-leader",
            qec_leader_retry_interval_seconds=15.0,
        )
        e = build_leader_election(settings_obj=cfg)
        assert e.mode == LEADER_MODE_NONE
        assert e.is_leader is True

    def test_advisory_lock_mode_from_config(self):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            qec_daemon_leader_election="advisory_lock",
            qec_leader_lock_key="fleet-42",
            qec_leader_retry_interval_seconds=5.0,
        )
        e = build_leader_election(settings_obj=cfg)
        assert e.mode == LEADER_MODE_ADVISORY_LOCK
        assert e.lock_key == lock_key_from_string("fleet-42")


# ══════════════════════ OPTIONAL live-Postgres integration ═════════════════
_TEST_DB_URL = os.getenv("QEC_TEST_DATABASE_URL")


@pytest.mark.skipif(
    not _TEST_DB_URL,
    reason="requires QEC_TEST_DATABASE_URL (real pg_advisory_lock path)",
)
def test_real_pg_advisory_lock_contention():  # pragma: no cover - live only
    import uuid

    from sqlalchemy.ext.asyncio import create_async_engine

    async def body():
        engine = create_async_engine(_TEST_DB_URL)
        key_str = f"qec-test-{uuid.uuid4().hex}"
        e1 = LeaderElection(
            mode=LEADER_MODE_ADVISORY_LOCK, lock_key_str=key_str, engine=engine,
        )
        e2 = LeaderElection(
            mode=LEADER_MODE_ADVISORY_LOCK, lock_key_str=key_str, engine=engine,
        )
        try:
            assert await e1.acquire() is True
            assert await e2.acquire() is False  # real contention
            await e1.release()
            assert await e2.acquire() is True   # failover
            await e2.release()
        finally:
            await engine.dispose()

    run(body())
