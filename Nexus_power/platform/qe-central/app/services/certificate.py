"""Certificate of Correctness — cross-ledger, signed, offline-verifiable (Phase 6).

Answers "prove this flow was tested correctly" by assembling, for one named run, the
verified hash chains (verdict + approval + baseline), the EXECUTED invariant results,
and the per-step grounded evidence into ONE deterministic, platform-signed certificate
that a regulator can verify OFFLINE without trusting the vendor.

Honest by construction — ``certified=true`` requires ALL of:
  * every chain re-derives (no ``hash_mismatch``);
  * every linked invariant was actually EXECUTED and proven — a positive run AND its
    mandatory must-refuse (break-mode) proof — never inferred from "the scenario ran
    green" (the key correction: an all-green scenario whose invariant was never evaluated
    is NOT certified);
  * every required invariant carries a valid signature;
  * no linked step is UNPROVEN.
Otherwise ``certified=false`` with an ENUMERATED reason list — no path green-washes.

The vendor-independent part is the chain RE-DERIVATION + this pure recomputation; the
platform signature proves the vendor asserted the certificate, not independent truth.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from .signing import canonical_bytes, sign_payload, verify_signature

# Enumerated false-reasons (stable strings).
R_CHAIN_BROKEN = "chain_broken"
R_INVARIANT_NOT_EXECUTED = "invariant_not_executed"
R_MUST_REFUSE_NOT_PROVEN = "must_refuse_not_proven"
R_UNSIGNED_INVARIANT = "unsigned_invariant"
R_UNPROVEN_STEP = "unproven_step"


def _digest(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _evaluate(
    chains: Mapping[str, Mapping],
    invariants: Iterable[Mapping],
    steps: Iterable[Mapping],
) -> list[str]:
    """Return the enumerated failure reasons (empty ⇒ certified). PURE."""
    reasons: list[str] = []

    for name, res in (chains or {}).items():
        if not (isinstance(res, Mapping) and res.get("verified")):
            reasons.append(f"{R_CHAIN_BROKEN}:{name}")

    for inv in invariants or ():
        iid = str(inv.get("invariant_id") or inv.get("id") or "?")
        if not inv.get("executed"):
            reasons.append(f"{R_INVARIANT_NOT_EXECUTED}:{iid}")
            continue
        if not (inv.get("positive_proven") and inv.get("must_refuse_proven")):
            reasons.append(f"{R_MUST_REFUSE_NOT_PROVEN}:{iid}")
        if inv.get("requires_signature") and not inv.get("signature_valid"):
            reasons.append(f"{R_UNSIGNED_INVARIANT}:{iid}")

    for st in steps or ():
        if str(st.get("status") or "").strip().lower() == "unproven":
            reasons.append(f"{R_UNPROVEN_STEP}:{st.get('step_id') or st.get('id') or '?'}")

    return reasons


def build_certificate(
    *,
    subject: Mapping[str, Any],
    chains: Mapping[str, Mapping],
    invariants: Iterable[Mapping],
    steps: Iterable[Mapping],
    registry_version: str = "1",
    signing_key_b64: str | None = None,
) -> dict:
    """Assemble a deterministic certificate. ``subject`` MUST pin the exact run
    (tenant/app/scenario/artifact/test/run ids) — a certificate certifies a NAMED run,
    never "the" artifact. Excludes wall-clock from the digest so it is replay-stable."""
    invariants = [dict(i) for i in invariants]
    steps = [dict(s) for s in steps]
    reasons = _evaluate(chains, invariants, steps)
    body = {
        "subject": dict(subject),
        "registry_version": registry_version,
        "chains": {k: dict(v) for k, v in (chains or {}).items()},
        "invariants": invariants,
        "steps": steps,
        "certified": not reasons,
        "reasons": sorted(reasons),
    }
    digest = _digest(body)
    cert = {"body": body, "certificate_digest": digest,
            "sig_alg": "ed25519" if signing_key_b64 else "", "signature": ""}
    if signing_key_b64:
        cert["signature"] = sign_payload(signing_key_b64, {"certificate_digest": digest})
    return cert


def verify_certificate(cert: Mapping[str, Any], *, public_key_b64: str | None = None) -> dict:
    """Independently RE-DERIVE a certificate offline. Returns ``{ok, reasons}``.

    Recomputes the digest from the body (detects tampering), re-derives ``certified``
    from the embedded chain/invariant/step results (so a flipped ``certified`` flag is
    caught), and verifies the platform signature when a key is supplied. Needs no DB.
    """
    problems: list[str] = []
    body = cert.get("body") if isinstance(cert, Mapping) else None
    if not isinstance(body, Mapping):
        return {"ok": False, "reasons": ["malformed_certificate"]}

    if _digest(body) != cert.get("certificate_digest"):
        problems.append("digest_mismatch")

    recomputed = _evaluate(body.get("chains") or {}, body.get("invariants") or (), body.get("steps") or ())
    if bool(recomputed) == bool(body.get("certified")):
        # certified should be True iff there are no reasons.
        problems.append("certified_flag_inconsistent")
    if sorted(recomputed) != sorted(body.get("reasons") or []):
        problems.append("reasons_inconsistent")

    if public_key_b64:
        if not cert.get("signature"):
            problems.append("missing_signature")
        elif not verify_signature(public_key_b64, {"certificate_digest": cert.get("certificate_digest")}, cert["signature"]):
            problems.append("bad_signature")

    return {
        "ok": not problems and bool(body.get("certified")),
        "certified": bool(body.get("certified")),
        "tampered": bool(problems),
        "reasons": problems,
    }
