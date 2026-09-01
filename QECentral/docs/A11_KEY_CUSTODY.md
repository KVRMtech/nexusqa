# A11 — Issuer Key Custody Runbook

Operational procedures for the platform's attestation signing key. The
*architecture* — why the key is envelope-sealed, what that does and does not
guarantee — is in `A11_ATTESTATION_ISSUER.md` §2. This document is what you
actually run, and in what order.

**What this key is.** One Ed25519 private key. Whoever holds it can mint a
provisioning proof, and a provisioning proof is the only thing that turns on
server-side mutation of a customer's application. There is no second factor
behind it and no human in the loop at mutation time. Treat every procedure here
as a change to the platform's root of trust.

---

## The one rule

> **PUBLISH BEFORE YOU SIGN.**

Explorers learn public keys from configuration (`QEC_ATTESTATION_PUBLIC_KEYS`).
A key that signs before the fleet has been told about it produces
`unknown_key_id` on every dispatch — which presents as a fleet-wide walk
persistence outage that looks like a network fault.

Every key endpoint returns the exact trust-store values to deploy, so nobody
transcribes key material by hand.

---

## 0. Prerequisites

```
NEXUS_KEK_PROVIDER=gcp_kms
NEXUS_KEK_GCP_KEY=projects/P/locations/L/keyRings/R/cryptoKeys/K
```

The service account needs `roles/cloudkms.cryptoKeyEncrypterDecrypter` on that
key and nothing more. On a GCP VM this is Application Default Credentials via
the metadata server — **no static key material on disk**.

Verify before proceeding:

```bash
curl -s https://<qe-central>/health | jq '.kek'
# provider must be "gcp_kms" and degraded must be false.
# "local" in a deployed environment is a ship-stopper (M0.5 boot gate).
```

If `/health` reports `degraded`, **stop**. Bootstrapping a key under a
development KEK produces a root of trust anybody holding this repository can
unseal.

---

## 1. Bootstrap (first key, once per deployment)

`issuer` **must equal** the explorer fleet's `QEC_ATTESTATION_ISSUER`. It is
supplied explicitly rather than read from config so that bootstrapping is a
deliberate statement of which fleet the key is for.

```bash
curl -sX POST https://<qe-central>/api/v1/qec/platform/attestation/keys \
  -H "Authorization: Bearer $PLATFORM_ADMIN_JWT" \
  -H 'Content-Type: application/json' \
  -d '{"issuer": "qe-central-platform"}'
```

```json
{
  "kid": "…", "public_key": "…", "issuer": "qe-central-platform",
  "alg": "ed25519", "retired_kid": "",
  "trust_store": {
    "QEC_ATTESTATION_PUBLIC_KEYS": "<base64>",
    "QEC_ATTESTATION_ISSUER": "qe-central-platform"
  }
}
```

Then, **before certifying anything**, push both `trust_store` values to every
explorer worker and restart them.

Confirm the fleet is armed:

```bash
# On a worker: the trust store must be `configured` (keys AND issuer present).
# An empty store denies everything with `no_trust_anchor` — fail-closed, and
# indistinguishable from "walk persistence is off" unless you look.
```

Expect one WARNING in the qe-central log:

```
qec.attest.issuer_key_generated kid=… issuer=… kek_provider=gcp_kms kek_id=… by=…
  — a NEW PLATFORM ROOT OF TRUST now exists; publish its public key to every
    explorer BEFORE it signs anything
```

---

## 2. Routine rotation (Ed25519 issuer key)

**Cadence: every 90 days.** The window in which a heap-disclosed key is useful
is bounded by this number.

```bash
curl -sX POST https://<qe-central>/api/v1/qec/platform/attestation/keys \
  -H "Authorization: Bearer $PLATFORM_ADMIN_JWT" \
  -H 'Content-Type: application/json' \
  -d '{"issuer": "qe-central-platform", "rotate": true}'
```

One transaction: the incumbent becomes `retiring`, a successor becomes `active`.
The database permits at most one `active` key (partial unique index), so the two
cannot race.

**The retired key is NOT revoked.** It stays *published*, so proofs already in
flight keep verifying until they expire. Revoking on rotation would invalidate
every in-flight crawl at once, turning routine hygiene into an outage.

Order of operations:

| # | Action | Why |
| --- | --- | --- |
| 1 | `rotate: true` | mints the successor |
| 2 | push the returned `QEC_ATTESTATION_PUBLIC_KEYS` (**contains both keys**) to every worker; restart | the new key is already signing |
| 3 | wait > proof lifetime (10 min default; ceiling 24 h) | lets the old key's proofs die naturally |
| 4 | *optionally* revoke the retired `kid` | tidies the trust store |

Step 2 is time-sensitive: between step 1 and step 2 the new key signs proofs the
fleet has not been told to trust. Keep the gap short, and expect
`unknown_key_id` refusals inside it. They are fail-closed (crawls catalogue
without persisting), not dangerous.

---

## 3. KEK rotation (re-wrap) — cheap, and not the same thing

When the Cloud KMS key version rotates:

```bash
curl -sX POST https://<qe-central>/api/v1/qec/platform/attestation/keys/rewrap \
  -H "Authorization: Bearer $PLATFORM_ADMIN_JWT"
```

Re-wraps each sealed DEK under the current KEK version. **The signing key does
not change**: no proof is invalidated, no public key moves, no explorer needs
reconfiguring, no downtime. Revoked keys are re-wrapped too — they are retained
for audit, and a blob nobody can decrypt is not evidence.

If one row fails, the others still rotate and the failure names the `kid`:

```
qec.attest.issuer_key_rewrap_failed kid=… error=…
```

---

## 4. Compromise response

Suspect the private key is disclosed — heap dump, core file, an RCE in
qe-central, an unexplained `qec.attest.proof_issued` line.

```bash
# 1. STOP THE BLEEDING — the key stops being published immediately.
curl -sX POST https://<qe-central>/api/v1/qec/platform/attestation/keys/$KID/revoke \
  -H "Authorization: Bearer $PLATFORM_ADMIN_JWT"

# 2. URGENT — refresh QEC_ATTESTATION_PUBLIC_KEYS on EVERY worker and restart.
#    Until you do, workers already running still trust the revoked key.

# 3. Revoke every environment that key could have minted proofs for.
#    Proofs already ADMITTED to a running crawl are not stopped by revocation
#    (see A11_ATTESTATION_ISSUER.md §4.3) — cancel those crawls.

# 4. Bootstrap a fresh key (§1) and re-publish.
```

**Blast radius is intentional.** Revoking a key makes every proof it ever signed
`unknown_key_id`, including the ones that look fine — because a compromised key
means all of them are suspect.

Revoking the **active** key leaves the platform with no signing authority. That
is the correct fail-closed state: walk persistence is simply off until a new key
is bootstrapped, and nothing else is affected.

Expect:

```
qec.attest.issuer_key_REVOKED kid=… by=… — every proof signed by this key must
  be treated as compromised; refresh every explorer's
  QEC_ATTESTATION_PUBLIC_KEYS NOW
```

---

## 5. Key states

| state | signs? | published? | how it gets there |
| --- | --- | --- | --- |
| `active` | yes | yes | bootstrap or rotate (exactly one, DB-enforced) |
| `retiring` | no | **yes** | superseded by a rotation |
| `revoked` | no | no | explicit compromise response |

`GET /api/v1/qec/platform/attestation/keys` lists active + retiring and returns
the current trust-store values. It carries no secret, so a deployment pipeline
may call it directly.

---

## 6. Disaster recovery

| Loss | Consequence | Recovery |
| --- | --- | --- |
| `attestation_issuer_keys` rows lost | no signing authority | bootstrap (§1). **Walk persistence off until then; nothing else breaks.** |
| KMS KEK destroyed | sealed keys unrecoverable | same — bootstrap a new key |
| KMS unreachable | issuance returns 503 | fix KMS; crawls run read-only meanwhile |
| Fleet trust store lost | `no_trust_anchor` everywhere | re-push from `GET .../keys` |

**The issuer key holds no irreplaceable state.** Losing it costs a re-bootstrap
and a config push — not evidence. This is deliberate. Evidence integrity is
owned by the hash-chain re-derivation described in `app/services/signing.py`,
not by this key.

Do **not** back up the sealed private key outside the normal database backup.
A copy of the blob plus KEK access is a copy of the root of trust, and a backup
you do not need is a backup somebody can steal.

---

## 7. What to watch

| Signal | Level | Meaning |
| --- | --- | --- |
| `qec.attest.issuer_key_generated` | WARNING | a new root of trust exists — **should be rare and expected** |
| `qec.attest.issuer_key_rotated` | WARNING | routine, if it matches your cadence |
| `qec.attest.issuer_key_REVOKED` | ERROR | compromise response — should never be a surprise |
| `qec.attest.proof_issued` | WARNING | a crawl gained mutation authority |
| `qec.attest.issue_refused` | WARNING | someone asked and was refused; the `reason` is stable |
| `qec.attest.revoked` | ERROR | an environment or proof was withdrawn |
| `qec.attest.issue_rate_limited` | WARNING | a principal is hammering a KMS-backed signing path |
| `qec.explorer.walk_persistence_granted` | WARNING | the far side agreed — pairs 1:1 with `proof_issued` |

**The pairing is the alert worth building.** A `walk_persistence_granted` with
no matching `proof_issued` row in `attestation_issuance_log` means a proof was
verified that this platform has no record of minting. That is the signal that
the key is out.

No log line in this subsystem carries key material, a signature, or a request
body.

---

## 8. Alternative: keys embedded in trusted explorer builds

`QEC_ATTESTATION_PUBLIC_KEYS` may be baked into the explorer image instead of
supplied by configuration. That is strictly stronger against a compromised
qe-central — an attacker who owns the platform still cannot add a trust anchor
to the fleet — and it costs a rebuild and redeploy per rotation.

Recommended for deployments where the explorer fleet and the platform have
different operators. Not the default, because it converts a 90-day config push
into a 90-day release.
