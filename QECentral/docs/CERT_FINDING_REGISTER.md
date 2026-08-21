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

### Now TOOL-EMITTED (2026-08-21) — it used to be prose only

This finding was invisible on first contact with an outside reviewer: the ARB
board read *"certified with ONE open finding"* because CERT-FINDING-2 is the one
the harness prints and this one lived only in a document. The general lesson,
credited to `nexusqa-39`: **a finding a tool emits gets tracked; a finding only
a human wrote down gets lost.**

So the harness now emits it. `issue_side.py` reads `attestation_keys.py` and
reports whether the false sentence is still present; `verify_side.py` asserts it
is gone. A crypto harness asserting on documentation is unusual and deliberate —
**the defect *is* documentation**, it is load-bearing (it justifies a plaintext
signing key in process heap), and it is what a future engineer will read when
deciding whether to revisit key custody.

It carries its own probe-integrity check: if the harness cannot READ
`attestation_keys.py`, that fails too. **A probe that cannot see its target must
never report "clean"** — the same fail-closed rule the subsystem under
certification follows. The finding closes automatically when the sentence is
corrected, and cannot be closed by anyone forgetting it existed.

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

### Scope corrected 2026-08-21 — the defect is CATEGORICAL, not a few cases

Originally raised from one IPv6 grant. `nexusqa-39` measured it as five forms.
Measured again by the certifier across a spread of IPv6 shapes: **8 of 8
non-idempotent — every IPv6 form tested.**

| Vector | `N(u)` | `N(N(u))` |
| --- | --- | --- |
| `https://[2001:db8::1]:8443/` | `https://2001:db8::1:8443` | `""` |
| `https://[2001:db8::1]/` | `https://2001:db8::1` | `""` |
| `https://[::1]/` | `https://::1` | `""` |
| `https://[::1]:8443/` | `https://::1:8443` | `""` |
| `https://[fe80::1%25eth0]/` | `https://fe80::1%25eth0` | `""` |
| `https://[::ffff:192.0.2.1]/` | `https://::ffff:192.0.2.1` | `""` |
| `https://[::]/` | `https://::` | `""` |
| `https://[2001:0db8:…:0001]/` | `https://2001:0db8:…:0001` | `""` |
| **control** `https://192.0.2.1:8443/` | unchanged | **idempotent** |
| **control** `https://staging.example.test:8443/` | unchanged | **idempotent** |
| **negative control** `https://[::1]:notaport/` | `""` | `""` (correctly fail-closed) |

It is not an enumeration of cases. The function reformats `host:port` **without
ever re-bracketing**, so *any* host containing `:` breaks. Stating it as "five
forms" would license a fix that repairs the listed examples and leaves the class
open.

### A11b — the class is now fenced by the harness

The certification harness carries a **shared origin-vector table**, defined once
in `issue_side.py` and shipped in the payload so both services provably test the
same vectors. It pins two invariants against **both** copies of
`normalize_origin`:

1. **Agreement** — the two duplicated copies must not diverge. This is what makes
   *"fix both or pin identical"* enforceable rather than hoped. **All 12
   agreement checks currently PASS: the copies have not diverged.**
2. **Idempotence** — `N(N(u)) == N(u)`. The output is signed into the claims and
   re-normalised by the verifier, so an output it cannot re-parse is a proof
   guaranteed to be refused.

The controls are the *fix's* guard: a repair that re-brackets too eagerly would
break IPv4/DNS idempotence, and the malformed-port negative control ensures an
unparseable authority still fails closed to `""` — the verifier's mismatch
sentinel — rather than becoming parseable as a side effect.

**Reproduced on every run of** `Nexus_power/certification/a11/run_certification.sh`:

```
CHECKS RUN : 148
FAILURES   : 2
  FAIL: [CERT-FINDING-2 | IPv6] ipv6: genuine attestation AUTHORIZED
        (got 'origin_mismatch' "proof_origin='' target_origin='https://2001:db8::1:8443'")
  FAIL: [CERT-FINDING-2 | A11b] normalize_origin is idempotent for every IPv6
        form (8/8 non-idempotent: ...)
EXIT CODE = 1
```

The two failures are the same defect at two layers — the **symptom** (a genuine
attestation refused end to end) and the **cause** (the idempotence invariant).
Both are kept: a fix that silences one without the other has not closed this.

Confirmed identical at 131 checks / 1 failure from a clean `git-archive` tree by
`nexusqa-39` at `8be8fff`, before the A11b table was added. **Exit 1 is the
expected, correct output while this finding is open** — the harness is honest,
not broken. It must not be read as a failed run, and must not be silenced.

### CI gate shape (proposed by `nexusqa-db`, adopted here)

When `run_certification.sh` lands in CI, encode **expected failures = OPEN
register entries** — strict-xfail semantics keyed to *this file*. The gate goes
red only on a **new** finding, or on an **unexpectedly passing** one, which
forces this register to be updated in the same diff before the pipeline can
green. The pattern is already proven in platform-api's `_KNOWN_REGRESSIONS` +
XPASS-fails gate.

**Expected failure count while both findings are open: 3.**

| Emitted failure | Finding | Layer |
| --- | --- | --- |
| `[CERT-FINDING-2 \| IPv6]` | 2 | symptom — a genuine attestation refused end to end |
| `[CERT-FINDING-2 \| A11b]` | 2 | cause — the idempotence invariant, 8/8 |
| `[CERT-FINDING-1 \| KMS]` | 1 | the false rationale, still present in source |

A gate must fail if that count moves in *either* direction. **History of the
count, so a reviewer can tell a hardening from a regression:**

| Checks / failures | What changed |
| --- | --- |
| 131 / 1 | original certification — CERT-FINDING-2 found via one IPv6 grant |
| 148 / 2 | A11b: the IPv6 class fenced (symptom + cause separated) |
| 150 / 3 | CERT-FINDING-1 made tool-emitted instead of prose-only |

Each rise is the harness covering *more*, not the product getting worse. **All
three go to 0 in one commit** when A11a and CERT-FINDING-1 land together; if the
count is not 0 after that commit, the fix is incomplete rather than the harness
being wrong.

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
