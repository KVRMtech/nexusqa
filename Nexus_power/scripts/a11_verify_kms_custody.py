"""A11.1 ACCEPTANCE PROBE — does the ISSUER KEY custody path work under REAL KMS?

WHY THIS EXISTS SEPARATELY FROM a37_verify_kms_decrypt.py
=========================================================
A37's probe unwraps ``client_apps.creds_blob`` — it proves the envelope path
works for CREDENTIALS that already exist. A11 seals a different thing, under a
different AAD, and there is no A11 row on any deployment yet (no operator has
bootstrapped an issuer key). So A37 passing says nothing about A11.

A11's open item was precise: *"the custody code paths are proven against
LocalKekProvider with real AES-GCM. GcpKmsProvider changes WHERE THE KEK LIVES,
not the envelope format, the AAD binding or the unwrap path — but that
substitution is unproven."* This closes exactly that.

WHAT IT PROVES
==============
The full A11 custody round-trip against the deployment's real Cloud KMS key:

    generate Ed25519  ->  seal (REAL KMS wrap, AAD=__platform__)
                      ->  EnvelopeBlob.to_bytes / from_bytes   (the storage form)
                      ->  unseal (REAL KMS unwrap)
                      ->  the unsealed half still matches the published public
                          half (the consistency check `active_signer` performs)
                      ->  it still SIGNS, and the signature verifies under the
                          canonical encoding the explorer's verifier uses

Plus three NEGATIVE CONTROLS, because a round-trip that succeeds proves the
happy path and nothing else. Each removes one guarantee and requires the
failure:

    * tampered ciphertext        must fail authentication
    * wrong AAD                  must be refused (the binding is real)
    * wrong KEK tenant           must be refused (cross-context replay)

Without those, a provider that ignored AAD entirely — or returned its input
unchanged — would pass.

SAFETY
======
    * PERSISTS NOTHING. No DB write, no row read, no KMS key created, rotated
      or scheduled for destruction. Only `encrypt` and `decrypt` on the
      EXISTING KEK — the same two calls the running service makes constantly.
    * The Ed25519 key it generates is ephemeral and never leaves this process;
      it is NOT the platform issuer key and is not stored anywhere.
    * NO KEY MATERIAL IS PRINTED. Only public-key ids, byte lengths and boolean
      outcomes leave this process.
    * Refuses to run unless NEXUS_KEK_PROVIDER=gcp_kms, so it cannot be mistaken
      for a pass while quietly exercising the local development KEK.

USAGE (from the VM, against the running service's own environment):

    sudo docker cp a11_verify_kms_custody.py nexus-qe-central:/tmp/
    sudo docker exec nexus-qe-central python /tmp/a11_verify_kms_custody.py

    exit 0  the A11 custody path works under real Cloud KMS
    exit 1  it does not, or a negative control failed to fail
"""
from __future__ import annotations

import asyncio
import os
import sys

PLATFORM_KEK_TENANT = "__platform__"   # mirrors app.services.attestation_keys
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


async def main() -> int:
    provider_name = os.environ.get("NEXUS_KEK_PROVIDER", "").lower()
    if provider_name != "gcp_kms":
        print(f"REFUSING: NEXUS_KEK_PROVIDER={provider_name!r}, not 'gcp_kms'.\n"
              f"This probe exists to prove the KMS substitution; running it "
              f"against the local development KEK would report a pass that "
              f"means the opposite of what it appears to.", file=sys.stderr)
        return 1

    from nexus_sdk.security.envelope import (
        EnvelopeBlob,
        EnvelopeService,
        GcpKmsProvider,
        IntegrityError,
    )
    from app.services.signing import (
        canonical_bytes,
        generate_keypair,
        public_key_of,
        sign_payload,
        verify_signature,
    )

    key_name = os.environ["NEXUS_KEK_GCP_KEY"]
    print(f"KEK        : {key_name}")
    print(f"provider   : {provider_name}")
    print(f"AAD tenant : {PLATFORM_KEK_TENANT}\n")

    async def _resolver(_tenant_id: str) -> str:
        return key_name

    envelope = EnvelopeService(GcpKmsProvider(kek_resolver=_resolver))
    aad = PLATFORM_KEK_TENANT.encode("utf-8")

    # ── 1. seal an ephemeral issuer key through REAL Cloud KMS ─────────────
    private_b64, public_b64 = generate_keypair()
    blob = await envelope.encrypt(PLATFORM_KEK_TENANT,
                                  private_b64.encode("ascii"), aad=aad)
    check("KMS wrap succeeded", bool(blob.wrapped_dek),
          f"wrapped_dek={len(blob.wrapped_dek)}B ciphertext={len(blob.ciphertext)}B")
    check("blob names the real KMS provider", blob.provider == "gcp_kms",
          f"provider={blob.provider}")
    check("blob names the deployment's CryptoKey", blob.kek_id == key_name)
    check("AAD is bound into the blob", blob.aad == aad)

    # The ciphertext must not contain the plaintext — a provider that returned
    # its input unchanged would otherwise pass every test below.
    raw = blob.to_bytes()
    check("stored bytes do not contain the private key",
          private_b64.encode("ascii") not in raw, f"blob={len(raw)}B")

    # ── 2. the storage form round-trips ────────────────────────────────────
    reparsed = EnvelopeBlob.from_bytes(raw)
    check("EnvelopeBlob.to_bytes/from_bytes round-trips",
          reparsed.kek_id == blob.kek_id and reparsed.aad == blob.aad)

    # ── 3. unseal through REAL Cloud KMS, and prove the key survived ───────
    unsealed = (await envelope.decrypt(PLATFORM_KEK_TENANT, reparsed,
                                       expected_aad=aad)).decode("ascii")
    check("KMS unwrap returned the same key",
          public_key_of(unsealed) == public_b64,
          f"kid_pub={public_b64[:12]}…")

    # ── 4. it still SIGNS, and the signature verifies ──────────────────────
    claims = {"v": 1, "probe": "a11-custody", "env_kind": "disposable"}
    signature = sign_payload(unsealed, claims)
    check("a KMS-unsealed key produces a verifying signature",
          verify_signature(public_b64, claims, signature),
          f"canonical={len(canonical_bytes(claims))}B sig={len(signature)}chars")

    # ── 5. NEGATIVE CONTROLS — each must FAIL, or the checks above are hollow ─
    print("\n  negative controls (each MUST fail):")

    tampered = EnvelopeBlob(
        version=reparsed.version, kek_id=reparsed.kek_id,
        provider=reparsed.provider, nonce=reparsed.nonce,
        ciphertext=reparsed.ciphertext[:-1] + bytes([reparsed.ciphertext[-1] ^ 0x01]),
        wrapped_dek=reparsed.wrapped_dek, aad=reparsed.aad)
    try:
        await envelope.decrypt(PLATFORM_KEK_TENANT, tampered, expected_aad=aad)
        check("tampered ciphertext is refused", False, "IT DECRYPTED")
    except Exception as exc:
        check("tampered ciphertext is refused", True, type(exc).__name__)

    try:
        await envelope.decrypt(PLATFORM_KEK_TENANT, reparsed,
                               expected_aad=b"some-other-context")
        check("wrong AAD is refused", False, "IT DECRYPTED")
    except IntegrityError as exc:
        check("wrong AAD is refused", True, type(exc).__name__)
    except Exception as exc:
        check("wrong AAD is refused", True, type(exc).__name__)

    try:
        await envelope.decrypt("some-tenant-that-is-not-the-platform", reparsed,
                               expected_aad=aad)
        check("wrong KEK tenant is refused", False, "IT DECRYPTED")
    except Exception as exc:
        check("wrong KEK tenant is refused", True, type(exc).__name__)

    print()
    if FAILURES:
        print(f"A11 KMS CUSTODY: FAILED ({len(FAILURES)}) -> {FAILURES}",
              file=sys.stderr)
        return 1
    print("A11 KMS CUSTODY: PASS — the issuer-key seal/unseal/sign path works "
          "against the deployment's real Cloud KMS key, and every negative "
          "control failed as required. Nothing was persisted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
