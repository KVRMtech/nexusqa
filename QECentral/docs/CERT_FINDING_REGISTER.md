# CERT-FINDING Register

The authoritative list of findings raised by independent certification, and the
disposition of each. Created 2026-08-21 at the ARB record-keeper's request:
`CERT-FINDING-2` was being raised by `verify_side.py` on every run and recorded
in no register, so a grep for its id returned only the line that emits it.

**Rule:** a finding may not be closed by the squad that authored the code it is
against. A finding raised by a certification is closed by re-certification, not
by assertion.

| ID | Severity | Status | Against | Raised by | Tracked as |
| --- | --- | --- | --- | --- | --- |
| CERT-FINDING-1 | Material (documentation / design rationale) | **OPEN** | `qe-central/app/services/attestation_keys.py` | A11 independent certification, 2026-08-20 | — |
| CERT-FINDING-2 | Medium (correctness / availability; fails closed) | **OPEN** | `qe-explorer/app/attest.py` + `qe-central/app/services/walk_attestation.py` | A11 independent certification, 2026-08-20 | **A11a** |

Full statements: `A11_INDEPENDENT_CERTIFICATION.md` §4.

---

## CERT-FINDING-1 — the KMS rationale is factually false

**Status: OPEN.** Not a merge blocker. Not exploitable.

`attestation_keys.py` accepts a real residual risk — the plaintext Ed25519
private key lives in qe-central's heap for the duration of a signature, and
Python cannot zero it — and justifies it by claiming Google Cloud KMS offers no
Ed25519 asymmetric-signing key type.

**Cloud KMS supports `EC_SIGN_ED25519`** (EdDSA on Curve25519, pure mode, raw
input). The follow-on claim that KMS-native signing would require changing the
algorithm on *both* sides is also false: `SIG_ALG` is the wire string
`"ed25519"` and KMS emits exactly those bytes, so the red-teamed verifier does
not change at all. Only the issuer's sign call changes, plus extracting the raw
32 bytes from the DER `SubjectPublicKeyInfo` that `GetPublicKey` returns.

**Disposition:** the envelope/KEK design may well remain correct — a KMS
round-trip per signature adds latency, couples issuance availability to KMS, and
`ASYMMETRIC_SIGN` is a different key purpose from the existing `ENCRYPT_DECRYPT`
KEK, needing new provisioning and IAM. **None of those are the reasons written
down.** Required remediation is to correct the rationale and re-take the
decision on true grounds. No code change is implied by the finding itself.

**Closes when:** the rationale in `attestation_keys.py` is corrected. Does not
require re-certification (it does not change the certified bytes' behaviour),
but does change `attestation_keys.py`, which the snapshot pins — so in practice
it lapses the record and should be landed with CERT-FINDING-2.

---

## CERT-FINDING-2 — `normalize_origin` is not idempotent for IPv6 literals

**Status: OPEN.** Tracked as **A11a**. Not a merge blocker. **Not a bypass —
fails closed.** Independently reproduced by `nexusqa-db`.

`normalize_origin` strips the brackets from an IPv6 host and emits a string it
cannot re-parse:

```
'https://[2001:db8::1]:8443/apply'  ->  'https://2001:db8::1:8443'  ->  ''
'https://[::1]/apply'               ->  'https://::1'               ->  ''
```

**Effect end to end:** the issuer pins and *signs* `https://2001:db8::1:8443`;
the verifier recomputes `normalize_origin(claims.target_origin)`, gets `""`, and
refuses with `origin_mismatch`. An IPv6-literal environment can be certified,
provisioned and issued a cryptographically valid proof that is **guaranteed to
be refused**. Depending on the stored form of `record.target_origin`, issuance
Gate 3 may instead refuse outright with *"pins no usable origin"*.

Every path fails closed, so there is no bypass. The harm is a live trap for any
disposable environment on `[::1]` or an IPv6 host, with an operator-facing error
(`origin_mismatch` on a correctly-provisioned environment) that points away from
its cause.

**Why the author's suite could not surface it:** the frozen contract golden uses
an ASCII host. A fixed envelope proves the two services agree on *one* payload;
the certifier's randomized grant set covers ten, including an IPv6 literal.

**Reproduced on every run of** `Nexus_power/certification/a11/run_certification.sh`:

```
CHECKS RUN : 131
FAILURES   : 1
  FAIL: [CERT-FINDING-2 | IPv6] ipv6: genuine attestation AUTHORIZED
        (got 'origin_mismatch' "proof_origin='' target_origin='https://2001:db8::1:8443'")
EXIT CODE = 1
```

Confirmed identical from a clean `git-archive` tree by `nexusqa-39` at
`8be8fff`. **Exit 1 is the expected, correct output while this finding is open**
— the harness is honest, not broken. It must not be read as a failed run, and
must not be silenced.

**Required remediation:** re-bracket hosts containing `:` when reformatting, in
**both** copies — `qe-explorer/app/attest.py:187` and
`qe-central/app/services/walk_attestation.py:119` — plus an idempotency test
(`N(N(u)) == N(u)`). The two copies are duplicated by design and must not
diverge: **fix both, or pin them identical.**

**Not fixed by the certifier, deliberately:** editing the artefact under
certification destroys the independence the record depends on.

**Closes when:** both copies are fixed together, an idempotency test exists, and
the certification is **re-run and re-issued** against the new SHA — because the
fix edits `attest.py`, which `A11_SNAPSHOT.sha256` pins, and therefore lapses
the record by construction. Landing CERT-FINDING-1 in the same commit costs one
re-certification instead of two.
