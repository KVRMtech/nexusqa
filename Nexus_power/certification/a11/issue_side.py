"""CERT-A11 (independent): ISSUER half. Runs with cwd=platform/qe-central.

Mints attestations for a spread of grants chosen by the CERTIFIER, using a
FRESHLY GENERATED key -- not the committed test key, not the frozen golden.
Emits JSON on stdout for the verifier half to consume in a separate process.
"""
import json, sys
from pathlib import Path
from app.services.signing import generate_keypair
from app.services.walk_attestation import (ProvisioningGrant, issue_attestation,
                                           issue_revocation_list,
                                           normalize_origin, _sign)

NOW = 1_760_000_000_000
priv, pub = generate_keypair()
ISSUER = "cert.independent.issuer"

# Grants the frozen golden does not cover: unicode, punycode, IPv6, odd ports,
# uppercase host, large budgets, empty reset procedure.
CASES = [
    ("ascii_simple",   "https://staging.example.test/apply",            "wipe-db", 1),
    ("uppercase_host", "https://STAGING.Example.TEST/apply",            "wipe-db", 1),
    ("explicit_443",   "https://staging.example.test:443/apply",        "wipe-db", 1),
    ("nonstd_port",    "https://staging.example.test:8443/apply",       "wipe-db", 2),
    ("http_80",        "http://staging.example.test:80/apply",          "wipe-db", 1),
    ("ipv6",           "https://[2001:db8::1]:8443/apply",              "wipe-db", 1),
    ("punycode",       "https://xn--bcher-kva.example/apply",           "wipe-db", 1),
    ("unicode_reset",  "https://staging.example.test/apply",            "réinitialiser — 全部消去", 1),
    ("empty_reset",    "https://staging.example.test/apply",            "", 1),
    ("big_budget",     "https://staging.example.test/apply",            "wipe-db", 10),
]

# A11b -- THE SHARED ORIGIN-VECTOR TABLE.
#
# Defined HERE and shipped in the payload so both services provably test the
# SAME vectors. normalize_origin is duplicated by design (the two services share
# no package), and the stated remediation for CERT-FINDING-2 is "fix both or pin
# identical" -- which is only enforceable if one side cannot quietly test a
# different list from the other.
ORIGIN_VECTORS = [
    # label,                url,                                          control?
    ("ipv6_full_port",      "https://[2001:db8::1]:8443/apply",           False),
    ("ipv6_full_noport",    "https://[2001:db8::1]/apply",                False),
    ("ipv6_loopback",       "https://[::1]/apply",                        False),
    ("ipv6_loopback_port",  "https://[::1]:8443/apply",                   False),
    ("ipv6_zone_id",        "https://[fe80::1%25eth0]/apply",             False),
    ("ipv6_v4mapped",       "https://[::ffff:192.0.2.1]/apply",           False),
    ("ipv6_all_zeros",      "https://[::]/apply",                         False),
    ("ipv6_full_8group",    "https://[2001:0db8:0000:0000:0000:0000:0000:0001]/apply", False),
    # CONTROLS -- these must stay idempotent through any fix. A repair that
    # re-brackets too eagerly would break these, so they are the fix's guard.
    ("ctl_ipv4_port",       "https://192.0.2.1:8443/apply",               True),
    ("ctl_dns_port",        "https://staging.example.test:8443/apply",    True),
    ("ctl_dns_plain",       "https://staging.example.test/apply",         True),
    # NEGATIVE CONTROL -- unparseable authority must fail CLOSED to "" and stay
    # there. "" is the verifier's mismatch sentinel, so this must never become
    # a parseable origin as a side effect of the repair.
    ("ctl_malformed_port",  "https://[::1]:notaport/apply",               True),
]

# CERT-FINDING-1 -- MAKE A PROSE FINDING TOOL-EMITTED.
#
# The lesson this whole certification produced (credit: nexusqa-39): a finding a
# TOOL emits gets tracked; a finding only a HUMAN wrote down gets lost. On first
# contact with an outside reviewer, CERT-FINDING-1 was invisible -- the ARB board
# read "certified with ONE open finding" because CERT-FINDING-2 is the one the
# harness prints and CERT-FINDING-1 lived only in prose.
#
# So the harness now emits it too. This is a DOCUMENTATION assertion, which is
# unusual in a crypto harness and is deliberate: the defect IS documentation. The
# claim below is false (Cloud KMS provides EC_SIGN_ED25519), it is load-bearing
# because it justifies keeping a plaintext signing key in process heap, and it is
# what a future engineer will read when deciding whether to revisit key custody.
# Pinning the sentence means the finding closes automatically when the rationale
# is corrected, and cannot be closed by anyone forgetting it existed.
_KEYS_SRC = Path(__file__).resolve().parents[2] / "platform" / "qe-central"     / "app" / "services" / "attestation_keys.py"
_FALSE_KMS_CLAIM = "offers no Ed25519 asymmetric-signing key type"

# The CORRECTION marker. The fixed file must AFFIRM the algorithm exists, not
# merely omit the false sentence -- deleting the whole rationale would otherwise
# read as "fixed".
_KMS_CORRECTION = "EC_SIGN_ED25519"

# The REFUTATION FRAME. The corrected docstring necessarily QUOTES the false
# sentence in order to say it is false, so "the string appears" and "the claim
# is made" are no longer the same question. An occurrence introduced by this
# phrase is a quotation; any other occurrence is an assertion.
_REFUTATION_FRAME = "used to assert that"
_FRAME_WINDOW = 80          # chars of normalised text before the needle


def _kms_claim_asserted(text: str) -> bool:
    """True iff the false KMS claim is ASSERTED (not merely quoted) in ``text``.

    CERT-FINDING-9. The first version of this probe was ``needle in text`` over
    RAW source, and it went green only because the corrected docstring happens to
    wrap the quoted sentence across a newline exactly at the needle boundary.
    The certifier re-introduced the claim as a plain assertion, wrapped the way
    the file already wraps, and the whole harness still reported 151/0.

    So: normalise whitespace first -- a line break must not hide the claim --
    then classify every occurrence. A probe that reports clean because of where
    the text happened to wrap is not a probe, it is a coincidence.
    """
    norm = " ".join(text.split())
    idx = norm.find(_FALSE_KMS_CLAIM)
    while idx != -1:
        window = norm[max(0, idx - _FRAME_WINDOW):idx]
        if _REFUTATION_FRAME not in window:
            return True                     # asserted, not quoted -> the defect
        idx = norm.find(_FALSE_KMS_CLAIM, idx + 1)
    return False


try:
    _keys_text = _KEYS_SRC.read_text(encoding="utf-8")
    kms_claim_present = _kms_claim_asserted(_keys_text)
    kms_correction_present = _KMS_CORRECTION in _keys_text
    kms_probe_read = True
except Exception:
    # A probe that cannot see its target must never report clean, so the two
    # content answers go to their FAILING values, not their passing ones.
    kms_claim_present, kms_correction_present, kms_probe_read = True, False, False

# CERT-FINDING-16 -- MUTATIONS THAT ACTUALLY REACH THE GATE THEY NAME.
#
# The verifier checks the SIGNATURE at step 4, before gates 6-12. So a mutation
# applied to the signed claims by the verifier half dies as `bad_signature` and
# never touches the control it is named for. Four of the ten did exactly that:
# env_kind=prod, budget escalated, tenant swapped, crawl swapped. The harness
# asserted only `not authorized`, which is true either way -- so DELETING the
# production-isolation gate from attest.py entirely still produced 152 checks and
# 0 failures. A check that would still pass if its subject were absent.
#
# These proofs are VALIDLY SIGNED with the same fresh key and carry hostile
# claims. That is not an artificial construction: it models the threat the
# certification record already names -- a compromised or over-generous ISSUER --
# which is precisely the threat gates 6-12 exist for, and the only way to reach
# them past step 4. The verifier half asserts the SPECIFIC reason for each, so a
# deleted gate changes the reason and fails the check.
HOSTILE_BASE = ProvisioningGrant(
    environment_id="env-hostile", tenant_id="tenant-cert",
    crawl_id="crawl-hostile", target_url="https://staging.example.test/apply",
    reset_procedure="wipe-db", max_walk_mutations_per_step=1,
)
_h_claims = HOSTILE_BASE.claims(issuer=ISSUER, issued_at_ms=NOW,
                                lifetime_ms=3_600_000)
_h_revocations = issue_attestation(
    HOSTILE_BASE, private_key_b64=priv, issuer=ISSUER,
    issued_at_ms=NOW, proof_lifetime_ms=3_600_000)["revocations"]


def _hostile(**overrides):
    """One attestation, signed for real, with the claims the issuer should never
    have emitted. Every field is inside the signature, so the verifier cannot
    reject it on integrity and MUST reject it on the named control instead."""
    c = dict(_h_claims)
    c.update(overrides)
    return {"proof": _sign(priv, c).as_dict(), "revocations": _h_revocations}


hostile = {
    # gate 7 - PRODUCTION ISOLATION. The payload the whole milestone turns on.
    "env_kind_prod":    _hostile(env_kind="prod"),
    "env_kind_staging": _hostile(env_kind="staging"),
    "env_kind_blank":   _hostile(env_kind=" "),
    # gate 9 - BINDINGS. A proof is for one tenant, one crawl, one origin.
    "tenant_swapped":   _hostile(tenant_id="tenant-OTHER"),
    "crawl_swapped":    _hostile(crawl_id="crawl-OTHER"),
    # gate 12 + the claims schema. 999 exceeds HARD_MAX_MUTATIONS_PER_STEP
    # (10) so ProofClaims refuses it outright as malformed -- a STRONGER
    # refusal than clamping, and a different control. The clamp itself needs
    # a budget that is schema-valid and still above the FLEET ceiling.
    "budget_over_hard_max": _hostile(max_walk_mutations_per_step=999),
    "budget_over_fleet":    _hostile(max_walk_mutations_per_step=10),
}

# gate 11 - THE REPLAY GUARD, reached for real.
#
# Its contract is "a proof_id may be admitted for exactly ONE crawl_id" -- NOT
# "one use". Re-verifying the same proof on the SAME crawl is deliberately
# admitted, so a second-use-same-crawl test asserts something the guard never
# promised. The guard is reached only by a proof whose CLAIMS name a different
# crawl (so crawl-binding at gate 9 passes) while its proof_id was already
# admitted for another one. That needs two validly signed proofs SHARING a
# proof_id, which only the issuer can mint -- which is why the old harness,
# mutating claims verifier-side, could not test this gate at all.
# CERT-FINDING-17 -- REVOCATION, WHICH THE HARNESS NEVER EXERCISED.
#
# Until now every revocation list minted here was EMPTY: no revoked_proof_ids, no
# revoked_environment_ids. A proof was never revoked and then presented, so the
# two enforcement gates and the list's own integrity gates were never reached.
# Measured: all eight could be deleted from attest.py with the harness still
# reporting 161 checks and 0 failures -- while the record certifies "Revocation is
# fail-closed ... issue-time and verify-time".
#
# An EMPTY list is not the absence of a list -- that distinction is the whole
# mechanism, and it is why the empty case was never obviously wrong. It is a
# positive signed statement ("I have revoked nothing"), and it is genuinely
# exercised by the ten grants above. What was missing is the other half: a list
# that actually revokes the thing being presented.
REVOKED_PROOF_ID = "a11cert0revoked0proof0id00000001"
REV_ENV = "env-revoked"
REV_CRAWL = "crawl-revoked"
REV_URL = "https://staging.example.test/apply"

_rev_grant = ProvisioningGrant(
    environment_id=REV_ENV, tenant_id="tenant-cert", crawl_id=REV_CRAWL,
    target_url=REV_URL, reset_procedure="wipe-db",
    max_walk_mutations_per_step=1, proof_id=REVOKED_PROOF_ID,
)
_rev_claims = _rev_grant.claims(issuer=ISSUER, issued_at_ms=NOW, lifetime_ms=3_600_000)
_rev_proof = _sign(priv, _rev_claims).as_dict()


def _with_revocations(**kw):
    """The same genuine proof, paired with a differently-populated list."""
    return {"proof": _rev_proof,
            "revocations": issue_revocation_list(
                private_key_b64=priv, issuer=kw.pop("issuer", ISSUER),
                issued_at_ms=NOW, **kw)}


revocation = {
    # CONTROL. Revoking something else must leave this proof authorised -- without
    # it, the two checks below would pass on a verifier that refuses everything.
    "control_other_revoked": _with_revocations(
        revoked_proof_ids=["some-other-proof-id-0000000000001"],
        revoked_environment_ids=["env-something-else"]),
    # gate 10b - this exact proof is revoked.
    "proof_revoked": _with_revocations(revoked_proof_ids=[REVOKED_PROOF_ID]),
    # gate 10c - the environment is revoked, whatever the proof says.
    "env_revoked": _with_revocations(revoked_environment_ids=[REV_ENV]),
    # R3 - a validly signed list from a DIFFERENT issuer. Cannot be produced by
    # tampering: `issuer` is inside the signature, so a torn list dies as
    # REVOCATION_BAD_SIGNATURE and never reaches the issuer comparison.
    "wrong_issuer": _with_revocations(issuer="cert.independent.OTHER"),
}

SHARED_PROOF_ID = "a11cert0replay0shared0id00000001"
replay_pair = {
    "a": _hostile(crawl_id="crawl-replay-A", proof_id=SHARED_PROOF_ID),
    "b": _hostile(crawl_id="crawl-replay-B", proof_id=SHARED_PROOF_ID),
}

origin_probe = {}
for label, url, _is_ctl in ORIGIN_VECTORS:
    once = normalize_origin(url)
    origin_probe[label] = {"url": url, "once": once, "twice": normalize_origin(once)}

out = []
for name, url, reset, budget in CASES:
    g = ProvisioningGrant(
        environment_id=f"env-{name}", tenant_id="tenant-cert",
        crawl_id=f"crawl-{name}", target_url=url,
        reset_procedure=reset, max_walk_mutations_per_step=budget,
    )
    att = issue_attestation(g, private_key_b64=priv, issuer=ISSUER,
                            issued_at_ms=NOW, proof_lifetime_ms=3_600_000)
    out.append({
        "name": name, "attestation": att,
        "target_url": url,
        "issuer_origin": normalize_origin(url),   # what the ISSUER computed
        "tenant_id": "tenant-cert", "crawl_id": f"crawl-{name}",
        "budget_requested": budget,
    })

json.dump({"public_key": pub, "issuer": ISSUER, "now_ms": NOW, "cases": out,
           "origin_vectors": [{"label": l, "url": u, "control": c}
                              for l, u, c in ORIGIN_VECTORS],
           "issuer_origin_probe": origin_probe,
           "kms_claim_present": kms_claim_present,
           "kms_correction_present": kms_correction_present,
           "kms_probe_read": kms_probe_read,
           "hostile": hostile,
           "replay_pair": replay_pair,
           "revocation": revocation,
           "revocation_env_id": REV_ENV,
           "revocation_crawl_id": REV_CRAWL,
           "revocation_target_url": REV_URL,
           "hostile_crawl_id": "crawl-hostile",
           "hostile_tenant_id": "tenant-cert",
           "hostile_target_url": "https://staging.example.test/apply"},
          sys.stdout)
