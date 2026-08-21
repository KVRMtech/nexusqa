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
      "[CERT-FINDING-1 | KMS] attestation_keys.py no longer claims Cloud KMS "
      "'offers no Ed25519 asymmetric-signing key type' -- it does provide "
      "EC_SIGN_ED25519, and that false claim is what justifies keeping a "
      "plaintext signing key in process heap")

print(f"CHECKS RUN : {len(results)}")
print(f"FAILURES   : {len(failures)}")
for f in failures:
    print("  FAIL:", f)
sys.exit(1 if failures else 0)
