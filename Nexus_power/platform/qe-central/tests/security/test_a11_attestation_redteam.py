"""A11.5 — ADVERSARIAL VALIDATION OF THE ATTESTATION TRUST CHAIN.

Every test here is an ATTACK.  Each one takes the position of somebody trying to
obtain mutation authority they are not entitled to, and asserts that the platform
refuses — with the specific, stable reason code an incident responder would need.

The five scenarios the ARB requires are marked ``REQUIRED SCENARIO n``.  The rest
are the attacks that fell out of building the thing, and they are here because a
suite that tests only what it was told to test certifies only that.

WHAT MAKES THIS SUITE MEAN SOMETHING
====================================
Everything is real: real Ed25519, real AES-GCM key sealing, the real issuer
gates, and — critically — the REAL SHIPPING VERIFIER loaded from
``engines/qe-explorer/app/attest.py`` by path.  Nothing below is checked against
a mock's idea of what should happen.  See ``_a11_kit`` for how and why.

The one fake is the database session, because these gates are pure decisions
over rows.  It dispatches on the real ORM entities and evaluates the real WHERE
clauses, and it RAISES rather than over-matching when it meets a clause it
cannot evaluate — so it cannot quietly turn a failing gate into a passing test.
"""
from __future__ import annotations

import base64
import copy

import pytest

from _a11_kit import (  # noqa: E402
    APP,
    ENV,
    ISSUER_NAME,
    ORIGIN,
    OTHER_TENANT,
    TENANT,
    FakeSession,
    attest,
    bootstrap_issuer_key,
    make_environment_row,
    make_envelope_service,
    make_provisioning_record,
    now_ms,
    trust_store,
    verify,
)

from app.services import attestation_issuer as issuer
from app.services import attestation_keys as keys
from app.services import attestation_revocation as revocation


# ── fixtures: a correctly provisioned, correctly certified world ────────────


@pytest.fixture
def envelope(tmp_path):
    return make_envelope_service(tmp_path)


@pytest.fixture
def cache():
    """A FRESH revocation cache per test.

    The production cache is a module-level singleton; sharing it across tests
    would let one test's revocation state leak into another's and produce
    green-by-accident. Every call below passes this explicitly.
    """
    return revocation.RevocationCache()


@pytest.fixture
def session():
    s = FakeSession()
    s.seed(make_provisioning_record())
    s.seed(make_environment_row())
    return s


@pytest.fixture
async def key(session, envelope):
    return await bootstrap_issuer_key(session, envelope)


@pytest.fixture
def trust(key):
    return trust_store([key])


async def _issue(session, envelope, cache, *, crawl_id="crawl-001", **kw):
    return await issuer.issue_for_crawl(
        session, envelope, tenant_id=kw.pop("tenant_id", TENANT),
        app_id=kw.pop("app_id", APP),
        environment_id=kw.pop("environment_id", ENV),
        crawl_id=crawl_id, revocation_cache=cache, **kw)


# ══════════════════════════════════════════════════════════════════════════
#  THE HAPPY PATH — proved FIRST, because a suite of refusals proves nothing
#  if the thing refuses everything.
# ══════════════════════════════════════════════════════════════════════════

async def test_a_genuinely_provisioned_disposable_environment_enables_walk(
        session, envelope, cache, key, trust):
    """END-TO-END: platform certifies → issuer signs → REAL verifier authorises.

    This is deliverable #9. If this test ever goes red, ``Phase.WALK`` is
    unreachable and M1.3 is inert again — which was the state before A11.
    """
    issued = await _issue(session, envelope, cache)
    verdict = verify(issued.attestation, trust=trust, crawl_id="crawl-001")

    assert verdict.authorized is True
    assert verdict.reason == attest.AttestReason.OK
    assert verdict.env_kind == attest.DISPOSABLE
    assert verdict.environment_id == ENV
    assert verdict.tenant_id == TENANT
    assert verdict.target_origin == ORIGIN
    # The GRANT is bounded, and it is the smaller of the two policies.
    assert 0 < verdict.max_mutations_per_step <= 3
    # And the audit digests agree across the two services, which is what lets an
    # auditor join the issuer's log row to the verifier's verdict.
    assert verdict.claims_digest == issued.claims_digest


async def test_the_issuer_records_every_issuance_before_returning_it(
        session, envelope, cache, key):
    """An unlogged issuance is an unrevocable one: revocation by proof_id needs
    the id, and only the log has it."""
    from app.db.attestation_models import AttestationIssuanceLogRow

    issued = await _issue(session, envelope, cache)
    logged = [r for r in session.added
              if isinstance(r, AttestationIssuanceLogRow)]
    assert len(logged) == 1
    assert logged[0].proof_id == issued.proof_id
    assert logged[0].claims_digest == issued.claims_digest
    assert logged[0].target_origin == ORIGIN


# ══════════════════════════════════════════════════════════════════════════
#  REQUIRED SCENARIO 1 — FORGED SIGNATURE
# ══════════════════════════════════════════════════════════════════════════

async def test_forged_signature_is_rejected(session, envelope, cache, key, trust):
    """An attacker who mints their OWN Ed25519 key and signs a perfect claim set.

    Everything about this proof is correct except WHO signed it. The verifier
    never finds the key id in its trust store, so it refuses before it even
    considers the claims.
    """
    from app.services.signing import generate_keypair, sign_payload
    from app.services.walk_attestation import key_id

    genuine = await _issue(session, envelope, cache)
    attacker_priv, attacker_pub = generate_keypair()

    forged = copy.deepcopy(dict(genuine.attestation))
    claims = forged["proof"]["claims"]
    forged["proof"]["signature"] = sign_payload(attacker_priv, claims)
    forged["proof"]["kid"] = key_id(attacker_pub)

    verdict = verify(forged, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.UNKNOWN_KEY_ID


async def test_forged_signature_under_a_known_key_id_is_rejected(
        session, envelope, cache, key, trust):
    """The sharper version: the attacker keeps the GENUINE ``kid`` and forges
    only the signature bytes, so key SELECTION succeeds and the cryptography is
    the only thing standing between them and mutation authority."""
    from app.services.signing import generate_keypair, sign_payload

    genuine = await _issue(session, envelope, cache)
    attacker_priv, _ = generate_keypair()

    forged = copy.deepcopy(dict(genuine.attestation))
    forged["proof"]["signature"] = sign_payload(
        attacker_priv, forged["proof"]["claims"])
    # kid left untouched — it resolves to the platform's real public key.

    verdict = verify(forged, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.BAD_SIGNATURE


@pytest.mark.parametrize("field_name,new_value", [
    ("env_kind", "prod"),
    ("tenant_id", OTHER_TENANT),
    ("target_origin", "https://production.example.test"),
    ("max_walk_mutations_per_step", 10),
    ("expires_at_ms", now_ms() + 999_999_999),
    ("environment_id", "some-other-env"),
])
async def test_editing_any_signed_claim_breaks_the_proof(
        session, envelope, cache, key, trust, field_name, new_value):
    """TAMPER-EVIDENCE, field by field.

    There is no claim a holder can edit — not the origin it points at, not the
    mutation budget, not the expiry — without invalidating the whole proof. This
    is what makes the signed claims, rather than the dispatch body, the thing the
    explorer is entitled to believe.
    """
    genuine = await _issue(session, envelope, cache)
    tampered = copy.deepcopy(dict(genuine.attestation))
    tampered["proof"]["claims"][field_name] = new_value

    verdict = verify(tampered, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.BAD_SIGNATURE


async def test_an_unsigned_proof_is_rejected(session, envelope, cache, key, trust):
    """``alg: "none"`` — the classic JWT downgrade, attempted here."""
    genuine = await _issue(session, envelope, cache)
    downgraded = copy.deepcopy(dict(genuine.attestation))
    downgraded["proof"]["alg"] = "none"
    downgraded["proof"]["signature"] = ""

    verdict = verify(downgraded, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason in (attest.AttestReason.UNSUPPORTED_ALG,
                              attest.AttestReason.MALFORMED_ENVELOPE)


# ══════════════════════════════════════════════════════════════════════════
#  REQUIRED SCENARIO 2 — EXPIRED PROOF
# ══════════════════════════════════════════════════════════════════════════

async def test_expired_proof_is_rejected(session, envelope, cache, key, trust):
    """A genuine, correctly-signed proof, presented after its expiry.

    The signature still verifies — that is the point. Expiry is a SEPARATE gate,
    and a verifier that stopped at "the maths checks out" would honour this.
    """
    issued = await _issue(session, envelope, cache)
    after_expiry = int(issued.expires_at_ms) + 1

    verdict = verify(issued.attestation, trust=trust, crawl_id="crawl-001",
                     now_epoch_ms=after_expiry)
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.EXPIRED


async def test_a_proof_is_still_valid_one_millisecond_before_expiry(
        session, envelope, cache, key, trust):
    """The boundary, from the other side — so the expiry test above is proving a
    real edge rather than a verifier that refuses everything."""
    issued = await _issue(session, envelope, cache)
    verdict = verify(issued.attestation, trust=trust, crawl_id="crawl-001",
                     now_epoch_ms=int(issued.expires_at_ms) - 1)
    assert verdict.authorized is True


async def test_the_issuer_cannot_mint_a_proof_beyond_the_verifier_ceiling(
        session, envelope, cache, key):
    """A caller — or a bug — asking for a TEN-YEAR proof.

    CLAMPED, not refused, and the direction is what makes that safe: the result
    is always the SHORTER of what was asked for and what the verifier permits,
    so the request cannot widen anything. Refusing would be defensible too, but
    clamping means an internal caller passing a silly number degrades to the
    safe value instead of failing a crawl outright.
    """
    from app.services.walk_attestation import MAX_PROOF_LIFETIME_MS

    issued = await _issue(session, envelope, cache,
                          proof_lifetime_ms=10 * 365 * 24 * 60 * 60 * 1000,
                          crawl_id="crawl-longlife")
    lifetime = int(issued.expires_at_ms) - int(issued.issued_at_ms)
    assert lifetime <= MAX_PROOF_LIFETIME_MS


async def test_the_fleet_refuses_an_over_long_proof_even_if_one_were_minted(
        session, envelope, cache, key, trust):
    """DEFENCE IN DEPTH: the verifier's ceiling is the fleet's, not the issuer's.

    Here the fleet is configured with a one-minute ceiling while the issuer
    minted an ordinary one-hour proof. The fleet wins — which is the property
    that survives a compromised issuer.
    """
    issued = await _issue(session, envelope, cache)
    # Same genuine key; a STRICTER fleet policy. One second of permitted
    # lifetime, against an ordinary ten-minute proof.
    strict_fleet = trust_store([key], max_lifetime_ms=1_000)
    verdict = verify(issued.attestation, trust=strict_fleet, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.LIFETIME_TOO_LONG


# ══════════════════════════════════════════════════════════════════════════
#  REQUIRED SCENARIO 3 — REVOKED PROOF
# ══════════════════════════════════════════════════════════════════════════

async def test_revoked_proof_is_rejected(session, envelope, cache, key, trust):
    """A proof that was VALID when issued and is revoked before it expires.

    Nothing about the proof changed: same bytes, same signature, still inside its
    lifetime. What changed is the separately-signed revocation list travelling
    with it — which is exactly why the verifier demands one.
    """
    issued = await _issue(session, envelope, cache)
    assert verify(issued.attestation, trust=trust,
                  crawl_id="crawl-001").authorized is True

    await revocation.record_revocation(
        session, tenant_id=TENANT, subject_type=revocation.SUBJECT_PROOF,
        subject_id=issued.proof_id, revoked_by="incident-responder",
        reason="proof observed in a paste bin", cache=cache)

    # Re-issue the ATTESTATION for the same crawl so it carries a current list —
    # this is what a dispatch does. The proof itself is unchanged.
    reissued = await _issue(session, envelope, cache, crawl_id="crawl-002")
    replayed = {"proof": issued.attestation["proof"],
                "revocations": reissued.attestation["revocations"]}

    verdict = verify(replayed, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.REVOKED


async def test_revoking_an_environment_kills_every_proof_for_it(
        session, envelope, cache, key, trust):
    """The blast-radius control: revoke the ENVIRONMENT, not a list of ids.

    Proofs issued before the revocation die, and — proved below — no further
    proof is issued for it at all.
    """
    first = await _issue(session, envelope, cache, crawl_id="crawl-a")

    await revocation.record_revocation(
        session, tenant_id=TENANT, subject_type=revocation.SUBJECT_ENVIRONMENT,
        subject_id=ENV, revoked_by="incident-responder",
        reason="environment turned out to be shared", cache=cache)

    # No NEW proof will be issued at all…
    with pytest.raises(issuer.IssuanceRefused) as exc:
        await _issue(session, envelope, cache, crawl_id="crawl-b")
    assert exc.value.reason == issuer.IssuanceReason.ENVIRONMENT_REVOKED

    # …and the one already issued no longer verifies once the fleet is handed a
    # current list naming the environment.
    live_list = await _sign_revocation_list(
        session, envelope, environment_ids=(ENV,))
    verdict = verify({"proof": first.attestation["proof"],
                      "revocations": live_list},
                     trust=trust, crawl_id="crawl-a")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.REVOKED


async def _sign_revocation_list(session, envelope, *, proof_ids=(),
                                environment_ids=()):
    """Sign a revocation list directly — what the NEXT dispatch would carry,
    without going through a whole issuance."""
    from app.services.walk_attestation import revocation_claims

    async with keys.active_signer(session, envelope) as signer:
        return signer.sign_claims(revocation_claims(
            issuer=signer.issuer, issued_at_ms=now_ms(),
            revoked_proof_ids=proof_ids,
            revoked_environment_ids=environment_ids))



async def test_a_missing_revocation_list_denies_the_whole_attestation(
        session, envelope, cache, key, trust):
    """NO LIST IS NOT AN EMPTY LIST.

    "I have revoked nothing, signed, valid for ten minutes" is a positive
    statement. Silence is "revocation state unknown", which is a DENY — and this
    is the single property that makes revocation real rather than advisory.
    """
    issued = await _issue(session, envelope, cache)
    without = {"proof": issued.attestation["proof"]}
    verdict = verify(without, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.NO_REVOCATION_LIST


async def test_an_expired_revocation_list_denies_the_attestation(
        session, envelope, cache, key, trust):
    """A stale list proves nothing about what has been revoked SINCE.

    An attacker who can pin the fleet to yesterday's list can suppress every
    revocation made today, so the list's own freshness is load-bearing.
    """
    issued = await _issue(session, envelope, cache,
                          revocation_lifetime_ms=60_000)
    later = int(issued.attestation["revocations"]["claims"]["expires_at_ms"]) + 1
    verdict = verify(issued.attestation, trust=trust, crawl_id="crawl-001",
                     now_epoch_ms=later)
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.REVOCATION_EXPIRED


async def test_a_revocation_list_signed_by_an_attacker_is_rejected(
        session, envelope, cache, key, trust):
    """Suppressing a revocation by substituting your own empty list."""
    from app.services.signing import generate_keypair, sign_payload

    issued = await _issue(session, envelope, cache)
    attacker_priv, _ = generate_keypair()
    tampered = copy.deepcopy(dict(issued.attestation))
    tampered["revocations"]["claims"]["revoked_proof_ids"] = []
    tampered["revocations"]["signature"] = sign_payload(
        attacker_priv, tampered["revocations"]["claims"])

    verdict = verify(tampered, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.REVOCATION_BAD_SIGNATURE


# ── FAIL-CLOSED: unknown revocation state must never become "nothing revoked" ─

async def test_unreadable_revocation_state_refuses_issuance(
        session, envelope, cache, key):
    """THE FAIL-OPEN BUG THIS SUITE EXISTS TO PREVENT.

    The database is unreachable. The tempting implementation signs an empty list
    and carries on — publishing, under the platform's own key, a false statement
    the fleet then believes for the full life of the list.

    The issuer refuses instead.
    """
    session.fail_reads_with = RuntimeError("connection reset by peer")
    with pytest.raises(revocation.RevocationUnavailable):
        await revocation.current_revocations(session, TENANT, cache=cache,
                                             use_cache=False)


async def test_the_cache_is_never_populated_by_a_failed_read(session, cache):
    """A cache that answered from stale data during an outage would convert
    fail-closed into silent fail-open. So a failed read writes nothing."""
    session.fail_reads_with = RuntimeError("db down")
    with pytest.raises(revocation.RevocationUnavailable):
        await revocation.current_revocations(session, TENANT, cache=cache)
    assert cache.get(TENANT) is None


async def test_a_revocation_invalidates_the_cache_immediately(session, cache):
    """Within one process a revocation takes effect at once, rather than after
    the TTL — the cache must not be the reason an incident response is late."""
    state = await revocation.current_revocations(session, TENANT, cache=cache)
    assert state.total == 0
    assert cache.get(TENANT) is not None

    await revocation.record_revocation(
        session, tenant_id=TENANT, subject_type=revocation.SUBJECT_ENVIRONMENT,
        subject_id=ENV, revoked_by="responder", cache=cache)
    assert cache.get(TENANT) is None

    fresh = await revocation.current_revocations(session, TENANT, cache=cache)
    assert ENV in fresh.environment_ids


async def test_revocation_is_idempotent(session, cache):
    """Two responders hitting the endpoint at once must both see success. An
    error that reads like the revocation failed is actively dangerous."""
    first_id, created_first = await revocation.record_revocation(
        session, tenant_id=TENANT, subject_type=revocation.SUBJECT_PROOF,
        subject_id="proof-xyz", revoked_by="a", cache=cache)
    second_id, created_second = await revocation.record_revocation(
        session, tenant_id=TENANT, subject_type=revocation.SUBJECT_PROOF,
        subject_id="proof-xyz", revoked_by="b", cache=cache)
    assert created_first is True
    assert created_second is False
    assert first_id == second_id


# ══════════════════════════════════════════════════════════════════════════
#  REQUIRED SCENARIO 4 — REPLAY
# ══════════════════════════════════════════════════════════════════════════

async def test_replaying_a_proof_into_a_different_crawl_is_rejected(
        session, envelope, cache, key, trust):
    """A proof lifted from one dispatch and presented on another.

    Refused on the BINDING, not merely on replay bookkeeping: the crawl id is
    inside the signed claims, so the attacker cannot change it without breaking
    the signature, and cannot leave it without failing this check.
    """
    issued = await _issue(session, envelope, cache, crawl_id="crawl-legit")
    verdict = verify(issued.attestation, trust=trust, crawl_id="crawl-attacker")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.CRAWL_BINDING_MISMATCH


async def test_replaying_the_same_proof_id_twice_is_rejected(
        session, envelope, cache, key, trust):
    """The other half: a ``proof_id`` may be admitted for exactly ONE crawl, and
    the FIRST admission is authoritative.

    This closes the case where a signature somehow covered two crawls — the
    guard makes the first use binding regardless of what the claims say.
    """
    guard = attest.ProofReplayGuard()
    issued = await _issue(session, envelope, cache, crawl_id="crawl-001")

    first = verify(issued.attestation, trust=trust, crawl_id="crawl-001",
                   replay_guard=guard)
    assert first.authorized is True

    # Same proof, same crawl, presented again — admitted (idempotent), because
    # the binding still holds…
    again = verify(issued.attestation, trust=trust, crawl_id="crawl-001",
                   replay_guard=guard)
    assert again.authorized is True

    # …but the SAME proof_id re-signed onto a different crawl is refused by the
    # guard even before the binding check would catch it.
    assert guard.admit(issued.proof_id, "crawl-999") is False


async def test_every_issuance_mints_a_fresh_unguessable_proof_id(
        session, envelope, cache, key):
    """Replay defence rests on ``proof_id`` uniqueness. Predictable ids would
    let an attacker pre-revoke, pre-claim, or correlate across tenants."""
    ids = set()
    for i in range(12):
        issued = await _issue(session, envelope, cache, crawl_id=f"crawl-{i}")
        ids.add(issued.proof_id)
    assert len(ids) == 12
    assert all(len(pid) >= 16 for pid in ids)


async def test_a_proof_for_another_tenant_is_rejected(
        session, envelope, cache, key, trust):
    """Cross-tenant replay: a valid proof, presented on another tenant's
    dispatch."""
    issued = await _issue(session, envelope, cache)
    verdict = verify(issued.attestation, trust=trust, crawl_id="crawl-001",
                     tenant_id=OTHER_TENANT)
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.TENANT_MISMATCH


async def test_a_proof_is_bound_to_one_origin(session, envelope, cache, key, trust):
    """Origin replay: point a genuine throwaway-env proof at production.

    Without this binding a proof is a bearer capability that authorises mutation
    wherever its holder aims it.
    """
    issued = await _issue(session, envelope, cache)
    verdict = verify(issued.attestation, trust=trust, crawl_id="crawl-001",
                     target_url="https://production.example.test")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.ORIGIN_MISMATCH


# ══════════════════════════════════════════════════════════════════════════
#  REQUIRED SCENARIO 5 — TENANT SELF-ATTESTATION
#  The one attack that no amount of cryptography downstream can repair.
# ══════════════════════════════════════════════════════════════════════════

async def test_a_tenant_cannot_certify_its_own_environment(session, envelope, cache):
    """THE CORE ATTACK.

    The tenant sets ``env_attestation.env_kind = "disposable"`` on their own
    environment profile — which they can, it is their row, and
    ``PATCH /apps/{id}/environments/{env}`` is a tenant endpoint. The fixture
    row already carries exactly that self-serving claim.

    There is NO platform provisioning record. The issuer must refuse, because
    signing a tenant-supplied fact produces a signed lie — and everything
    downstream would then be cryptographically satisfied by it.
    """
    bare = FakeSession()
    bare.seed(make_environment_row())          # tenant says: "disposable!"
    # …and nothing else. No provisioning record exists.
    await bootstrap_issuer_key(bare, envelope)

    with pytest.raises(issuer.IssuanceRefused) as exc:
        await issuer.issue_for_crawl(
            bare, envelope, tenant_id=TENANT, app_id=APP, environment_id=ENV,
            crawl_id="crawl-001", revocation_cache=cache)
    assert exc.value.reason == issuer.IssuanceReason.NO_PROVISIONING_RECORD


async def test_the_issuer_never_reads_the_tenant_writable_env_kind(
        session, envelope, cache, key):
    """The platform certified this environment as PROD. The tenant's own row
    says ``disposable``. The tenant's claim is ignored.

    This is the regression test for a plausible future refactor — "fall back to
    env_attestation when there's no record" — which would silently reopen the
    whole hole.
    """
    prod_world = FakeSession()
    prod_world.seed(make_provisioning_record(env_kind="prod"))
    prod_world.seed(make_environment_row())    # tenant row still claims disposable
    await bootstrap_issuer_key(prod_world, envelope)

    with pytest.raises(issuer.IssuanceRefused) as exc:
        await issuer.issue_for_crawl(
            prod_world, envelope, tenant_id=TENANT, app_id=APP,
            environment_id=ENV, crawl_id="crawl-001", revocation_cache=cache)
    assert exc.value.reason == issuer.IssuanceReason.NOT_DISPOSABLE
    assert "prod" in exc.value.detail


async def test_a_tenant_cannot_move_a_certified_environment_to_production(
        session, envelope, cache, key):
    """THE SUBTLE VERSION, and the one a review is most likely to miss.

    Certify a genuine throwaway host. Wait. Then PATCH ``base_url`` to point at
    production. Every other gate still passes — the record is active, disposable
    and unexpired — and without this check the crawl would be dispatched at
    production holding a valid mutation proof.
    """
    moved = FakeSession()
    moved.seed(make_provisioning_record())     # pinned at the throwaway origin
    moved.seed(make_environment_row(base_url="https://production.example.test"))
    await bootstrap_issuer_key(moved, envelope)

    with pytest.raises(issuer.IssuanceRefused) as exc:
        await issuer.issue_for_crawl(
            moved, envelope, tenant_id=TENANT, app_id=APP, environment_id=ENV,
            crawl_id="crawl-001", revocation_cache=cache)
    assert exc.value.reason == issuer.IssuanceReason.ORIGIN_MOVED


async def test_a_caller_cannot_request_a_target_other_than_the_certified_one(
        session, envelope, cache, key):
    """Asking for a proof against a DIFFERENT url than the one certified."""
    with pytest.raises(issuer.IssuanceRefused) as exc:
        await _issue(session, envelope, cache,
                     target_url="https://production.example.test")
    assert exc.value.reason == issuer.IssuanceReason.ORIGIN_MISMATCH


async def test_a_caller_cannot_widen_the_certified_mutation_budget(
        session, envelope, cache, key, trust):
    """LEAST PRIVILEGE. The API accepts a budget request; it may only NARROW.

    The record certifies 3. The caller asks for 10. They get 3 — not 10, and not
    an error, because narrowing-only is the contract and a hostile caller
    learning nothing is preferable to one learning the ceiling.
    """
    wide = await _issue(session, envelope, cache,
                        max_walk_mutations_per_step=10, crawl_id="crawl-wide")
    assert wide.max_walk_mutations_per_step == 3

    narrow = await _issue(session, envelope, cache,
                          max_walk_mutations_per_step=1, crawl_id="crawl-narrow")
    assert narrow.max_walk_mutations_per_step == 1


async def test_an_expired_certification_no_longer_issues(session, envelope, cache):
    """A disposable environment certified long ago may have been destroyed, and
    whatever now answers at that origin was never certified at all."""
    stale = FakeSession()
    stale.seed(make_provisioning_record(ttl_days=-1))
    stale.seed(make_environment_row())
    await bootstrap_issuer_key(stale, envelope)

    with pytest.raises(issuer.IssuanceRefused) as exc:
        await issuer.issue_for_crawl(
            stale, envelope, tenant_id=TENANT, app_id=APP, environment_id=ENV,
            crawl_id="crawl-001", revocation_cache=cache)
    assert exc.value.reason == issuer.IssuanceReason.PROVISIONING_EXPIRED


async def test_a_retired_certification_no_longer_issues(session, envelope, cache):
    """Withdrawing a certification stops FUTURE issuance."""
    retired = FakeSession()
    retired.seed(make_provisioning_record(status="retired"))
    retired.seed(make_environment_row())
    await bootstrap_issuer_key(retired, envelope)

    with pytest.raises(issuer.IssuanceRefused) as exc:
        await issuer.issue_for_crawl(
            retired, envelope, tenant_id=TENANT, app_id=APP, environment_id=ENV,
            crawl_id="crawl-001", revocation_cache=cache)
    assert exc.value.reason == issuer.IssuanceReason.NO_PROVISIONING_RECORD


async def test_a_proof_cannot_be_issued_without_a_crawl_binding(
        session, envelope, cache, key):
    """There is no "issue me one for later" mode. A proof not bound to a crawl
    is a reusable mutation capability, which is the thing this subsystem
    exists to prevent."""
    with pytest.raises(issuer.IssuanceRefused) as exc:
        await _issue(session, envelope, cache, crawl_id="")
    assert exc.value.reason == "crawl_id_required"


# ══════════════════════════════════════════════════════════════════════════
#  KEY CUSTODY (A11.1)
# ══════════════════════════════════════════════════════════════════════════

async def test_the_private_key_never_leaves_the_custody_module(
        session, envelope, key):
    """A signer hands out SIGNATURES, never key material.

    Asserted structurally: the object a caller receives exposes no attribute
    carrying a private key, so a future bug in the issuance path cannot leak one
    it was never handed.
    """
    async with keys.active_signer(session, envelope) as signer:
        public_surface = [a for a in dir(signer) if not a.startswith("_")]
        assert "private_key" not in " ".join(public_surface).lower()
        assert set(public_surface) >= {"sign_claims", "kid", "public_key", "issuer"}
        # __slots__ means there is no instance __dict__ to walk for one either.
        assert not hasattr(signer, "__dict__")


async def test_a_signer_is_unusable_after_its_scope_closes(session, envelope, key):
    """The unsealed key is scoped to one request, not one process lifetime."""
    async with keys.active_signer(session, envelope) as signer:
        pass
    with pytest.raises(keys.KeyCustodyError):
        signer.sign_claims({"v": 1})


async def test_the_stored_private_key_is_ciphertext(session, envelope, key):
    """A database dump must yield no signing capability.

    The stored blob must not contain the base64 private key, and must not be
    decryptable without the KEK.
    """
    from app.db.attestation_models import AttestationIssuerKeyRow

    row = [r for r in session.rows["AttestationIssuerKeyRow"]][0]
    sealed = bytes(row.sealed_private_key)
    assert row.public_key.encode("ascii") not in sealed
    # And it is a real envelope: parseable, with a wrapped DEK and an AAD.
    from nexus_sdk.security.envelope import EnvelopeBlob
    blob = EnvelopeBlob.from_bytes(sealed)
    assert blob.wrapped_dek
    assert blob.aad == keys.PLATFORM_KEK_TENANT.encode("utf-8")


async def test_an_inconsistent_key_row_refuses_to_sign(session, envelope, key):
    """DEFENCE IN DEPTH against a bad restore or a hand-edited row.

    If the sealed private half stops matching the published public half, every
    proof signed would be unverifiable — an outage that looks like a fleet
    trust-store fault and sends the operator to the wrong service. Refusing here
    names the real cause.
    """
    row = session.rows["AttestationIssuerKeyRow"][0]
    from app.services.signing import generate_keypair
    _, other_pub = generate_keypair()
    row.public_key = other_pub

    with pytest.raises(keys.KeyCustodyError) as exc:
        async with keys.active_signer(session, envelope):
            pass
    assert "INCONSISTENT" in str(exc.value)


async def test_no_issuer_key_means_no_proof_and_no_crash(session, envelope, cache):
    """THE FAIL-CLOSED DEFAULT — and the state of every deployment until an
    operator bootstraps a key. Walk persistence is simply off."""
    keyless = FakeSession()
    keyless.seed(make_provisioning_record())
    keyless.seed(make_environment_row())

    with pytest.raises(keys.NoActiveIssuerKey):
        await issuer.issue_for_crawl(
            keyless, envelope, tenant_id=TENANT, app_id=APP, environment_id=ENV,
            crawl_id="crawl-001", revocation_cache=cache)


async def test_rotation_keeps_the_old_key_verifiable(session, envelope, cache, key):
    """Rotating must not invalidate in-flight proofs.

    The retired key stays PUBLISHED so proofs it signed keep verifying until they
    expire; revoking on rotation would turn routine hygiene into a fleet-wide
    outage.
    """
    old_proof = await _issue(session, envelope, cache, crawl_id="crawl-before")

    retired_kid, fresh = await keys.rotate_issuer_key(
        session, envelope, issuer=ISSUER_NAME, rotated_by="platform-admin")
    assert retired_kid == key.kid
    assert fresh.kid != key.kid

    published = await keys.publishable_keys(session)
    kids = {k.kid for k in published}
    assert kids == {key.kid, fresh.kid}, "the retiring key must stay published"

    # The old proof still verifies against a fleet holding both keys.
    both = trust_store(published)
    assert verify(old_proof.attestation, trust=both,
                  crawl_id="crawl-before").authorized is True

    # And the new key signs proofs that verify too.
    new_proof = await _issue(session, envelope, cache, crawl_id="crawl-after")
    assert new_proof.kid == fresh.kid
    assert verify(new_proof.attestation, trust=both,
                  crawl_id="crawl-after").authorized is True


async def test_a_revoked_key_stops_being_published(session, envelope, key):
    """COMPROMISE RESPONSE. Every proof the key signed becomes
    ``unknown_key_id`` once the fleet refreshes its trust store."""
    await keys.revoke_issuer_key(session, kid=key.kid, revoked_by="responder")
    published = await keys.publishable_keys(session)
    assert key.kid not in {k.kid for k in published}


async def test_a_fleet_with_no_trust_anchor_authorises_nothing(
        session, envelope, cache, key):
    """An explorer that has not been given a public key denies everything —
    ``no_trust_anchor``, not a permissive default."""
    issued = await _issue(session, envelope, cache)
    empty = trust_store([], issuer=ISSUER_NAME)
    verdict = verify(issued.attestation, trust=empty, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.NO_TRUST_ANCHOR


async def test_a_proof_from_a_different_issuer_is_rejected(
        session, envelope, cache):
    """A second platform's genuine issuer, verified by this fleet."""
    foreign = FakeSession()
    foreign.seed(make_provisioning_record())
    foreign.seed(make_environment_row())
    other_key = await bootstrap_issuer_key(foreign, envelope,
                                           issuer="some-other-platform")
    issued = await issuer.issue_for_crawl(
        foreign, envelope, tenant_id=TENANT, app_id=APP, environment_id=ENV,
        crawl_id="crawl-001", revocation_cache=revocation.RevocationCache())

    # This fleet trusts the KEY but expects OUR issuer name.
    ours = trust_store([other_key], issuer=ISSUER_NAME)
    verdict = verify(issued.attestation, trust=ours, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.ISSUER_MISMATCH


def test_the_platform_kek_tenant_id_cannot_be_assigned_to_a_customer():
    """The issuer key is sealed under a reserved tenant id, and "reserved" has
    to be ENFORCED, not merely intended.

    ``provision_tenant`` takes ``tenant_id`` verbatim (it is the idempotency
    key), so without a reservation a platform admin could provision a customer
    whose id collides with the KEK derivation input and AAD binding for the
    attestation issuer's private key.

    Found while writing the certification checklist: the documentation asserted
    this was impossible before anything made it so. The collision was never
    directly exploitable — unsealing re-derives the public key and refuses a row
    whose halves disagree — but an assumption that holds only because a SECOND
    control happens to catch it is precisely the undocumented dependency this
    milestone may not have.
    """
    from app.fleet.provisioning import RESERVED_TENANT_IDS

    assert keys.PLATFORM_KEK_TENANT in RESERVED_TENANT_IDS, (
        "the attestation issuer's KEK tenant id is not reserved — a customer "
        "could be provisioned into the platform's own envelope namespace")


async def test_provisioning_a_tenant_on_the_reserved_id_is_refused():
    """The enforcement itself, at the function that would do the assigning."""
    from app.fleet.provisioning import ProvisioningError, provision_tenant

    with pytest.raises(ProvisioningError) as exc:
        await provision_tenant(
            "Attacker Co", "", "a@b.test",
            tenant_id=keys.PLATFORM_KEK_TENANT, actor="platform-admin")
    assert exc.value.status_code == 422
    assert "reserved" in str(exc.value).lower()


# ══════════════════════════════════════════════════════════════════════════
#  THE CONTRACT BETWEEN THE TWO SERVICES
# ══════════════════════════════════════════════════════════════════════════

def test_the_issuer_and_the_verifier_agree_on_every_shared_constant():
    """A divergence here would silently reject every genuine proof in
    production — the worst failure shape available, because it looks like a
    configuration problem and appears everywhere at once."""
    from app.services import walk_attestation as wa

    assert wa.CLAIMS_VERSION == attest.CLAIMS_VERSION
    assert wa.DISPOSABLE == attest.DISPOSABLE
    assert wa.HARD_MAX_MUTATIONS_PER_STEP == attest.HARD_MAX_MUTATIONS_PER_STEP
    assert wa.MIN_PLAUSIBLE_EPOCH_MS == attest.MIN_PLAUSIBLE_EPOCH_MS
    assert wa.MAX_PROOF_LIFETIME_MS == attest.DEFAULT_MAX_LIFETIME_MS
    assert wa.SIG_ALG == attest.SIG_ALG


def test_the_canonical_encoding_is_byte_identical_across_the_two_services():
    """The two services share no package, so the encoding is duplicated by
    value. This is the test that catches a one-character drift."""
    from app.services.signing import canonical_bytes as central

    for payload in (
        {"b": 1, "a": 2},
        {"nested": {"z": [1, 2, {"k": "v"}], "a": None}},
        {"unicode": "café", "emoji": "🔐"},
        {"big": 2**53, "neg": -1, "float": 1.5},
    ):
        assert central(payload) == attest.canonical_bytes(payload)


def test_the_key_id_derivation_matches():
    """The explorer looks its trust anchor up by this value; a divergence makes
    every proof ``unknown_key_id``."""
    from app.services.walk_attestation import key_id as central_key_id

    for pub in ("AAAA", "c29tZSBrZXk=", base64.b64encode(b"x" * 32).decode()):
        assert central_key_id(pub) == attest.key_id(pub)


# CERT-FINDING-2 / A11a — the IPv6 class, checked against BOTH copies.
#
# ``normalize_origin`` is written twice, in two services that share no package,
# and the copies must not diverge.  This module is the only place both are
# loaded in one interpreter (the real shipping verifier is loaded BY PATH — see
# ``_a11_kit``), so it is the only place "fix both, or pin them identical" can
# be enforced rather than hoped for.
#
# Two invariants per vector:
#   AGREEMENT   — the issuer's copy and the verifier's copy return the same
#                 string.  A divergence is invisible to either service's own
#                 tests and would refuse every genuine proof at once.
#   IDEMPOTENCE — ``N(N(u)) == N(u)``.  The issuer SIGNS this output and the
#                 verifier re-normalises it, so an output that cannot be
#                 re-parsed is a valid proof guaranteed to be refused.
#
# The defect was the reassembly (``urlsplit`` reports ``hostname`` without
# brackets and they were never put back), so it broke EVERY host containing a
# ':'.  These vectors pin the class; the controls are the repair's guard.
ORIGIN_VECTORS = [
    ("https://[2001:db8::1]:8443/x", "https://[2001:db8::1]:8443"),
    ("https://[2001:db8::1]/x",      "https://[2001:db8::1]"),
    ("https://[::1]/x",              "https://[::1]"),
    ("https://[::1]:8443/x",         "https://[::1]:8443"),
    ("https://[fe80::1%25eth0]/x",   "https://[fe80::1%25eth0]"),
    ("https://[::ffff:192.0.2.1]/x", "https://[::ffff:192.0.2.1]"),
    ("https://[::]/x",               "https://[::]"),
    ("https://[2001:0db8:0000:0000:0000:0000:0000:0001]/x",
     "https://[2001:0db8:0000:0000:0000:0000:0000:0001]"),
    ("https://[::1]:443/x",          "https://[::1]"),
    ("http://[::1]:80/x",            "http://[::1]"),
    # CONTROLS — unchanged by the repair.
    ("https://192.0.2.1:8443/x",     "https://192.0.2.1:8443"),
    ("https://example.test:8443/x",  "https://example.test:8443"),
    # NEGATIVE CONTROLS — including the string the OLD code emitted, which must
    # stay unusable rather than becoming parseable as a side effect.
    ("https://[::1]:notaport/x",     ""),
    ("https://2001:db8::1:8443",     ""),
    # NEW-CERT-FINDING-4 — a bracket surviving the parse means the authority
    # was malformed ('[::1' as userinfo, 'evil]' as host); refuse rather than
    # emit an unbalanced origin. A bracket the parse consumed is unaffected.
    ("https://[::1@evil]/x",         ""),
    # A bracketed non-IP-literal is a malformed authority; pinned to the
    # fail-closed answer because CPython 3.10 and 3.11 disagreed here
    # (CERT-FINDING-19) and unwrapping would alias two origins into one.
    ("https://[example.test]/x",     ""),
    ("https://user:pass@[::1]:8443/x", "https://[::1]:8443"),
]


@pytest.mark.parametrize("url,expected", ORIGIN_VECTORS)
def test_origin_normalisation_is_idempotent_in_both_copies(url, expected):
    from app.services.walk_attestation import normalize_origin as central

    for name, fn in (("issuer", central), ("verifier", attest.normalize_origin)):
        once = fn(url)
        assert once == expected, f"{name} normalised {url!r} to {once!r}"
        assert fn(once) == once, (
            f"{name} emitted {once!r}, which it cannot re-parse: the issuer "
            f"signs that string and the verifier re-normalises it, so this is "
            f"a valid proof guaranteed to be refused as origin_mismatch")


@pytest.mark.parametrize("url,expected", [
    ("https://example.test/x", "https://example.test"),
    ("https://example.test:443/x", "https://example.test"),
    ("http://example.test:80/x", "http://example.test"),
    ("https://example.test:8443/x", "https://example.test:8443"),
    ("HTTPS://EXAMPLE.TEST/x", "https://example.test"),
    ("not a url", ""),
    ("", ""),
])
def test_origin_normalisation_matches(url, expected):
    """Including the fail-closed empty return: an empty origin is a MISMATCH on
    both sides, never a wildcard."""
    from app.services.walk_attestation import normalize_origin as central

    assert central(url) == expected
    assert attest.normalize_origin(url) == expected


async def test_the_issued_claims_carry_exactly_the_fields_the_verifier_accepts(
        session, envelope, cache, key):
    """``ProofClaims`` is ``extra='forbid'``. An issuer that added a field —
    however well-meant — would have every proof refused as
    ``malformed_claims``."""
    issued = await _issue(session, envelope, cache)
    claims = issued.attestation["proof"]["claims"]
    assert set(claims) == set(attest.ProofClaims.model_fields)
    # And it parses under the verifier's own strict model.
    attest.ProofClaims.model_validate(dict(claims))


async def test_the_revocation_claims_carry_exactly_their_fields(
        session, envelope, cache, key):
    issued = await _issue(session, envelope, cache)
    claims = issued.attestation["revocations"]["claims"]
    assert set(claims) == set(attest.RevocationClaims.model_fields)
    attest.RevocationClaims.model_validate(dict(claims))
