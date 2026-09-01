"""A11.5 — the shared harness for the attestation red-team suite.

THE ONE DESIGN DECISION IN THIS FILE
====================================
These tests verify against the REAL, SHIPPING explorer verifier —
``engines/qe-explorer/app/attest.py`` — loaded from source by path, not against
a copy, a mock, or a frozen fixture.

That is possible because ``attest.py`` imports nothing from its own package: it
is stdlib plus pydantic all the way down.  So it can be loaded into this
interpreter under a distinct module name without dragging in the explorer's
``app`` package, which would otherwise collide with qe-central's own top-level
``app``.  (That collision is why the Gate-1 contract test freezes the seam as
DATA instead; this suite is the complementary half — same seam, checked live.)

WHY IT MATTERS FOR CERTIFICATION.  A red-team suite that verifies against its
own idea of a verifier proves nothing about production.  Every ``rejected``
below is a rejection by the same bytes that run on a crawl worker, so an
independent squad re-running this suite is re-running the real decision.  If
somebody edits ``attest.py``, these tests change behaviour immediately — which
is the point.

EVERYTHING ELSE IS REAL TOO
===========================
* real Ed25519 (``cryptography``), never a stub signature;
* real AES-GCM envelope sealing via ``EnvelopeService`` + ``LocalKekProvider``,
  so key custody is exercised rather than bypassed;
* the real issuer gates in ``app.services.attestation_issuer``.

The ONLY fake is the database session (:class:`FakeSession`), because the gate
logic under test is pure decision-making over rows and a live Postgres would
turn a security suite into an infrastructure dependency.  The fake dispatches on
the ORM entity in the statement, so it exercises the production queries rather
than standing in for them.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ── the REAL explorer verifier, loaded from source ──────────────────────────

_EXPLORER_ATTEST = (
    pathlib.Path(__file__).resolve().parents[4]
    / "engines" / "qe-explorer" / "app" / "attest.py"
)


def load_real_verifier():
    """Import ``qe-explorer/app/attest.py`` under a non-colliding module name.

    Fails LOUDLY if the file has moved.  A red-team suite that silently fell
    back to a local copy when it could not find production would report green on
    a verifier nobody ships.
    """
    if not _EXPLORER_ATTEST.is_file():
        raise AssertionError(
            f"the production verifier is not at {_EXPLORER_ATTEST}. This suite "
            f"certifies the SHIPPING verifier and must not be repointed at a "
            f"copy to make it run.")
    name = "qec_a11_real_attest"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _EXPLORER_ATTEST)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


attest = load_real_verifier()


# ── real envelope encryption (KMS stand-in with real AES-GCM) ───────────────


def make_envelope_service(tmp_path):
    """A real ``EnvelopeService`` over a real ``LocalKekProvider``.

    Not a mock.  Sealing and unsealing are genuine AES-GCM with a real wrapped
    DEK and a real AAD binding, so :mod:`app.services.attestation_keys` is
    exercised end to end — including the AAD mismatch path, which a mock that
    just echoed bytes would silently skip.

    ``LocalKekProvider`` stands in for Cloud KMS here only in WHERE the KEK
    lives.  The envelope format, the AAD binding and the unwrap path are
    identical to production; what a real KMS adds is that the KEK is not on
    disk, which is a deployment property no unit test can assert.
    """
    from nexus_sdk.security.envelope import EnvelopeService, LocalKekProvider

    os.environ.setdefault("NEXUS_ENV", "test")
    key_path = pathlib.Path(tmp_path) / "kek" / "master.key"
    return EnvelopeService(LocalKekProvider(master_key_path=str(key_path)))


# ── a session fake that dispatches on the real ORM entity ───────────────────


class _Update:
    """Applies a real ``sqlalchemy.update()`` to the in-memory rows."""

    def __init__(self, session, stmt) -> None:
        entity = stmt.entity_description["entity"]
        rows = [r for r in session.rows.get(entity.__name__, [])
                if _matches(stmt, r)]
        values = {c.key if hasattr(c, "key") else str(c): v
                  for c, v in stmt._values.items()}
        for row in rows:
            for name, value in values.items():
                setattr(row, name, getattr(value, "value", value))
        self.rowcount = len(rows)


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = list(rows)

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        if len(self._rows) > 1:
            raise AssertionError(
                "scalar_one_or_none matched multiple rows — the production "
                "query expects at most one, and a fake that hid that would hide "
                "a real bug")
        return self._rows[0]

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


@dataclass
class FakeSession:
    """In-memory stand-in for ``AsyncSession``, dispatching on ORM entity.

    Deliberately NOT a general query engine.  It applies the WHERE clauses this
    codebase actually issues, by evaluating them against stored rows through the
    same column objects the production code used.  Anything it cannot evaluate
    raises rather than silently matching everything — a fake that over-matches
    turns a failing gate into a passing test.
    """

    rows: dict = field(default_factory=dict)
    #: Set to an exception to make every read fail — used to prove the
    #: fail-closed path when revocation state cannot be determined.
    fail_reads_with: Optional[Exception] = None
    committed: bool = False
    added: list = field(default_factory=list)

    def seed(self, row: Any) -> Any:
        self.rows.setdefault(type(row).__name__, []).append(row)
        return row

    async def execute(self, stmt):
        if self.fail_reads_with is not None:
            raise self.fail_reads_with
        if not hasattr(stmt, "column_descriptions"):
            # An UPDATE (``keys.revoke_issuer_key``). Applied in place against
            # the stored rows so the production statement is exercised rather
            # than stubbed out.
            return _Update(self, stmt)
        entity = stmt.column_descriptions[0]["entity"]
        candidates = list(self.rows.get(entity.__name__, []))
        matched = [r for r in candidates if _matches(stmt, r)]
        # A column-list select (``select(A.x, A.y)``) yields tuples, matching
        # what the production revocation reader destructures.
        descs = stmt.column_descriptions
        if len(descs) > 1 or descs[0].get("expr") is not entity:
            cols = [d["name"] for d in descs]
            return _Result([tuple(getattr(r, c) for c in cols) for r in matched])
        return _Result(matched)

    def add(self, row) -> None:
        self.added.append(row)
        self.seed(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None


def _matches(stmt, row) -> bool:
    """Evaluate the statement's WHERE clause against one in-memory row."""
    clause = stmt.whereclause
    if clause is None:
        return True
    return _eval(clause, row)


def _eval(clause, row) -> bool:
    from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList

    if isinstance(clause, BooleanClauseList):
        results = [_eval(c, row) for c in clause.clauses]
        op = clause.operator.__name__ if hasattr(clause.operator, "__name__") else ""
        return all(results) if "and" in op else any(results)
    if isinstance(clause, BinaryExpression):
        left, right = clause.left, clause.right
        name = getattr(left, "key", None) or getattr(left, "name", None)
        if name is None:
            raise AssertionError(f"FakeSession cannot evaluate {clause!r}")
        actual = getattr(row, name)
        op = clause.operator.__name__ if hasattr(clause.operator, "__name__") else ""
        wanted = getattr(right, "value", right)
        if op in ("in_op",):
            return actual in list(wanted)
        if op in ("ne", "isnot", "is_not"):
            return actual != wanted
        return actual == wanted
    raise AssertionError(f"FakeSession cannot evaluate {type(clause).__name__}")


# ── convenience builders ────────────────────────────────────────────────────

TENANT = "tenant-alpha"
OTHER_TENANT = "tenant-beta"
APP = "app-widgets"
ENV = "env-throwaway-01"
ORIGIN = "https://throwaway.example.test"
ISSUER_NAME = "qe-central-platform"


def now_ms() -> int:
    return int(time.time() * 1000)


def make_provisioning_record(
    *, tenant_id: str = TENANT, app_id: str = APP, environment_id: str = ENV,
    env_kind: str = "disposable", target_origin: str = ORIGIN,
    budget: int = 3, ttl_days: int = 30, status: str = "active",
    provisioned_by: str = "platform-admin@nexus.test",
):
    """A row as ``POST /platform/attestation/provisioning-records`` would write
    it — i.e. the PLATFORM's own certification, not anything a tenant supplied."""
    from app.db.attestation_models import EnvProvisioningRecordRow

    now = datetime.now(timezone.utc)
    return EnvProvisioningRecordRow(
        provisioning_id=uuid.uuid4().hex,
        tenant_id=tenant_id, app_id=app_id, environment_id=environment_id,
        env_kind=env_kind, target_origin=target_origin,
        reset_procedure="terraform destroy -auto-approve",
        evidence={"namespace": "eph-4417", "teardown_job": "nightly-reap"},
        provisioned_by=provisioned_by, provisioned_at=now,
        expires_at=now + timedelta(days=ttl_days),
        max_walk_mutations_per_step=budget, status=status,
    )


def make_environment_row(*, tenant_id: str = TENANT, app_id: str = APP,
                         environment_id: str = ENV, base_url: str = ORIGIN):
    """The TENANT-WRITABLE environment profile.

    Its ``base_url`` and its ``env_attestation`` are both things a tenant admin
    can set with a PATCH — which is exactly why the issuer must not believe
    either of them. Several tests below set ``env_attestation`` to a self-serving
    lie to prove it is never read.
    """
    from app.db.models import ClientAppEnvironmentRow

    return ClientAppEnvironmentRow(
        environment_id=environment_id, tenant_id=tenant_id, app_id=app_id,
        name="throwaway", base_url=base_url,
        env_attestation={"env_kind": "disposable", "attested_by": "tenant@self"},
    )


async def bootstrap_issuer_key(session, envelope, *, issuer: str = ISSUER_NAME):
    """Mint and seal an issuer key exactly the way the platform-admin endpoint
    does, and return its public identity."""
    from app.services import attestation_keys

    return await attestation_keys.generate_issuer_key(
        session, envelope, issuer=issuer, created_by="platform-admin@nexus.test")


def trust_store(public_keys, *, issuer: str = ISSUER_NAME, **policy):
    """A verifier trust store over the REAL ``attest.TrustStore``."""
    return attest.TrustStore.from_public_keys(
        [k.public_key if hasattr(k, "public_key") else k for k in public_keys],
        issuer=issuer, **policy)


def verify(attestation, *, trust, crawl_id: str, tenant_id: str = TENANT,
           target_url: str = ORIGIN, now_epoch_ms: Optional[int] = None,
           replay_guard=None):
    """Run the REAL production verifier over an issued attestation.

    A fresh :class:`ProofReplayGuard` per call by default: replay is a property
    the tests assert deliberately (by SHARING a guard), never one they trip over
    by accident because a previous test happened to run first.
    """
    return attest.verify_provisioning_proof(
        attestation,
        trust=trust,
        crawl_id=crawl_id,
        tenant_id=tenant_id,
        target_url=target_url,
        now_epoch_ms=now_epoch_ms,
        replay_guard=replay_guard or attest.ProofReplayGuard(),
    )
