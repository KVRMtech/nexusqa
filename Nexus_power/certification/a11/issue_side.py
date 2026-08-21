"""CERT-A11 (independent): ISSUER half. Runs with cwd=platform/qe-central.

Mints attestations for a spread of grants chosen by the CERTIFIER, using a
FRESHLY GENERATED key -- not the committed test key, not the frozen golden.
Emits JSON on stdout for the verifier half to consume in a separate process.
"""
import json, sys
from app.services.signing import generate_keypair
from app.services.walk_attestation import ProvisioningGrant, issue_attestation, normalize_origin

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
           "issuer_origin_probe": origin_probe},
          sys.stdout)
