"""Gate 1 / T-WP-02 — THE QE-CENTRAL HALF of the walk-attestation contract.

The explorer half lives at
``engines/qe-explorer/tests/test_gate1_attestation_contract.py`` and proves that
the PRODUCTION VERIFIER accepts what this issuer mints.  It cannot prove anything
about the issuer itself: it only ever sees the frozen output.

This half proves the three properties the issuer owns outright, and it proves
them by RE-MINTING the frozen golden from the inputs recorded beside it:

* **determinism** — the same grant and the same timestamp re-derive the same
  bytes, which is what makes an attestation reproducible evidence rather than a
  receipt taken on faith;
* **fail-at-mint** — a grant that could never be authorised raises HERE, so the
  failure names its real cause instead of surfacing as a stable refusal code on
  a distant worker hours later;
* **encoding agreement** — the canonical encoding is duplicated across two
  services that share no package, and a divergence would silently reject every
  genuine proof in production.

The two services collide in one interpreter (each ships a top-level ``app``
package), which is why the seam is frozen as data rather than tested by import.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.services import walk_attestation as wa
from app.services.signing import canonical_bytes, generate_keypair


def _contract() -> dict:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "gate1_walk_attestation_v1.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError(
        "contracts/gate1_walk_attestation_v1.json not found above %s — the "
        "frozen attestation contract is the only thing tying this issuer to "
        "qe-explorer's verifier, and it must not be deleted to make a test pass"
        % here)


CONTRACT = _contract()
GRANT_DATA = CONTRACT["grant"]
PRIVATE_KEY = CONTRACT["test_keys"]["private_key_b64"]
ISSUED_MS = int(CONTRACT["issued_at_ms"])


def _grant(**overrides) -> wa.ProvisioningGrant:
    kwargs = dict(
        environment_id=GRANT_DATA["environment_id"],
        tenant_id=GRANT_DATA["tenant_id"],
        crawl_id=GRANT_DATA["crawl_id"],
        target_url=GRANT_DATA["target_url"],
        reset_procedure=GRANT_DATA["reset_procedure"],
        env_kind=GRANT_DATA["env_kind"],
        max_walk_mutations_per_step=GRANT_DATA["max_walk_mutations_per_step"],
        proof_id=GRANT_DATA["proof_id"],
    )
    kwargs.update(overrides)
    return wa.ProvisioningGrant(**kwargs)


def _reissue(**overrides) -> dict:
    kwargs = dict(
        private_key_b64=PRIVATE_KEY, issuer=CONTRACT["issuer"],
        issued_at_ms=ISSUED_MS,
        proof_lifetime_ms=CONTRACT["proof_lifetime_ms"],
        revocation_lifetime_ms=CONTRACT["revocation_lifetime_ms"],
        revoked_proof_ids=CONTRACT["revoked_proof_ids"],
        revoked_environment_ids=CONTRACT["revoked_environment_ids"],
    )
    kwargs.update(overrides)
    return wa.issue_attestation(_grant(), **kwargs)


# ─── determinism: the golden is re-derivable from its recorded inputs ───────

def test_reissuing_the_golden_reproduces_it_byte_for_byte():
    """THE REPRODUCIBILITY CLAIM, tested rather than asserted.

    An auditor holding the recorded inputs can re-mint the attestation on file
    and compare.  Ed25519 is deterministic (RFC 8032) and the encoding is
    canonical, so this holds exactly — not approximately.
    """
    assert _reissue() == CONTRACT["attestation"]


def test_reissuing_twice_in_a_row_is_stable():
    assert _reissue() == _reissue()


def test_the_clock_is_injected_so_the_issue_can_be_re_run():
    """A function that read ``time.time()`` internally could not be re-run, and
    the reproducibility property above would be untestable and unfalsifiable."""
    early = wa.issue_provisioning_proof(
        _grant(), private_key_b64=PRIVATE_KEY, issuer=CONTRACT["issuer"],
        issued_at_ms=ISSUED_MS, lifetime_ms=CONTRACT["proof_lifetime_ms"])
    later = wa.issue_provisioning_proof(
        _grant(), private_key_b64=PRIVATE_KEY, issuer=CONTRACT["issuer"],
        issued_at_ms=ISSUED_MS + 1000,
        lifetime_ms=CONTRACT["proof_lifetime_ms"])
    assert early != later, "a different issue time is a different proof"
    assert early["claims"]["expires_at_ms"] + 1000 == later["claims"]["expires_at_ms"]


def test_a_revocation_list_is_order_independent():
    """The same revocation STATE must sign to the same bytes regardless of the
    order the database returned it in, or an attestation would appear to change
    every time a query plan did."""
    forward = wa.issue_revocation_list(
        private_key_b64=PRIVATE_KEY, issuer=CONTRACT["issuer"],
        issued_at_ms=ISSUED_MS, revoked_proof_ids=["b" * 32, "a" * 32],
        revoked_environment_ids=["env-2", "env-1"])
    reverse = wa.issue_revocation_list(
        private_key_b64=PRIVATE_KEY, issuer=CONTRACT["issuer"],
        issued_at_ms=ISSUED_MS, revoked_proof_ids=["a" * 32, "b" * 32],
        revoked_environment_ids=["env-1", "env-2"])
    assert forward == reverse


def test_a_duplicate_revocation_entry_does_not_change_the_signature():
    once = wa.issue_revocation_list(
        private_key_b64=PRIVATE_KEY, issuer=CONTRACT["issuer"],
        issued_at_ms=ISSUED_MS, revoked_proof_ids=["a" * 32])
    twice = wa.issue_revocation_list(
        private_key_b64=PRIVATE_KEY, issuer=CONTRACT["issuer"],
        issued_at_ms=ISSUED_MS, revoked_proof_ids=["a" * 32, "a" * 32])
    assert once == twice


# ─── an empty list is a POSITIVE statement, not an absence ─────────────────

def test_an_empty_revocation_list_is_still_signed_and_still_shipped():
    """"I have revoked nothing, signed, valid for ten minutes" is a statement the
    verifier can act on; no list at all is "revocation state unknown", which is a
    DENY.  So the empty case is the one that must not be optimised away."""
    attestation = _reissue(revoked_proof_ids=[], revoked_environment_ids=[])
    revocations = attestation["revocations"]
    assert revocations["claims"]["revoked_proof_ids"] == []
    assert revocations["signature"], "signed even when empty"
    assert revocations["claims"]["expires_at_ms"] > revocations["claims"]["issued_at_ms"]


def test_issue_attestation_always_produces_both_halves():
    """A caller that could obtain a proof without a list would eventually ship a
    dispatch refused for a reason the operator cannot see from the request."""
    attestation = _reissue()
    assert set(attestation) == {"proof", "revocations"}
    for half in attestation.values():
        assert set(half) == {"claims", "alg", "kid", "signature"}
        assert half["alg"] == wa.SIG_ALG


# ─── fail at mint, not on a distant worker ─────────────────────────────────

def test_a_non_disposable_environment_is_refused_at_mint():
    with pytest.raises(wa.IssuerError, match="disposable"):
        _grant(env_kind="persistent").claims(
            issuer=CONTRACT["issuer"], issued_at_ms=ISSUED_MS,
            lifetime_ms=60_000)


def test_a_target_url_with_no_origin_is_refused_at_mint():
    """The verifier treats an empty origin on either side as a MISMATCH rather
    than a wildcard, so minting one guarantees a refusal nobody can diagnose."""
    with pytest.raises(wa.IssuerError, match="origin"):
        _grant(target_url="not a url").claims(
            issuer=CONTRACT["issuer"], issued_at_ms=ISSUED_MS, lifetime_ms=60_000)


def test_a_lifetime_beyond_the_verifier_ceiling_is_refused_at_mint():
    with pytest.raises(wa.IssuerError, match="ceiling"):
        _grant().claims(issuer=CONTRACT["issuer"], issued_at_ms=ISSUED_MS,
                        lifetime_ms=wa.MAX_PROOF_LIFETIME_MS + 1)


def test_a_mutation_budget_above_the_hard_ceiling_is_refused_at_mint():
    with pytest.raises(wa.IssuerError, match="max_walk_mutations_per_step"):
        _grant(max_walk_mutations_per_step=wa.HARD_MAX_MUTATIONS_PER_STEP + 1
               ).claims(issuer=CONTRACT["issuer"], issued_at_ms=ISSUED_MS,
                        lifetime_ms=60_000)


def test_a_non_epoch_timestamp_is_refused_at_mint():
    """A "now" below the plausible-epoch floor is a monotonic since-start
    reading, a zeroed clock, or seconds mistaken for millis.  The verifier
    refuses rather than compare across clock domains; so does this."""
    with pytest.raises(wa.IssuerError, match="epoch-ms"):
        _grant().claims(issuer=CONTRACT["issuer"], issued_at_ms=1_700_000,
                        lifetime_ms=60_000)


@pytest.mark.parametrize("field_name", ["environment_id", "tenant_id", "crawl_id"])
def test_every_binding_field_is_required(field_name):
    with pytest.raises(wa.IssuerError, match=field_name):
        _grant(**{field_name: "  "}).claims(
            issuer=CONTRACT["issuer"], issued_at_ms=ISSUED_MS, lifetime_ms=60_000)


def test_an_empty_issuer_is_refused():
    with pytest.raises(wa.IssuerError, match="issuer"):
        _grant().claims(issuer="", issued_at_ms=ISSUED_MS, lifetime_ms=60_000)


# ─── the claims the verifier will destructure ──────────────────────────────

def test_the_claims_carry_exactly_the_fields_the_verifier_accepts():
    """``app.attest.ProofClaims`` is ``extra="forbid"``: an unexpected field
    REFUSES the proof, because a proof carrying fields the verifier does not
    understand may be relying on them for its meaning.  Exact equality, so
    ADDING a field here fails this test rather than every proof in production."""
    claims = _reissue()["proof"]["claims"]
    assert set(claims) == {
        "v", "proof_id", "issuer", "environment_id", "env_kind", "tenant_id",
        "crawl_id", "target_origin", "reset_procedure", "issued_at_ms",
        "expires_at_ms", "max_walk_mutations_per_step",
    }


def test_the_revocation_claims_carry_exactly_their_fields():
    claims = _reissue()["revocations"]["claims"]
    assert set(claims) == {
        "v", "issuer", "issued_at_ms", "expires_at_ms",
        "revoked_proof_ids", "revoked_environment_ids",
    }


def test_the_target_url_is_stored_as_a_normalised_origin():
    claims = _reissue()["proof"]["claims"]
    assert claims["target_origin"] == CONTRACT["expected_target_origin"]
    assert "/apply" not in claims["target_origin"], "path is not part of an origin"


@pytest.mark.parametrize("url,origin", [
    ("https://example.test/x", "https://example.test"),
    ("https://example.test:443/x", "https://example.test"),
    ("http://example.test:80/x", "http://example.test"),
    ("https://EXAMPLE.test:8443/x", "https://example.test:8443"),
    ("not a url", ""),
    ("", ""),
])
def test_origin_normalisation_matches_the_verifier_rules(url, origin):
    assert wa.normalize_origin(url) == origin


# ─── encoding agreement across the two services ────────────────────────────

def test_the_signature_verifies_under_the_shared_canonical_encoding():
    """The self-check exists because the encoding is written twice, in two
    services with no shared package.  A divergence there is the worst possible
    failure shape: it looks like a configuration problem and appears everywhere
    at once."""
    assert wa.self_check(PRIVATE_KEY, _reissue()["proof"]["claims"])


def test_the_frozen_signature_still_verifies_under_this_encoding():
    """Guards the direction the test above cannot: a change to
    ``canonical_bytes`` would keep ``self_check`` green (it signs and verifies
    with the same new encoding) while invalidating every proof already issued."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    proof = CONTRACT["attestation"]["proof"]
    pub = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(CONTRACT["test_keys"]["public_key_b64"], validate=True))
    pub.verify(base64.b64decode(proof["signature"], validate=True),
               canonical_bytes(proof["claims"]))


def test_the_key_id_is_derived_the_way_the_verifier_derives_it():
    assert wa.key_id(CONTRACT["test_keys"]["public_key_b64"]) == \
        CONTRACT["test_keys"]["kid"] == CONTRACT["attestation"]["proof"]["kid"]


# ─── tamper-evidence, from this side ───────────────────────────────────────

@pytest.mark.parametrize("field_name,new_value", [
    ("crawl_id", "crawl-somebody-elses"),
    ("expires_at_ms", 9_999_999_999_999),
    ("max_walk_mutations_per_step", 10),
])
def test_editing_a_signed_claim_breaks_its_own_signature(field_name, new_value):
    import base64
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    proof = copy.deepcopy(CONTRACT["attestation"]["proof"])
    proof["claims"][field_name] = new_value
    pub = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(CONTRACT["test_keys"]["public_key_b64"], validate=True))
    with pytest.raises(InvalidSignature):
        pub.verify(base64.b64decode(proof["signature"], validate=True),
                   canonical_bytes(proof["claims"]))


# ─── key handling ──────────────────────────────────────────────────────────

def test_a_fresh_key_produces_a_different_kid_and_a_different_signature():
    other_priv, other_pub = generate_keypair()
    mine = _reissue()
    theirs = _reissue(private_key_b64=other_priv)
    assert theirs["proof"]["kid"] == wa.key_id(other_pub)
    assert theirs["proof"]["kid"] != mine["proof"]["kid"]
    assert theirs["proof"]["signature"] != mine["proof"]["signature"]
    assert theirs["proof"]["claims"] == mine["proof"]["claims"], (
        "the CLAIMS are identical — only the signature and key id differ")


def test_a_proof_id_is_minted_when_the_grant_does_not_supply_one():
    first = _grant(proof_id="").claims(
        issuer=CONTRACT["issuer"], issued_at_ms=ISSUED_MS, lifetime_ms=60_000)
    second = _grant(proof_id="").claims(
        issuer=CONTRACT["issuer"], issued_at_ms=ISSUED_MS, lifetime_ms=60_000)
    assert first["proof_id"] != second["proof_id"], "unique per provisioning event"
    assert 16 <= len(first["proof_id"]) <= 128
