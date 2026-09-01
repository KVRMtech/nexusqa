"""A11.2 / T-WP-02 — THE ISSUANCE DECISION: is this environment genuinely
disposable, and may this caller have a proof for it?

This module is where the milestone's security actually lives.  The cryptography
is already correct and already tested (``walk_attestation``); the key is already
in custody (``attestation_keys``); the verifier is already red-teamed
(``qe-explorer/app/attest.py``).  What was missing is the part no signature can
supply: a truthful answer to *"is this thing really a throwaway environment?"*

WHY THE OBVIOUS ANSWER IS THE WRONG ONE
=======================================
``app_environments.env_attestation`` already holds an ``env_kind``, and it is
right there on the row the endpoint has to load anyway.  Reading it would have
been one line.  It is also the single worst thing this module could do, because
that column is written by ``PATCH /apps/{id}/environments/{env}`` — a TENANT
endpoint.  A tenant who types ``"env_kind": "disposable"`` into their own
environment profile would thereby cause the platform to sign a statement that
their environment is safe to mutate, and the explorer — which correctly trusts
signatures over dispatch bodies — would believe it.

Signing a tenant-supplied fact does not make it true.  It makes it a *signed
lie*, and one that is harder to detect than the unsigned kind because everything
downstream is now cryptographically satisfied.  This is the ``tenant
self-attestation`` scenario in the A11.5 matrix, and closing it is the reason
``env_provisioning_records`` exists.

THE FIVE GATES, IN ORDER, ALL FAIL-CLOSED
=========================================
Every one of them returns a stable reason code and STOPS.  None is skipped
because an earlier one was ambiguous.

  1. **A provisioning record exists, is active, and is unexpired.**  No record
     ⇒ the platform never certified this environment ⇒ refuse.  An EXPIRED
     record is refused too: a disposable environment certified six months ago
     may have been torn down, and whatever answers at that origin now is not
     what was certified.
  2. **The record says ``disposable``.**  Recorded by a platform admin, never
     copied from a tenant payload.  Any other kind refuses — and cites the
     record, so the operator learns *"the platform certified this as prod"*
     rather than *"denied"*.
  3. **The origin has not moved.**  The record PINS the origin that was
     certified.  The environment row's ``base_url`` is tenant-writable, so it is
     re-checked against the pin: certify a throwaway host, then quietly re-point
     ``base_url`` at production, and this gate is what stops the proof.  The
     claims are then built from the PIN, never from the row.
  4. **The environment is not revoked.**  Checked at ISSUE time as well as at
     verify time, so an operator who revoked an environment gets an actionable
     error from the API they called instead of a puzzling ``revoked`` on a
     distant worker.
  5. **Revocation state is READABLE.**  If it is not, issuance refuses
     (``RevocationUnavailable``).  Never an empty list — see
     :mod:`app.services.attestation_revocation`.

WHAT THE ISSUER DELIBERATELY DOES NOT DECIDE
============================================
Lifetime ceilings, the mutation budget ceiling, origin/tenant/crawl binding,
issuer identity and replay are all RE-ENFORCED by the verifier against the
fleet's own policy.  This module cannot widen any of them; the effective grant
is always ``min(what was asked for, what the fleet permits)``.  That asymmetry
is intentional and load-bearing: a compromised issuer still cannot mint a proof
this fleet will honour beyond fleet policy.

THE AUDIT ROW IS WRITTEN BEFORE THE PROOF IS RETURNED
=====================================================
An issuance the platform cannot see is an issuance it cannot revoke by id.  The
log row is therefore part of the same transaction as the decision, not a
best-effort afterthought: if the audit write fails, the issuance fails.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.attestation_models import (
    PROVISIONING_ACTIVE,
    AttestationIssuanceLogRow,
    EnvProvisioningRecordRow,
)
from ..db.models import ClientAppEnvironmentRow
from . import attestation_revocation as revocation
from .attestation_keys import active_signer
from .walk_attestation import (
    DEFAULT_REVOCATION_LIFETIME_MS,
    DISPOSABLE,
    HARD_MAX_MUTATIONS_PER_STEP,
    MAX_PROOF_LIFETIME_MS,
    ProvisioningGrant,
    normalize_origin,
    revocation_claims,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROOF_LIFETIME_MS", "IssuanceRefused", "IssuanceReason",
    "IssuedAttestation", "issue_for_crawl", "resolve_provisioning_record",
]

#: How long an issued proof is good for.
#:
#: TIED TO THE REVOCATION LIST'S LIFETIME, AND THAT IS NOT AN ARBITRARY CHOICE.
#: The verifier requires BOTH a valid proof AND a currently-valid revocation
#: list, so an attestation's real usable life is ``min(proof, revocation list)``.
#: An earlier draft of this module minted one-hour proofs alongside ten-minute
#: revocation lists; the A11.5 suite caught it. The proof reported a one-hour
#: ``expires_at_ms``, and at any point past ten minutes the whole attestation
#: was refused as ``revocation_expired`` — a fail-CLOSED refusal (so never
#: unsafe), but the API was telling callers a validity window that was fifty
#: minutes longer than the truth.
#:
#: The revocation list is the half that must stay short: it is the ONLY
#: mechanism that withdraws a proof early, so its lifetime is the window in
#: which a revocation has been decided and is not yet being enforced. So the
#: PROOF moves down to meet it rather than the list moving up.
#:
#: Ten minutes is ample: a proof is minted per dispatch, and a dispatch reaches
#: a worker in seconds. Callers who fetch a proof through the API and sit on it
#: get a clear ``expired``/``revocation_expired`` refusal rather than a
#: capability with a long tail.
DEFAULT_PROOF_LIFETIME_MS = DEFAULT_REVOCATION_LIFETIME_MS


class IssuanceReason:
    """Stable refusal vocabulary.  Every value is a DENY except :attr:`OK`.

    Deliberately parallel to ``app.attest.AttestReason``: an operator reading a
    qe-central refusal and an explorer refusal should be reading the same
    language, because in an incident they will be reading both at once.
    """

    OK = "ok"
    NO_PROVISIONING_RECORD = "no_provisioning_record"
    PROVISIONING_RETIRED = "provisioning_retired"
    PROVISIONING_EXPIRED = "provisioning_expired"
    NOT_DISPOSABLE = "not_disposable"
    ORIGIN_MOVED = "origin_moved"
    ORIGIN_MISMATCH = "origin_mismatch"
    ENVIRONMENT_REVOKED = "environment_revoked"
    REVOCATION_UNAVAILABLE = "revocation_unavailable"
    NO_ISSUER_KEY = "no_issuer_key"
    UNKNOWN_ENVIRONMENT = "unknown_environment"
    CLOCK_DOMAIN_ERROR = "clock_domain_error"


class IssuanceRefused(Exception):
    """A proof that must not be minted.

    Carries a stable ``reason`` and a human ``detail``.  The router maps reason
    to HTTP status; nothing in the detail is echoed from an untrusted body, so
    it is safe to return to the caller.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class IssuedAttestation:
    """What issuance produced: the wire object, plus everything the audit trail
    and the API response need.  Contains no key material and no signature the
    caller did not already receive."""

    attestation: Mapping[str, Any]
    proof_id: str
    kid: str
    issuer: str
    environment_id: str
    target_origin: str
    issued_at_ms: int
    expires_at_ms: int
    max_walk_mutations_per_step: int
    provisioning_id: str
    claims_digest: str
    revoked_count: int = 0
    #: When the REVOCATION LIST expires. The attestation as a whole is unusable
    #: from ``min(expires_at_ms, revocation_expires_at_ms)`` — see
    #: :attr:`effective_expires_at_ms`, which is the number a caller should act
    #: on and the one the API returns.
    revocation_expires_at_ms: int = 0

    @property
    def effective_expires_at_ms(self) -> int:
        """When this ATTESTATION stops being honoured — the earlier of the two
        halves. Computed rather than assumed so it stays true if either lifetime
        is ever re-tuned independently."""
        if not self.revocation_expires_at_ms:
            return int(self.expires_at_ms)
        return min(int(self.expires_at_ms), int(self.revocation_expires_at_ms))

    def as_response(self) -> dict[str, Any]:
        """The API body.  The ``attestation`` is the part the caller forwards to
        the explorer verbatim; the rest is metadata for humans and dashboards."""
        return {
            "attestation": dict(self.attestation),
            "proof_id": self.proof_id,
            "kid": self.kid,
            "issuer": self.issuer,
            "environment_id": self.environment_id,
            "target_origin": self.target_origin,
            "issued_at_ms": int(self.issued_at_ms),
            # The PROOF's own expiry, and the one that actually governs. They are
            # equal by default; both are reported so a caller can never be
            # surprised by the shorter of two numbers it was never shown.
            "proof_expires_at_ms": int(self.expires_at_ms),
            "revocation_expires_at_ms": int(self.revocation_expires_at_ms),
            "expires_at_ms": self.effective_expires_at_ms,
            "max_walk_mutations_per_step": int(self.max_walk_mutations_per_step),
            "claims_digest": self.claims_digest,
        }


def _now_ms(now_epoch_ms: Optional[int]) -> int:
    now = int(time.time() * 1000) if now_epoch_ms is None else int(now_epoch_ms)
    # Same doctrine as the verifier (M0.5 T-SEC-08): a "now" below the plausible
    # epoch floor is a monotonic reading or a zeroed clock, and comparing across
    # clock domains is how an expiry check silently stops expiring anything.
    if now < 1_000_000_000_000:
        raise IssuanceRefused(
            IssuanceReason.CLOCK_DOMAIN_ERROR,
            f"now_ms={now} is not an epoch-ms reading; refusing to stamp a "
            f"proof whose expiry could not be compared")
    return now


async def resolve_provisioning_record(
    session: AsyncSession,
    *,
    tenant_id: str,
    app_id: str,
    environment_id: str,
    now: Optional[datetime] = None,
) -> EnvProvisioningRecordRow:
    """Gates 1 and 2 — the AUTHORITATIVE answer, or a refusal.

    Reads ``env_provisioning_records`` and nothing else.  In particular it does
    NOT fall back to ``app_environments.env_attestation`` when no record exists:
    a fallback to a tenant-writable field is precisely the hole this table was
    added to close, and "be lenient when the strict source is empty" is how such
    holes get reintroduced during a later refactor.
    """
    now = now or datetime.now(timezone.utc)
    row = (await session.execute(
        select(EnvProvisioningRecordRow).where(
            EnvProvisioningRecordRow.tenant_id == tenant_id,
            EnvProvisioningRecordRow.app_id == app_id,
            EnvProvisioningRecordRow.environment_id == environment_id,
            EnvProvisioningRecordRow.status == PROVISIONING_ACTIVE,
        )
    )).scalar_one_or_none()
    if row is None:
        raise IssuanceRefused(
            IssuanceReason.NO_PROVISIONING_RECORD,
            f"environment {environment_id!r} has no active provisioning record. "
            f"The platform has never certified it as disposable, and a tenant's "
            f"own env_attestation is not evidence. A platform administrator must "
            f"POST /provisioning-record first.")

    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        # Defensive: a driver or a fixture that hands back a naive datetime would
        # otherwise raise inside the comparison and surface as a 500 on a path
        # whose whole job is to refuse legibly.
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and now >= expires_at:
        raise IssuanceRefused(
            IssuanceReason.PROVISIONING_EXPIRED,
            f"the provisioning record for {environment_id!r} expired at "
            f"{expires_at.isoformat()}. A disposable environment certified in "
            f"the past may since have been torn down; re-certify it.")

    if (row.env_kind or "").strip().lower() != DISPOSABLE:
        raise IssuanceRefused(
            IssuanceReason.NOT_DISPOSABLE,
            f"the platform certified {environment_id!r} as "
            f"{row.env_kind!r}, not {DISPOSABLE!r}. Walk mutation is authorised "
            f"for disposable environments only.")
    return row


async def _check_origin_has_not_moved(
    session: AsyncSession, *, tenant_id: str, environment_id: str,
    record: EnvProvisioningRecordRow, requested_target_url: str,
) -> str:
    """Gate 3 — the pinned origin still describes reality, and the caller is
    asking about that same origin.

    Returns the origin to SIGN, which is always the record's pin.

    THE ATTACK THIS STOPS: certify a genuine throwaway host, wait for the
    provisioning record, then ``PATCH`` the environment's ``base_url`` to point
    at production.  Every later gate would pass — the record is active,
    disposable and unexpired — and the crawl would be dispatched at production
    holding a valid mutation proof.  ``base_url`` is tenant-writable; the pin is
    not; so the pin is the authority and a disagreement is a refusal.
    """
    pinned = normalize_origin(record.target_origin)
    if not pinned:
        # A record with no usable pin cannot be honoured: the verifier treats an
        # empty origin as a mismatch, so signing it would guarantee a refusal.
        raise IssuanceRefused(
            IssuanceReason.ORIGIN_MISMATCH,
            f"the provisioning record for {environment_id!r} pins no usable "
            f"origin ({record.target_origin!r}); re-certify the environment")

    env_row = (await session.execute(
        select(ClientAppEnvironmentRow).where(
            ClientAppEnvironmentRow.tenant_id == tenant_id,
            ClientAppEnvironmentRow.environment_id == environment_id,
        )
    )).scalar_one_or_none()
    if env_row is None:
        raise IssuanceRefused(
            IssuanceReason.UNKNOWN_ENVIRONMENT,
            f"environment {environment_id!r} does not exist for this tenant")

    live = normalize_origin(env_row.base_url or "")
    if live != pinned:
        raise IssuanceRefused(
            IssuanceReason.ORIGIN_MOVED,
            f"environment {environment_id!r} was certified disposable at "
            f"{pinned!r} but its base_url now resolves to {live!r}. The "
            f"certification does not transfer to a different origin — "
            f"re-certify, or restore the base_url.")

    # The caller must be asking about the origin that was certified. A request
    # for a proof against some other URL is not a mistake worth guessing at.
    wanted = normalize_origin(requested_target_url or "")
    if wanted and wanted != pinned:
        raise IssuanceRefused(
            IssuanceReason.ORIGIN_MISMATCH,
            f"requested target {wanted!r} is not the certified origin {pinned!r}")
    return pinned


async def issue_for_crawl(
    session: AsyncSession,
    envelope: Any,
    *,
    tenant_id: str,
    app_id: str,
    environment_id: str,
    crawl_id: str,
    target_url: str = "",
    issued_to: str = "",
    request_id: str = "",
    now_epoch_ms: Optional[int] = None,
    proof_lifetime_ms: int = DEFAULT_PROOF_LIFETIME_MS,
    revocation_lifetime_ms: int = DEFAULT_REVOCATION_LIFETIME_MS,
    max_walk_mutations_per_step: Optional[int] = None,
    revocation_cache: Optional[revocation.RevocationCache] = None,
) -> IssuedAttestation:
    """Run all five gates and, if every one passes, mint a bound attestation.

    ``crawl_id`` is REQUIRED and is bound into the claims: the proof authorises
    ONE crawl.  There is deliberately no "issue me a proof for later" mode — a
    proof not bound to a crawl is a bearer token for mutation, which is the
    thing this whole subsystem exists to avoid.

    Raises :class:`IssuanceRefused` on any gate, ``NoActiveIssuerKey`` /
    ``KeyCustodyError`` when the platform has no usable root of trust, and
    ``RevocationUnavailable`` when revocation state cannot be read.  It does not
    return a weaker proof under any circumstances.
    """
    tenant_id = str(tenant_id or "").strip()
    app_id = str(app_id or "").strip()
    environment_id = str(environment_id or "").strip()
    crawl_id = str(crawl_id or "").strip()
    if not crawl_id:
        raise IssuanceRefused(
            "crawl_id_required",
            "a provisioning proof is bound to ONE crawl; issuing one without a "
            "crawl_id would create a reusable mutation capability")

    now_ms = _now_ms(now_epoch_ms)
    now_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)

    # ── Gates 1 + 2 ───────────────────────────────────────────────────────
    record = await resolve_provisioning_record(
        session, tenant_id=tenant_id, app_id=app_id,
        environment_id=environment_id, now=now_dt)

    # ── Gate 3 ────────────────────────────────────────────────────────────
    origin = await _check_origin_has_not_moved(
        session, tenant_id=tenant_id, environment_id=environment_id,
        record=record, requested_target_url=target_url)

    # ── Gates 4 + 5 ───────────────────────────────────────────────────────
    # Forced fresh: a proof must never be minted against a cached "not revoked"
    # when the caller is about to be handed mutation authority. The 30s cache is
    # for the dispatch READ path; issuance pays for the truth.
    state = await revocation.current_revocations(
        session, tenant_id, cache=revocation_cache, use_cache=False)
    if revocation.is_revoked(state, environment_id=environment_id):
        raise IssuanceRefused(
            IssuanceReason.ENVIRONMENT_REVOKED,
            f"environment {environment_id!r} is revoked; no further proof will "
            f"be issued for it")

    # LEAST PRIVILEGE: the caller may ask for LESS than the record allows, never
    # more. min() in this direction is the whole rule — an issuer that honoured a
    # larger request would let the API widen a platform-admin decision.
    budget = int(record.max_walk_mutations_per_step)
    if max_walk_mutations_per_step is not None:
        budget = min(budget, int(max_walk_mutations_per_step))
    budget = max(0, min(budget, HARD_MAX_MUTATIONS_PER_STEP))

    lifetime = min(int(proof_lifetime_ms), MAX_PROOF_LIFETIME_MS)

    grant = ProvisioningGrant(
        environment_id=environment_id,
        tenant_id=tenant_id,
        crawl_id=crawl_id,
        # The PIN, not the row. Gate 3 proved they agree; signing the pin means
        # a future refactor that loses gate 3 still cannot sign a moved origin.
        target_url=origin,
        reset_procedure=record.reset_procedure or "",
        env_kind=DISPOSABLE,
        max_walk_mutations_per_step=budget,
    )
    # The issuer NAME comes from the KEY ROW, not from configuration. A config
    # edit must never be able to re-attribute proofs to a different issuer than
    # the one whose key signs them — that pairing is what the verifier checks.
    # ``grant.claims`` raises IssuerError rather than minting anything the
    # verifier would refuse, so a bad grant fails inside this block with a
    # message naming its real cause.
    async with active_signer(session, envelope) as signer:
        claims = grant.claims(issuer=signer.issuer, issued_at_ms=now_ms,
                              lifetime_ms=lifetime)
        proof = signer.sign_claims(claims)
        rev_claims = revocation_claims(
            issuer=signer.issuer,
            issued_at_ms=now_ms,
            revoked_proof_ids=state.proof_ids,
            revoked_environment_ids=state.environment_ids,
            lifetime_ms=revocation_lifetime_ms,
        )
        revocations = signer.sign_claims(rev_claims)
        kid, issuer_name = signer.kid, signer.issuer

    attestation = {"proof": proof, "revocations": revocations}
    digest = _claims_digest(claims)

    # ── The audit row, in the SAME transaction as the decision ────────────
    session.add(AttestationIssuanceLogRow(
        proof_id=claims["proof_id"],
        tenant_id=tenant_id,
        app_id=app_id,
        environment_id=environment_id,
        crawl_id=crawl_id,
        kid=kid,
        claims_digest=digest,
        target_origin=origin,
        issued_at_ms=int(claims["issued_at_ms"]),
        expires_at_ms=int(claims["expires_at_ms"]),
        max_walk_mutations_per_step=budget,
        issued_to=str(issued_to or "")[:200],
        request_id=str(request_id or "")[:64],
        provisioning_id=record.provisioning_id,
    ))
    await session.flush()

    logger.warning(
        "qec.attest.proof_issued tenant=%s app=%s env=%s crawl=%s proof_id=%s "
        "kid=%s origin=%s budget=%d expires_ms=%d to=%s — this crawl may now "
        "perform bounded server-side MUTATION of the target",
        tenant_id, app_id, environment_id, crawl_id, claims["proof_id"], kid,
        origin, budget, int(claims["expires_at_ms"]), issued_to)

    return IssuedAttestation(
        attestation=attestation,
        proof_id=str(claims["proof_id"]),
        kid=kid,
        issuer=issuer_name,
        environment_id=environment_id,
        target_origin=origin,
        issued_at_ms=int(claims["issued_at_ms"]),
        expires_at_ms=int(claims["expires_at_ms"]),
        max_walk_mutations_per_step=budget,
        provisioning_id=record.provisioning_id,
        claims_digest=digest,
        revoked_count=state.total,
        revocation_expires_at_ms=int(rev_claims["expires_at_ms"]),
    )


def _claims_digest(claims: Mapping[str, Any]) -> str:
    """The SAME digest ``app.attest`` reports in its verdict.

    Computed identically (sha256 over the canonical encoding, first 32 hex) so
    an auditor can join a line in the explorer's log to a row in
    ``attestation_issuance_log`` without either side holding the proof itself.
    """
    import hashlib

    from .signing import canonical_bytes
    return hashlib.sha256(canonical_bytes(dict(claims))).hexdigest()[:32]
