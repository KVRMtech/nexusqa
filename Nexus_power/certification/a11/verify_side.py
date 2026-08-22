"""CERT-A11 (independent): VERIFIER half. Runs with cwd=engines/qe-explorer.

Consumes the issuer half's JSON and checks, in a SEPARATE interpreter:
  1. every genuine attestation is AUTHORIZED (interop for arbitrary grants);
  2. the two services agree on normalize_origin for each target_url;
  3. the granted budget is what was asked for;
  4. adversarial mutations are each DENIED with the right reason.
"""
import copy, json, sys
from app.attest import (verify_provisioning_proof, TrustStore, normalize_origin,
                        ProofReplayGuard, AttestReason)

data = json.load(open(sys.argv[1], encoding="utf-8"))
NOW = data["now_ms"]
trust = TrustStore.from_public_keys([data["public_key"]], issuer=data["issuer"])

results, failures = [], []

def check(cond, label):
    results.append((cond, label))
    if not cond:
        failures.append(label)

for c in data["cases"]:
    n, att = c["name"], c["attestation"]
    kw = dict(trust=trust, crawl_id=c["crawl_id"], tenant_id=c["tenant_id"],
              target_url=c["target_url"], now_epoch_ms=NOW + 1000)

    # 1 - genuine proof authorises
    v = verify_provisioning_proof(att, replay_guard=ProofReplayGuard(), **kw)
    label = f"{n}: genuine attestation AUTHORIZED (got {v.reason!r} {v.detail!r})"
    if n == "ipv6" and not v.authorized:
        label = "[CERT-FINDING-2 | IPv6] " + label
    check(v.authorized, label)
    # 3 - budget honoured
    # The verifier applies min(signed claim, FLEET ceiling) -- defence in depth,
    # confirmed intended (attest.py:561). Certifier's first expectation was wrong.
    if v.authorized:
        expect = min(c["budget_requested"], trust.max_mutations_per_step)
        check(v.max_mutations_per_step == expect,
              f"{n}: budget {v.max_mutations_per_step} == min(requested"
              f"={c['budget_requested']}, fleet={trust.max_mutations_per_step})")

    # 2 - CROSS-SERVICE normalize_origin agreement on this URL
    check(normalize_origin(c["target_url"]) == c["issuer_origin"],
          f"{n}: normalize_origin agrees across services "
          f"(explorer={normalize_origin(c['target_url'])!r} issuer={c['issuer_origin']!r})")

    # 4 - adversarial mutations, each must DENY
    def denied(mutate, label, **over):
        a = copy.deepcopy(att); mutate(a)
        k = dict(kw); k.update(over)
        r = verify_provisioning_proof(a, replay_guard=ProofReplayGuard(), **k)
        check(not r.authorized, f"{n}: {label} DENIED (got authorized={r.authorized})")
        return r

    denied(lambda a: a["proof"]["claims"].update(env_kind="prod"), "env_kind=prod")
    denied(lambda a: a["proof"]["claims"].update(max_walk_mutations_per_step=999),
           "budget escalated in claims")
    denied(lambda a: a["proof"]["claims"].update(tenant_id="tenant-other"), "tenant swapped")
    denied(lambda a: a["proof"]["claims"].update(crawl_id="crawl-other"), "crawl swapped")
    denied(lambda a: a["proof"].update(signature="A" * 86), "signature forged")
    denied(lambda a: a["proof"].update(kid="0" * 16), "unknown kid")
    denied(lambda a: a["proof"].update(alg="hs256"), "alg downgraded")
    denied(lambda a: a.pop("revocations"), "revocation list stripped")
    denied(lambda a: None, "wrong origin at verify time",
           target_url="https://production.example.test/apply")
    denied(lambda a: None, "expired", now_epoch_ms=NOW + 3_600_000 + 5000)

# replay: same proof, two different crawls, shared guard
guard = ProofReplayGuard()
c0 = data["cases"][0]
verify_provisioning_proof(c0["attestation"], trust=trust, crawl_id=c0["crawl_id"],
                          tenant_id=c0["tenant_id"], target_url=c0["target_url"],
                          now_epoch_ms=NOW + 1000, replay_guard=guard)
r2 = verify_provisioning_proof(c0["attestation"], trust=trust, crawl_id="crawl-DIFFERENT",
                               tenant_id=c0["tenant_id"], target_url=c0["target_url"],
                               now_epoch_ms=NOW + 1000, replay_guard=guard)
check(not r2.authorized, f"replay of one proof onto a second crawl DENIED (got {r2.authorized})")

# an unconfigured trust store proves nothing
empty = TrustStore.from_public_keys([], issuer=data["issuer"])
r3 = verify_provisioning_proof(c0["attestation"], trust=empty, crawl_id=c0["crawl_id"],
                               tenant_id=c0["tenant_id"], target_url=c0["target_url"],
                               now_epoch_ms=NOW + 1000, replay_guard=ProofReplayGuard())
check(not r3.authorized and r3.reason == AttestReason.NO_TRUST_ANCHOR,
      f"unconfigured trust store denies (reason={r3.reason!r})")

# ---------------------------------------------------------------------------
# CERT-FINDING-16 -- assert the REASON, on proofs that reach the gate.
#
# Every check below would go RED today if its gate were deleted from attest.py,
# which is the property the mutation checks above lack: those edit signed claims,
# so they die at the signature check and pass whatever gates 6-12 do. Measured:
# with production isolation deleted the harness still reported 152/0.
#
# Asserting the reason is half of the fix and the cheaper half. The other half is
# that these proofs are validly signed, so they REACH the gate. One without the
# other does not close this: reason-asserting a torn proof would demand
# `not_disposable` and get `bad_signature` on correct code.
hostile = data.get("hostile") or {}
h_kw = dict(trust=trust, crawl_id=data.get("hostile_crawl_id"),
            tenant_id=data.get("hostile_tenant_id"),
            target_url=data.get("hostile_target_url"), now_epoch_ms=NOW + 1000)

def hostile_denied(name, expect_reason):
    att = hostile.get(name)
    # A missing payload must FAIL, never silently skip: a check that cannot see
    # its subject must not report clean.
    if att is None:
        check(False, f"hostile[{name}]: payload present (issuer half did not emit it)")
        return
    r = verify_provisioning_proof(att, replay_guard=ProofReplayGuard(), **h_kw)
    check(not r.authorized and r.reason == expect_reason,
          f"hostile[{name}]: SIGNED hostile claim denied as {expect_reason!r} "
          f"(got authorized={r.authorized} reason={r.reason!r}) -- this proof is "
          f"validly signed, so it reaches the gate instead of dying at step 4")

hostile_denied("env_kind_prod",    AttestReason.NOT_DISPOSABLE)
hostile_denied("env_kind_staging", AttestReason.NOT_DISPOSABLE)
hostile_denied("env_kind_blank",   AttestReason.NOT_DISPOSABLE)
hostile_denied("tenant_swapped",   AttestReason.TENANT_MISMATCH)
hostile_denied("crawl_swapped",    AttestReason.CRAWL_BINDING_MISMATCH)

# The claims SCHEMA: a signed budget above HARD_MAX is refused outright, not
# clamped. Stronger than the clamp and a separate control, so pinned separately.
hostile_denied("budget_over_hard_max", AttestReason.MALFORMED_CLAIMS)

# gate 12 - the CLAMP, on a schema-valid signed budget above the FLEET ceiling.
_b = hostile.get("budget_over_fleet")
if _b is None:
    check(False, "hostile[budget_over_fleet]: payload present")
else:
    rb = verify_provisioning_proof(_b, replay_guard=ProofReplayGuard(), **h_kw)
    check(rb.authorized and rb.max_mutations_per_step == trust.max_mutations_per_step,
          f"hostile[budget_over_fleet]: a SIGNED budget of 10 is AUTHORISED but "
          f"clamped to the fleet ceiling {trust.max_mutations_per_step} "
          f"(authorized={rb.authorized} budget={rb.max_mutations_per_step})")

# gate 11 - THE REPLAY GUARD, actually reached.
#
# The pre-existing replay check replays onto a DIFFERENT crawl, which gate 9's
# crawl-binding refuses FIRST -- so gate 11 is never evaluated, and deleting the
# guard left the harness at 152/0.
#
# The guard's contract is "a proof_id may be admitted for exactly ONE crawl_id".
# NOT "one use": re-verifying the same proof on the SAME crawl is deliberately
# admitted (`return bound == cid`), so a second-use-same-crawl test asserts
# something the guard never promised and fails on correct code. Reaching gate 11
# requires two validly signed proofs that SHARE a proof_id but name DIFFERENT
# crawls -- claims consistent, so gate 9 passes and the guard decides.
_pair = data.get("replay_pair") or {}
if not _pair.get("a") or not _pair.get("b"):
    check(False, "replay_pair: payload present (issuer half did not emit it)")
else:
    _g = ProofReplayGuard()
    _ra = verify_provisioning_proof(
        _pair["a"], trust=trust, crawl_id="crawl-replay-A",
        tenant_id=data["hostile_tenant_id"], target_url=data["hostile_target_url"],
        now_epoch_ms=NOW + 1000, replay_guard=_g)
    # CONTROL. Without it the check below could pass because the proof was never
    # good -- the absence-assertion trap this repository names by name.
    check(_ra.authorized,
          f"replay[control]: proof A is authorised on its own crawl "
          f"(got {_ra.reason!r})")
    _rb = verify_provisioning_proof(
        _pair["b"], trust=trust, crawl_id="crawl-replay-B",
        tenant_id=data["hostile_tenant_id"], target_url=data["hostile_target_url"],
        now_epoch_ms=NOW + 1000, replay_guard=_g)
    check(not _rb.authorized and _rb.reason == AttestReason.PROOF_REPLAYED,
          f"replay[gate 11]: a SECOND proof sharing proof_id, bound to a DIFFERENT "
          f"crawl, is denied as proof_replayed -- its claims are internally "
          f"consistent so gate 9 passes and the guard is what refuses it "
          f"(got authorized={_rb.authorized} reason={_rb.reason!r})")


# ---------------------------------------------------------------------------
# CERT-FINDING-17 -- REVOCATION, exercised for the first time.
#
# Every list this harness minted was EMPTY, so a proof was never revoked and then
# presented. All eight revocation guards deleted cleanly at 161/0. These reach
# them. The CONTROL is load-bearing: without it, a verifier that refused every
# attestation would satisfy the two revocation checks below.
rev = data.get("revocation") or {}
r_kw = dict(trust=trust, crawl_id=data.get("revocation_crawl_id"),
            tenant_id=data.get("hostile_tenant_id"),
            target_url=data.get("revocation_target_url"), now_epoch_ms=NOW + 1000)

def _rev(name, **over):
    att = rev.get(name)
    if att is None:
        return None
    k = dict(r_kw); k.update(over)
    return verify_provisioning_proof(att, replay_guard=ProofReplayGuard(), **k)

_rc = _rev("control_other_revoked")
check(_rc is not None and _rc.authorized,
      f"revocation[CONTROL]: a proof is AUTHORISED when the list revokes something "
      f"else (got {_rc and _rc.reason!r}) -- without this control, the two checks "
      f"below would pass on a verifier that refuses everything")

for name, why, what in (
    ("proof_revoked", AttestReason.REVOKED, "this proof_id is on the list"),
    ("env_revoked",   AttestReason.REVOKED, "this environment_id is on the list"),
    ("wrong_issuer",  AttestReason.REVOCATION_ISSUER_MISMATCH,
     "a validly signed list from ANOTHER issuer"),
):
    r = _rev(name)
    check(r is not None and not r.authorized and r.reason == why,
          f"revocation[{name}]: {what} -> denied as {why!r} "
          f"(got authorized={r and r.authorized} reason={r and r.reason!r})")

# R6 - a STALE list proves nothing about what has been revoked since. The list
# lives 10 minutes and the proof an hour, so at +20 minutes the proof is still
# valid and only the list has expired -- which must still be a refusal.
_re = _rev("control_other_revoked", now_epoch_ms=NOW + 20 * 60 * 1000)
check(_re is not None and not _re.authorized
      and _re.reason == AttestReason.REVOCATION_EXPIRED,
      f"revocation[expired list]: a stale list is refused as revocation_expired "
      f"even though the PROOF is still within its lifetime "
      f"(got authorized={_re and _re.authorized} reason={_re and _re.reason!r})")

# R4 - the list's own signature. Verifier-side tampering is legitimate here: this
# gate exists precisely to catch a torn list.
if rev.get("control_other_revoked") is not None:
    _forged = copy.deepcopy(rev["control_other_revoked"])
    _forged["revocations"]["signature"] = "A" * 86
    _rf = verify_provisioning_proof(_forged, replay_guard=ProofReplayGuard(), **r_kw)
    check(not _rf.authorized and _rf.reason == AttestReason.REVOCATION_BAD_SIGNATURE,
          f"revocation[forged list signature]: denied as revocation_bad_signature "
          f"(got authorized={_rf.authorized} reason={_rf.reason!r})")
    _dg = copy.deepcopy(rev["control_other_revoked"])
    _dg["revocations"]["alg"] = "hs256"
    _rd = verify_provisioning_proof(_dg, replay_guard=ProofReplayGuard(), **r_kw)
    check(not _rd.authorized and _rd.reason == AttestReason.REVOCATION_BAD_SIGNATURE,
          f"revocation[list alg downgraded]: denied as revocation_bad_signature "
          f"(got authorized={_rd.authorized} reason={_rd.reason!r})")
else:
    check(False, "revocation[control_other_revoked]: payload present")


# ---------------------------------------------------------------------------
# A11b -- ORIGIN-VECTOR TABLE: fence the CLASS, not the one instance.
#
# CERT-FINDING-2 was found through a single IPv6 grant. Measurement since shows
# the defect is CATEGORICAL rather than a handful of cases: normalize_origin
# reformats host:port without ever re-bracketing, so EVERY host containing ':'
# breaks. A finding fenced by one example is a finding that comes back in a form
# nobody tested, so the whole class is pinned here.
#
# Two invariants, both checked against BOTH services' copies:
#   1. AGREEMENT  -- the duplicated copies must not diverge. This is what makes
#                    "fix both or pin identical" enforceable rather than hoped.
#   2. IDEMPOTENCE -- N(N(u)) == N(u). The function's OUTPUT is signed into the
#                    claims and re-normalised by the verifier, so an output it
#                    cannot re-parse is a proof guaranteed to be refused.
vectors = data.get("origin_vectors") or []
probe = data.get("issuer_origin_probe") or {}
broken = []
for v in vectors:
    label, url, is_ctl = v["label"], v["url"], v["control"]
    once = normalize_origin(url)
    twice = normalize_origin(once)
    iss = probe.get(label) or {}

    # 1 - the two copies must agree, control or not. A divergence here is its
    #     own defect and would not be caught by either service's own tests.
    check(iss.get("once") == once,
          f"origin[{label}]: the two normalize_origin copies AGREE "
          f"(issuer={iss.get('once')!r} verifier={once!r})")

    # 2 - idempotence. Controls MUST hold it today and must still hold it after
    #     any repair; a fix that re-brackets too eagerly breaks them.
    if is_ctl:
        check(twice == once,
              f"origin[{label}]: CONTROL is idempotent "
              f"({once!r} -> {twice!r})")
    elif twice != once:
        broken.append(f"{label} {url!r} -> {once!r} -> {twice!r}")

# One aggregated line for the open finding, so the failure COUNT stays stable
# and meaningful: a CI gate keyed to "expected failures" must go red on a NEW
# defect, not on the size of a known one.
check(not broken,
      "[CERT-FINDING-2 | A11b] normalize_origin is idempotent for every IPv6 "
      f"form ({len(broken)}/{len([v for v in vectors if not v['control']])} "
      f"non-idempotent: {'; '.join(broken[:3])}"
      + (" ..." if len(broken) > 3 else "") + ")")

# ---------------------------------------------------------------------------
# CERT-FINDING-1 -- emitted by the harness, not left in prose.
#
# See issue_side.py for why a crypto harness asserts on documentation: the defect
# IS documentation, it is load-bearing (it justifies a plaintext signing key in
# process heap), and a finding only a human wrote down is one an outside reviewer
# cannot see. This closes automatically when the rationale is corrected.
check(data.get("kms_probe_read") is True,
      "CERT-FINDING-1 probe could READ attestation_keys.py (a probe that cannot "
      "read its target must not report 'clean')")
check(data.get("kms_claim_present") is False,
      "[CERT-FINDING-1 | KMS] attestation_keys.py no longer ASSERTS that Cloud "
      "KMS 'offers no Ed25519 asymmetric-signing key type' -- it does provide "
      "EC_SIGN_ED25519, and that false claim is what justifies keeping a "
      "plaintext signing key in process heap")

# CERT-FINDING-9 -- the third assertion. "The false sentence is absent" and
# "the rationale was deleted wholesale" are indistinguishable from the check
# above, and only one of them is a fix. This one requires the corrected file to
# AFFIRM the algorithm exists, so silence cannot pass as a correction. It is the
# same two-assertions-never-one-truthy-test rule the register states for
# probe-integrity, applied to the content instead of the file handle.
check(data.get("kms_correction_present") is True,
      "[CERT-FINDING-1 | KMS] attestation_keys.py AFFIRMS EC_SIGN_ED25519 "
      "exists -- omitting the false sentence is not the same as correcting "
      "the rationale, and deleting it entirely must not read as a fix")

print(f"CHECKS RUN : {len(results)}")
print(f"FAILURES   : {len(failures)}")
for f in failures:
    print("  FAIL:", f)
sys.exit(1 if failures else 0)
