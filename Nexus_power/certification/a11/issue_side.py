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

json.dump({"public_key": pub, "issuer": ISSUER, "now_ms": NOW, "cases": out},
          sys.stdout)
