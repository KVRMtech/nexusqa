# T1.3 — KMS-native attestation-key custody ceremony

The attestation issuer is a Cloud KMS **`EC_SIGN_ED25519`** CryptoKeyVersion.
QE-Central reads its public key and asks KMS to sign canonical proof bytes; it
never creates, receives, stores, or backs up the private key. The explorer
continues to verify ordinary Ed25519 signatures with the published raw 32-byte
public key.

## Bootstrap

1. A platform security administrator creates an asymmetric-signing CryptoKey
   with algorithm `EC_SIGN_ED25519` in the deployment’s KMS location.
2. Grant the QE-Central runtime service account only
   `roles/cloudkms.signerVerifier` on that key. Do not grant decrypt, admin, or
   key-export permissions for this signing key.
3. Set `NEXUS_ATTESTATION_GCP_KEY_VERSION` to the immutable active
   `.../cryptoKeyVersions/N` resource name. This must not name the symmetric
   envelope KEK (`NEXUS_KEK_GCP_KEY`); they have different purposes and IAM.
4. As a platform admin, call the existing issuer-key bootstrap endpoint. The
   resulting database row records the public key, key version name, and
   `custody=gcp_kms_native_ec_sign_ed25519`; `sealed_private_key` is empty.
5. Fetch the published public-key list and update every explorer’s
   `QEC_ATTESTATION_PUBLIC_KEYS` before issuing the first proof. Exercise a
   disposable-environment proof and verify it from an explorer process.

Record the KMS key version, platform-admin actor, deployment SHA, explorer
trust-store rollout, and proof-verification result in the custody ticket. KMS
Audit Logs are the independent record of `GetPublicKey` and `AsymmetricSign`.

## Rotation and incident response

Create a new KMS key version, update the environment variable, then use the
existing issuer rotation endpoint. It marks the old public key `retiring`; both
keys remain published until the maximum proof lifetime has elapsed. Only then
may the old key be disabled or destroyed.

For suspected compromise, disable the affected KMS version, revoke its issuer
row, refresh all explorer trust stores, and cancel active crawls with proofs
from that `kid`. Bootstrap a new version only after the trust-store rollout is
ready. A KMS/API failure is fail-closed: proof issuance stops and crawls remain
observation-only.
