#!/usr/bin/env python3
"""Re-wrap envelope DEKs from the LOCAL KEK to a GCP-KMS KEK.

WHY THIS EXISTS
---------------
Switching ``NEXUS_KEK_PROVIDER`` from ``local`` to ``gcp_kms`` is NOT a config
flip. Every stored blob is self-describing (``EnvelopeBlob``): it records the
``kek_id`` and ``provider`` it was sealed with. After the switch:

  * ``EnvelopeService.decrypt`` refuses outright on ``provider`` mismatch
    (blob=local vs service=gcp_kms), and
  * ``GcpKmsProvider.unwrap`` hands the stored ``kek_id`` to KMS as a CryptoKey
    resource name -- and ``local:__platform__`` is not one.

So a naive flip does not "degrade"; it makes every existing secret permanently
unreadable. On verdict-box that is 9 rows of ``client_apps.creds_blob`` -- the
recorded logins every gated crawl depends on.

WHAT IT DOES
------------
Envelope encryption separates the two keys: the DATA is encrypted under a
per-blob DEK, and only the DEK is wrapped by the KEK. Migrating therefore never
touches ciphertext. For each row this unwraps the DEK with the local KEK, wraps
that same DEK under KMS, and rewrites ONLY kek_id / provider / wrapped_dek.
``nonce``, ``ct`` and ``aad`` are copied byte-for-byte.

ORDER IS LOAD-BEARING. Run this while the fleet is still
``NEXUS_ENV=development`` and ``NEXUS_KEK_PROVIDER=local`` -- the local provider
is the only thing that can read the current blobs. Flip the env and restart
IMMEDIATELY afterwards: between the rewrite and the restart the blobs say
gcp_kms while the running service still says local, and decrypts will fail.

SAFETY
------
  * dry-run is the DEFAULT; --apply is required to write.
  * every row is verified by a full KMS round-trip (unwrap the new wrapped DEK
    through KMS, decrypt the untouched ciphertext) and the plaintext digest is
    compared against the pre-migration one. A row that does not round-trip
    aborts the whole run before anything is written.
  * one transaction: all rows migrate or none do.
  * idempotent: rows already at provider=gcp_kms are skipped.
  * plaintexts are never printed or persisted -- only SHA-256 digests, held in
    memory, to prove the round-trip.

Usage (inside the qe-central container, BEFORE the production flip):
    python scripts/kek_rewrap_local_to_kms.py                # dry run
    python scripts/kek_rewrap_local_to_kms.py --apply        # migrate
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys

sys.path.insert(0, "/app/service")

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from nexus_sdk.security.envelope import (  # noqa: E402
    EnvelopeBlob,
    GcpKmsProvider,
    LocalKekProvider,
)

TABLE = "client_apps"
COLUMN = "creds_blob"
KEY_COLUMN = "app_id"


def _dsn() -> str:
    """The DSN to migrate through -- and it MUST bypass RLS.

    ``client_apps`` has row-level security ENABLED and FORCED
    (relrowsecurity = relforcerowsecurity = 't'), so the service's own least-
    privilege ``qec`` role sees ZERO rows without a tenant context -- and FORCE
    means even the table owner is filtered. The first preflight run against the
    box used QEC_DATABASE_URL and cheerfully reported "rows found: 0 /
    PREFLIGHT OK", which is the most dangerous possible output: a migration that
    silently covers nothing and calls itself a success.

    So the admin DSN is explicit and separate. KEK_MIGRATION_DSN must name a
    superuser or a BYPASSRLS role -- this is a cross-tenant maintenance task by
    definition, and there is no tenant context that would make it correct.
    """
    raw = os.environ.get("KEK_MIGRATION_DSN")
    if not raw:
        sys.exit(
            "FATAL: KEK_MIGRATION_DSN is not set.\n"
            "  client_apps has RLS enabled AND forced, so the service role sees\n"
            "  no rows and this migration would silently do nothing. Point this\n"
            "  at a superuser/BYPASSRLS DSN, e.g.\n"
            "    KEK_MIGRATION_DSN=postgresql://nexus:<pw>@postgres:5432/qecentral"
        )
    # asyncpg wants a bare postgresql:// URL, not SQLAlchemy's +asyncpg form.
    return raw.replace("+asyncpg", "")


async def _resolve(name: str) -> str:
    return name


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the migration (default is a dry run)")
    # PREFLIGHT exists because the two halves of this migration become possible
    # at different times. Reading the blobs needs only the local KEK, which is
    # on the box today; writing them needs KMS, which the box cannot reach until
    # its instance scopes are widened (a stop/start). Preflight answers the
    # question that gates everything else -- "can all 9 rows still be unwrapped
    # and decrypted at all?" -- without a single KMS call, so the risky input to
    # the migration is proven long before the maintenance window opens.
    ap.add_argument("--preflight", action="store_true",
                    help="verify every row unwraps under the LOCAL KEK; no KMS "
                         "calls, no writes")
    ap.add_argument("--expect", type=int, default=None,
                    help="exact row count you expect; abort on any other number")
    args = ap.parse_args()

    kms_key = os.environ.get("NEXUS_KEK_GCP_KEY")
    if not kms_key and not args.preflight:
        sys.exit("FATAL: NEXUS_KEK_GCP_KEY is not set (the target CryptoKey)")
    local_path = os.environ.get("NEXUS_LOCAL_KEK_PATH",
                                "/app/service/data/kek/master.key")
    if not os.path.exists(local_path):
        sys.exit("FATAL: local KEK not found at %s - without it nothing can be "
                 "unwrapped. Do NOT proceed." % local_path)

    import asyncpg

    local = LocalKekProvider(local_path)
    # The tenant->key resolver is constant here: one CryptoKey for the platform.
    # Not constructed at all in preflight -- GcpKmsProvider builds a KMS client
    # in __init__, which is exactly the thing that cannot work yet.
    gcp = None if args.preflight else GcpKmsProvider(
        lambda _tenant: _resolve(kms_key))

    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch(
            "SELECT %s, %s FROM %s WHERE %s IS NOT NULL ORDER BY %s"
            % (KEY_COLUMN, COLUMN, TABLE, COLUMN, KEY_COLUMN)
        )
        print("mode        : %s" % ("PREFLIGHT" if args.preflight
                                    else "APPLY" if args.apply else "DRY RUN"))
        print("target key  : %s" % (kms_key or "<none - preflight>"))
        print("rows found  : %d\n" % len(rows))

        # ZERO IS NOT SUCCESS. An empty result here means the DSN cannot see the
        # data (RLS, wrong database, wrong role) far more often than it means
        # there is nothing to migrate -- and "0 rows migrated, all good" would
        # send an operator into the production flip believing the blobs were
        # converted. Refuse to be the thing that says fine when it looked at
        # nothing. --expect makes the count an explicit contract.
        if not rows:
            sys.exit(
                "FATAL: found 0 rows with a non-null %s.\n"
                "  This is almost certainly the DSN, not an empty table:\n"
                "  %s has RLS enabled and FORCED, so a non-superuser role\n"
                "  returns nothing. Verify with, as superuser:\n"
                "    SELECT count(*) FROM %s WHERE %s IS NOT NULL;"
                % (COLUMN, TABLE, TABLE, COLUMN)
            )
        if args.expect is not None and len(rows) != args.expect:
            sys.exit("FATAL: expected %d row(s), found %d - refusing to run on "
                     "a set that is not the one you checked."
                     % (args.expect, len(rows)))

        migrated = []
        skipped = 0

        for row in rows:
            app_id = str(row[KEY_COLUMN])
            blob = EnvelopeBlob.from_bytes(bytes(row[COLUMN]))

            if blob.provider == "gcp_kms":
                print("  SKIP    %s  already gcp_kms" % app_id[:8])
                skipped += 1
                continue
            if blob.provider != "local":
                sys.exit("FATAL: %s has unexpected provider %r - aborting, "
                         "nothing written" % (app_id, blob.provider))

            # kek_id is "local:<tenant>"; that tenant is the wrap-time AAD.
            tenant = blob.kek_id.split(":", 1)[1]

            dek = await local.unwrap(tenant, blob.kek_id, blob.wrapped_dek)
            plain = AESGCM(dek).decrypt(blob.nonce, blob.ciphertext, blob.aad)
            before = hashlib.sha256(plain).hexdigest()

            if args.preflight:
                print("  READY   %s  local:%s unwrapped + decrypted (%s, %d B)"
                      % (app_id[:8], tenant, before[:12], len(plain)))
                migrated.append((app_id, b""))
                continue

            new_kek_id, new_wrapped = await gcp.wrap(tenant, dek)
            new_blob = EnvelopeBlob(
                version=blob.version,
                kek_id=new_kek_id,
                provider="gcp_kms",
                nonce=blob.nonce,             # untouched
                ciphertext=blob.ciphertext,   # untouched
                wrapped_dek=new_wrapped,      # the only new material
                aad=blob.aad,                 # untouched
            )

            # PROVE it before trusting it: back through KMS, then decrypt the
            # untouched ciphertext with the DEK KMS hands back.
            check_dek = await gcp.unwrap(tenant, new_blob.kek_id,
                                         new_blob.wrapped_dek)
            check = AESGCM(check_dek).decrypt(new_blob.nonce,
                                              new_blob.ciphertext,
                                              new_blob.aad)
            after = hashlib.sha256(check).hexdigest()
            if before != after:
                sys.exit("FATAL: %s failed the KMS round-trip (%s != %s) - "
                         "aborting, nothing written"
                         % (app_id, before[:12], after[:12]))

            print("  OK      %s  local:%s -> gcp_kms  round-trip verified (%s)"
                  % (app_id[:8], tenant, before[:12]))
            migrated.append((app_id, new_blob.to_bytes()))

        print("\nverified    : %d   skipped: %d" % (len(migrated), skipped))

        if args.preflight:
            print("\nPREFLIGHT - every row above is readable under the local "
                  "KEK, so the migration has a valid input set. No KMS calls "
                  "were made and nothing was written.")
            return 0
        if not args.apply:
            print("\nDRY RUN - nothing written. Re-run with --apply to migrate.")
            return 0
        if not migrated:
            print("nothing to do.")
            return 0

        async with conn.transaction():
            for app_id, raw in migrated:
                await conn.execute(
                    "UPDATE %s SET %s = $1 WHERE %s = $2"
                    % (TABLE, COLUMN, KEY_COLUMN),
                    raw, app_id,
                )
        print("APPLIED - %d row(s) re-wrapped in one transaction." % len(migrated))
        print("NEXT: flip NEXUS_ENV=production + NEXUS_KEK_PROVIDER=gcp_kms and "
              "restart NOW; until you do, the running service still reads local.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
