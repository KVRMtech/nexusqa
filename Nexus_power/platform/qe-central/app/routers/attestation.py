"""A11.2 / A11.3 — THE ATTESTATION API: issuance, revocation, and custody.

TWO AUTHORITIES, AND THE SPLIT IS THE SECURITY MODEL
====================================================
Every endpoint here is JWT-authenticated.  What differs — and what matters — is
WHICH admin each one requires.

**Platform super-admin** (``role=admin`` AND the ``platform_admin`` claim, which
a tenant-scoped token structurally cannot carry):

  * certifying that an environment is genuinely disposable
    (``POST /platform/attestation/provisioning-records``);
  * everything touching the issuer key.

**Tenant admin** (``role=admin``):

  * asking for a proof for an environment the platform ALREADY certified;
  * revoking their own environments and proofs.

THE ASYMMETRY IS DELIBERATE.  Issuance is safe to delegate to a tenant admin
precisely because it cannot invent authority: it can only convert an existing
platform-admin certification into a short-lived, crawl-bound, origin-bound,
tenant-bound capability.  Every dangerous input — ``env_kind``, the origin, the
mutation ceiling — is read from the platform's own record, never from the
request.  A tenant with a valid token and a mind to abuse it can obtain exactly
the proofs a platform admin already decided they may have, and no others.

REVOCATION IS DELEGATED DOWNWARD ON PURPOSE.  A tenant may revoke, but not
un-revoke.  Every action available at the lower privilege level moves in the
fail-closed direction, so delegating it costs nothing: the worst a compromised
tenant token achieves is turning its own walk persistence off.  Requiring a
platform admin to revoke would mean an incident waits for an escalation, which
is precisely backwards.

WHY ISSUANCE HAS ITS OWN RATE LIMIT
===================================
The global ``PrincipalRateLimiter`` is default-OFF (``QEC_API_RATE_LIMIT=0``), so
"there is a limiter somewhere" is not a control this endpoint may rely on.
Issuance performs a Cloud KMS decrypt per call.  Unbounded, that is a billable
denial-of-service against the platform's own root of trust, and a way to drive
KMS quota exhaustion that would take issuance down for every tenant.  So this
router carries its own limiter, ON by default, sized for the real workload: a
proof is minted once per crawl dispatch, not once per request.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..api_protect import PrincipalRateLimiter, get_request_id
from ..auth import require_role
from ..db import new_id, tenant_scoped_qec_session
from ..db.attestation_models import (
    PROVISIONING_ACTIVE,
    PROVISIONING_RETIRED,
    EnvProvisioningRecordRow,
)
from ..fleet.rbac import require_platform_admin
from ..services import attestation_issuer as issuer
from ..services import attestation_keys as keys
from ..services import attestation_revocation as revocation
from ..services.walk_attestation import DISPOSABLE, normalize_origin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Attestation"])

_SUPER_ADMIN = require_platform_admin
_TENANT_ADMIN = require_role("admin")

#: Per-principal ceiling on PROOF ISSUANCE specifically. Default 1/s with a
#: burst of 5: a dispatch mints one proof, and even an aggressive fleet
#: re-dispatching failed crawls stays orders of magnitude under this. Set
#: ``QEC_ATTESTATION_ISSUE_RATE=0`` to disable — which should only ever be done
#: knowingly, because it removes the only bound on KMS spend from this path.
_ISSUE_LIMITER = PrincipalRateLimiter(
    rate_per_sec=float(os.environ.get("QEC_ATTESTATION_ISSUE_RATE", "1.0") or 0),
    burst_factor=float(os.environ.get("QEC_ATTESTATION_ISSUE_BURST", "5.0") or 5.0),
)

#: How long a provisioning certification stays valid before it must be renewed.
#: Bounded on purpose (see ``qec_023``): a disposable environment certified long
#: ago may since have been destroyed, and whatever now answers at that origin was
#: never certified at all.
DEFAULT_PROVISIONING_TTL_DAYS = 30
MAX_PROVISIONING_TTL_DAYS = 365


def _actor(user: dict) -> str:
    return str(user.get("sub") or user.get("email") or "operator")


def _tenant_of(user: dict) -> str:
    tenant_id = str(user.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=403, detail="token carries no tenant_id")
    return tenant_id


def _envelope_or_503(request: Request):
    """The process ``EnvelopeService``, or a 503 that says what is wrong.

    Not a 500: an unavailable KMS is an operational state, not a bug, and the
    caller should retry rather than open a ticket. And NOT a fallback to an
    unsealed key — there is no such thing here by construction.
    """
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(
            status_code=503,
            detail=("envelope/KMS service unavailable — the issuer key can only "
                    "be unsealed through KMS, so no proof can be issued "
                    "(fail-closed)"))
    return envelope


def _enforce_issue_rate(request: Request, principal: str) -> None:
    allowed, retry_after = _ISSUE_LIMITER.allow(principal)
    if allowed:
        return
    logger.warning("qec.attest.issue_rate_limited principal=%s", principal)
    raise HTTPException(
        status_code=429,
        detail="provisioning-proof issuance rate exceeded",
        headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
    )


# ── Request models ──────────────────────────────────────────────────────────


class ProvisioningProofRequest(BaseModel):
    """Ask for a proof.

    ``extra='forbid'`` is a security control, not tidiness. Without it a caller
    could send ``env_kind``/``target_origin``/``max_walk_mutations_per_step`` and
    a future refactor that started reading one of them would silently reintroduce
    tenant self-attestation. Forbidding unknown fields makes that a 422 today
    rather than a breach later.
    """

    model_config = ConfigDict(extra="forbid")

    #: REQUIRED. The proof is bound to exactly one crawl; see
    #: ``attestation_issuer.issue_for_crawl``.
    crawl_id: str = Field(min_length=1, max_length=128)
    #: Optional cross-check. If supplied it must equal the CERTIFIED origin —
    #: it can only ever narrow, never redirect (gate 3).
    target_url: str = Field(default="", max_length=2000)
    #: Optional voluntary de-escalation: ask for FEWER mutations per step than
    #: the platform certified. A larger number is silently floored to the
    #: certified value — the API cannot widen a platform-admin decision.
    max_walk_mutations_per_step: Optional[int] = Field(default=None, ge=0, le=10)


class ProvisioningRecordRequest(BaseModel):
    """PLATFORM ADMIN ONLY — certify what an environment actually is.

    This is the authoritative statement the whole trust chain rests on. It is
    not a description of what the tenant says; it is the platform's own finding,
    attributable to the principal that made it.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    app_id: str = Field(min_length=1, max_length=64)
    environment_id: str = Field(min_length=1, max_length=64)
    #: The finding. ``disposable`` is the only value that can ever authorise a
    #: walk mutation; the others are recordable so a refusal can cite an explicit
    #: certification rather than an absence.
    env_kind: str = Field(min_length=1, max_length=32)
    #: The origin being certified. PINNED here and re-checked against the
    #: environment's live ``base_url`` at every issuance.
    target_origin: str = Field(min_length=1, max_length=512)
    reset_procedure: str = Field(default="", max_length=512)
    #: WHAT was verified and HOW — a namespace id, a Terraform run, a teardown
    #: job handle, a written justification. Evidence for an auditor; never an
    #: input to a decision.
    evidence: dict = Field(default_factory=dict)
    max_walk_mutations_per_step: int = Field(default=1, ge=0, le=10)
    ttl_days: int = Field(default=DEFAULT_PROVISIONING_TTL_DAYS, ge=1,
                          le=MAX_PROVISIONING_TTL_DAYS)


class RevocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: str = Field(min_length=1, max_length=20)
    subject_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="", max_length=500)


class IssuerKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: MUST equal the explorer fleet's ``QEC_ATTESTATION_ISSUER``. Supplied
    #: explicitly rather than read from this service's config so bootstrapping a
    #: key is a deliberate statement of which fleet it is for.
    issuer: str = Field(min_length=1, max_length=128)
    #: Set on a rotation to retire the incumbent in the same transaction.
    rotate: bool = False
    meta: dict = Field(default_factory=dict)


# ── A11.2 — issuance ────────────────────────────────────────────────────────


@router.post("/apps/{app_id}/environments/{environment_id}/provisioning-proof")
async def issue_provisioning_proof(
    app_id: str,
    environment_id: str,
    payload: ProvisioningProofRequest,
    request: Request,
    response: Response,
    user: dict = Depends(_TENANT_ADMIN),
) -> dict:
    """Mint a signed, crawl-bound provisioning proof for a certified
    disposable environment.

    THE ONLY WAY ``Phase.WALK`` IS EVER ENABLED. Read
    :mod:`app.services.attestation_issuer` for the five gates; every one of them
    fails closed, and none of them reads anything from this request body except
    the crawl binding and a voluntary de-escalation.

    Returns 200 with ``{attestation: {proof, revocations}, ...}``. The
    ``attestation`` object is forwarded to the explorer verbatim — qe-central
    does not, and must not, interpret it further.
    """
    tenant_id = _tenant_of(user)
    principal = f"{tenant_id}:{_actor(user)}"
    _enforce_issue_rate(request, principal)
    envelope = _envelope_or_503(request)
    request_id = get_request_id(request)

    async with tenant_scoped_qec_session(tenant_id) as session:
        try:
            issued = await issuer.issue_for_crawl(
                session, envelope,
                tenant_id=tenant_id,
                app_id=app_id,
                environment_id=environment_id,
                crawl_id=payload.crawl_id,
                target_url=payload.target_url,
                issued_to=_actor(user),
                request_id=request_id,
                max_walk_mutations_per_step=payload.max_walk_mutations_per_step,
            )
        except issuer.IssuanceRefused as exc:
            # 403, not 400: the request was well-formed and the caller was
            # authenticated — the platform is refusing to make the STATEMENT.
            # The distinction matters to whoever reads the log at 3am.
            logger.warning(
                "qec.attest.issue_refused tenant=%s app=%s env=%s reason=%s "
                "actor=%s", tenant_id, app_id, environment_id, exc.reason,
                _actor(user))
            raise HTTPException(
                status_code=403,
                detail={"reason": exc.reason, "message": exc.detail})
        except revocation.RevocationUnavailable as exc:
            # FAIL-CLOSED. Never an empty revocation list.
            logger.error("qec.attest.issue_revocation_unavailable tenant=%s",
                         tenant_id)
            raise HTTPException(
                status_code=503,
                detail={"reason": issuer.IssuanceReason.REVOCATION_UNAVAILABLE,
                        "message": str(exc)})
        except keys.NoActiveIssuerKey as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": issuer.IssuanceReason.NO_ISSUER_KEY,
                        "message": str(exc)})
        except keys.KeyCustodyError as exc:
            logger.error("qec.attest.issue_custody_error tenant=%s error=%s",
                         tenant_id, type(exc).__name__)
            raise HTTPException(
                status_code=503,
                detail={"reason": issuer.IssuanceReason.NO_ISSUER_KEY,
                        "message": str(exc)})
        except ValueError as exc:
            # walk_attestation.IssuerError — a grant that could never be
            # authorised. Refused at mint so the message names the real cause.
            raise HTTPException(status_code=422, detail=str(exc)[:400])
        await session.commit()

    # A capability, not a document: never cached, never stored by an intermediary.
    response.headers["Cache-Control"] = "no-store"
    return issued.as_response()


@router.get("/apps/{app_id}/environments/{environment_id}/provisioning-record")
async def read_provisioning_record(
    app_id: str, environment_id: str, user: dict = Depends(_TENANT_ADMIN),
) -> dict:
    """What the platform has certified about this environment.

    Readable by the tenant so "why can't I enable walk persistence?" is
    answerable without a support ticket — and so a tenant can SEE that the
    platform, not they, decides what their environment is.
    """
    tenant_id = _tenant_of(user)
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (await session.execute(
            select(EnvProvisioningRecordRow).where(
                EnvProvisioningRecordRow.tenant_id == tenant_id,
                EnvProvisioningRecordRow.app_id == app_id,
                EnvProvisioningRecordRow.environment_id == environment_id,
                EnvProvisioningRecordRow.status == PROVISIONING_ACTIVE,
            )
        )).scalar_one_or_none()
    if row is None:
        return {
            "certified": False,
            "environment_id": environment_id,
            "reason": issuer.IssuanceReason.NO_PROVISIONING_RECORD,
            "message": ("no active provisioning record — a platform "
                        "administrator must certify this environment before a "
                        "provisioning proof can be issued"),
        }
    return {
        "certified": True,
        "provisioning_id": row.provisioning_id,
        "environment_id": row.environment_id,
        "env_kind": row.env_kind,
        "walk_capable": (row.env_kind or "").strip().lower() == DISPOSABLE,
        "target_origin": row.target_origin,
        "reset_procedure": row.reset_procedure,
        "max_walk_mutations_per_step": int(row.max_walk_mutations_per_step),
        "provisioned_by": row.provisioned_by,
        "provisioned_at": row.provisioned_at.isoformat() if row.provisioned_at else "",
        "expires_at": row.expires_at.isoformat() if row.expires_at else "",
        # Evidence is deliberately included: a tenant is entitled to see the
        # basis on which their environment was classified.
        "evidence": dict(row.evidence or {}),
    }


# ── A11.3 — revocation ──────────────────────────────────────────────────────


@router.post("/attestation/revocations", status_code=201)
async def revoke(
    payload: RevocationRequest, user: dict = Depends(_TENANT_ADMIN),
) -> dict:
    """Revoke a proof or an entire environment.

    IDEMPOTENT — revoking twice is a success. In an incident two responders
    will hit this at once, and an error that reads like the revocation failed is
    actively dangerous.

    Takes effect for every NEW dispatch. A crawl already admitted under a proof
    verified before this call runs to completion unless separately cancelled —
    see the honest limit documented in
    :mod:`app.services.attestation_revocation`.
    """
    tenant_id = _tenant_of(user)
    if payload.subject_type not in revocation.VALID_SUBJECTS:
        raise HTTPException(
            status_code=422,
            detail=(f"subject_type must be one of "
                    f"{'|'.join(revocation.VALID_SUBJECTS)}"))
    async with tenant_scoped_qec_session(tenant_id) as session:
        revocation_id, created = await revocation.record_revocation(
            session,
            tenant_id=tenant_id,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            revoked_by=_actor(user),
            reason=payload.reason,
        )
        await session.commit()
    revocation.invalidate_cache(tenant_id)
    return {
        "revoked": True, "revocation_id": revocation_id, "created": created,
        "subject_type": payload.subject_type, "subject_id": payload.subject_id,
        "effective": ("immediately for new dispatches; an already-admitted "
                      "crawl must be cancelled separately"),
    }


@router.get("/attestation/revocations")
async def list_revocations(user: dict = Depends(_TENANT_ADMIN)) -> dict:
    """The tenant's current revocation state.

    Reads THROUGH the cache (``use_cache=False``) so an operator who just
    revoked something sees it, rather than being told for up to 30 seconds that
    their revocation did not land.
    """
    tenant_id = _tenant_of(user)
    async with tenant_scoped_qec_session(tenant_id) as session:
        try:
            state = await revocation.current_revocations(
                session, tenant_id, use_cache=False)
        except revocation.RevocationUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))
    return {
        "revoked_proof_ids": list(state.proof_ids),
        "revoked_environment_ids": list(state.environment_ids),
        "total": state.total,
    }


# ── A11.1 — custody + the authoritative provisioning record (PLATFORM ONLY) ──


@router.post("/platform/attestation/provisioning-records", status_code=201)
async def certify_environment(
    payload: ProvisioningRecordRequest, user: dict = Depends(_SUPER_ADMIN),
) -> dict:
    """PLATFORM ADMIN ONLY — certify what an environment actually is.

    THE ROOT OF THE WHOLE TRUST CHAIN. Everything downstream is arithmetic on
    this statement: the issuer signs it, the explorer verifies the signature, and
    the walker mutates a customer's application on the strength of it. It is
    therefore the one place where a human being takes responsibility, and the row
    records who.

    Re-certifying an environment RETIRES the previous record in the same
    transaction — the partial unique index permits only one active record per
    environment, so there is never a tie for the issuer to break.
    """
    env_kind = (payload.env_kind or "").strip().lower()
    origin = normalize_origin(payload.target_origin)
    if not origin:
        raise HTTPException(
            status_code=422,
            detail=(f"target_origin {payload.target_origin!r} does not normalise "
                    f"to a scheme://host[:port] origin; the verifier treats an "
                    f"empty origin as a MISMATCH, so this record could never "
                    f"authorise anything"))
    tenant_id = str(payload.tenant_id).strip()

    async with tenant_scoped_qec_session(tenant_id) as session:
        previous = (await session.execute(
            select(EnvProvisioningRecordRow).where(
                EnvProvisioningRecordRow.tenant_id == tenant_id,
                EnvProvisioningRecordRow.environment_id == payload.environment_id,
                EnvProvisioningRecordRow.status == PROVISIONING_ACTIVE,
            )
        )).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if previous is not None:
            previous.status = PROVISIONING_RETIRED
            previous.retired_at = now
            previous.retired_by = _actor(user)
            await session.flush()

        record = EnvProvisioningRecordRow(
            provisioning_id=new_id(),
            tenant_id=tenant_id,
            app_id=str(payload.app_id).strip(),
            environment_id=str(payload.environment_id).strip(),
            env_kind=env_kind,
            target_origin=origin,
            reset_procedure=str(payload.reset_procedure or "")[:512],
            evidence=dict(payload.evidence or {}),
            provisioned_by=_actor(user),
            provisioned_at=now,
            expires_at=now + timedelta(days=int(payload.ttl_days)),
            max_walk_mutations_per_step=int(payload.max_walk_mutations_per_step),
            status=PROVISIONING_ACTIVE,
        )
        session.add(record)
        try:
            await session.flush()
        except Exception as exc:
            # The CHECK constraint on env_kind is the likely cause; surface it
            # as a 422 naming the vocabulary rather than a 500.
            raise HTTPException(
                status_code=422,
                detail=(f"could not record the certification "
                        f"({type(exc).__name__}) — env_kind must be one of "
                        f"disposable|staging|uat|test|dev|prod")) from exc
        await session.commit()
        provisioning_id = record.provisioning_id

    log = logger.warning if env_kind == DISPOSABLE else logger.info
    log("qec.attest.environment_certified tenant=%s app=%s env=%s kind=%s "
        "origin=%s by=%s retired=%s — %s",
        tenant_id, payload.app_id, payload.environment_id, env_kind, origin,
        _actor(user), previous.provisioning_id if previous else "-",
        ("this environment may now be granted SERVER-SIDE MUTATION proofs"
         if env_kind == DISPOSABLE else "no walk mutation is authorised"))
    return {
        "provisioning_id": provisioning_id,
        "tenant_id": tenant_id,
        "environment_id": payload.environment_id,
        "env_kind": env_kind,
        "walk_capable": env_kind == DISPOSABLE,
        "target_origin": origin,
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(days=int(payload.ttl_days))).isoformat(),
        "retired_previous": previous.provisioning_id if previous else "",
    }


@router.delete("/platform/attestation/provisioning-records/{provisioning_id}")
async def retire_certification(
    provisioning_id: str, tenant_id: str, user: dict = Depends(_SUPER_ADMIN),
) -> dict:
    """Withdraw a certification. No NEW proof is issued afterwards.

    NOT the same as revocation, and the difference matters in an incident:
    retiring stops future issuance; proofs ALREADY issued stay valid until they
    expire. To kill those too, revoke the environment as well. The endpoint says
    so in its response rather than leaving an operator to discover it.
    """
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (await session.execute(
            select(EnvProvisioningRecordRow).where(
                EnvProvisioningRecordRow.tenant_id == tenant_id,
                EnvProvisioningRecordRow.provisioning_id == provisioning_id,
            )
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="no such provisioning record")
        already = row.status != PROVISIONING_ACTIVE
        if not already:
            row.status = PROVISIONING_RETIRED
            row.retired_at = datetime.now(timezone.utc)
            row.retired_by = _actor(user)
            await session.commit()
    logger.warning("qec.attest.certification_retired tenant=%s id=%s by=%s",
                   tenant_id, provisioning_id, _actor(user))
    return {
        "retired": True, "provisioning_id": provisioning_id,
        "already_retired": already,
        "note": ("no NEW proof will be issued; proofs already issued remain "
                 "valid until they expire — revoke the environment to kill "
                 "those as well"),
    }


@router.post("/platform/attestation/keys", status_code=201)
async def create_or_rotate_issuer_key(
    payload: IssuerKeyRequest, request: Request,
    user: dict = Depends(_SUPER_ADMIN),
) -> dict:
    """Bootstrap or ROTATE the platform's issuer key.

    Returns the PUBLIC key and the exact ``QEC_ATTESTATION_PUBLIC_KEYS`` value
    to deploy. The private key is never returned, never logged, and never leaves
    the KMS envelope except into one signing scope at a time.

    PUBLISH BEFORE YOU SIGN. A freshly created key is ACTIVE immediately, so
    every explorer that has not yet been given its public key will refuse proofs
    with ``unknown_key_id``. The response carries the trust-store value for
    exactly this reason; the ordering is in ``docs/A11_KEY_CUSTODY.md``.
    """
    envelope = _envelope_or_503(request)
    async with tenant_scoped_qec_session(keys.PLATFORM_KEK_TENANT) as session:
        try:
            if payload.rotate:
                retired, fresh = await keys.rotate_issuer_key(
                    session, envelope, issuer=payload.issuer,
                    rotated_by=_actor(user), meta=payload.meta)
            else:
                retired, fresh = None, await keys.generate_issuer_key(
                    session, envelope, issuer=payload.issuer,
                    created_by=_actor(user), meta=payload.meta)
            published = await keys.publishable_keys(session)
        except keys.KeyCustodyError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        await session.commit()
    return {
        "kid": fresh.kid,
        "public_key": fresh.public_key,
        "issuer": fresh.issuer,
        "alg": "ed25519",
        "retired_kid": retired or "",
        "trust_store": {
            "QEC_ATTESTATION_PUBLIC_KEYS": keys.trust_store_env_value(published),
            "QEC_ATTESTATION_ISSUER": fresh.issuer,
        },
        "next_step": ("deploy the trust_store values to EVERY explorer worker "
                      "before relying on proofs signed by this key"),
    }


@router.get("/platform/attestation/keys")
async def list_issuer_keys(user: dict = Depends(_SUPER_ADMIN)) -> dict:
    """The PUBLIC keys the fleet should currently trust (active + retiring).

    This is the public-key DISTRIBUTION endpoint. It carries no secret, so a
    deployment pipeline may call it and paste the result straight into the
    explorer's configuration — which is the point: an operator transcribing key
    material by hand is an operator who will eventually transcribe it wrong.
    """
    async with tenant_scoped_qec_session(keys.PLATFORM_KEK_TENANT) as session:
        published = await keys.publishable_keys(session)
    return {
        "keys": [k.as_dict() for k in published],
        "trust_store": {
            "QEC_ATTESTATION_PUBLIC_KEYS": keys.trust_store_env_value(published),
            "QEC_ATTESTATION_ISSUER": published[0].issuer if published else "",
        },
        "count": len(published),
    }


@router.post("/platform/attestation/keys/{kid}/revoke")
async def revoke_issuer_key(kid: str, user: dict = Depends(_SUPER_ADMIN)) -> dict:
    """COMPROMISE RESPONSE — stop publishing a key entirely.

    Every proof this key ever signed becomes ``unknown_key_id`` at the verifier
    once the fleet's trust store is refreshed. That is the intended blast radius:
    a compromised key means every proof it signed is suspect, including the ones
    that look fine.

    Revoking the ACTIVE key leaves the platform with no signing authority, which
    is the correct fail-closed state — walk persistence is simply off until a new
    key is bootstrapped.
    """
    async with tenant_scoped_qec_session(keys.PLATFORM_KEK_TENANT) as session:
        changed = await keys.revoke_issuer_key(
            session, kid=kid, revoked_by=_actor(user))
        published = await keys.publishable_keys(session)
        await session.commit()
    return {
        "revoked": changed, "kid": kid,
        "already_revoked": not changed,
        "trust_store": {
            "QEC_ATTESTATION_PUBLIC_KEYS": keys.trust_store_env_value(published),
        },
        "urgent": ("refresh QEC_ATTESTATION_PUBLIC_KEYS on EVERY explorer "
                   "worker now — until you do, the revoked key is still trusted "
                   "by workers already running"),
    }


@router.post("/platform/attestation/keys/rewrap")
async def rewrap_issuer_keys(request: Request,
                             user: dict = Depends(_SUPER_ADMIN)) -> dict:
    """KEK rotation — re-wrap every sealed key under the current KMS key version.

    A different and much cheaper operation than rotating the Ed25519 issuer key:
    the signing key does not change, so no proof is invalidated, no public key
    moves, and no explorer needs reconfiguring. Only the wrapping changes.
    """
    envelope = _envelope_or_503(request)
    async with tenant_scoped_qec_session(keys.PLATFORM_KEK_TENANT) as session:
        try:
            count = await keys.rewrap_issuer_keys(session, envelope)
        except keys.KeyCustodyError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        await session.commit()
    return {"rewrapped": count,
            "note": "signing keys unchanged; no proof was invalidated"}
