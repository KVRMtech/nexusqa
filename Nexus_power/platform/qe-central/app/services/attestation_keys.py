"""A11.1 / T-WP-02 — CUSTODY OF THE PLATFORM'S ROOT OF TRUST.

WHAT THIS KEY IS
================
One Ed25519 private key.  Whoever holds it can mint a provisioning proof, and a
provisioning proof is the ONLY thing that turns on server-side mutation of a
customer's application (``Phase.WALK``).  There is no second factor behind it and
no human in the loop at mutation time — that is the whole design of M1.3.  So
this key is not "a credential"; it is the platform's root of trust, and it is
handled accordingly.

WHY THE KEY IS SEALED AND NOT KMS-RESIDENT — an operational choice, not a
constraint
=========================================================================
The strongest possible custody is a key that never exists outside the HSM:
Cloud KMS holds it and ``asymmetricSign`` is called for every signature.  We do
not do that.  The reason is worth stating precisely rather than leaving as
folklore — and the reason recorded here until 2026-08-21 was FALSE.

**CORRECTED (CERT-FINDING-1, A11 independent certification 2026-08-20).**  This
docstring used to assert that *Cloud KMS offers no Ed25519 asymmetric-signing
key type*, and that KMS-native signing would therefore mean changing the
algorithm on BOTH sides of a red-teamed verifier.  Both claims are wrong:

  * Cloud KMS provides ``EC_SIGN_ED25519`` — EdDSA over Curve25519, pure mode,
    raw input.  It is a supported asymmetric-signing algorithm, not an absence.
  * The verifier therefore does not change AT ALL.  ``app/attest.py`` pins
    ``SIG_ALG = "ed25519"`` and accepts trust anchors only as raw 32-byte
    Ed25519 public keys, and ``EC_SIGN_ED25519`` emits exactly those bytes.
    Only the issuer's sign call moves, plus unwrapping the raw 32 bytes from
    the DER ``SubjectPublicKeyInfo`` that ``GetPublicKey`` returns.

That matters beyond tidiness: a false impossibility is a decision nobody can
re-open.  It read as "there is no alternative", and it was the stated
justification for the residual risk below — a plaintext signing key in this
process's heap.  A future engineer weighing key custody must weigh a real
trade-off, not a fiction.

THE ACTUAL GROUNDS FOR THE ENVELOPE (KEK re-wrap) PATTERN
=========================================================
The choice may well still be correct, but it is a WEAKER case than the false one
it replaces, and stating it at full strength would repeat the original error in
a new costume.  Corrected again after re-certification (NEW-CERT-FINDING-3):
the first draft of this section claimed a latency and an availability advantage
that this system, AS BUILT, does not have.  Measured against the code:

  * PROVISIONING AND IAM — **this ground stands.** ``ASYMMETRIC_SIGN`` is a
    different key PURPOSE from the ``ENCRYPT_DECRYPT`` KEK this platform already
    provisions (M0.5), and a key's purpose is fixed at creation.  So it needs a
    new key, new IAM bindings and new rotation handling rather than reusing what
    exists.  That is real work, and it is the honest reason we have not done it.

  * LATENCY — **real but modest, and much smaller than first written.** The
    claim was "a KMS round-trip on every signature, versus one unseal per
    signer".  That is only true of a signer amortised across issuances, and ours
    is not: ``active_signer`` is opened inside ``issue_for_crawl`` and closed
    when that block exits (``attestation_issuer.py``).  So the envelope already
    pays ONE KMS ``decrypt`` per issuance; it then signs the proof AND the
    revocation list locally.  KMS-native would be TWO ``asymmetricSign``
    round-trips for the same issuance.  The true figure is roughly a doubling of
    KMS calls on the issuance path — not the order-of-magnitude the first
    version implied.

  * AVAILABILITY COUPLING — **this ground is FALSE and is withdrawn.** It said a
    KMS outage blocks a fresh unseal but not "a signer that is already live".
    No signer is ever already live: the scope is one issuance.  ``_unseal``
    raises ``KeyCustodyError`` on KMS failure and ``active_signer`` fails closed
    when ``envelope is None``, so **issuance availability is already fully
    coupled to KMS today.**  Moving to ``asymmetricSign`` would change WHICH KMS
    method issuance depends on, not WHETHER it depends on KMS.

Note what the second and third points cost us, because it is the same fact twice:
the signer's one-issuance scope is a deliberate SECURITY property — it bounds the
window in which a plaintext key is reachable to one request rather than one
process lifetime — and it is exactly that property that destroys the latency and
availability arguments for the envelope.  The custody design and the performance
argument for it pull in opposite directions, and only one of them is load-bearing.

So the honest summary is: we keep the envelope pattern because ``ASYMMETRIC_SIGN``
provisioning has not been done, at a cost of roughly halving the KMS calls per
issuance and a plaintext key in heap for the duration of one request.  That is an
accepted trade, revisitable on evidence, and a thinner justification than this
file has ever previously admitted to.  What we use is the ENVELOPE (KEK re-wrap)
pattern established in M0.5, which is also what ``services/signing.py`` was
written for ("the envelope sealing of the private key lives in the persistence
layer").

THE HONEST SECURITY STATEMENT — no undocumented assumptions
===========================================================
What the envelope model DOES give:

  * the private key is never on disk, never in an env var, never in config,
    never in the image, never in a deployment manifest, never in git;
  * at rest it is an AES-GCM ciphertext whose DEK is wrapped by Cloud KMS, so a
    full database dump — or a stolen backup — yields NO signing capability
    without live ``cloudkms.cryptoKeyEncrypterDecrypter`` permission on the KEK;
  * every unseal is a KMS API call, so every unseal is visible in Cloud Audit
    Logs whether or not this service is trusted to report it.

What it does NOT give, stated plainly:

  * the plaintext key exists in this process's heap for the duration of a sign.
    A running-process memory disclosure (heap dump, core file, arbitrary-read
    RCE) inside qe-central discloses it.  Python cannot zero a ``str``, so
    :meth:`Signer.close` drops references and no more; it is hygiene, not
    erasure, and it is not claimed as more.

That residual risk is bounded by rotation (below) and detected by KMS audit
logs.  It is a documented, accepted assumption — not a gap.  It is NOT, as this
file previously claimed, the unavoidable price of keeping the audited Ed25519
verifier: ``EC_SIGN_ED25519`` would remove it without touching that verifier, and
the only ground that survives scrutiny for not doing so is the provisioning and
IAM work above.  The risk is accepted on that ground and can be revisited on it.

THE PRIVATE KEY NEVER CROSSES A MODULE BOUNDARY
===============================================
Nothing in this module returns a plaintext private key.  :func:`active_signer`
yields a :class:`Signer`, which exposes ``sign_claims`` and its own public
identity and nothing else.  Callers — including
:mod:`app.services.attestation_issuer` — obtain SIGNATURES, never key material,
so a future bug in the issuance path cannot leak a key it was never handed.

ROTATION
========
``attestation_issuer_keys`` permits AT MOST ONE ``active`` row (a partial unique
index, enforced by the database).  Rotation is therefore a sequence, not a race:

  1. :func:`generate_issuer_key` mints a new keypair while the current one is
     still active — refused, by the index, unless the current one is retired
     first, which is why :func:`rotate_issuer_key` does both in ONE transaction;
  2. the retiring key stays ``retiring``, so its public key is still PUBLISHED
     and proofs already in flight keep verifying until they expire;
  3. once every proof the old key signed has expired (bounded by
     ``MAX_PROOF_LIFETIME_MS`` — 24h), the old key may be revoked.

PUBLISH BEFORE YOU SIGN.  The explorer fleet learns public keys from
configuration, so a key that signs before the fleet has been told about it
produces ``unknown_key_id`` on every dispatch.  :func:`publishable_keys` returns
BOTH active and retiring keys for exactly this reason, and the operational
sequence is in ``docs/A11_KEY_CUSTODY.md``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import new_id
from ..db.attestation_models import (
    KEY_ACTIVE,
    KEY_RETIRING,
    KEY_REVOKED,
    PUBLISHABLE_KEY_STATES,
    AttestationIssuerKeyRow,
)
from .signing import SIG_ALG, generate_keypair, public_key_of, sign_payload
from .walk_attestation import key_id

logger = logging.getLogger(__name__)

__all__ = [
    "PLATFORM_KEK_TENANT", "KeyCustodyError", "NoActiveIssuerKey",
    "IssuerPublicKey", "Signer",
    "active_signer", "generate_issuer_key", "publishable_keys",
    "revoke_issuer_key", "rewrap_issuer_keys", "rotate_issuer_key",
    "trust_store_env_value",
]

#: The KEK "tenant" the issuer key is sealed under, and the AAD bound into the
#: envelope.  The issuer key is FLEET infrastructure — it belongs to the
#: deployment, not to a customer — so it cannot honestly borrow a customer's
#: tenant id.  A reserved id that no tenant can be provisioned with (the double
#: underscores are not legal in a tenant slug) keeps the envelope's AAD binding
#: meaningful: a blob sealed for the platform cannot be replayed under a tenant,
#: and vice versa.
PLATFORM_KEK_TENANT = "__platform__"


class KeyCustodyError(RuntimeError):
    """A custody operation that cannot be completed safely.

    Always fatal to the operation in progress.  Never downgraded to a warning:
    every failure mode here ends in either "we cannot sign" or "we might sign
    with the wrong key", and both must stop the request.
    """


class NoActiveIssuerKey(KeyCustodyError):
    """No usable signing key exists.

    THE FAIL-CLOSED DEFAULT, and the state of every deployment until an operator
    runs the bootstrap.  It means walk persistence is off, which is exactly what
    should happen when the platform has no root of trust: nothing is issued,
    every dispatch verifies with ``no_proof``, and every crawl behaves precisely
    as it did before this feature existed.
    """


@dataclass(frozen=True)
class IssuerPublicKey:
    """A PUBLIC key and its state, for distribution.  Carries no secret, and is
    safe to log, serve over the API, and paste into a deployment manifest."""

    kid: str
    public_key: str
    issuer: str
    status: str
    created_at: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kid": self.kid, "public_key": self.public_key,
            "issuer": self.issuer, "status": self.status, "alg": SIG_ALG,
            "created_at": (self.created_at.isoformat()
                           if self.created_at is not None else ""),
        }


class Signer:
    """A scoped signing capability over ONE unsealed issuer key.

    Holds the plaintext private key privately and exposes only
    :meth:`sign_claims`.  Callers get signatures; they never get key material,
    so nothing downstream can leak, log or persist a key it was never handed.

    Not reusable across requests by design: it is created inside
    :func:`active_signer` and closed when that scope exits, so the window in
    which a plaintext key is reachable is one request rather than one process
    lifetime.
    """

    __slots__ = ("_private_key_b64", "kid", "public_key", "issuer", "_closed")

    def __init__(self, private_key_b64: str, *, kid: str, public_key: str,
                 issuer: str) -> None:
        self._private_key_b64 = private_key_b64
        self.kid = kid
        self.public_key = public_key
        self.issuer = issuer
        self._closed = False

    def sign_claims(self, claims: Mapping[str, Any]) -> dict[str, Any]:
        """Sign ``claims``, returning the envelope shape the verifier
        destructures (``{claims, alg, kid, signature}``).

        The claims are passed through UNCHANGED.  This function does not
        validate them and must not: validation belongs to
        ``walk_attestation.ProvisioningGrant.claims``, which refuses at mint
        time with a message naming the real cause.  A signer that silently
        repaired a claim would be signing something its caller did not ask for.
        """
        if self._closed:
            raise KeyCustodyError(
                "signer used after its scope closed — the key it held is no "
                "longer considered reachable, and re-opening it silently would "
                "widen the window this class exists to narrow")
        payload = dict(claims)
        return {
            "claims": payload,
            "alg": SIG_ALG,
            "kid": self.kid,
            "signature": sign_payload(self._private_key_b64, payload),
        }

    def close(self) -> None:
        """Drop the reference to the plaintext key.

        HYGIENE, NOT ERASURE.  CPython cannot zero a ``str``'s buffer, so the
        bytes may persist in the heap until the allocator reuses them.  This
        shortens the window and makes the intent explicit; it does not make a
        memory disclosure safe, and the module docstring says so.
        """
        self._private_key_b64 = ""
        self._closed = True

    def identity(self) -> IssuerPublicKey:
        return IssuerPublicKey(kid=self.kid, public_key=self.public_key,
                               issuer=self.issuer, status=KEY_ACTIVE)


# ── unsealing ───────────────────────────────────────────────────────────────


async def _unseal(envelope: Any, sealed: bytes) -> str:
    """Unwrap one sealed private key via the KMS-backed envelope service.

    Every failure is a :class:`KeyCustodyError`.  In particular an AAD mismatch
    — a blob sealed for something other than the platform issuer — is a hard
    failure rather than a retry: it means the row is not what this code thinks
    it is, and signing with it would attribute a proof to the wrong custodian.
    """
    from nexus_sdk.security.envelope import EnvelopeBlob

    try:
        blob = EnvelopeBlob.from_bytes(sealed)
    except Exception as exc:
        raise KeyCustodyError(
            f"issuer key blob is unreadable: {str(exc)[:200]}") from exc
    try:
        plaintext = await envelope.decrypt(
            PLATFORM_KEK_TENANT, blob,
            expected_aad=PLATFORM_KEK_TENANT.encode("utf-8"))
    except Exception as exc:
        # Deliberately does not echo the exception's full text: KMS client
        # errors can carry resource names and principal identifiers.
        raise KeyCustodyError(
            f"could not unseal the issuer key via KMS "
            f"({type(exc).__name__}) — refusing to issue") from exc
    return plaintext.decode("ascii")


@dataclass(frozen=True)
class _ActiveKey:
    kid: str
    public_key: str
    issuer: str
    sealed: bytes


async def _active_key_row(session: AsyncSession) -> _ActiveKey:
    row = (await session.execute(
        select(AttestationIssuerKeyRow).where(
            AttestationIssuerKeyRow.status == KEY_ACTIVE)
    )).scalar_one_or_none()
    if row is None:
        raise NoActiveIssuerKey(
            "no ACTIVE attestation issuer key — the platform has no root of "
            "trust configured, so no provisioning proof can be issued and walk "
            "persistence stays off (fail-closed)")
    return _ActiveKey(kid=row.kid, public_key=row.public_key,
                      issuer=row.issuer, sealed=bytes(row.sealed_private_key))


class active_signer:
    """Async context manager yielding a :class:`Signer` for the active key.

    Usage::

        async with active_signer(session, envelope) as signer:
            proof = signer.sign_claims(claims)

    The signer is closed on exit — including on an exception — so an error
    partway through issuance does not leave an unsealed key reachable from a
    traceback frame's locals for the lifetime of the request.
    """

    __slots__ = ("_session", "_envelope", "_signer")

    def __init__(self, session: AsyncSession, envelope: Any) -> None:
        if envelope is None:
            raise KeyCustodyError(
                "no EnvelopeService — the issuer key can only be unsealed "
                "through KMS, so with no envelope service there is nothing to "
                "sign with (fail-closed)")
        self._session = session
        self._envelope = envelope
        self._signer: Optional[Signer] = None

    async def __aenter__(self) -> Signer:
        key = await _active_key_row(self._session)
        private_key_b64 = await _unseal(self._envelope, key.sealed)
        # DEFENCE IN DEPTH: prove the unsealed private half really is the half
        # of the published public key before signing anything with it. A row
        # whose columns have drifted apart (a bad restore, a hand-edited row)
        # would otherwise sign proofs under a `kid` whose public key cannot
        # verify them — an outage that looks like a fleet trust-store problem
        # and sends the operator to the wrong service entirely.
        try:
            derived = public_key_of(private_key_b64)
        except Exception as exc:
            raise KeyCustodyError(
                f"unsealed issuer key is not a valid Ed25519 private key "
                f"({type(exc).__name__})") from exc
        if derived != key.public_key or key_id(derived) != key.kid:
            raise KeyCustodyError(
                f"issuer key row kid={key.kid} is INCONSISTENT: the unsealed "
                f"private key does not match the published public key. "
                f"Refusing to sign — every proof would be unverifiable.")
        self._signer = Signer(private_key_b64, kid=key.kid,
                              public_key=key.public_key, issuer=key.issuer)
        return self._signer

    async def __aexit__(self, *exc_info: Any) -> bool:
        if self._signer is not None:
            self._signer.close()
            self._signer = None
        return False


# ── custody operations ──────────────────────────────────────────────────────


async def generate_issuer_key(
    session: AsyncSession,
    envelope: Any,
    *,
    issuer: str,
    created_by: str,
    meta: Optional[Mapping[str, Any]] = None,
) -> IssuerPublicKey:
    """Mint a new issuer keypair, seal the private half, and store it ACTIVE.

    Refused by the database's partial unique index if an active key already
    exists — deliberately.  Replacing the root of trust must go through
    :func:`rotate_issuer_key`, which retires the incumbent in the SAME
    transaction, so the fleet is never in a state where two different keys are
    both legitimately signing.
    """
    from nexus_sdk.security.envelope import EnvelopeBlob  # noqa: F401 (contract)

    issuer = (issuer or "").strip()
    if not issuer:
        raise KeyCustodyError(
            "issuer name is required — it is bound into every claim this key "
            "signs and must equal the explorer fleet's QEC_ATTESTATION_ISSUER")
    if envelope is None:
        raise KeyCustodyError(
            "no EnvelopeService — refusing to generate an issuer key that "
            "could only be stored in the clear")

    private_key_b64, public_key_b64 = generate_keypair()
    try:
        blob = await envelope.encrypt(
            PLATFORM_KEK_TENANT, private_key_b64.encode("ascii"),
            aad=PLATFORM_KEK_TENANT.encode("utf-8"))
    except Exception as exc:
        raise KeyCustodyError(
            f"could not seal the new issuer key via KMS "
            f"({type(exc).__name__}) — nothing was stored") from exc
    finally:
        # The generated plaintext is not needed past this point; see
        # Signer.close for what this does and does not achieve.
        private_key_b64 = ""

    kid = key_id(public_key_b64)
    row = AttestationIssuerKeyRow(
        kid=kid,
        public_key=public_key_b64,
        issuer=issuer,
        sealed_private_key=blob.to_bytes(),
        kek_provider=str(getattr(blob, "provider", "") or ""),
        kek_id=str(getattr(blob, "kek_id", "") or "")[:500],
        status=KEY_ACTIVE,
        created_by=str(created_by or "")[:200],
        meta=dict(meta or {}),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise KeyCustodyError(
            "an ACTIVE issuer key already exists — rotate it (retire, then "
            "generate) rather than creating a second signing authority"
        ) from exc

    # AUDIT: never the key, never the blob. The kid and the KMS key name are
    # exactly what an auditor needs and exactly what an attacker cannot use.
    logger.warning(
        "qec.attest.issuer_key_generated kid=%s issuer=%s kek_provider=%s "
        "kek_id=%s by=%s — a NEW PLATFORM ROOT OF TRUST now exists; publish "
        "its public key to every explorer BEFORE it signs anything",
        kid, issuer, row.kek_provider, row.kek_id, row.created_by)
    return IssuerPublicKey(kid=kid, public_key=public_key_b64, issuer=issuer,
                           status=KEY_ACTIVE, created_at=row.created_at)


async def rotate_issuer_key(
    session: AsyncSession,
    envelope: Any,
    *,
    issuer: str,
    rotated_by: str,
    meta: Optional[Mapping[str, Any]] = None,
) -> tuple[Optional[str], IssuerPublicKey]:
    """Retire the current key and mint its successor in ONE transaction.

    Returns ``(retired_kid_or_None, new_public_key)``.

    The old key becomes ``retiring``, NOT ``revoked``: proofs it already signed
    stay legitimate until they expire (at most ``MAX_PROOF_LIFETIME_MS``), and
    its public key therefore stays in :func:`publishable_keys`.  Revoking a key
    on rotation would invalidate every in-flight crawl's proof at once, which
    turns a routine hygiene operation into a fleet-wide outage.
    """
    retired_kid: Optional[str] = None
    current = (await session.execute(
        select(AttestationIssuerKeyRow).where(
            AttestationIssuerKeyRow.status == KEY_ACTIVE)
    )).scalar_one_or_none()
    if current is not None:
        retired_kid = current.kid
        current.status = KEY_RETIRING
        current.retired_at = datetime.now(timezone.utc)
        # Flushed BEFORE the insert so the one-active index sees the retirement.
        await session.flush()

    fresh = await generate_issuer_key(
        session, envelope, issuer=issuer, created_by=rotated_by,
        meta={**dict(meta or {}), "rotated_from": retired_kid or ""})
    logger.warning(
        "qec.attest.issuer_key_rotated retired_kid=%s new_kid=%s by=%s — the "
        "retired key still VERIFIES in-flight proofs and must stay published "
        "until they expire", retired_kid or "-", fresh.kid, rotated_by)
    return retired_kid, fresh


async def revoke_issuer_key(session: AsyncSession, *, kid: str,
                            revoked_by: str) -> bool:
    """Mark a key ``revoked`` — a COMPROMISE response, not a rotation step.

    A revoked key drops out of :func:`publishable_keys` immediately, so every
    proof it ever signed becomes ``unknown_key_id`` at the verifier as soon as
    the fleet's trust store is refreshed.  That is the intended blast radius:
    if the key is compromised, every proof it signed is suspect, including ones
    that look legitimate.

    Revoking the ACTIVE key leaves the platform with no signing authority, and
    that is correct — no root of trust means no proofs, which means walk
    persistence is off until an operator bootstraps a new key.
    """
    result = await session.execute(
        update(AttestationIssuerKeyRow)
        .where(AttestationIssuerKeyRow.kid == str(kid or "").strip(),
               AttestationIssuerKeyRow.status != KEY_REVOKED)
        .values(status=KEY_REVOKED, retired_at=datetime.now(timezone.utc))
    )
    changed = int(result.rowcount or 0) > 0
    if changed:
        logger.error(
            "qec.attest.issuer_key_REVOKED kid=%s by=%s — every proof signed "
            "by this key must be treated as compromised; refresh every "
            "explorer's QEC_ATTESTATION_PUBLIC_KEYS NOW", kid, revoked_by)
    return changed


async def publishable_keys(session: AsyncSession) -> list[IssuerPublicKey]:
    """The PUBLIC keys an explorer fleet should currently trust.

    Active AND retiring — a fleet that trusts only the active key would refuse
    every proof minted moments before a rotation, which is a self-inflicted
    outage on a routine operation.  Revoked keys are excluded by construction.

    Ordered active-first so the head of the list is the key now signing; the
    explorer's trust store is a set, so order is presentation only.
    """
    rows = (await session.execute(
        select(AttestationIssuerKeyRow)
        .where(AttestationIssuerKeyRow.status.in_(PUBLISHABLE_KEY_STATES))
        .order_by(AttestationIssuerKeyRow.status.asc(),
                  AttestationIssuerKeyRow.created_at.desc())
    )).scalars().all()
    return [IssuerPublicKey(kid=r.kid, public_key=r.public_key, issuer=r.issuer,
                            status=r.status, created_at=r.created_at)
            for r in rows]


async def rewrap_issuer_keys(session: AsyncSession, envelope: Any) -> int:
    """Re-wrap every stored key's DEK under the CURRENT KEK version.

    This is KEK rotation (the M0.5 re-wrap pattern), which is a different and
    much cheaper operation than rotating the Ed25519 issuer key: the signing key
    does not change, so no proof is invalidated, no public key moves, and the
    fleet needs no reconfiguration.  Only the wrapping changes.

    Returns the number of rows re-wrapped.  Revoked keys are re-wrapped too —
    they are retained for audit, and a blob nobody can decrypt is not evidence.
    """
    from nexus_sdk.security.envelope import EnvelopeBlob

    if envelope is None:
        raise KeyCustodyError("no EnvelopeService — cannot re-wrap")
    rows = (await session.execute(select(AttestationIssuerKeyRow))).scalars().all()
    rewrapped = 0
    for row in rows:
        try:
            old = EnvelopeBlob.from_bytes(bytes(row.sealed_private_key))
            fresh = await envelope.rotate_kek(
                PLATFORM_KEK_TENANT, old,
                expected_aad=PLATFORM_KEK_TENANT.encode("utf-8"))
        except Exception as exc:
            # One unreadable row must not abort the rotation of the others; the
            # operator needs to know WHICH row is bad, and the rest still get
            # their new wrapping.
            logger.error("qec.attest.issuer_key_rewrap_failed kid=%s error=%s",
                         row.kid, type(exc).__name__)
            continue
        row.sealed_private_key = fresh.to_bytes()
        row.kek_id = str(getattr(fresh, "kek_id", "") or "")[:500]
        row.kek_provider = str(getattr(fresh, "provider", "") or "")
        rewrapped += 1
    logger.warning("qec.attest.issuer_keys_rewrapped count=%d of=%d",
                   rewrapped, len(rows))
    return rewrapped


def trust_store_env_value(keys: Sequence[IssuerPublicKey]) -> str:
    """The exact string an explorer's ``QEC_ATTESTATION_PUBLIC_KEYS`` wants.

    Comma-separated base64 public keys — the format
    ``app.config.Settings.attestation_trust_store`` splits on.  Generated here
    rather than described in a runbook so the value an operator pastes is
    produced by the same code that owns the keys, and cannot drift from it.
    """
    return ",".join(k.public_key for k in keys if (k.public_key or "").strip())
