"""T-FE-09 — LEARNING MUST SURVIVE MORE THAN ONE CRAWL.

The defect, precisely: ``tp_field_memory`` was keyed
``(tenant_id, artifact_id, signature)``, the ciphertext was bound to the
artifact through its AAD, and a re-crawl MINTS A NEW ARTIFACT.  So crawl N wrote
its answers under artifact N; crawl N+1 read artifact N (the "latest completed"
one) and wrote to artifact N+1; and crawl N+2 could no longer see anything crawl
N had learned.  Each crawl inherited exactly one generation of memory and then
dropped it — which, from the outside, looks exactly like learning that works.

These tests drive the real store against an in-memory SQLite database and a fake
envelope service that ENFORCES AAD the way the real one does.  A fake that
ignored it would let every test pass while the real service silently failed
every decrypt — and that failure is invisible from the outside, because an
unreadable memory is indistinguishable from an empty one.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.services.test_factory import field_learning as fl

TENANT = "acme"
APP = "summit-life"
SIG = "a" * 32


class _Blob:
    def __init__(self, raw: bytes):
        self.raw = bytes(raw)

    def to_bytes(self) -> bytes:
        return self.raw

    @classmethod
    def from_bytes(cls, raw: bytes) -> "_Blob":
        return cls(bytes(raw))


class FakeEnvelope:
    """Encryption that actually enforces the AAD."""

    async def encrypt(self, tenant_id, payload, *, aad):
        return _Blob(tenant_id.encode() + b"|" + aad + b"|" + payload)

    async def decrypt(self, tenant_id, blob, *, expected_aad):
        prefix = tenant_id.encode() + b"|" + expected_aad + b"|"
        if not blob.raw.startswith(prefix):
            raise ValueError("AAD mismatch — this blob was sealed for another scope")
        return blob.raw[len(prefix):]


class Store:
    """A real table, a real session, and ONE event loop THIS test owns.

    ``asyncio.get_event_loop()`` hands back whichever loop the process happens to
    be holding, and another test in the suite may already have closed it — the
    engine is then created on one loop and used on another, which passes when
    the file runs alone and errors when it runs in the suite.  Owning the loop
    here removes the coupling entirely."""

    def __init__(self, loop, engine, maker):
        self.loop, self.engine, self.maker = loop, engine, maker

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    # ── the operations under test, at the level the endpoint calls them ──
    def remember(self, *, artifact, app_id, value, signature=SIG):
        async def _go():
            async with self.maker() as session:
                out = await fl.remember(
                    session, envelope=FakeEnvelope(), tenant_id=TENANT,
                    artifact_id=artifact, app_id=app_id, signature=signature,
                    value=value, semantic_type="postal_code", field_label="ZIP")
                await session.commit()
                return out
        return self.run(_go())

    def recall(self, *, artifact, app_id):
        async def _go():
            async with self.maker() as session:
                return await fl.recall(session, envelope=FakeEnvelope(),
                                       tenant_id=TENANT, artifact_id=artifact,
                                       app_id=app_id)
        return self.run(_go())

    def outcome(self, *, artifact, app_id, accepted, signature=SIG):
        async def _go():
            async with self.maker() as session:
                await fl.record_outcome(session, tenant_id=TENANT,
                                        artifact_id=artifact, app_id=app_id,
                                        signature=signature, accepted=accepted)
                await session.commit()
        return self.run(_go())

    def row(self):
        async def _go():
            async with self.maker() as session:
                return (await session.execute(
                    select(fl.TpFieldMemoryRow))).scalars().first()
        return self.run(_go())


@pytest.fixture()
def store(monkeypatch):
    monkeypatch.setattr(fl, "EnvelopeBlob", _Blob)
    loop = asyncio.new_event_loop()

    async def _build():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: fl.TpFieldMemoryRow.__table__.create(c, checkfirst=True))
        return engine, sessionmaker(engine, class_=AsyncSession,
                                    expire_on_commit=False)

    engine, maker = loop.run_until_complete(_build())
    handle = Store(loop, engine, maker)
    try:
        yield handle
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()


# ── the headline: memory outlives the crawl that produced it ─────────────────

def test_learning_survives_repeated_crawls(store):
    """Every crawl carries a DIFFERENT artifact.  Under the old key this
    returned nothing from the second crawl onward."""
    store.remember(artifact="artifact-1", app_id=APP, value="94112")
    for crawl in range(2, 8):
        assert store.recall(artifact=f"artifact-{crawl}", app_id=APP) \
            == {SIG: "94112"}, f"memory lost by crawl {crawl}"


def test_a_second_application_of_the_same_tenant_is_isolated(store):
    # Distinct artifacts because that is what reality produces: an artifact is
    # minted by one crawl of one application and is never shared between two.
    store.remember(artifact="a1", app_id=APP, value="94112")
    store.remember(artifact="b1", app_id="other-product", value="10001")
    assert store.recall(artifact="a9", app_id=APP) == {SIG: "94112"}
    assert store.recall(artifact="b9", app_id="other-product") == {SIG: "10001"}


def test_a_rewrite_under_the_same_application_replaces_rather_than_duplicates(store):
    store.remember(artifact="a1", app_id=APP, value="94112")
    store.remember(artifact="a2", app_id=APP, value="10001")
    assert store.recall(artifact="a3", app_id=APP) == {SIG: "10001"}


# ── nothing already stored is lost ───────────────────────────────────────────

def test_a_legacy_artifact_keyed_row_is_still_readable(store):
    """Rewriting ``artifact_id`` in place would have made every existing blob
    undecryptable — a silent, total loss indistinguishable from "we never knew"."""
    store.remember(artifact="legacy-artifact", app_id="", value="60601")
    assert store.recall(artifact="legacy-artifact", app_id=APP) == {SIG: "60601"}


def test_a_legacy_row_is_superseded_the_next_time_the_field_is_remembered(store):
    """A migration that costs nothing and cannot lose anything."""
    store.remember(artifact="legacy-artifact", app_id="", value="60601")
    store.remember(artifact="new-artifact", app_id=APP, value="94112")
    assert store.recall(artifact="new-artifact", app_id=APP) == {SIG: "94112"}


def test_the_durable_row_wins_when_both_scopes_hold_a_value(store):
    store.remember(artifact="legacy-artifact", app_id="", value="legacy")
    store.remember(artifact="new-artifact", app_id=APP, value="durable")
    assert store.recall(artifact="legacy-artifact", app_id=APP) == {SIG: "durable"}


# ── the encryption binding is real ───────────────────────────────────────────

def test_a_value_learned_for_one_application_never_reaches_another(store):
    """Even when the artifact matches.  A row that carries an application belongs
    to that application and to no other — the same class of leak as crossing
    tenants, and just as unrecoverable."""
    store.remember(artifact="a1", app_id=APP, value="94112")
    assert store.recall(artifact="a1", app_id="a-different-app") == {}


def test_the_two_aad_forms_are_different_shapes_not_just_different_values():
    legacy = fl._aad(TENANT, "artifact-1", SIG)
    scoped = fl._app_aad(TENANT, APP, SIG)
    assert legacy != scoped
    assert b"::app::" in scoped and b"::app::" not in legacy
    assert scoped.startswith(f"fieldmem/v{fl.SCOPE_VERSION}::".encode())


def test_a_scope_version_is_stored_so_old_rows_stay_identifiable(store):
    store.remember(artifact="a1", app_id=APP, value="94112")
    row = store.row()
    assert (row.app_id, row.scope_version) == (APP, fl.SCOPE_VERSION)
    assert row.artifact_id == "a1", "the per-run handle is kept for provenance"


# ── the application's verdict still closes the loop ──────────────────────────

def test_a_value_the_application_rejects_stops_being_offered(store):
    """A remembered wrong answer is worse than no answer: it looks like one."""
    store.remember(artifact="a1", app_id=APP, value="94112")
    store.outcome(artifact="a2", app_id=APP, accepted=False)
    assert store.recall(artifact="a3", app_id=APP) == {}


def test_the_verdict_reaches_a_row_written_under_the_application(store):
    store.remember(artifact="a1", app_id=APP, value="94112")
    store.outcome(artifact="", app_id=APP, accepted=True)
    row = store.row()
    assert (row.accept_count, row.last_outcome) == (1, "accepted")
