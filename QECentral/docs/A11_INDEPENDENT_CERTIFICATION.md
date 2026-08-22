# A11 / T-WP-02 — Independent Certification Record

**Verdict: CERTIFIED. Zero open findings** as of the 2026-08-21 re-certification
below. The original verdict was CERTIFIED WITH FINDINGS; both findings, and the
four raised while closing them, are now closed and re-certified against a named
SHA by a non-author squad. No finding in this chain was ever a security bypass —
every one failed closed. A12 / T-WP-01 is **unblocked** by this record.

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
> ### 🔒 CLOSING VERDICT — CERTIFIED at `876b105`, zero open findings (2026-08-21)
>
> **Issued by the certifying squad after nine rounds. This is the certification
> of record; everything above it is the history that produced it.**
>
> **What "zero open findings" means here — the certifier's own words, kept
> verbatim because the author is not the right party to phrase this:**
>
> > "Zero open findings" means every finding raised has been closed, and the
> > record now describes its own coverage accurately — **not** that A11 is
> > exhaustively verified. The independent harness executes **2 of the 9 pinned
> > files** and exercises **15 of 27 verifier guards**; the eleven uncovered
> > guards and the eight read-only claims are named in §3.2 and the register.
> > Those are documented gaps, not closed ones. A reader relying on this
> > certification is relying on a **measured boundary**, which is the strongest
> > thing a certification can honestly offer.
>
> **The distinction that makes it defensible:** an undocumented gap is a defect
> in the record; a measured and named gap is a scope decision. This record now
> has the second kind. That is the difference between the state at `54e7735` and
> the state now.
>
> | | |
> |---|---|
> | Certified SHA | `876b105` — twelve files pinned by SHA-256 |
> | Reproducer | **168 checks, 0 failures, exit 0**, drift gate live |
> | Findings | **18 raised across 9 rounds, 18 closed.** 6 against the product, 12 against the machinery that asserts it |
> | Coverage | 15 of 27 verifier guards exercised; 11 named gaps; 1 (`R2`) redundant by construction |
> | Files executed | 2 of 9 pinned (`attest.py`, `walk_attestation.py`) |
> | Manifest | independently re-derived by a non-author **every round**, identical every time |
>
> **NOT claimed.** A11 is **implemented, certified against named bytes, NOT
> deployed, NOT live-proven.** Nine rounds ran against source in scratch archives
> on one Windows box under CPython 3.10.11 — no deployment, no real IPv6 host, no
> live Cloud KMS, no database. The eleven uncovered guards all work at this SHA;
> they were deleted only to measure what the harness notices. The issuer-side
> revocation reads in `attestation_revocation.py` remain unexecuted.
>
> **The three-way shape worth carrying to the NEXT certification, not filed in
> this one's history:**
>
> * **16** — checks that ran but could not fail (mutations dying before their gate)
> * **17** — controls with no checks at all (an empty fixture that looked correct)
> * **18** — claims verified by a method the heading misdescribed
>
> **None a product defect. All three invisible to a reader of the record.** What
> made them findable was one question, asked of every control: *what would have
> to be deleted for the evidence to notice?* It has now been asked of 27 guards
> and 9 files; before round 7 it had been asked of none.
>
> ---

> ---
>
> ### ✅ RE-CERTIFIED 2026-08-21 — BOTH ORIGINAL FINDINGS CLOSED, ZERO OPEN
>
> **Verdict on the nine pinned files: CERTIFIED at `54e7735`.** Issued by a
> non-author squad from a clean `git archive` of the pushed SHA. The author of
> the fixes did not certify them, and the certifier did not write, edit or
> review-by-authorship any of the code it certified.
>
> **Each round's verdict, against the SHA that actually received it** — an
> earlier draft of this block collapsed these into one and attributed a clean
> `54e7735` result to `da5b5d0`, which is CERT-FINDING-8:
>
> | SHA | Round | Verdict |
> |---|---|---|
> | `d0605ba` | 1 | CERTIFIED WITH FINDINGS — CERT-FINDING-1 and -2 closed; -3 and -4 raised |
> | `da5b5d0` | 2 | CERTIFIED WITH FINDINGS — -3 and -4 closed; -5 raised |
> | `54e7735` | 3 | **nine pinned files CERTIFIED**; -5 closed; -7, -8, -9 raised against the accounting layer, all outside the pinned nine |
>
> At `d0605ba` and `da5b5d0` the reproducer's drift gate correctly exits 2 — the
> record is lapsed until re-pinned — and the 151/0 figures for those SHAs came
> from running the two halves directly. **`run_certification.sh` passes end to
> end only from `54e7735` onward.**
>
> | | |
> |---|---|
> | Implementation | `d0605ba`, `da5b5d0`, and the accounting commits that follow |
> | Certified SHA | `54e7735` for the nine pinned files; the accounting layer re-checked separately |
> | Reproducer | **exit 0, 9/9 digests, drift gate live** — first full green in the chain, at `54e7735` |
> | Findings | nine raised across the chain. See `CERT_FINDING_REGISTER.md` for the live count — **this document is not the register and must not be read as its status field** |
> | Corroboration | `nexusqa-db` and `nexusqa-39` each reported an independent measurement. **Recorded by the implementation squad, NOT verified by the certifier** — the certifier has no evidence about what those sessions did and did not endorse this line |
>
> **The check count was 151 at `54e7735`, not the 150 this record and the register
> both predicted in print, and that was the fix working.** (It is **152** at HEAD:
> CERT-FINDING-9 added a third KMS assertion. The register's history table carries
> every rise with its reason — read that, not this paragraph, for the live count.) `verify_side.py` gates its budget
> assertion on `if v.authorized`, so while the IPv6 grant was refused that check
> never ran. It runs now. The harness files are byte-identical across
> `d0605ba^`, `d0605ba` and `da5b5d0`, so the count could not have been gamed;
> the certifier proved the delta by set-differencing the check labels — exactly
> one added, none removed. See the register for the full accounting.
>
> **What certification caught that the author's own tests could not.** The
> author's suites were green at every point in this chain. Three of the six
> findings were raised against the *fixes themselves*, by a party reading the code
> behind the prose:
>
> * CERT-FINDING-3 — the replacement KMS rationale was false the same way the
>   original was, because it claimed a latency and availability advantage this
>   system does not have.
> * CERT-FINDING-4 — a residual non-idempotent class (`https://[::1@evil]/x`) that
>   the IPv6 class fence did not reach.
> * CERT-FINDING-5 — the corrected rationale contradicted its own bullets, with an
>   inverted sign on the one surviving figure.
>
> **It took three passes to state one design decision truthfully.** That is the
> argument for independent certification stated as cheaply as it can be stated.
>
> **What this re-certification does NOT claim.** Everything ran against source in
> scratch archives on one Windows box under CPython 3.10.11 — no deployment, no
> real IPv6 host, no live Cloud KMS, no database. The IPv6 interop is proven
> across two *processes*, not two deployed services. The vector space is not
> exhausted: 134 hand-chosen vectors, not a fuzzer over the URL grammar —
> CERT-FINDING-4 exists precisely because round 1's 98 were not enough. What
> bounds the residual risk is not enumeration but the guard's monotonicity
> (`N_new(u) ∈ {"", N_old(u)}`), which is proven by construction. Environments
> provisioned before `d0605ba` on an IPv6 or malformed host are **not
> auto-repaired** and must be re-certified; that consequence was established by
> code-read, not executed against a provisioning table. The 2026-08-20 record's
> claim-by-claim review of the security core was not repeated.
>
> ---

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

> ---
>
> ### 🔗 RE-ATTESTATION AGAINST COMMITTED BLOBS (2026-08-21)
>
> Requested by the ARB record-keeper (`nexusqa-db`), and a fair request: every
> prior verification in this document compared **working-tree** files. A record
> that binds to a working tree is the thing §5.1 was written about.
>
> **This attestation compares the COMMITTED BLOBS.** Each path in
> `A11_SNAPSHOT.sha256` was read with `git cat-file blob <sha>:<path>` — never
> from disk — and hashed:
>
> ```
> committed blobs matching : 9/9
> certified bytes landed at : 1065083e017ffdcc8fbef972dc477ad65154e510
> re-verified unchanged at  : db509f644703d3bb9fd80ecdd86b04e4475283fb (HEAD)
> ```
>
> **The certification is pinned to `1065083`** — the commit in which the
> certified bytes landed and the last commit to touch any of the nine paths.
> `HEAD` has since moved for unrelated work; the nine blobs are byte-identical
> there, which is why the record still holds. It lapses when any of those nine
> paths changes, not when `HEAD` moves.
>
> ### ⚠️ RELAY CORRECTION — this certification never reported a pass
>
> A relayed account of the certifier's run stated that it passed. **It did not,
> and this document never said it did.** Re-run at the commit above, exit code
> captured verbatim:
>
> ```
> CHECKS RUN : 131
> FAILURES   : 1
>   FAIL: [CERT-FINDING-2 | IPv6] ipv6: genuine attestation AUTHORIZED
>         (got 'origin_mismatch' "proof_origin='' target_origin='https://2001:db8::1:8443'")
> EXIT CODE = 1
> ```
>
> That is **identical** to the independent clean-`git-archive` run by
> `nexusqa-39` at `8be8fff`: 131 checks, 1 failure, exit 1. **There is no
> environmental discrepancy between the two trees, and exit 1 was never read as
> success.** The verdict line of this document has read *"CERTIFIED WITH
> FINDINGS"* since it was written, §3.1 has read *"131 checks, 1 failure"*, and
> Finding 2 has carried a required remediation throughout.
>
> The ARB disposition *"certified with one open finding"* is therefore **correct
> as it stands** and this document needs no correction on that point. The defect
> was in the relay, not in either run.
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

### 3.2 Claims checked by READING the code — a code review, not independent execution

**RE-LABELLED 2026-08-21 (CERT-FINDING-18). This section was under the heading
*Independent verification*, and its rows read as independently verified. They
were READ, not RUN.** The distinction is the one this very record insists on
elsewhere — *"certification is not re-running the author's suite"* — and it
applies to reading just as much: a claim checked by reading is a code review,
which is valuable and is not execution.

**Measured, not estimated.** The independent harness was run under `runpy` and
`sys.modules` inspected. Of the nine files this record pins by SHA-256, the
harness executes **two**:

| Pinned file | Executed by the independent harness? |
|---|---|
| `engines/qe-explorer/app/attest.py` | **yes** — the verifier half |
| `qe-central/app/services/walk_attestation.py` | **yes** — the issuer half |
| `qe-central/app/services/attestation_keys.py` | no — opened as *text* by the CERT-FINDING-1 probe, never imported |
| `qe-central/app/services/attestation_issuer.py` | no — never imported |
| `qe-central/app/services/attestation_revocation.py` | no — never imported |
| `qe-central/app/routers/attestation.py` | no — never imported |
| `qe-central/app/db/attestation_models.py` | no — never imported |
| `alembic_qec/versions/qec_023_attestation_issuer.py` | no — never imported |
| `contracts/gate1_walk_attestation_v1.json` | no — its only reference in the harness directory is the digest manifest itself |

**Eight of the ten claims below live in modules the independent harness never
runs.** Their only executable coverage is the author's own suites. That does not
make them wrong — nothing here suggests they are — but it means this section is
a **second reader's code review**, and should be relied on as one.

What the independent half *does* cover, and what §3.1 correctly claims, is the
cross-process interop seam that the author's design structurally cannot test:
two services, two interpreters, one contract. That claim stands unchanged.

**Not in this work package:** extending the harness across the issuance path
(`attestation_issuer.py`, the router, the models) needs a database and is a
different piece of work. The certifier recommended against holding certification
for it, and recommended recording the gap instead. This table is that record.

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
