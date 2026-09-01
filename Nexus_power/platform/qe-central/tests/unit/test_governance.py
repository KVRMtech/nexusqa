"""Phase 6 — signing primitives + Certificate of Correctness (offline-verifiable).

Proves: a real Ed25519 signature verifies and fails on tamper; a fully-proven run yields
certified=true that the OFFLINE verifier confirms; and — the key correction — an all-green
run whose invariant was never EXECUTED (or whose must-refuse was not proven) yields
certified=false with the exact enumerated reason. Tampering any embedded result or the
signed digest is detected.
"""
from __future__ import annotations

from app.services import certificate as cert
from app.services import signing


# ── signing primitives ────────────────────────────────────────────────────────
def test_sign_and_verify_roundtrip():
    priv, pub = signing.generate_keypair()
    payload = {"b": 2, "a": 1}
    sig = signing.sign_payload(priv, payload)
    assert signing.verify_signature(pub, payload, sig)


def test_verify_fails_on_tampered_payload():
    priv, pub = signing.generate_keypair()
    sig = signing.sign_payload(priv, {"amount": "250"})
    assert not signing.verify_signature(pub, {"amount": "180"}, sig)


def test_verify_fails_with_wrong_key():
    priv1, _ = signing.generate_keypair()
    _, pub2 = signing.generate_keypair()
    sig = signing.sign_payload(priv1, {"x": 1})
    assert not signing.verify_signature(pub2, {"x": 1}, sig)


def test_public_key_of_matches_generated():
    priv, pub = signing.generate_keypair()
    assert signing.public_key_of(priv) == pub


def test_canonical_bytes_is_key_order_independent():
    assert signing.canonical_bytes({"a": 1, "b": 2}) == signing.canonical_bytes({"b": 2, "a": 1})


# ── certificate: the fully-proven happy path ──────────────────────────────────
def _proven_inputs():
    return dict(
        subject={"tenant_id": "t1", "app_id": "a1", "scenario_id": "s1",
                 "artifact_id": "art1", "test_id": "tc1", "run_id": "r1"},
        chains={
            "verdict": {"verified": True},
            "approval": {"verified": True},
            "baseline": {"verified": True},
        },
        invariants=[{"invariant_id": "inv1", "executed": True, "positive_proven": True,
                     "must_refuse_proven": True, "requires_signature": True, "signature_valid": True}],
        steps=[{"step_id": "st1", "status": "proven"}],
    )


def test_fully_proven_run_certifies_and_verifies_offline():
    priv, pub = signing.generate_keypair()
    c = cert.build_certificate(**_proven_inputs(), signing_key_b64=priv)
    assert c["body"]["certified"] is True and c["body"]["reasons"] == []
    v = cert.verify_certificate(c, public_key_b64=pub)
    assert v["ok"] is True and v["certified"] is True and not v["tampered"]


# ── the KEY correction: green scenario ≠ certified ────────────────────────────
def test_invariant_not_executed_blocks_certification():
    args = _proven_inputs()
    args["invariants"] = [{"invariant_id": "inv1", "executed": False,
                           "positive_proven": True, "must_refuse_proven": True}]
    c = cert.build_certificate(**args)
    assert c["body"]["certified"] is False
    assert any(r.startswith(cert.R_INVARIANT_NOT_EXECUTED) for r in c["body"]["reasons"])


def test_must_refuse_not_proven_blocks_certification():
    args = _proven_inputs()
    args["invariants"] = [{"invariant_id": "inv1", "executed": True,
                           "positive_proven": True, "must_refuse_proven": False}]
    c = cert.build_certificate(**args)
    assert any(r.startswith(cert.R_MUST_REFUSE_NOT_PROVEN) for r in c["body"]["reasons"])
    assert c["body"]["certified"] is False


def test_broken_chain_blocks_certification():
    args = _proven_inputs()
    args["chains"]["verdict"] = {"verified": False}
    c = cert.build_certificate(**args)
    assert f"{cert.R_CHAIN_BROKEN}:verdict" in c["body"]["reasons"]
    assert c["body"]["certified"] is False


def test_unproven_step_blocks_certification():
    args = _proven_inputs()
    args["steps"] = [{"step_id": "st1", "status": "unproven"}]
    c = cert.build_certificate(**args)
    assert any(r.startswith(cert.R_UNPROVEN_STEP) for r in c["body"]["reasons"])


def test_unsigned_required_invariant_blocks_certification():
    args = _proven_inputs()
    args["invariants"][0]["signature_valid"] = False
    c = cert.build_certificate(**args)
    assert any(r.startswith(cert.R_UNSIGNED_INVARIANT) for r in c["body"]["reasons"])


# ── tamper detection by the offline verifier ──────────────────────────────────
def test_tampered_body_is_detected():
    priv, pub = signing.generate_keypair()
    c = cert.build_certificate(**_proven_inputs(), signing_key_b64=priv)
    # Flip a chain result AFTER signing, leaving the digest stale.
    c["body"]["chains"]["verdict"]["verified"] = False
    v = cert.verify_certificate(c, public_key_b64=pub)
    assert v["ok"] is False and v["tampered"] is True
    assert "digest_mismatch" in v["reasons"]


def test_flipped_certified_flag_is_detected():
    c = cert.build_certificate(**{**_proven_inputs(),
                                  "invariants": [{"invariant_id": "x", "executed": False}]})
    # An attacker flips certified True without fixing reasons/digest.
    c["body"]["certified"] = True
    v = cert.verify_certificate(c)
    assert v["tampered"] is True
    assert "certified_flag_inconsistent" in v["reasons"] or "digest_mismatch" in v["reasons"]


def test_bad_signature_is_detected():
    priv, _ = signing.generate_keypair()
    _, other_pub = signing.generate_keypair()
    c = cert.build_certificate(**_proven_inputs(), signing_key_b64=priv)
    v = cert.verify_certificate(c, public_key_b64=other_pub)
    assert v["ok"] is False and "bad_signature" in v["reasons"]


def test_certificate_digest_is_deterministic():
    a = cert.build_certificate(**_proven_inputs())
    b = cert.build_certificate(**_proven_inputs())
    assert a["certificate_digest"] == b["certificate_digest"]
