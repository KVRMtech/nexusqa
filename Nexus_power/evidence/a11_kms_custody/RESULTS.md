# A11.1 — issuer-key custody proven against REAL Cloud KMS

**Date:** 2026-08-22 · **Commit:** `5b5d8b7` · **Host:** `verdict-box`
(asia-southeast1-a) · **Container:** `nexus-qe-central` · **Probe:**
`Nexus_power/scripts/a11_verify_kms_custody.py`

Closes the A11 open item recorded as *"GCP KMS is not exercised. The custody
code paths are proven against `LocalKekProvider` with real AES-GCM.
`GcpKmsProvider` changes where the KEK lives, not the envelope format, the AAD
binding or the unwrap path — **but that substitution is unproven here** and must
be verified on the VM."*

It is now verified on the VM.

---

## Why A37's probe did not already cover this

`scripts/a37_verify_kms_decrypt.py` unwraps `client_apps.creds_blob`. It proves
the envelope path works for credentials **that already exist**. A11 seals a
different object under a **different AAD** (`__platform__`), and no deployment
has an issuer-key row yet — no operator has bootstrapped one. A37 passing
therefore said nothing about A11, which is why this probe exists separately.

## Deployment posture at the time of the run

```
NEXUS_KEK_PROVIDER  gcp_kms
NEXUS_KEK_GCP_KEY   projects/project-8d85a07a-396c-40aa-9b6/locations/
                    asia-southeast1/keyRings/verdict/cryptoKeys/kek
NEXUS_ENV           production
/health .kek        {'provider': 'gcp_kms', 'is_production_grade': True,
                     'envelope_ready': True}
```

The probe **refuses to run** unless `NEXUS_KEK_PROVIDER=gcp_kms`, so it cannot
report a pass while quietly exercising the local development KEK.

## Result — 8 checks, 3 negative controls, exit 0

```
sudo docker cp a11_verify_kms_custody.py nexus-qe-central:/tmp/
sudo docker exec -e PYTHONPATH=/app/service -w /app/service \
     nexus-qe-central python /tmp/a11_probe.py

  [PASS] KMS wrap succeeded — wrapped_dek=113B ciphertext=60B
  [PASS] blob names the real KMS provider — provider=gcp_kms
  [PASS] blob names the deployment's CryptoKey
  [PASS] AAD is bound into the blob
  [PASS] stored bytes do not contain the private key — blob=439B
  [PASS] EnvelopeBlob.to_bytes/from_bytes round-trips
  [PASS] KMS unwrap returned the same key — kid_pub=A6yL60w+Bjvv…
  [PASS] a KMS-unsealed key produces a verifying signature

  negative controls (each MUST fail):
  [PASS] tampered ciphertext is refused — IntegrityError
  [PASS] wrong AAD is refused         — IntegrityError
  [PASS] wrong KEK tenant is refused  — KekProviderError

EXIT=0
```

**The negative controls are the point.** A round-trip that succeeds proves the
happy path and nothing else: a provider that ignored AAD, or returned its input
unchanged, would pass every positive check above. Each control removes one
guarantee and requires the failure — so "the AAD binding is real" is measured,
not assumed.

## Safety properties of the run

* **Persisted nothing.** No DB write, no row read, no KMS key created, rotated
  or scheduled for destruction. Only `encrypt` + `decrypt` on the existing KEK —
  the same two calls the running service makes continuously.
* The Ed25519 key generated is **ephemeral**, never left the process, and is
  **not** the platform issuer key.
* No key material printed: only public-key prefixes, byte lengths, booleans.

## What this does and does not close

**Closes:** the A11.1 KMS substitution. `EnvelopeService(GcpKmsProvider)` seals
and unseals an Ed25519 issuer key under the production KEK with the AAD A11
uses, the unsealed half still matches its published public half (the consistency
check `active_signer` performs), and it still produces a verifying signature.

**Does NOT close:** there is still no issuer key *bootstrapped* on this
deployment, so walk persistence remains off in production — which is the correct
fail-closed default. Bootstrapping one is a deliberate operator action
(`POST /platform/attestation/keys`), and it creates a platform root of trust;
see `A11_KEY_CUSTODY.md` §1 for the publish-before-you-sign ordering.

**Still does NOT close:** the live `Phase.WALK` proof (T3), which needs a
certified disposable environment and a crawl that actually mutates a real
application.

---

# A11e — cross-interpreter convergence, now green in CI

Same commit. The A11e remediation landed as **advisory** jobs and ran on real
runners:

| Job | Result |
| --- | --- |
| certified bytes unchanged (linux) | success |
| certified bytes unchanged (windows) | success |
| independent reproducer (168 checks) | success |
| **A11e convergence sweep (advisory, py3.10)** | success |
| **A11e convergence sweep (advisory, py3.11)** | success |
| **A11e convergence: agree within AND across (advisory)** | success |
| A11 certification gate | success |

The two copies of `normalize_origin` now demonstrably agree **within 3.10,
within 3.11, and ACROSS both**, on 24 frozen vectors — measured by a machine on
every push, rather than written down by three people.

**The across-version half is the whole point**, and its teeth were proven before
landing by replaying the real pre-fix data: a within-only comparison **passes**
(blind to the defect), the across-version comparison **fails** and names both
copies and both answers.

**Promotion to a required check remains a separate, later decision** — the jobs
are `continue-on-error` and deliberately absent from `a11-gate`'s `needs`, and
per the agreed sequencing must not be promoted during a merge.
