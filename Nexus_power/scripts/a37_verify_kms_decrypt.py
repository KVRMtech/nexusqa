"""A37.1 ACCEPTANCE PROBE — do the REAL production credentials decrypt under KMS?

WHY THIS EXISTS SEPARATELY FROM kek_rewrap_local_to_kms.py.
The migration script is idempotent: rows already at provider=gcp_kms are
SKIPPED. Once the migration has run, re-running it proves nothing — it reports
"0 to migrate" and exits clean whether the blobs are readable or permanently
undecryptable. That is exactly the state verdict-box is in, and exactly the
output the ARB warning describes as most dangerous.

So the acceptance criterion ("existing credentials decrypt successfully under
KMS") needs its own instrument. This one does not care what the metadata says.
It builds the SAME provider the running service builds from env, and performs a
real Cloud KMS unwrap of every stored blob.

WHAT IT PROVES
    Every client_apps.creds_blob row can be unwrapped through the production
    CryptoKey and decrypted. A row that cannot is reported as FAIL with the
    exception type, and the run exits non-zero.

SAFETY
    * READ-ONLY. No UPDATE, no INSERT, no KMS key mutation. Encrypt is never
      called; only unwrap + AES-GCM open.
    * Plaintext is NEVER printed, written or logged. Only a byte length and a
      SHA-256 prefix leave this process — enough to prove a distinct secret was
      recovered, useless as a credential.
    * It refuses to run unless NEXUS_KEK_PROVIDER=gcp_kms, so it cannot be
      mistaken for a pass while silently exercising the local KEK.

USAGE (from the VM, against the running service's own environment):
    PGPW=$(sudo docker exec nexus-postgres printenv POSTGRES_PASSWORD)
    sudo docker cp a37_verify_kms_decrypt.py nexus-qe-central:/tmp/
    sudo docker exec -e PGPW="$PGPW" nexus-qe-central python /tmp/a37_verify_kms_decrypt.py

    exit 0  every stored credential decrypted through KMS
    exit 1  one or more failed, or there were no rows to check
    exit 2  refused — the service is not configured for gcp_kms

Requires an RLS-bypassing role: client_apps has FORCE row-level security, and
the application role sees zero rows. Zero rows is reported as FAIL, never as
success — a probe that checks nothing must not be able to pass.
"""
import asyncio
import hashlib
import os
import sys

sys.path.insert(0, "/app/service")

from nexus_sdk.security.envelope import (  # noqa: E402
    EnvelopeBlob,
    EnvelopeService,
    GcpKmsProvider,
)

CANDIDATE_HOSTS = ["postgres", "nexus-postgres", "127.0.0.1"]


async def _connect(asyncpg, pw):
    last = None
    for host in CANDIDATE_HOSTS:
        dsn = "postgresql://nexus:%s@%s:5432/qecentral" % (pw, host)
        try:
            return await asyncpg.connect(dsn), host
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise SystemExit("could not connect to postgres: %r" % (last,))


async def main() -> int:
    key_name = os.environ.get("NEXUS_KEK_GCP_KEY")
    provider_env = os.environ.get("NEXUS_KEK_PROVIDER", "local").lower()
    print("service provider from env : %s" % provider_env)
    print("target CryptoKey          : %s" % key_name)
    if provider_env != "gcp_kms":
        print("REFUSING: this probe asserts the KMS path; env says %r" % provider_env)
        return 2

    async def _resolver(_tenant_id: str) -> str:
        return key_name

    svc = EnvelopeService(GcpKmsProvider(kek_resolver=_resolver))
    print("EnvelopeService provider  : %s" % svc.provider_id)

    import asyncpg  # noqa: PLC0415

    pw = os.environ["PGPW"]
    conn, host = await _connect(asyncpg, pw)
    print("postgres host             : %s" % host)

    rows = await conn.fetch(
        "select app_id, tenant_id, creds_blob from client_apps "
        "where creds_blob is not null order by app_id"
    )
    print("rows with creds_blob      : %d" % len(rows))
    print("")
    print("%-14s %-10s %-9s %-8s %s" % ("app_id", "tenant", "decrypt", "bytes", "sha256[:12]"))
    print("-" * 66)

    ok = 0
    fail = 0
    for r in rows:
        app_id = str(r["app_id"])[:12]
        tenant = str(r["tenant_id"])[:8]
        try:
            blob = EnvelopeBlob.from_bytes(bytes(r["creds_blob"]))
            pt = await svc.decrypt(str(r["tenant_id"]), blob)
            digest = hashlib.sha256(pt).hexdigest()[:12]
            n = len(pt)
            del pt
            ok += 1
            print("%-14s %-10s %-9s %-8d %s" % (app_id, tenant, "OK", n, digest))
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print("%-14s %-10s %-9s %-8s %s"
                  % (app_id, tenant, "FAIL", "-", type(exc).__name__ + ": " + str(exc)[:40]))

    await conn.close()
    print("")
    print("A37.1 DECRYPT-UNDER-KMS: %d OK / %d FAIL" % (ok, fail))
    # ZERO IS NOT SUCCESS. An empty result set means RLS filtered the probe, not
    # that every credential is healthy — the same trap the rewrap migration hit.
    if fail or not ok:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS — every stored credential decrypts through Cloud KMS")
    return 0


sys.exit(asyncio.run(main()))
