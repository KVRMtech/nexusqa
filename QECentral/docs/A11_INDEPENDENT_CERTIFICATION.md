# A11 / T-WP-02 — Independent Certification Record

**Verdict: CERTIFIED WITH FINDINGS.** Neither finding is a security bypass; both
fail closed. A12 / T-WP-01 is **unblocked** by this record.

| | |
|---|---|
| Work package | A11 — Attestation Issuer (T-WP-02) |
| Certifying squad | Independent (non-author). Did not write, edit, or review-by-authorship any A11 source. |
| Author squad | Concurrent session (Security & Trust). Actively writing A11 during this certification. |
| Date | 2026-08-20 |
| Branch | `feat/qec-dynamic-catalog-p0-p6` |
| Artifact | 9 files, pinned by SHA-256 in `Nexus_power/certification/a11/A11_SNAPSHOT.sha256` |
| Reproducer | `bash Nexus_power/certification/a11/run_certification.sh` |

> ---
>
> ### ⚠️ AUTHOR'S NOTE — the digest manifest was REGENERATED after this record was written (2026-08-21)
>
> **Added by the A11 author, not by the certifying squad. It changes no finding
> and no verdict; it is recorded here because it changes the digests this record
> binds to, and hiding that would make the record untrustworthy.**
>
> When this certification was written, the nine pinned files existed only as
> untracked files in one Windows working tree, with **CRLF** line endings. The
> repository's root `.gitattributes` declares `*.py text eol=lf`, so **any** git
> checkout — a clean clone, CI, or this same tree after a re-checkout — produces
> **LF**. Six of the nine digests therefore described bytes that no clone would
> ever contain, and `run_certification.sh` would have exited 2 with
> *"REFUSING: A11 sources have drifted ... the certification record has LAPSED"*
> on every machine except the one it was written on.
>
> That is the failure this repository already reverted once, in Gate 0
> `0052ab7` — an artefact that verifies in exactly one working tree.
>
> **What was done:** the six drifting files were normalised CRLF→LF (line
> terminators only; no character of source changed), and
> `A11_SNAPSHOT.sha256` was regenerated over the *same nine paths in the same
> order* against the normalised bytes. The implementation was then committed, so
> the manifest now pins bytes that are reachable from a named SHA.
>
> **What this does and does not mean.** The certifier's checks
> (`verify_side.py`, 131 of them) test *behaviour*, not line endings, and are
> untouched — the reproducer still runs 131 checks with the single expected
> CERT-FINDING-2 failure. So the substance of the verdict is unaffected. But the
> author regenerated the certifier's own artefact, and **an author touching the
> evidence is exactly what independence exists to prevent.**
>
> **Therefore: the certifying squad should re-run `run_certification.sh` against
> the commit and re-affirm.** Until they do, treat the verdict below as sound in
> substance and the manifest as author-regenerated.
>
> ---

> ---
>
> ### ✅ CERTIFYING SQUAD — RE-AFFIRMED (2026-08-21)
>
> **Written by the certifying squad in response to the author's note above. The
> verdict stands unchanged.**
>
> The author was right to flag this rather than quietly regenerate the manifest,
> and right that an author touching the certifier's evidence is what independence
> exists to prevent. So the claim was **proven, not accepted**.
>
> **1. "Line terminators only; no character of source changed" — verified
> mechanically.** The certifier's ORIGINAL digests were recovered from commit
> `79808cb` (this record's own first commit, written before the author touched
> anything) and each current file was re-hashed after converting LF→CRLF. If the
> normalised file re-hashes to the original digest, the change was provably
> nothing but line endings:
>
> | Result | Files |
> |---|---|
> | LINE-ENDINGS-ONLY | `attestation_keys.py`, `attestation_issuer.py`, `attestation_revocation.py`, `walk_attestation.py`, `routers/attestation.py`, `gate1_walk_attestation_v1.json` |
> | BYTES UNCHANGED | `attestation_models.py`, `qec_023_attestation_issuer.py`, `attest.py` |
>
> All nine accounted for. **Not one character of source changed.** This did not
> require trusting the author, and that is the point.
>
> **2. Re-run against the committed state.** `run_certification.sh`: 9/9 digests
> OK, **131 checks, 1 failure — the same expected CERT-FINDING-2 (IPv6)**.
> Author's suites re-reproduced: **143 passed, 0 skipped** (141 at first
> certification; the author has since added 2).
>
> **3. The reproducibility limitation in §5.1 is now CLOSED.** All nine pinned
> files are tracked, and the manifest pins bytes reachable from a named commit
> rather than from one working tree.
>
> **Both findings stand unaltered.** Finding 2 (IPv6) is still reproduced by the
> harness on every run and is still unfixed.
>
> **⚠️ One process defect to record, and it is the certifier's own.** The commit
> that carried the certifier's CRLF fix (`1065083`) also contains the A11
> implementation and several unrelated files. On this checkout the git **index is
> shared between nine concurrent sessions**, so `git add <explicit paths> && git
> commit` is unsafe: another session staged its work between the two commands and
> the commit took everything staged. The correct form here is a pathspec commit —
> `git commit -- <paths>` — which ignores the index entirely. The commit message
> for `1065083` therefore understates what it contains. Nothing was lost and
> nothing was overwritten, but the attribution is wrong and is corrected here
> rather than by rewriting history other sessions have already built on.
>
> ---

The ARB rule requires certification by a squad that did not author the work. This
record satisfies that rule. It certifies **specific bytes**, not "whatever is in
the tree": nine files were pinned by digest before certification began and
re-verified unchanged at the end. The reproducer refuses to run if they drift.

---

## 1. Why this record exists and what it is worth

The author's own architecture record (`A11_ATTESTATION_ISSUER.md`) states A11 is
"NOT independently certified." That is the gap this closes.

Certification is **not** re-running the author's suite. That proves only that
their tests agree with their code. This record therefore has two halves:

1. **Reproduction** — the author's validation runs green in a second engineer's
   hands. Necessary, weak on its own.
2. **Independent verification** — checks derived from the *contract and the
   threat model*, written without reusing the author's fixtures
   (`tests/security/_a11_kit.py` is deliberately not imported), aimed
   specifically at what the author's design **structurally cannot** test.

Half 2 is where both findings came from.

---

## 2. Reproduction (half 1)

| Suite | Result |
|---|---|
| `tests/security/test_a11_api_authorization.py` | pass |
| `tests/security/test_a11_attestation_redteam.py` | pass |
| `tests/security/test_a11_dispatch_integration.py` | pass |
| `tests/contract/test_gate1_walk_attestation_contract.py` | pass |
| **Total** | **141 passed, 0 failed, 0 skipped** |

Skips were checked explicitly (`-rs`), not assumed: the three security suites
report **108 passed / 0 skipped**. A green run that hides skipped infrastructure
tests is the failure mode this repository has been burned by before; it is not
present here.

Baseline, whole-suite, same tree: qe-explorer **2005 passed**, qe-central
**2296 passed / 146 skipped**. No regression attributable to A11.

> **Environment note (not a defect).** `pytest-randomly`, present in this
> machine's global site-packages, crashes collection via `thinc`'s reseed hook
> (`ValueError: Seed must be between 0 and 2**32 - 1`). It is not a project
> dependency and CI does not install it. Run with `-p no:randomly` to match CI.

---

## 3. Independent verification (half 2)

### 3.1 What the author's design cannot test, and what was built to cover it

qe-explorer and qe-central both ship a top-level `app` package, so they cannot be
imported into one interpreter. The author's answer is a **frozen golden
envelope** (`contracts/gate1_walk_attestation_v1.json`) that each side asserts
against in its own process. That design is sound and the reasoning is correct.

Its blind spot is inherent: a fixed envelope proves the two services agree on
**one** payload. It cannot prove they agree on **arbitrary** payloads.

The certifier therefore built a two-process differential harness
(`certification/a11/`) that mints with a **freshly generated key** — not the
committed test key — across ten grant shapes the golden does not contain
(uppercase host, explicit `:443`, non-standard port, `http:80`, IPv6 literal,
punycode, non-ASCII reset procedure, empty reset procedure, budget floor and
ceiling), then verifies each in a separate interpreter, then adversarially
mutates every one.

**Result: 131 checks, 1 failure — Finding 2, below.**

Adversarial mutations confirmed DENIED for every case: `env_kind=prod`,
budget escalated inside signed claims, tenant swapped, crawl swapped, signature
forged, unknown `kid`, algorithm downgraded to `hs256`, revocation list
stripped, wrong origin at verification time, expired proof. Also confirmed:
replaying one proof onto a second crawl is denied, and an unconfigured trust
store denies with `no_trust_anchor` (fail-closed, not fail-open).

### 3.2 Claims verified by reading the code against its own prose

A docstring describing a control is not the control. Each was checked in the
implementation:

| Claim | Verified |
|---|---|
| Integrity is checked **before** the typed parse, so parser normalisation cannot sit between signed and checked bytes | ✅ `attest.py` step 4 verifies over `dict(raw_claims)`; `ProofClaims.model_validate` runs after |
| Five issuance gates, in order, all fail-closed | ✅ all five present in `issue_for_crawl`, each raising `IssuanceRefused` |
| Tenant self-attestation is impossible | ✅ `env_kind` comes from `env_provisioning_records`, never from the tenant-writable `app_environments.env_attestation` |
| A moved `base_url` cannot inherit a certification | ✅ Gate 3 re-checks the tenant-writable `base_url` against the pin and **signs the pin, not the row** |
| Revocation is fail-closed | ✅ `current_revocations` raises `RevocationUnavailable` on read failure; does not fall back to cache or to an empty list |
| Issuance forces a fresh revocation read | ✅ `use_cache=False` on the issuance path |
| Least privilege on budget | ✅ `min(record, requested)` then clamped to `HARD_MAX_MUTATIONS_PER_STEP` |
| Issuer name comes from the key row, not config | ✅ `signer.issuer`, so a config edit cannot re-attribute proofs |
| A tenant token structurally cannot carry `platform_admin` | ✅ `mint_tenant_jwt` hard-codes `platform_admin=False`; the marker is only set by `mint_platform_admin_jwt` |
| Audit row written in the same transaction as the decision | ✅ `session.add(AttestationIssuanceLogRow(...))` inside the issuing block |

**Assessment: the security core of A11 is correct, and unusually well
reasoned.** The crux — *"is this environment genuinely disposable?"* — is
answered from platform-controlled provisioning records rather than from a
tenant-supplied field, and the author identified that trap explicitly rather
than falling into it.

### 3.3 One certifier error, corrected

The harness initially flagged a signed budget of 10 being granted as 3. That is
**intended** defence-in-depth: the verifier applies `min(signed claim, fleet
ceiling)` (`attest.py:561`), so a compromised or over-generous issuer still
cannot exceed the fleet's own limit. The certifier's expectation was wrong, not
the product. Recorded here because a certification that hides its own false
starts is not auditable.

---

## 4. Findings

### FINDING 1 — The KMS rationale is factually false *(Material; documentation and design rationale; not exploitable)*

`app/services/attestation_keys.py` accepts a real residual risk — the plaintext
Ed25519 private key lives in qe-central's heap for the duration of a signature,
and Python cannot zero it — and justifies it as follows:

> *"Google Cloud KMS offers no Ed25519 asymmetric-signing key type. Its
> asymmetric sign algorithms are RSA and NIST-curve ECDSA. So KMS-native signing
> would require changing the algorithm on BOTH sides of a verifier that has
> already been through red-team review."*

**Both halves of that are wrong.**

1. Cloud KMS supports **`EC_SIGN_ED25519`** — EdDSA on Curve25519 in pure mode,
   taking raw (unhashed) input. Verified against Google's published algorithm
   reference, not from recollection.
2. Even adopting it, **the verifier does not change at all.** `SIG_ALG` is the
   wire string `"ed25519"`, and KMS `EC_SIGN_ED25519` produces exactly Ed25519
   signature bytes over exactly the raw input the verifier already canonicalises.
   Only the *issuer's* sign call changes, plus extracting the raw 32 bytes from
   the DER `SubjectPublicKeyInfo` that `GetPublicKey` returns, because
   `TrustStore.from_public_keys` accepts only raw 32-byte keys.

The envelope/KEK design may still be the right call — a KMS round-trip per
signature adds latency and couples issuance availability to KMS, and
`ASYMMETRIC_SIGN` is a different key purpose from the existing
`ENCRYPT_DECRYPT` KEK, so it needs new key provisioning and IAM. **None of those
are the reasons given.**

The harm is not today's bytes; it is that this document is the record future
engineers will consult, and it currently tells them a door is locked that is
open. The residual risk is presented as *"the price of keeping the audited
Ed25519 verifier"* — and that price is not real.

**Required remediation:** correct the rationale, and re-take the
envelope-vs-KMS-native decision on true grounds. Not a merge blocker.

### FINDING 2 — `normalize_origin` is not idempotent for IPv6 literals *(Medium; correctness/availability; fails closed)*

`normalize_origin` drops the brackets from an IPv6 host, emitting a string it
**cannot itself re-parse**:

```
'https://[2001:db8::1]:8443/apply'
   normalize once  -> 'https://2001:db8::1:8443'
   normalize twice -> ''            <-- not idempotent
'https://[::1]/apply'
   normalize once  -> 'https://::1'
   normalize twice -> ''
```

Consequence, end to end: the issuer pins and **signs** `https://2001:db8::1:8443`;
the verifier's step 9 computes `got_origin = normalize_origin(claims.target_origin)`,
gets `""`, and refuses with `origin_mismatch`. **An IPv6-literal environment can
be certified, provisioned and issued a cryptographically valid proof that is
guaranteed to be refused.** Depending on the stored form of
`record.target_origin`, Gate 3 may instead refuse issuance outright with *"pins
no usable origin"*.

This is **not a bypass** — every path fails closed — but it is a live trap for
any disposable environment on `[::1]` or an IPv6 host, and the operator-facing
failure (`origin_mismatch` on a correctly-provisioned environment) points away
from the cause.

The frozen golden uses an ASCII host, which is why the author's contract suite
could not surface this. The certifier's randomized grant set did.

**Required remediation:** re-bracket hosts containing `:` when reformatting, in
**both** copies of `normalize_origin` (`qe-explorer/app/attest.py` and
`qe-central/app/services/walk_attestation.py`) — they are duplicated by design
and must not diverge — plus an idempotency test (`N(N(u)) == N(u)`). Not a merge
blocker. **Not fixed by the certifier**, deliberately: editing the artifact under
certification would destroy the independence this record depends on.

### Non-finding, recorded for completeness

Cross-interpreter divergence in the duplicated `normalize_origin` (qe-central
runs Python 3.11 in CI, qe-explorer is pinned to 3.10 and ships a CPython 3.10
image) was investigated as a possible origin-binding bypass. **It is not one.**
Both sides of the security comparison — `want_origin` and `got_origin` — are
computed by the *explorer's* copy in the *explorer's* process; the issuer's copy
only mints the claim. A version divergence could therefore cause false denials,
never a false acceptance. Severity: availability only.

---

## 4.1 Implication for downstream work — do not extend the signed claims

Recorded here because it follows from what was certified, and because the
question reached the certifying squad from Gate 4 (session `nexusqa-9e`, re:
A30's signed vision rung).

**Adding any field to the signed claims de-certifies A11 and breaks every
existing proof.** Two independent mechanisms make this stricter than it looks:

1. `ProofClaims` is `extra="forbid"`, so an unknown field is **refused at schema
   validation** (`malformed_claims`) — not ignored, not tolerated.
2. Integrity is verified over the **raw claims, before the typed parse**
   (`attest.py` step 4). So the signature covers exactly the bytes that arrived,
   and a new field changes them.

A new claim therefore requires re-cutting the frozen contract on **both** sides
of a seam whose whole design exists because the two services cannot share an
interpreter — and it edits `attest.py`, which this record pins, so it lapses
this certification as a side effect.

**The supported pattern instead:** derive the downstream fact on the explorer
from its own verification verdict, exactly as `walk_attested` already does
(`guard_context.walk_attested` reads `verdict.authorized` and nothing else). The
verdict is already trustworthy at that point; re-signing a restatement of it buys
nothing and costs a contract migration.

---

## 5. Certification statement

Against the A11 acceptance criteria:

| Criterion | Status |
|---|---|
| Second engineering squad successfully reproduces validation | ✅ 141/141, 0 skipped, plus 131 independent checks |
| Certification formally recorded | ✅ this document |
| Trusted proof issuance operational | ✅ issuer → verifier interop proven across process boundary on 10 grant shapes with a fresh key |
| KMS-backed signing | ⚠️ KMS-backed **envelope custody**, not KMS-native signing. Operational and honestly documented as to *what* it does — see Finding 1 as to *why* |
| Provisioning-proof endpoint | ✅ `app/routers/attestation.py`, platform-admin gated |
| Revocation | ✅ issue-time and verify-time, fail-closed |
| Explorer verification | ✅ red-teamed verifier, untouched by A11, interop independently confirmed |

### 5.1 Reproducibility limitation — this record is not yet fetchable

**Raised by a second reviewer (session `nexusqa-9e`) after this record was
written, verified here, and correct.**

At the time of certification, **six of the nine pinned files existed in no commit
on any branch**:

```
git log --all --oneline -- '*attestation_issuer*'   ->  (empty)
```

| Pinned file | State |
| --- | --- |
| `attestation_keys.py`, `attestation_issuer.py`, `attestation_revocation.py` | **untracked** |
| `routers/attestation.py`, `db/attestation_models.py`, `qec_023_attestation_issuer.py` | **untracked** |
| `walk_attestation.py`, `attest.py`, `gate1_walk_attestation_v1.json` | tracked |

The digest-binding design is right — a certification that floats free of the
bytes it certifies is worthless — but digests of untracked files describe a state
that lives in **one working tree on one machine**. A clean clone has none of
them, so `run_certification.sh` refuses to run for anyone else, and the ARB
standard both squads are working to (*"a second party reproduces a result from a
named commit"*) is **not yet met**. The certification is real; it is not yet
portable.

There is also a live risk: nine sessions share this checkout, and a `git clean
-fd` would destroy the A11 implementation and this record together.

**What closes it:** the A11 author commits the implementation, and this record is
then re-run and re-issued against that SHA so it names a **commit** rather than a
digest set. Because the snapshot verified unchanged before and after
certification, re-running is cheap and the findings below are expected to stand
unaltered.

**Until that happens, treat this record as: certified, reproducible only in the
originating working tree.** Note that fixing Finding 2 edits `attest.py`, which
this snapshot pins — so that fix requires re-certification regardless, and the
two are best landed together.

**A11 is CERTIFIED WITH FINDINGS. A12 / T-WP-01 may proceed.**

Findings 1 and 2 are recorded as required remediations. Neither is a security
bypass and neither blocks merge. This certification binds to the digests in
`A11_SNAPSHOT.sha256`; **if any listed file changes, this record lapses** and
re-certification is required. The reproducer enforces that automatically.
