"""A11.3 / T-WP-02 — REVOCATION: withdrawing trust before it expires.

WHY AN EXPIRY IS NOT REVOCATION
===============================
A provisioning proof is a bearer capability with a lifetime.  If it leaks ten
minutes after issue, an expiry does nothing for the rest of that lifetime — and
the lifetime is up to 24 hours (``MAX_PROOF_LIFETIME_MS``).  Revocation is the
only mechanism that can withdraw a proof inside its own validity window, which
is why ``app/attest.py`` makes a signed, unexpired revocation list MANDATORY on
every dispatch rather than optional: no list, a stale list, or a list whose
signature does not verify is a DENY for the whole attestation.

TWO SUBJECTS, AND THE SECOND ONE IS THE IMPORTANT ONE
=====================================================
``proof``       — one specific issued proof, by ``proof_id``.
``environment`` — EVERY proof for an environment, including ones not yet issued.

The environment form is the blast-radius control.  When an environment turns out
not to be disposable after all — it was shared, it was re-pointed at a real
host, a teardown job silently stopped running — you do not want to first
enumerate which proofs exist for it and revoke them one by one, racing an issuer
that is still minting more.  You revoke the environment, and every proof past,
present and future is dead.

WHAT "FAIL CLOSED" MEANS HERE, PRECISELY
========================================
The rule is: *if revocation status cannot be determined, the proof is treated as
revoked.*  That rule lands in two different places, and conflating them is how
fail-open bugs get written.

  * **At the VERIFIER** (explorer, already shipped): an unusable list is a DENY.
    Nothing here can weaken that.
  * **At the ISSUER** (this module): if the revocation state cannot be READ, the
    issuer must REFUSE TO ISSUE.  It must never sign a list saying "nothing is
    revoked" because the database was unreachable — that is a signed lie, and it
    would be a signed lie the fleet believes for the full life of the list.
    :func:`current_revocations` therefore RAISES on an unreadable state, and the
    issuance path lets that raise reach the caller as a 503.

THE CACHE, AND THE ONE THING IT MUST NEVER DO
=============================================
Signing every dispatch's list means reading revocations on every dispatch, so a
short TTL cache is worth having.  The invariant that keeps it safe:

    A CACHE ENTRY MAY ONLY EVER BE POPULATED BY A SUCCESSFUL READ.

A failed read never writes the cache, never extends an existing entry's TTL, and
never causes an expired entry to be served.  A cache that answered from stale
data when the database was down would convert exactly the outage this module
fails closed on into a silent fail-open.  Writes invalidate their tenant's entry
synchronously, so within one process a revocation is effective immediately.

HONEST LIMIT — WHEN REVOCATION TAKES EFFECT
===========================================
The explorer verifies the attestation ONCE, when a crawl is dispatched
(``main._walk_authorization``).  Revocation is therefore enforced at ADMISSION:
a revoked proof cannot start a crawl, and cannot be replayed into another one
(the claims bind ``crawl_id``, and ``ProofReplayGuard`` binds a ``proof_id`` to
the first crawl that used it).  It does NOT retroactively stop a crawl that is
already running under a proof admitted before the revocation was recorded.

The exposure window is therefore "the remainder of an in-flight crawl", not "the
remainder of the proof's lifetime" — and it is bounded further by the mutation
budget the verifier grants (``min(proof, fleet policy)`` per wizard step) and by
the crawl's own budgets.  To stop an in-flight crawl, revoke AND cancel the
crawl; the two are different operations because they have different authorities.
This is stated so it is a known bound rather than an assumed guarantee.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import new_id
from ..db.attestation_models import (
    SUBJECT_ENVIRONMENT,
    SUBJECT_PROOF,
    AttestationRevocationRow,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SUBJECT_PROOF", "SUBJECT_ENVIRONMENT", "VALID_SUBJECTS",
    "RevocationUnavailable", "RevocationState", "RevocationCache",
    "current_revocations", "invalidate_cache", "is_revoked", "record_revocation",
    "revocation_cache",
]

VALID_SUBJECTS = (SUBJECT_PROOF, SUBJECT_ENVIRONMENT)

#: How long a successfully-read revocation state may be reused within one
#: process.  Deliberately much shorter than the signed list's own lifetime
#: (``DEFAULT_REVOCATION_LIFETIME_MS``, 10 min): the cache adds a staleness
#: window ON TOP of the list's, and the two compound.  30s keeps the sum well
#: inside the operational expectation that a revocation is effective "within a
#: minute or so", while still collapsing a burst of dispatches into one read.
DEFAULT_CACHE_TTL_S = 30.0


class RevocationUnavailable(RuntimeError):
    """Revocation state could not be determined.

    THE FAIL-CLOSED SIGNAL.  Callers must convert this into a refusal to issue —
    never into an empty revocation list.  Signing "nothing is revoked" when the
    truth is unknown publishes a falsehood the fleet will believe for the full
    lifetime of the list.
    """


@dataclass(frozen=True)
class RevocationState:
    """The issuer's current revocation state for ONE tenant.

    Tenant-scoped on purpose: a dispatch carries only its own tenant's
    revocations, so one customer's list never discloses the existence, count or
    identifiers of another customer's revoked environments.
    """

    proof_ids: tuple[str, ...] = ()
    environment_ids: tuple[str, ...] = ()
    read_at: float = 0.0

    @property
    def total(self) -> int:
        return len(self.proof_ids) + len(self.environment_ids)


@dataclass
class RevocationCache:
    """Per-tenant TTL cache. See the module docstring for the one invariant.

    Not locked: CPython dict get/set are atomic, and the worst case a race can
    produce is two concurrent reads of the same tenant, which is harmless.  A
    lock here would serialise dispatch on a cache that exists to make dispatch
    faster.
    """

    ttl_s: float = DEFAULT_CACHE_TTL_S
    _entries: dict = field(default_factory=dict)

    def get(self, tenant_id: str) -> Optional[RevocationState]:
        entry = self._entries.get(tenant_id)
        if entry is None:
            return None
        expires_at, state = entry
        if time.monotonic() >= expires_at:
            # Expired entries are DROPPED, not returned-with-a-warning. There is
            # no code path in this module that may serve stale revocation state.
            self._entries.pop(tenant_id, None)
            return None
        return state

    def put(self, tenant_id: str, state: RevocationState) -> None:
        """Only ever called after a SUCCESSFUL read — see the invariant."""
        self._entries[tenant_id] = (time.monotonic() + float(self.ttl_s), state)

    def invalidate(self, tenant_id: str = "") -> None:
        if tenant_id:
            self._entries.pop(tenant_id, None)
        else:
            self._entries.clear()

    def snapshot(self) -> dict:
        """Diagnostics for ``/health``. Counts only — never the revoked ids,
        which are tenant data."""
        return {"ttl_s": float(self.ttl_s), "tenants_cached": len(self._entries)}


#: Process-wide cache.  Injectable in tests; a fresh instance per test keeps
#: revocation state from leaking between cases.
revocation_cache = RevocationCache()


def invalidate_cache(tenant_id: str = "") -> None:
    revocation_cache.invalidate(tenant_id)


async def record_revocation(
    session: AsyncSession,
    *,
    tenant_id: str,
    subject_type: str,
    subject_id: str,
    revoked_by: str,
    reason: str = "",
    prune_after: Optional[datetime] = None,
    cache: Optional[RevocationCache] = None,
) -> tuple[str, bool]:
    """Record ONE revocation.  Returns ``(revocation_id, newly_created)``.

    IDEMPOTENT.  Revoking an already-revoked subject is a success, not a
    conflict: an incident responder hitting the endpoint twice — or two
    responders hitting it at once — must not see an error that reads like the
    revocation failed.  The original ``revoked_at`` and ``revoked_by`` are
    preserved, because the first revocation is the one that is true.

    INSERT-ONLY.  There is no un-revoke here and there should not be: the
    audit record of a withdrawal of trust must not be erasable by the same API
    that can withdraw it.  Re-permitting an environment means provisioning it
    afresh, which produces a new environment record and new proof ids.
    """
    tenant_id = str(tenant_id or "").strip()
    subject_type = str(subject_type or "").strip().lower()
    subject_id = str(subject_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if subject_type not in VALID_SUBJECTS:
        raise ValueError(
            f"subject_type must be one of {VALID_SUBJECTS}, got {subject_type!r}")
    if not subject_id:
        raise ValueError("subject_id is required")

    existing = (await session.execute(
        select(AttestationRevocationRow).where(
            AttestationRevocationRow.tenant_id == tenant_id,
            AttestationRevocationRow.subject_type == subject_type,
            AttestationRevocationRow.subject_id == subject_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        (cache or revocation_cache).invalidate(tenant_id)
        return existing.revocation_id, False

    revocation_id = new_id()
    session.add(AttestationRevocationRow(
        revocation_id=revocation_id,
        tenant_id=tenant_id,
        subject_type=subject_type,
        subject_id=subject_id,
        reason=str(reason or "")[:500],
        revoked_by=str(revoked_by or "")[:200],
        revoked_at=datetime.now(timezone.utc),
        prune_after=prune_after,
    ))
    try:
        await session.flush()
    except IntegrityError:
        # Lost a race with a concurrent responder. The unique constraint did its
        # job; the subject IS revoked, which is what the caller wanted.
        await session.rollback()
        (cache or revocation_cache).invalidate(tenant_id)
        return "", False

    # Invalidated BEFORE the caller commits, and again is harmless: the only
    # unsafe direction is serving a cached "not revoked" after a revocation, and
    # dropping the entry early can only cause an extra read.
    (cache or revocation_cache).invalidate(tenant_id)
    logger.error(
        "qec.attest.revoked tenant=%s subject_type=%s subject_id=%s by=%s "
        "reason=%s — every NEW dispatch quoting this subject is now refused; "
        "an already-admitted crawl runs to completion unless separately "
        "cancelled",
        tenant_id, subject_type, subject_id, revoked_by, str(reason)[:200])
    return revocation_id, True


async def current_revocations(
    session: AsyncSession,
    tenant_id: str,
    *,
    cache: Optional[RevocationCache] = None,
    use_cache: bool = True,
) -> RevocationState:
    """The tenant's current revocation state.

    RAISES :class:`RevocationUnavailable` if the state cannot be read.  That is
    the entire fail-closed contract of this module: the caller has no way to
    obtain an empty list by accident, because the only empty list this function
    ever returns is one it positively read from the database.

    ``use_cache=False`` forces a fresh read — used by the revocation endpoint
    itself, which must reflect a write that just happened in the same request.
    """
    tenant_id = str(tenant_id or "").strip()
    if not tenant_id:
        raise RevocationUnavailable(
            "no tenant_id — cannot determine revocation state, refusing")
    active_cache = cache or revocation_cache
    if use_cache:
        hit = active_cache.get(tenant_id)
        if hit is not None:
            return hit

    try:
        rows = (await session.execute(
            select(AttestationRevocationRow.subject_type,
                   AttestationRevocationRow.subject_id)
            .where(AttestationRevocationRow.tenant_id == tenant_id)
        )).all()
    except Exception as exc:
        # NOT swallowed, NOT defaulted to empty, and the cache is NOT consulted
        # as a fallback. "I could not read the revocation list" and "nothing is
        # revoked" are different facts and must produce different behaviour.
        logger.error(
            "qec.attest.revocation_read_failed tenant=%s error=%s — refusing to "
            "issue rather than sign an unverified 'nothing is revoked'",
            tenant_id, type(exc).__name__)
        raise RevocationUnavailable(
            f"revocation state unreadable ({type(exc).__name__}) — refusing to "
            f"issue (fail-closed)") from exc

    proof_ids = tuple(sorted({str(r[1]) for r in rows if r[0] == SUBJECT_PROOF}))
    env_ids = tuple(sorted(
        {str(r[1]) for r in rows if r[0] == SUBJECT_ENVIRONMENT}))
    state = RevocationState(proof_ids=proof_ids, environment_ids=env_ids,
                            read_at=time.time())
    if use_cache:
        active_cache.put(tenant_id, state)
    return state


def is_revoked(state: RevocationState, *, proof_id: str = "",
               environment_id: str = "") -> bool:
    """Would the verifier refuse this subject against ``state``?

    Mirrors ``app.attest._verify`` step 10 so the ISSUER can refuse to mint a
    proof for an already-revoked environment.  That refusal is not redundant
    with the verifier's: minting a proof the fleet is guaranteed to reject
    produces a confusing ``revoked`` at a distant worker instead of an
    actionable error at the API the operator actually called.
    """
    if proof_id and str(proof_id) in state.proof_ids:
        return True
    if environment_id and str(environment_id) in state.environment_ids:
        return True
    return False
