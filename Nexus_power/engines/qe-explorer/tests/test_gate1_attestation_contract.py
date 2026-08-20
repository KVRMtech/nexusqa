"""Gate 1 / T-WP-02 — THE EXPLORER HALF of the walk-attestation contract.

THE HOLE THIS CLOSES.  :mod:`app.attest` is a complete, fail-closed verifier and
has been since M1.3.  Nothing minted a proof for it to verify, so every path
through it ended in a DENY and walk persistence (T-WP-01) has been
shipped-but-unreachable ever since.  ``qe-central``'s
``app/services/walk_attestation.py`` is now the issuer half.

THE SEAM CANNOT BE TESTED BY IMPORTING BOTH SIDES: qe-explorer and qe-central each
ship a top-level ``app`` package and collide in one interpreter.  So — following
the shape M1.7 established for the business-rule wire contract — the seam is
frozen as DATA in ``contracts/gate1_walk_attestation_v1.json`` and each side
asserts against it in its own process.

IT IS FROZEN AS A SIGNATURE, NOT AS A FIELD LIST, and that is stronger than the
M1.7 contract it copies.  A field-shape contract proves both sides AGREE ABOUT
NAMES; this one carries a real Ed25519 envelope minted by the real issuer, so
what is asserted here is that THE PRODUCTION VERIFIER ACCEPTS PRODUCTION ISSUER
OUTPUT — the actual question.  If the canonical encoding drifts on either side,
if a claim is renamed, if the key-id derivation changes, this file goes red.

See ``platform/qe-central/tests/contract/test_gate1_walk_attestation_contract.py``
for the other half, which re-mints this exact envelope from the recorded inputs
and proves the issuer deterministic.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app import attest


def _contract() -> dict:
    """Load the frozen contract by walking up to the ``Nexus_power`` root.

    Walked rather than hard-coded because this suite is collected from the
    SERVICE root in CI and from the repository root by some local runners.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "gate1_walk_attestation_v1.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError(
        "contracts/gate1_walk_attestation_v1.json not found above %s — the "
        "frozen attestation contract is the only thing tying this verifier to "
        "qe-central's issuer, and it must not be deleted to make a test pass"
        % here)


CONTRACT = _contract()
GRANT = CONTRACT["grant"]
ISSUED_MS = int(CONTRACT["issued_at_ms"])
#: A moment comfortably inside the proof's hour-long life.
NOW_MS = ISSUED_MS + 60_000


def _trust(**overrides) -> attest.TrustStore:
    kwargs = dict(
        public_keys=[CONTRACT["test_keys"]["public_key_b64"]],
        issuer=CONTRACT["issuer"],
    )
    kwargs.update(overrides)
    return attest.TrustStore.from_public_keys(**kwargs)


def _verify(payload=None, *, trust=None, now_ms=NOW_MS, **overrides):
    """Verify with a FRESH replay guard every time.

    The module-level guard is process-global and remembers proof ids, so a suite
    sharing it would have the second test that verifies this golden fail as
    ``proof_replayed`` — a real property of the verifier, asserted deliberately
    in its own test below rather than allowed to leak into every other one.
    """
    kwargs = dict(
        crawl_id=GRANT["crawl_id"], tenant_id=GRANT["tenant_id"],
        target_url=GRANT["target_url"],
    )
    kwargs.update(overrides)
    return attest.verify_provisioning_proof(
        CONTRACT["attestation"] if payload is None else payload,
        trust=trust or _trust(), now_epoch_ms=now_ms,
        replay_guard=attest.ProofReplayGuard(), **kwargs)


# ─── THE POINT: production issuer output, production verifier ───────────────

def test_the_verifier_accepts_a_proof_the_issuer_minted():
    """The single most important assertion in Gate 1's WP6.

    Before the issuer existed this could not be written at all, and every other
    attestation test in this repository asserts a REFUSAL.  This is the first
    time anything has proven the authorising path reachable.
    """
    verdict = _verify()
    assert verdict.authorized, verdict.reason
    assert verdict.reason == attest.AttestReason.OK


def test_the_authorised_mutation_budget_is_the_lesser_of_claim_and_policy():
    """Least privilege, and the direction of it: an issuer cannot WIDEN this
    fleet's policy from the outside."""
    generous = _verify(trust=_trust(max_mutations_per_step=3))
    assert generous.authorized
    assert generous.max_mutations_per_step == 3

    strict = _verify(trust=_trust(max_mutations_per_step=1))
    assert strict.authorized
    assert strict.max_mutations_per_step == 1, (
        "the claim asked for 3 and this fleet permits 1 — the fleet wins")


def test_the_key_id_derivations_agree_across_the_two_services():
    """A divergence here makes every genuine proof ``unknown_key_id`` — the worst
    failure shape there is, because it looks like a configuration problem and
    appears everywhere at once."""
    assert (attest.key_id(CONTRACT["test_keys"]["public_key_b64"])
            == CONTRACT["test_keys"]["kid"]
            == CONTRACT["attestation"]["proof"]["kid"])


def test_the_origin_normalisations_agree_across_the_two_services():
    assert (attest.normalize_origin(GRANT["target_url"])
            == CONTRACT["expected_target_origin"]
            == CONTRACT["attestation"]["proof"]["claims"]["target_origin"])


# ─── tamper-evidence, field by field ────────────────────────────────────────

@pytest.mark.parametrize("field_name,new_value", [
    ("crawl_id", "crawl-somebody-elses"),
    ("tenant_id", "tenant-attacker"),
    ("environment_id", "env-production"),
    ("target_origin", "https://production.example.test"),
    ("env_kind", "persistent"),
    ("max_walk_mutations_per_step", 9),
    ("expires_at_ms", ISSUED_MS + 90 * 24 * 3600 * 1000),
    ("issuer", "qe-central.someone-else"),
    ("proof_id", "ffffffffffffffffffffffffffffffff"),
])
def test_editing_any_claim_invalidates_the_proof(field_name, new_value):
    """There is NO field a holder can edit — not the crawl id, not the expiry,
    not the mutation budget.  One changed byte changes the canonical encoding and
    the signature no longer verifies."""
    payload = copy.deepcopy(CONTRACT["attestation"])
    payload["proof"]["claims"][field_name] = new_value
    verdict = _verify(payload)
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.BAD_SIGNATURE, (
        f"editing {field_name} was caught as {verdict.reason!r}; it must fail on "
        f"the SIGNATURE, because a claim-level check could be evaded by editing "
        f"a field that check does not read")


def test_editing_the_revocation_list_invalidates_it():
    """The revocation list is separately signed for exactly this reason: a holder
    who could strip their own id from it would have defeated revocation."""
    payload = copy.deepcopy(CONTRACT["attestation"])
    payload["revocations"]["claims"]["revoked_proof_ids"] = []
    verdict = _verify(payload)
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.REVOCATION_BAD_SIGNATURE


def test_a_proof_signed_by_an_unknown_key_is_refused():
    verdict = _verify(trust=_trust(public_keys=[
        "3+Xk1qYJ0ZQ5lqvHnGvMYm5rTjLxV0oV3kQ0J0hZ8Rk="]))
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.UNKNOWN_KEY_ID


# ─── binding: the proof authorises ONE crawl, tenant and origin ─────────────

def test_a_proof_is_bound_to_one_crawl():
    """A proof lifted from one dispatch and replayed on another is refused on the
    BINDING, not merely on replay — so it fails even on a fresh worker whose
    replay guard has never seen it."""
    verdict = _verify(crawl_id="crawl-different")
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.CRAWL_BINDING_MISMATCH


def test_a_proof_is_bound_to_one_tenant():
    verdict = _verify(tenant_id="tenant-other")
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.TENANT_MISMATCH


def test_a_proof_is_bound_to_one_origin():
    """The claim that stops a throwaway-staging proof from authorising mutation
    against production."""
    verdict = _verify(target_url="https://production.example.test/apply")
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.ORIGIN_MISMATCH


def test_a_different_port_is_a_different_origin():
    verdict = _verify(target_url="https://staging.example.test:9999/apply")
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.ORIGIN_MISMATCH


# ─── time, and the ceiling the verifier enforces on the issuer ─────────────

def test_an_expired_proof_is_refused():
    verdict = _verify(now_ms=ISSUED_MS + 3_600_000 + 400_000)
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.EXPIRED


def test_a_proof_from_the_future_is_refused():
    verdict = _verify(now_ms=ISSUED_MS - 400_000)
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.ISSUED_IN_FUTURE


def test_the_fleet_enforces_its_own_lifetime_ceiling():
    """A compromised or buggy issuer that mints a long-lived proof still gets a
    proof THIS fleet refuses: the window in which a stolen proof is useful is
    bounded by the verifier's policy, not by the issuer's good behaviour."""
    verdict = _verify(trust=_trust(max_lifetime_ms=60_000))
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.LIFETIME_TOO_LONG


def test_an_expired_revocation_list_denies_the_whole_attestation():
    """An unusable list is not "no revocations known", it is "revocation state
    unknown" — and that is a DENY.  The list is deliberately short-lived, so this
    is the ordinary failure mode of a dispatch that sat in a queue too long."""
    verdict = _verify(now_ms=ISSUED_MS + 600_000 + 400_000)
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.REVOCATION_EXPIRED


def test_an_attestation_with_no_revocation_list_is_refused():
    payload = copy.deepcopy(CONTRACT["attestation"])
    payload.pop("revocations")
    verdict = _verify(payload)
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.NO_REVOCATION_LIST


# ─── revocation actually revokes ────────────────────────────────────────────

def test_a_revoked_proof_id_is_refused():
    """The mechanism an expiry cannot provide.  Re-signing the list with this
    proof's id in it withdraws it immediately, without waiting out its life."""
    payload = copy.deepcopy(CONTRACT["attestation"])
    revoked = _revocation_with(proof_ids=[GRANT["proof_id"]])
    if revoked is None:
        pytest.skip("qe-central issuer not importable from the explorer service")
    payload["revocations"] = revoked
    verdict = _verify(payload)
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.REVOKED


def test_a_revoked_environment_id_is_refused():
    payload = copy.deepcopy(CONTRACT["attestation"])
    revoked = _revocation_with(environment_ids=[GRANT["environment_id"]])
    if revoked is None:
        pytest.skip("qe-central issuer not importable from the explorer service")
    payload["revocations"] = revoked
    verdict = _verify(payload)
    assert not verdict.authorized
    assert verdict.reason == attest.AttestReason.REVOKED


def _revocation_with(*, proof_ids=(), environment_ids=()):
    """Re-sign a revocation list HERE, using only the primitives this service
    already has.

    Deliberately not an import of the qe-central issuer — that is the collision
    this contract exists to route around.  It re-implements nothing either: the
    canonical encoding is ``attest.canonical_bytes``, the verifier's own, so a
    list built this way is signed exactly as the issuer signs one.  If that
    equivalence ever stopped holding, the golden list in the contract file would
    stop verifying and the tests above would say so first.
    """
    try:
        import base64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except Exception:
        return None
    priv = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(CONTRACT["test_keys"]["private_key_b64"], validate=True))
    claims = dict(CONTRACT["attestation"]["revocations"]["claims"])
    claims["revoked_proof_ids"] = sorted(proof_ids)
    claims["revoked_environment_ids"] = sorted(environment_ids)
    signature = base64.b64encode(
        priv.sign(attest.canonical_bytes(claims))).decode("ascii")
    return {"claims": claims, "alg": attest.SIG_ALG,
            "kid": CONTRACT["attestation"]["revocations"]["kid"],
            "signature": signature}


# ─── replay ─────────────────────────────────────────────────────────────────

def test_re_dispatching_the_same_crawl_is_idempotent_not_a_replay():
    """The guard binds ``proof_id`` -> ``crawl_id`` and makes the FIRST admission
    authoritative.  A worker that retries the same dispatch is therefore NOT
    refused, which is the correct behaviour: a redelivered message must not
    become an outage."""
    guard = attest.ProofReplayGuard()
    kwargs = dict(trust=_trust(), crawl_id=GRANT["crawl_id"],
                  tenant_id=GRANT["tenant_id"], target_url=GRANT["target_url"],
                  now_epoch_ms=NOW_MS, replay_guard=guard)

    first = attest.verify_provisioning_proof(CONTRACT["attestation"], **kwargs)
    second = attest.verify_provisioning_proof(CONTRACT["attestation"], **kwargs)

    assert first.authorized
    assert second.authorized, "a redelivered dispatch is the same admission"


def test_one_proof_id_may_not_be_minted_for_two_crawls():
    """WHAT ``PROOF_REPLAYED`` ACTUALLY CATCHES, which is an ISSUER defect rather
    than a holder attack.

    The claims bind a proof to one crawl, so a stolen envelope replayed against a
    different crawl fails on ``crawl_binding_mismatch`` long before this guard is
    reached.  The case left over is an issuer that REUSES a ``proof_id`` across
    two validly-signed proofs — at which point the platform's own uniqueness
    assumption has broken, and the guard makes the first admission stand rather
    than letting one id authorise an unbounded number of crawls.
    """
    second_proof = _proof_for_crawl("crawl-a-second-one")
    if second_proof is None:
        pytest.skip("cryptography unavailable")

    guard = attest.ProofReplayGuard()
    common = dict(trust=_trust(), tenant_id=GRANT["tenant_id"],
                  target_url=GRANT["target_url"], now_epoch_ms=NOW_MS,
                  replay_guard=guard)

    first = attest.verify_provisioning_proof(
        CONTRACT["attestation"], crawl_id=GRANT["crawl_id"], **common)
    second = attest.verify_provisioning_proof(
        {"proof": second_proof,
         "revocations": CONTRACT["attestation"]["revocations"]},
        crawl_id="crawl-a-second-one", **common)

    assert first.authorized
    assert not second.authorized
    assert second.reason == attest.AttestReason.PROOF_REPLAYED


def _proof_for_crawl(crawl_id: str):
    """Re-sign the golden claims for a DIFFERENT crawl, keeping the proof_id.

    Signed here with the contract's test key for the same reason
    ``_revocation_with`` is: importing the qe-central issuer is the package
    collision this contract file exists to route around, and the canonical
    encoding used is ``attest.canonical_bytes`` — the verifier's own.
    """
    try:
        import base64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except Exception:
        return None
    priv = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(CONTRACT["test_keys"]["private_key_b64"], validate=True))
    claims = dict(CONTRACT["attestation"]["proof"]["claims"])
    claims["crawl_id"] = crawl_id
    return {"claims": claims, "alg": attest.SIG_ALG,
            "kid": CONTRACT["attestation"]["proof"]["kid"],
            "signature": base64.b64encode(
                priv.sign(attest.canonical_bytes(claims))).decode("ascii")}


# ─── the contract file itself ───────────────────────────────────────────────

def test_the_contract_declares_the_versions_this_verifier_implements():
    assert CONTRACT["version"] == attest.CLAIMS_VERSION
    assert CONTRACT["attestation"]["proof"]["claims"]["v"] == attest.CLAIMS_VERSION
    assert CONTRACT["attestation"]["proof"]["alg"] == attest.SIG_ALG
    assert GRANT["env_kind"] == attest.DISPOSABLE


def test_the_contract_says_its_private_key_is_a_test_key():
    """A committed private key must be UNMISTAKABLY a test key.  This asserts the
    warning is present, so a future edit that drops it fails here rather than
    leaving a repository with an unexplained key in it."""
    comment = " ".join(CONTRACT["$comment"])
    assert "TEST KEY" in comment
    assert "authorises nothing" in comment
