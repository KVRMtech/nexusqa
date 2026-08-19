"""M1.3 / T-WP-02 + T-WP-04 — THE ATTESTATION SAFETY PROOF.

This suite is the argument that walk mutation cannot be authorised by anything a
tenant can write.  It is exhaustive over the verifier's refusal ladder, and its
FIRST test is the one the whole milestone turns on: the payload the brief calls
out by name —

    {"env_kind": "disposable"}

— must never, under any configuration, authorise a mutation.

Pure: no browser, no network, no crawler.
"""
from __future__ import annotations

import base64

import pytest

from app.attest import (AttestReason, ProofReplayGuard, TrustStore,
                        normalize_origin, verify_provisioning_proof,
                        verify_revocation_list)
from tests._attest_kit import CRAWL_ID, TARGET_URL, TENANT_ID, Issuer, now_ms, tampered


@pytest.fixture()
def issuer() -> Issuer:
    return Issuer()


@pytest.fixture()
def guard() -> ProofReplayGuard:
    # A FRESH replay guard per test: the process-wide one would make the second
    # test using the same proof_id fail for the right reason at the wrong time.
    return ProofReplayGuard()


def _verify(payload, issuer: Issuer, guard: ProofReplayGuard, **over):
    kwargs = dict(trust=issuer.trust(), crawl_id=CRAWL_ID, tenant_id=TENANT_ID,
                  target_url=TARGET_URL, now_epoch_ms=now_ms(), replay_guard=guard)
    kwargs.update(over)
    return verify_provisioning_proof(payload, **kwargs)


# ─── THE HEADLINE: tenant self-attestation authorises nothing ───────────────

@pytest.mark.parametrize("payload", [
    {"env_kind": "disposable"},
    {"attested_by": "the tenant", "env_kind": "disposable",
     "expires_at_ms": 32503680000000},
    {"env_kind": "disposable", "proof": {"claims": {"env_kind": "disposable"}}},
    {"env_kind": "disposable", "proof": {"claims": {"env_kind": "disposable"},
                                         "alg": "none", "kid": "x", "signature": "y"}},
    {"proof": "disposable"},
    {}, None, "disposable", 42, [],
], ids=["bare-claim", "legacy-attestation", "unsigned-proof", "alg-none",
        "proof-is-a-string", "empty", "none", "string", "int", "list"])
def test_tenant_self_attestation_never_authorizes(payload, issuer, guard):
    """T-WP-02.  Not one of these is a platform proof, and not one of them may
    produce an authorising verdict — with the trust store fully configured, so
    the refusal is the verifier's doing and not an accident of configuration."""
    verdict = _verify(payload, issuer, guard)
    assert verdict.authorized is False
    assert verdict.max_mutations_per_step == 0
    assert verdict.reason in {AttestReason.NO_PROOF, AttestReason.MALFORMED_ENVELOPE,
                              AttestReason.UNSUPPORTED_ALG}


# ─── The positive case ──────────────────────────────────────────────────────

def test_platform_issued_proof_authorizes(issuer, guard):
    verdict = _verify(issuer.envelope(), issuer, guard)
    assert verdict.authorized is True
    assert verdict.reason == AttestReason.OK
    assert verdict.env_kind == "disposable"
    assert verdict.kid == issuer.kid
    assert verdict.target_origin == "https://app.char"
    assert verdict.max_mutations_per_step == 3
    assert len(verdict.claims_digest) == 32


def test_verdict_audit_dict_carries_no_secret(issuer, guard):
    audit = _verify(issuer.envelope(), issuer, guard).as_audit_dict()
    blob = repr(audit)
    assert issuer.public_key_b64 not in blob
    assert "signature" not in audit and "claims" not in audit
    assert audit["proof_id"] and audit["kid"] == issuer.kid


# ─── The refusal ladder, one test per rung ──────────────────────────────────

def test_no_trust_anchor_refuses_even_a_valid_proof(issuer, guard):
    """An unconfigured fleet is a fleet that authorises nothing.  This is the
    SHIPPED DEFAULT — no public keys, no issuer, no walk mutation."""
    verdict = _verify(issuer.envelope(), issuer, guard, trust=TrustStore())
    assert verdict.authorized is False
    assert verdict.reason == AttestReason.NO_TRUST_ANCHOR


def test_unknown_key_id_refuses(issuer, guard):
    other = Issuer()
    # Signed by an issuer this fleet does not trust, naming the right issuer.
    payload = {"proof": other.proof(), "revocations": other.revocations()}
    verdict = _verify(payload, issuer, guard)
    assert verdict.reason == AttestReason.UNKNOWN_KEY_ID


def test_invalid_signature_refuses(issuer, guard):
    """T-WP-04 case 5.  A single edited claim, signature left intact."""
    forged = tampered(issuer.envelope(), environment_id="env-production-primary")
    verdict = _verify(forged, issuer, guard)
    assert verdict.authorized is False
    assert verdict.reason == AttestReason.BAD_SIGNATURE


def test_signature_over_a_different_env_kind_refuses(issuer, guard):
    """The most valuable forgery to attempt: flip prod -> disposable."""
    forged = tampered(issuer.envelope(), env_kind="disposable",
                      environment_id="prod-eu-1")
    assert _verify(forged, issuer, guard).reason == AttestReason.BAD_SIGNATURE


def test_truncated_and_garbage_signatures_refuse(issuer, guard):
    env = issuer.envelope()
    for bad in ("", "!!!!", "AAAA", env["proof"]["signature"][:-4],
                base64.b64encode(b"\x00" * 64).decode()):
        broken = {k: dict(v) for k, v in env.items()}
        broken["proof"]["signature"] = bad
        assert _verify(broken, issuer, guard).authorized is False


@pytest.mark.parametrize("env_kind", ["prod", "production", "staging", "uat",
                                      "production_test", "", "  ", "Disposable!",
                                      "disposable-ish", "unknown"])
def test_only_the_word_disposable_passes(env_kind, issuer, guard):
    """T-WP-06.  Every non-disposable environment kind, correctly SIGNED, is
    still refused — production isolation does not depend on a bad signature."""
    payload = {"proof": issuer.proof(env_kind=env_kind),
               "revocations": issuer.revocations()}
    verdict = _verify(payload, issuer, guard)
    assert verdict.authorized is False
    assert verdict.reason in {AttestReason.NOT_DISPOSABLE,
                              AttestReason.MALFORMED_CLAIMS}


def test_expired_proof_refuses(issuer, guard):
    """T-WP-04 case 3."""
    issued = now_ms() - 7_200_000
    payload = {"proof": issuer.proof(issued_at_ms=issued,
                                     expires_at_ms=issued + 3_600_000),
               "revocations": issuer.revocations()}
    assert _verify(payload, issuer, guard).reason == AttestReason.EXPIRED


def test_proof_issued_in_the_future_refuses(issuer, guard):
    future = now_ms() + 3_600_000
    payload = {"proof": issuer.proof(issued_at_ms=future,
                                     expires_at_ms=future + 3_600_000),
               "revocations": issuer.revocations()}
    assert _verify(payload, issuer, guard).reason == AttestReason.ISSUED_IN_FUTURE


def test_overlong_lifetime_refuses_even_though_unexpired(issuer, guard):
    """The verifier enforces its OWN ceiling.  A genuinely-signed, currently-
    valid ten-year proof is refused, so a compromised issuer cannot mint itself
    an indefinite grant."""
    issued = now_ms()
    payload = {"proof": issuer.proof(issued_at_ms=issued,
                                     expires_at_ms=issued + 10 * 365 * 86_400_000),
               "revocations": issuer.revocations()}
    assert _verify(payload, issuer, guard).reason == AttestReason.LIFETIME_TOO_LONG


def test_monotonic_clock_reading_refuses(issuer, guard):
    """M0.5 T-SEC-08's lesson, re-applied.  A caller handing a since-start
    reading must be REFUSED, never treated as "very early, therefore fresh"."""
    verdict = _verify(issuer.envelope(), issuer, guard, now_epoch_ms=12_345)
    assert verdict.reason == AttestReason.CLOCK_DOMAIN_ERROR


def test_issuer_mismatch_refuses(issuer, guard):
    payload = {"proof": issuer.proof(issuer="someone-else"),
               "revocations": issuer.revocations()}
    assert _verify(payload, issuer, guard).reason == AttestReason.ISSUER_MISMATCH


def test_unknown_claim_field_refuses(issuer, guard):
    """Strict schema.  A proof relying on a field this verifier does not
    understand is refused rather than interpreted."""
    payload = {"proof": issuer.proof(allow_everything=True),
               "revocations": issuer.revocations()}
    assert _verify(payload, issuer, guard).reason == AttestReason.MALFORMED_CLAIMS


# ─── Bindings: a proof is not a bearer token ────────────────────────────────

def test_proof_for_another_crawl_refuses(issuer, guard):
    assert _verify(issuer.envelope(), issuer, guard,
                   crawl_id="a-different-crawl").reason == \
        AttestReason.CRAWL_BINDING_MISMATCH


def test_proof_for_another_tenant_refuses(issuer, guard):
    """Tenant impersonation: a genuine proof from tenant A cannot authorise a
    crawl running as tenant B."""
    assert _verify(issuer.envelope(), issuer, guard,
                   tenant_id="tenant-victim").reason == AttestReason.TENANT_MISMATCH


@pytest.mark.parametrize("target", [
    "https://prod.example.com/apply",       # production escape
    "https://app.char.evil.net/apply",      # suffix trick
    "http://app.char/apply",                # scheme downgrade
    "https://app.char:8443/apply",          # port swap
    "",                                     # unparseable
])
def test_proof_pointed_at_another_origin_refuses(target, issuer, guard):
    """T-WP-06.  A genuine disposable-env proof aimed anywhere but the attested
    origin is refused — otherwise it is a bearer token for arbitrary targets."""
    verdict = _verify(issuer.envelope(), issuer, guard, target_url=target)
    assert verdict.authorized is False
    assert verdict.reason == AttestReason.ORIGIN_MISMATCH


def test_origin_normalisation_elides_default_ports_only():
    assert normalize_origin("https://app.char:443/x") == "https://app.char"
    assert normalize_origin("http://app.char:80/x") == "http://app.char"
    assert normalize_origin("https://app.char:8443/x") == "https://app.char:8443"
    assert normalize_origin("HTTPS://APP.CHAR/x") == "https://app.char"
    assert normalize_origin("not a url") == ""
    assert normalize_origin("") == ""


# ─── Revocation ─────────────────────────────────────────────────────────────

def test_revoked_proof_id_refuses(issuer, guard):
    claims = issuer.proof_claims()
    payload = {"proof": issuer.proof(**{k: v for k, v in claims.items()
                                        if k in ("proof_id",)}),
               "revocations": issuer.revocations(
                   revoked_proof_ids=[claims["proof_id"]])}
    assert _verify(payload, issuer, guard).reason == AttestReason.REVOKED


def test_revoked_environment_refuses(issuer, guard):
    """Revoking the ENVIRONMENT kills every proof issued for it, which is what
    an operator actually reaches for when a throwaway env is repurposed."""
    payload = {"proof": issuer.proof(),
               "revocations": issuer.revocations(
                   revoked_environment_ids=["env-disposable-7f2a"])}
    assert _verify(payload, issuer, guard).reason == AttestReason.REVOKED


def test_missing_revocation_list_refuses(issuer, guard):
    """Revocation is MANDATORY.  A proof with no accompanying list leaves
    revocation state unknown, and unknown is not permission."""
    assert _verify({"proof": issuer.proof()}, issuer, guard).reason == \
        AttestReason.NO_REVOCATION_LIST


def test_expired_revocation_list_refuses(issuer, guard):
    stale = now_ms() - 1_200_000
    payload = {"proof": issuer.proof(),
               "revocations": issuer.revocations(issued_at_ms=stale,
                                                 expires_at_ms=stale + 600_000)}
    assert _verify(payload, issuer, guard).reason == AttestReason.REVOCATION_EXPIRED


def test_forged_revocation_list_refuses(issuer, guard):
    """An attacker who could forge an EMPTY revocation list could un-revoke
    their own proof, so the list's signature is checked exactly as hard as the
    proof's."""
    forged = issuer.revocations()
    forged["claims"] = dict(forged["claims"], revoked_proof_ids=[])
    forged["claims"]["issued_at_ms"] += 1     # break the signed bytes
    payload = {"proof": issuer.proof(), "revocations": forged}
    assert _verify(payload, issuer, guard).reason == \
        AttestReason.REVOCATION_BAD_SIGNATURE


def test_revocation_list_from_another_issuer_refuses(issuer, guard):
    other = Issuer(name=issuer.name)          # right NAME, wrong KEY
    payload = {"proof": issuer.proof(), "revocations": other.revocations()}
    assert _verify(payload, issuer, guard).reason == \
        AttestReason.REVOCATION_BAD_SIGNATURE


def test_revocation_verifier_is_total():
    """Never raises, whatever it is handed."""
    trust = Issuer().trust()
    for junk in (None, "", 0, [], {}, {"claims": 1}, {"claims": {}, "kid": "x"}):
        result, why = verify_revocation_list(junk, trust=trust, now_epoch_ms=now_ms())
        assert result is None and why


# ─── Replay ─────────────────────────────────────────────────────────────────

def test_same_proof_twice_for_the_same_crawl_is_idempotent(issuer, guard):
    """A retried dispatch of the SAME crawl is not an attack."""
    payload = issuer.envelope()
    assert _verify(payload, issuer, guard).authorized is True
    assert _verify(payload, issuer, guard).authorized is True


def test_same_proof_replayed_against_another_crawl_refuses(issuer, guard):
    """Two layers stop this and BOTH are asserted: the crawl binding inside the
    claims, and the process replay guard for a proof whose binding somehow
    matched."""
    assert _verify(issuer.envelope(), issuer, guard).authorized is True
    replayed = _verify(issuer.envelope(), issuer, guard, crawl_id="crawl-victim")
    assert replayed.authorized is False
    assert replayed.reason == AttestReason.CRAWL_BINDING_MISMATCH

    # And with the binding forged to match, the replay guard still refuses.
    second = {"proof": issuer.proof(crawl_id="crawl-victim"),
              "revocations": issuer.revocations()}
    verdict = verify_provisioning_proof(
        second, trust=issuer.trust(), crawl_id="crawl-victim",
        tenant_id=TENANT_ID, target_url=TARGET_URL, now_epoch_ms=now_ms(),
        replay_guard=guard)
    assert verdict.authorized is False
    assert verdict.reason == AttestReason.PROOF_REPLAYED


def test_replay_guard_is_thread_safe():
    """Exactly ONE admission wins under concurrency."""
    import threading
    rg = ProofReplayGuard()
    results: list = []
    barrier = threading.Barrier(16)

    def go(i: int) -> None:
        barrier.wait()
        results.append(rg.admit("prf-" + "0" * 12 + "abcd", f"crawl-{i}"))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r) == 1


# ─── Least privilege ────────────────────────────────────────────────────────

def test_fleet_ceiling_caps_what_a_proof_may_request(issuer, guard):
    payload = {"proof": issuer.proof(max_walk_mutations_per_step=10),
               "revocations": issuer.revocations()}
    verdict = _verify(payload, issuer, guard,
                      trust=issuer.trust(max_mutations_per_step=2))
    assert verdict.authorized is True
    assert verdict.max_mutations_per_step == 2, "a proof must not widen fleet policy"


def test_a_proof_may_ask_for_less(issuer, guard):
    payload = {"proof": issuer.proof(max_walk_mutations_per_step=1),
               "revocations": issuer.revocations()}
    verdict = _verify(payload, issuer, guard,
                      trust=issuer.trust(max_mutations_per_step=5))
    assert verdict.max_mutations_per_step == 1


def test_a_proof_may_ask_for_zero(issuer, guard):
    """A zero-budget proof authenticates the environment and grants no writes."""
    payload = {"proof": issuer.proof(max_walk_mutations_per_step=0),
               "revocations": issuer.revocations()}
    verdict = _verify(payload, issuer, guard)
    assert verdict.authorized is True and verdict.max_mutations_per_step == 0


def test_a_proof_asking_for_more_than_the_hard_cap_is_malformed(issuer, guard):
    payload = {"proof": issuer.proof(max_walk_mutations_per_step=1000),
               "revocations": issuer.revocations()}
    assert _verify(payload, issuer, guard).reason == AttestReason.MALFORMED_CLAIMS


# ─── Totality ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("junk", [
    None, "", 0, [], {}, {"proof": None}, {"proof": []}, {"proof": {"claims": []}},
    {"proof": {"claims": {}, "alg": "ed25519", "kid": "", "signature": ""}},
])
def test_verifier_never_raises(junk, issuer, guard):
    verdict = _verify(junk, issuer, guard)
    assert verdict.authorized is False and verdict.reason


def test_trust_store_drops_malformed_public_keys():
    store = TrustStore.from_public_keys(
        ["not-base64!", base64.b64encode(b"short").decode(), ""], issuer="x")
    assert store.keys == {} and store.configured is False
