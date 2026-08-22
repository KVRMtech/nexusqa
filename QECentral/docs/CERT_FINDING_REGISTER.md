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
| CERT-FINDING-1 | Material (documentation / design rationale) | **CLOSED** | `qe-central/app/services/attestation_keys.py` | A11 independent certification, 2026-08-20 | — |
| CERT-FINDING-2 | Medium (correctness / availability; fails closed) | **CLOSED** | `qe-explorer/app/attest.py` + `qe-central/app/services/walk_attestation.py` | A11 independent certification, 2026-08-20 | **A11a** |
| CERT-FINDING-3 | Low (documentation / design rationale) | **CLOSED** | `qe-central/app/services/attestation_keys.py` | A11 re-certification round 1, 2026-08-21 | — |
| CERT-FINDING-4 | Informational (correctness; fails closed; pre-existing) | **CLOSED** | `qe-explorer/app/attest.py` + `qe-central/app/services/walk_attestation.py` | A11 re-certification round 1, 2026-08-21 | — |
| CERT-FINDING-5 | Low (documentation / design rationale) | **CLOSED** | `qe-central/app/services/attestation_keys.py` | A11 re-certification round 2, 2026-08-21 | — |
| CERT-FINDING-6 | Medium (process / CI accounting) | **CLOSED** | `.github/workflows/a11-attestation-certification.yml` | Implementation squad 2026-08-21; confirmed by `nexusqa-39` + `nexusqa-db` | **A11d** |
| CERT-FINDING-7 | Medium (process / CI accounting; **fail-open**) | **CLOSED** | `.github/workflows/a11-attestation-certification.yml` | A11 re-certification round 3, 2026-08-21 | — |
| CERT-FINDING-8 | Low–Medium (record accuracy) | **CLOSED** | `QECentral/docs/A11_*.md`, `GATE1_EXIT_STATUS.md` | A11 re-certification round 3, 2026-08-21 | — |
| CERT-FINDING-9 | Medium (certification integrity; **fail-open**) | **CLOSED** | `Nexus_power/certification/a11/issue_side.py` | A11 re-certification round 3, 2026-08-21 | — |
| CERT-FINDING-10 | Medium–High (certification integrity; **fail-open**) | **CLOSED** | `Nexus_power/certification/a11/` — the harness itself | A11 re-certification round 4, 2026-08-21 | — |
| CERT-FINDING-11 | Low (documentation / rationale) | **CLOSED** | `qe-central/app/services/attestation_keys.py` | A11 re-certification round 4, 2026-08-21 | — |
| CERT-FINDING-12 | Low (record accuracy) | **CLOSED** | `CERT_FINDING_REGISTER.md` + `run_certification.sh` | A11 re-certification round 5, 2026-08-21 | — |
| CERT-FINDING-13 | Low (documentation / rationale) | **CLOSED** | `qe-central/app/services/attestation_keys.py` | A11 re-certification round 5, 2026-08-21 | — |
| CERT-FINDING-14 | Low (CI accounting; **fail-closed**) | **CLOSED** | `.github/workflows/a11-attestation-certification.yml` | The gate itself, 2026-08-21 | — |
| CERT-FINDING-15 | Low (documentation / record accuracy) | **CLOSED** | `qe-central/app/services/attestation_keys.py` | A11 re-certification round 6, 2026-08-21 | — |
| CERT-FINDING-16 | Medium–High (certification integrity; **blind verifier**) | **CLOSED** | `Nexus_power/certification/a11/` — the harness's own coverage | A11 re-certification round 7, 2026-08-21 | — |

**ZERO OPEN.** The A11 certification is intact with no open findings, re-issued
against a named SHA by a non-author squad — see `A11_INDEPENDENT_CERTIFICATION.md`.
The certifier numbered findings 3–5 `NEW-CERT-FINDING-n`; they are renumbered here
without the prefix so the table has one id vocabulary.

> **This table is load-bearing for CI.** `a11-attestation-certification.yml`
> parses these rows and fails unless its `EXPECTED_FAILURES` literal equals the
> number of **OPEN** ones. Opening a finding without raising that count, or
> dropping the count without closing a row, fails the build — so the number and
> the reason for it can only move in the same diff. That is CERT-FINDING-6's
> remediation, and it is why the Status column is a fixed vocabulary
> (`OPEN` / `CLOSED`) rather than prose. The parser also fails closed when this
> file is unreadable or the table shape changes: it must never mistake *"I cannot
> see the register"* for *"nothing is open"*.

Full statements: `A11_INDEPENDENT_CERTIFICATION.md` §4.

---

## CERT-FINDING-1 — the KMS rationale is factually false

**Status: CLOSED at `d0605ba`**, re-certified by a non-author squad. Never a
merge blocker, never exploitable. Correcting it took three attempts: the
replacement rationale was itself found false (CERT-FINDING-3) and then found to
contradict its own bullets (CERT-FINDING-5). Both are closed; see below.

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

#### Attacked, and it held

`nexusqa-39` did not read this design — they attacked it, in an isolated copy of
the tree, by hiding the probe's target:

```
mv attestation_keys.py attestation_keys.py.hidden
issue_side.py  -> exit 0   (kms_probe_read=False, kms_claim_present=False)
verify_side.py -> CHECKS RUN 150 | FAILURES 3
   FAIL: CERT-FINDING-1 probe could READ attestation_keys.py
```

The finding did **not** convert to passing. The third failure **changed
identity** — from *"the claim is still present"* to *"the probe could not see its
target"* — a one-for-one swap: count stays 3, reason moves.

#### The rule for the next prose-to-tool conversion

**Use TWO separate assertions, never one truthy test.**

```python
check(data.get("kms_probe_read") is True,   ...)   # I could see the target
check(data.get("kms_claim_present") is False, ...) # and the defect is gone
```

Collapsing these into a single boolean **passes on a missing file**, because
*"the sentence is absent"* and *"the file is absent"* are indistinguishable from
one truthy value. That is the failure mode a prose-to-tool conversion is most
likely to introduce, and it would silently convert an open finding into a closed
one — the precise harm the conversion exists to prevent. The next person
converting a finding will reach for the single check; this is why not to.

---

## CERT-FINDING-2 — `normalize_origin` is not idempotent for IPv6 literals

**Status: CLOSED at `d0605ba`**, re-certified at `da5b5d0`. Tracked as **A11a**.
Never a bypass — it failed closed throughout. Independently reproduced by
`nexusqa-db`, who also independently confirmed the fix: 12/12 vectors clean, and
both stale broken outputs still returning the mismatch sentinel.

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

**Expected failure count while both findings were open: 3. It is now 0**, and
the workflow no longer carries that number as a hand-maintained literal alone -
see CERT-FINDING-6.

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
| **151 / 0** | **both findings fixed at `d0605ba`** — read the note below before concluding anything |
| **152 / 0** | CERT-FINDING-9: a third KMS assertion added — the correction must be PRESENT, not merely the false claim absent |
| **161 / 0** | CERT-FINDING-16: nine checks that reach gates 7, 9, 11 and 12 for the first time |

The **pinned-file count** also moved, and for a different reason: **9 → 12**. CERT-FINDING-10 added the three harness files to `A11_SNAPSHOT.sha256`. The nine original digests did not move (bar `attestation_keys.py`, which CERT-FINDING-11 edits); three rows were appended.

**READ THE 150 → 151 RISE BEFORE CONCLUDING ANYTHING.** This register predicted
150 in print, and *"expected 150, got 151"* is exactly the shape that reads as a
regression to someone checking a literal. It is not. `verify_side.py` runs its
budget assertion only `if v.authorized`, so while the IPv6 grant was refused,
that one check never ran at all. With the grant authorising, the IPv6 case now
executes the same **13** checks every other grant executes.

Nothing was added to the harness: `issue_side.py` and `verify_side.py` are
**byte-identical** at `d0605ba^`, `d0605ba` and `da5b5d0`, so the count cannot
have been gamed. Three parties measured 151/0 independently — the certifier of
record, `nexusqa-39`, and the implementation squad — and the certifier proved
the delta by set-differencing the check LABELS: exactly one added
(`ipv6: budget 1 == min(requested=1, fleet=3)`), none removed. **A dead check
coming back to life is coverage restored, not a moved goalpost.**

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

**CLOSED at `d0605ba`**, exactly as specified: `if ":" in host: host = f"[{host}]"`
before the reassembly, **two identical executable lines in each copy**, with an
idempotency test in both suites, and CERT-FINDING-1 landed in the same commit so
it cost one re-certification rather than two.

The certifier proved the copies had not diverged **mechanically**: the AST of
`normalize_origin` with docstrings stripped is identical between the two files,
with a control confirming the comparator can see a real difference. The
idempotency tests were run against the **pre-fix** function to prove they can go
red — 10 of 18 and 10 of 14 vectors fail there, while every control and negative
control stays green.

The fix targets the **reassembly**, not the eight reported forms. That was the
point of the scope correction above, and it is what made the residual class
(CERT-FINDING-4) findable rather than invisible.
---

## CERT-FINDING-3 — the *replacement* rationale was false the same way

**Status: CLOSED at `da5b5d0`.** Low. Documentation only. Raised by the certifier
of record against the fix for CERT-FINDING-1, in round 1 of re-certification.

The correction to CERT-FINDING-1 replaced one false sentence with three grounds
for keeping the envelope pattern. The certifier checked them against the code
instead of reading them, and **two did not survive**:

| Ground | Verdict |
| --- | --- |
| LATENCY — *"a KMS round-trip on every signature, versus one unseal per signer"* | **Misleading.** True only of a signer amortised across issuances. `active_signer` is opened inside `issue_for_crawl` and closed when the block exits — one call site, no reuse — so the envelope already pays **one KMS `decrypt` per issuance** and then signs two claims objects locally. KMS-native would be **two `asymmetricSign`** calls. The true figure is **exactly 2.0×**, not an order of magnitude. |
| AVAILABILITY COUPLING — *"a KMS outage does not block a signer that is already live"* | **FALSE, withdrawn.** No signer is ever already live. `_unseal` raises `KeyCustodyError` and `active_signer` fails closed when `envelope is None`, so **issuance availability is already fully coupled to KMS**. Moving to `asymmetricSign` changes *which* KMS method issuance depends on, not *whether*. |
| PROVISIONING AND IAM | **Stands.** `ASYMMETRIC_SIGN` and `ENCRYPT_DECRYPT` are distinct Cloud KMS key purposes and a key's purpose is fixed at creation, so a new key, new IAM bindings and new rotation handling are genuinely required. |

**The lesson is sharper than the finding.** CERT-FINDING-1 was a false
*impossibility*. Its replacement was a *flattering* justification — three grounds
where the honest answer was one and a half. Both make a decision look more
settled than it is, and the second is harder to catch precisely because nothing
in it is flatly untrue. **Correcting a false rationale is not done when the false
sentence is gone; it is done when the replacement has been checked against the
code with the same hostility the original earned.**

The file now also records the uncomfortable part, because it is the same fact
twice: the signer's one-issuance scope is a deliberate *security* property — it
bounds the plaintext-key window to one request — and it is exactly that property
which destroys the latency and availability arguments for the envelope. The
custody design and the performance case for it pull in opposite directions.

---

## CERT-FINDING-4 — a residual non-idempotent class the IPv6 fix did not reach

**Status: CLOSED at `da5b5d0`.** Informational. Fails closed. **Pre-existing —
not a regression:** byte-identical behaviour before and after `d0605ba`.

```
N('https://[::1@evil]/x')  ->  'https://evil]'  ->  ''
```

`urlsplit` splits the authority at the **last** `@`, reading `[::1` as userinfo
and `evil]` as the host. That host contains no `:`, so the CERT-FINDING-2
re-bracketing never fires, and the emitted origin carries an unmatched `]` it
cannot re-parse. Of 19 non-idempotent cases in the certifier's injection round,
`d0605ba` repaired **18**; this was the survivor.

**Remediation:** in both copies, refuse when a bracket *survived* the parse —

```python
if "[" in host or "]" in host:
    return ""
```

A bracket that survived means the authority was malformed. A bracket the parse
*consumed* is an ordinary host and is untouched, which is what the two green
control vectors (`https://[example.test]/x`, `https://user:pass@[::1]:8443/x`)
exist to prove.

**Why this one matters out of proportion to its severity.** CERT-FINDING-2's own
scope correction insisted the defect was a class, not an enumeration — and the
fix honoured that. This finding shows the *class fence itself* was drawn one step
too narrow: "every host containing a colon" was true, and was still not the whole
of "every host the reassembly mishandles". **A class fence is bounded by whoever
writes the vectors.** What bounds the remaining risk is not enumeration but
monotonicity: the guard is a pure early return, so for every input
`N_new(u) ∈ {"", N_old(u)}` — it can only move things *into* the fail-closed
sentinel, never out of it. The certifier verified 0 violations across 97 vectors,
0 vectors moving out of `""`, and an equivalence-class partition in which the
only classes that changed were malformed ones collapsing into `""`.

---

## CERT-FINDING-5 — the corrected rationale contradicted its own bullets

**Status: CLOSED.** Low. Documentation only. Raised in round 2 of
re-certification, against the fix for CERT-FINDING-3.

Two errors in the summary paragraph, both **understating** the author's own case:

1. **A sign inversion in the one surviving number.** *"…at a cost of roughly
   halving the KMS calls per issuance and a plaintext key in heap"* — `at a cost
   of` governed both items, but halving the KMS calls is what the envelope
   **buys**, not what it costs. It contradicted the LATENCY bullet fifteen lines
   above.
2. *"the only ground that survives scrutiny"* — but LATENCY was **retained** in
   reduced form; only AVAILABILITY was withdrawn. Two survive, not one.

**The direction is the interesting part.** CERT-FINDING-1 and -3 were the file
flattering itself. This one is the file running itself down. The correction
overshot, which is the predictable failure mode of a third pass at the same
paragraph — and it is still a defect, because a sign-inverted figure in the
summary line is what a hurried engineer reads before deciding whether to revisit
key custody. **Three passes to state one design decision truthfully**, and every
pass was caught by someone who was not the author.

---

## CERT-FINDING-6 (A11d) — the gate that watches for a lapsed certification had itself lapsed

**Status: CLOSED.** Medium (process). Found by the implementation squad while
closing the accounting; independently confirmed at source by `nexusqa-39` and
`nexusqa-db`.

`a11-attestation-certification.yml` carried `EXPECTED_FAILURES=1` as a bare
literal and referenced this register **zero times**. Nothing kept them in step:

| Commit | Harness emits | Literal | State |
| --- | --- | --- | --- |
| `02181d0` | 1 | 1 | correct when written |
| `0fb4275` | 2 (A11b fenced the IPv6 class) | 1 | **RED** |
| `a947e33` | 3 (CERT-FINDING-1 made tool-emitted) | 1 | **RED** |
| `d0605ba` | 0 (both findings fixed) | 1 | **RED** |

**Proven, not inferred.** The reproducer was run from a clean `git archive` of
`766ef3a` and its output fed through the workflow step's own logic verbatim:
150 checks / 3 failures against an expected 1 → reproducer job fails → `a11-gate`
**NO-GO**. So `a11-gate` had been red since `0fb4275`, for about six hours, *on a
tree that was in exactly the state this register described*.

**Why it went unnoticed is the finding, not the staleness.** A red gate with a
plausible message — *"3 failures, expected 1"* — is indistinguishable from a
genuinely failing gate. The certification gate failing closed looked exactly like
the certification being incomplete, which it also was. **A gate can only be
trusted while its expectations are as maintained as the thing it watches**, and
this one's expectation was a hand-copied number. It has had four different
correct values across four commits.

**Remediation:** the literal stays — it is what someone greps for at 2am, and it
still governs the comparison — but the workflow now asserts it **equals the
number of OPEN rows in this register**, with the marker cross-checked. Moving one
without the other fails the build.

**Deliberately an assertion, not a derivation.** `nexusqa-db` proposed deriving
the count from this register, then withdrew it in favour of the narrower form on
the failure asymmetry: with derivation the parser is load-bearing, so a parsing
bug produces a wrong expected count and — whenever that wrong number happens to
equal the actual failure count — a **silent green with real failures
outstanding**. With assert-equal, a parsing bug can only produce a **false red
with a legible message**. Parser bugs should be noisy, not dangerous.

**The parser fails closed on its own blindness.** An unreadable register, or a
table whose shape has changed, must not read as "zero open findings" — that is
the same shape as the single-truthy-test hole documented under CERT-FINDING-1,
and it was reproduced during development before being fixed: with the table
stripped out, the naive parser reported `open_count=0`, which *equals* the
literal, and passed. It now fails with a distinct message. Verified against five
states: both findings open (red), both closed (green), a third finding opened
later (red), table removed (red), file missing (red).

---

## Two lessons this round produced, recorded because both are recurrences

### 1. A check's own blind spot produces a false reading about its subject

Credit: `nexusqa-db`. *"Before reporting an anomaly, verify the instrument on a
case whose answer is known."*

Three instances in one area in one day:

* the single-truthy-test that would pass on a missing file (CERT-FINDING-1's
  probe design);
* `nexusqa-db`'s comment-stripper, which left trailing comments in place and
  reported a divergence between the two `normalize_origin` copies that did not
  exist — the sole delta was an inline `# malformed port in the authority`;
* the certifier's own round-2 probe, which reported "30 unstable at 3rd
  application" by merging two probe output formats in which index 2 meant
  different things. The true figure was 0.

In every case the instrument, not the subject, was defective — and in every case
the false reading was *alarming* rather than reassuring, which is the only reason
anyone looked. **The dangerous version of this class is the one that reads
clean.** That is why every probe added in this round carries a
"could-I-see-my-target" assertion separate from its verdict. Note that the
certifier volunteered its own false start rather than quietly correcting it: a
certification that hides its instrument failures is not auditable.

### 2. Prose is not tracked; a tool is

Already recorded under CERT-FINDING-1, and re-confirmed here from the other side.
`nexusqa-39` re-ran the probe-integrity attack **after** the sentence was
genuinely corrected, and that is the run that counted. Before the fix, "sentence
corrected" and "file hidden" differed on two axes, so the probe could have
separated them by accident. Once the finding closes, `kms_claim_present=False` is
the *passing* value, and only the separate `kms_probe_read` assertion
distinguishes "fixed" from "file missing". It held: file present → 151/0; file
hidden → 151/**1**, `FAIL: CERT-FINDING-1 probe could READ attestation_keys.py`.

**A probe is worth least on the day it is written and most on the day its finding
closes**, because that is the day its passing state becomes indistinguishable
from its blind state.

---

## Operational consequences carried out of this work — not findings

* **No data migration ships for environments provisioned before `d0605ba`.** A
  provisioning record whose `target_origin` was stored in the old broken form
  (`https://2001:db8::1:8443`) now normalises to `""`, so issuance refuses at
  `attestation_issuer.py` Gate 3 with *"pins no usable origin … re-certify the
  environment"*. Fail-closed and self-describing, and the blast radius is nil —
  such an environment could never have completed a walk anyway, which is the
  defect. **But the row is not auto-repaired: affected environments must be
  re-certified.** Established by code-read plus unit-verified `normalize_origin`
  output; **not** executed against a provisioning table.
* **`A11_SNAPSHOT.sha256` has no `.gitattributes` eol rule.** It is read as bytes
  by `sha256sum -c`, which treats a trailing CR as part of the *filename*. Both
  the reproducer and the CI job already strip CR before checking, so this is
  currently harmless — but it is the same unpinned-extension shape that produced
  the original lapse window, and it is recorded rather than fixed because it was
  outside this task's scope.


---

## CERT-FINDING-7 — the gate built to stop drift reported GREEN with a finding open

**Status: CLOSED.** Medium (process / CI accounting). **Fail-open**, which is why
it is the most serious finding in this chain despite touching no product code.
Raised by the certifier of record in round 3, against CERT-FINDING-6's own
remediation.

The register cross-check shipped in the accounting commit recognised a row as
open only when its Status cleaned to the exact token `OPEN`, and treated
**everything else as CLOSED**. Nine states in which a finding is open and the
gate passed:

| State | Shipped gate | Correct |
| --- | --- | --- |
| `Open` / `open` | **PASS** | FAIL |
| `**OPEN** (A11a)` — this register's own annotation style | **PASS** | FAIL |
| `**OPEN** — regression` | **PASS** | FAIL |
| `REOPENED` | **PASS** | FAIL |
| `CLOSD` — any typo, any unrecognised status | **PASS** | FAIL |
| an empty Status cell | **PASS** | FAIL |
| a column inserted before Status (`$4` silently re-points) | **PASS** | FAIL |
| an OPEN row indented two spaces (`^\|` anchor misses it) | **PASS** | FAIL |

**The shape is exact and it is the third recurrence in this file.** The gate
guarded *"I cannot see the file"* and *"I cannot see the table"*, and both fail
closed correctly. The uncovered case was **"I can see the table and silently
misread a row"** — and it resolved to the reassuring answer. That is the same
defect as the single-truthy-test that passes on a missing file, one level in:
*an unrecognised input must never resolve to the value that means "nothing is
wrong".*

**Worse, the author wrote both the gate and its controls.** The five states
verified before landing all held; they were the five the author thought of. The
certifier's nine were the ones the author did not. **A control suite written by
the person who wrote the check inherits its blind spots** — that is why this
chain has a non-author certifier at all, and it caught the gate as readily as it
caught the code.

**Remediation, verified in both directions:** locate the Status column by its
**header name** rather than by position, tolerate leading whitespace, upper-case
before comparing, and **fail closed — only an explicit `CLOSED` closes a row;
anything else counts as OPEN.** A fourth guard was added: if the number of
classified rows does not equal the number of finding rows, the gate fails rather
than silently dropping a row it could not read.

Verified against thirteen states — the baseline passes, and all twelve
adversarial states fail — using the step's `run:` script **extracted from the
parsed YAML**, so the thing tested is the thing that ships, not a hand-copy of
it. The install step re-parses the workflow and asserts the extracted script is
byte-identical to the tested one.

---

## CERT-FINDING-8 — the record both overclaimed and underclaimed

**Status: CLOSED.** Low–Medium (record accuracy). Raised in round 3.

Four distinct defects, in the documents whose whole purpose is to be accurate:

1. **A verdict attributed to a commit that did not receive it.**
   `A11_INDEPENDENT_CERTIFICATION.md` said *"Verdict at `da5b5d0`: CERTIFIED. No
   open findings."* The round-2 verdict at `da5b5d0` was CERTIFIED **WITH
   FINDINGS**, with CERT-FINDING-5 open — as this register said on the same day,
   in the same commit. **The document contradicted its own register.**
2. **A `54e7735` result attributed to `da5b5d0`, in two files.** *"151 checks, 0
   failures, exit 0 — `run_certification.sh` end to end"* was recorded under the
   `da5b5d0` heading. At `da5b5d0` that script exits **2**; the 151/0 came from
   running the halves directly past the lapsed drift gate. `GATE1_EXIT_STATUS.md`
   repeated it with *"9/9 digests"*, which was true only at `54e7735`.
3. **The record pre-declared its own certification.** *"Certified SHA:
   `da5b5d0`, plus the accounting commit that re-pins this record"* — that
   accounting commit was the one being written, which no non-author had seen. It
   is true now because round 3 made it true; it was **circular when committed**.
4. **The issuer doc simultaneously asserted both findings were still OPEN, in
   three surviving passages.** The edit replaced only the first line of item 7
   and orphaned its continuation, leaving *"not yet in the `attestation_keys.py`
   docstring"*, a heading *"Two findings, both open, neither a bypass"*, and a
   table reading *"docstring still wrong"* / *"not fixed"*. **A reader believes
   whichever they reach first.**

**Both directions of error, in one commit, in the same subject.** Defects 1–3
claim more than was proven; defect 4 claims the work was never done. **A partial
edit to a document is a worse outcome than no edit**, because the untouched half
now carries the authority of a file that looks maintained. The mechanism was
mundane: a single-line replacement against a multi-line claim.

**Remediation:** each round's verdict is now recorded against **the SHA that
actually received it**, in a table; the reproducer row states what is true at
which SHA; the certification record explicitly says it is **not** the register
and must not be read as its status field; and the issuer doc's three passages are
corrected. Acceptance criteria were the certifier's, and are mechanical —
`grep -c "both open"`, `"not fixed"`, `"still wrong"` all return 0.

**One line was corrected by weakening it, deliberately.** The record claimed
*"`nexusqa-db` and `nexusqa-39` each measured independently, neither told what to
expect."* That is true, and the implementation squad has the message log — but
**the certifier has no evidence for it and declined to endorse it.** It is now
attributed to the implementation squad and explicitly marked as not verified by
the certifier. A record should not launder an unverified claim through a
certification.

---

## CERT-FINDING-9 — the CERT-FINDING-1 probe was green by line-wrap

**Status: CLOSED.** Medium (certification integrity). **Fail-open.** Raised in
round 3, against the **certifier's own artefact** — `issue_side.py`, not any
file the author wrote.

The probe tested `"offers no Ed25519 asymmetric-signing key type" in _keys_text`
— a raw substring match. But the corrected docstring *legitimately quotes that
sentence in order to refute it*, so the string **is** present in the file. The
probe returned `False` only because the quotation happens to wrap across a
newline at exactly the needle boundary:

```
docstring used to assert that *Cloud KMS offers no Ed25519 asymmetric-signing
key type*, and that KMS-native signing would therefore mean …
```

**The green was a coincidence of typography.** The certifier proved the
consequence rather than arguing it: injecting the claim back as a plain
assertion, wrapped the way the file already wraps, produced
`kms_claim_present=False` and **151 checks / 0 failures / exit 0**. The defect
was restored and the certification harness reported clean.

**This is the sharpest instance of the class in the whole chain**, because the
probe passed its own integrity test: it *could* read its target — `kms_probe_read`
was `True` — and still could not see the claim. **"I can see my target" and "I can
see the thing I am looking for in my target" are different assertions**, and the
register's existing rule only covered the first.

**Remediation, verified in all four directions:**

* **normalise whitespace before matching** — a line break must not hide the claim;
* **distinguish assertion from quotation** — an occurrence introduced by the
  refutation frame (*"used to assert that"*) is a quotation; any other occurrence
  is an assertion and fails. A naive whitespace-normalisation alone would have
  false-RED on the correct file, which is why this half is necessary;
* **a third assertion: the correction must be PRESENT.** `EC_SIGN_ED25519` must
  appear. *"The false sentence is absent"* and *"the rationale was deleted
  wholesale"* were previously indistinguishable, and only one of them is a fix.

| State | `probe_read` | `claim_asserted` | `correction_present` | Harness |
| --- | --- | --- | --- | --- |
| the corrected file | True | False | True | **PASS** |
| claim re-asserted, wrapped | True | **True** | True | **FAIL** |
| correction deleted | True | False | **False** | **FAIL** |
| target hidden | **False** | True | False | **FAIL** |

The exception path now sets the two content answers to their **failing** values
rather than their passing ones, so an unreadable target cannot report clean by
default.

**Check count 151 → 152**, and the reason is this third assertion. Recorded here
so the rise is not read as drift — the same discipline as the 150 → 151 note.

**A note on who fixed it.** This finding is against the certifier's harness, and
it was repaired by the **author** of the code under certification, which is the
independence problem in mirror image. It is acceptable only because the
acceptance criteria were stated by the certifier **before** the fix, are
mechanical and two-directional (*the injected re-assertion must FAIL; the clean
file must PASS*), and were re-checked by a non-author afterwards. **A fix to a
probe, written by the party the probe watches, has to be verified by someone
else or it is not a fix.**


---

## CERT-FINDING-10 — the harness that certifies everything was itself pinned by nothing

**Status: CLOSED.** Medium–High (certification integrity). **Fail-open.**
Pre-existing since the harness was written; became load-bearing at `15cceb9`.
Raised by the certifier of record in round 4.

`issue_side.py`, `verify_side.py` and `run_certification.sh` appeared in
`A11_SNAPSHOT.sha256` **zero** times, and in no other digest file in the
repository. **The nine files under certification were pinned. The three files
doing the certifying were not.**

The certifier demonstrated the consequence rather than arguing it. One token
changed in the probe (`key type` → `key typo`), the false KMS claim re-asserted
as plain fact in `attestation_keys.py`, and the manifest regenerated **the way
any legitimate change regenerates it**:

```
all digests: OK
CHECKS RUN : 152
FAILURES   : 0
EXIT CODE = 0

attestation_keys.py:122:
  Google Cloud KMS offers no Ed25519 asymmetric-signing key type, so KMS-native
```

**A perfect green with CERT-FINDING-1's defect fully restored in the certified
file.** The drift gate catches the naive form of this — editing a pinned file
without re-pinning still exits 2 — but not the realistic form, because any
legitimate change to a pinned file requires a re-pin anyway, and once the
manifest is regenerated a neutered probe is invisible to everything in the repo.

**Why it became load-bearing exactly here.** Through rounds 1–3 the certifier
could write *"the harness is byte-identical across all commits, so the count
cannot have been gamed"* — and this register said so in print, twice, as the
argument that the 150 → 151 rise was honest. At `15cceb9` the harness changed for
the first time, to fix CERT-FINDING-9. **That argument retired at that commit and
nothing replaced it.** A guarantee that quietly stops holding is worse than one
that was never claimed, because the sentence asserting it stays on the page.

**Remediation:** the three harness files are now pinned, 9 → 12. An edit to
`issue_side.py` or `verify_side.py` presents as drift and forces re-certification
exactly as an edit to `attest.py` does — proven in both directions. **This does
NOT extend to `run_certification.sh` itself; see CERT-FINDING-12.**

**And re-deriving the digests is NOT, by itself, sufficient** — the certifier
proved this rather than asserting it. On a tampered tree with a regenerated
manifest, an independent re-derivation **matches**. It proves manifest↔bytes
consistency at a named SHA; it does not prove the bytes are the right bytes.

### Standing obligations on the certifier — every round, not optional

These are what actually close CERT-FINDING-10, and they are recorded here because
**a future certifier who follows this register as written and only re-derives
digests would be satisfied by a tampered tree.**

1. **Diff all twelve pinned files against the last certified SHA and account for
   every difference.** This is what catches a neutered `issue_side.py` or a
   neutered `run_certification.sh`.
2. **Run the tamper test in BOTH directions:** neuter the probe, require exit 2;
   restore, require exit 0. This is the **only** check that validates
   `run_certification.sh` itself, because a neutered driver yields exit 0 where
   2 is required.

The certifier accepted the arrangement — that it certifies an artefact whose
re-pinning the author controls — **on the basis that (1) and (2) are standing
obligations**. The acceptance is conditional on them and does not survive without
them.

**This is the finding the author was groping toward and mis-stated.** The author
asked whether it was proper to have fixed CERT-FINDING-9 in the certifier's own
artefact. The certifier's answer was that authorship is not what preserves
independence — verification is, and the fix was verified against criteria set in
advance. But the question was pointing at something real one level up: *nothing
pinned the harness, so neither party could be checked on it.* **The instinct was
right and the diagnosis was wrong**, which is a good reason to voice an unease
even when you cannot name its cause.

---

## CERT-FINDING-11 — the connective sentence contradicted its own bullets

**Status: CLOSED.** Low. Documentation only. **In a pinned file**, so this one
did lapse the manifest — the only finding after `d0605ba` that does.

`attestation_keys.py` said the signer's one-issuance scope *"destroys the latency
and availability arguments for the envelope"*, while its own bullets fifteen
lines above retain LATENCY as *"real but modest"* and withdraw only AVAILABILITY
as *"FALSE"*. The scope destroys availability and **shrinks** latency.

**Third instance of one pattern in one paragraph block** — CERT-FINDING-5 carried
the other two. Corrected to *"destroys the AVAILABILITY argument and SHRINKS the
latency one"*.

**How it was found is the part worth keeping.** The certifier saw this in round
2, did not report it; reported it in round 3 but graded it below the finding bar;
and raised it in round 4 only after the author pushed back and asked directly.
The certifier then named its own inconsistency: CERT-FINDING-5(b) was raised for
*"the only ground that survives scrutiny"* — a sentence of **identical structure,
contradicted by the same bullets** — and graded differently only because it was
found first.

**Two instances of one pattern cannot receive two verdicts because of the order
they were noticed in.** The certifier volunteered this rather than quietly
raising the finding, which is the same discipline it applied to its own
instrument failure in round 2. **The certifier needed correcting too, and the
record should show it.**

---

## Accepted limitations — recorded, not fixed

Two residuals the certifier graded below the finding bar. Both are recorded
because an accepted limitation that is not written down is indistinguishable from
one nobody noticed.

* **The CERT-FINDING-1 probe pins a STRING, not a CLAIM.** After the
  CERT-FINDING-9 hardening it survives reflow, quotation and deletion of the
  correction, but a **paraphrase** evades it — as do title case and a
  hyphen-less spelling, both of which have been evadable since the probe was
  written. The realistic regression is a future engineer restating the false idea
  in their own words, not restating it byte-exactly. Catching that needs semantic
  matching, which would introduce false reds against a file whose whole job is to
  *discuss* the false claim. **Accepted:** this probe pins one sentence; a
  paraphrase gets past it.
* **Two evasions were CREATED by the CERT-FINDING-9 fix**, and they are the cost
  of the refutation frame: an assertion smuggled within 80 characters after the
  phrase *"used to assert that"*, or framed by an unrelated use of that phrase,
  reads as a quotation. This is the standard shape of an exemption — the
  exemption becomes the hiding place. It is accepted because the alternative
  (no frame) false-REDs on the correct file, and because the frame is robust to
  the thing that actually caused CERT-FINDING-9: whitespace normalisation removes
  line breaks, and the frame sits about twelve characters from the needle.

**A third residual was NOT accepted.** The gate's `^CLOSED` prefix match let
`**CLOSED** (pending re-cert)` — which reads to a human as *not yet closed* —
pass as closed. The certifier offered "accept and document" as legitimate. It was
fixed instead, to an exact-token comparison: this is the eleventh finding in a
chain whose recurring shape is *an unrecognised input resolving to the reassuring
answer*, and a status cell that reads "pending" must not count as closed. The
cost is that the Status column takes no annotation; caveats belong in the
finding's own section, which is where every other caveat in this register lives.


---

## CERT-FINDING-12 — "not circular" was true for two of the three

**Status: CLOSED.** Low (record accuracy). Not a security issue. Raised in round
5, against CERT-FINDING-10's own remediation.

The register claimed pinning the harness was *"not circular: `run_certification.sh`
verifies the manifest, so an edit to the harness presents as drift"*. **True for
`issue_side.py` and `verify_side.py`, and proven so. False for
`run_certification.sh` itself.**

The certifier demonstrated it: with the drift gate disabled *inside*
`run_certification.sh`, the probe neutered, the false claim re-asserted, and the
honest manifest left untouched on disk —

```
manifest pins run_certification.sh : e71f20d4d370…
actual file                        : 0f931773eeec…
CHECKS RUN : 152   FAILURES : 0   EXIT CODE = 0
```

**The file diverges from its own pin and the run reports green**, because the
code that checks the manifest is the code being checked. **You cannot bootstrap
trust in a checker from the checker.**

That much is inherent and not a defect in the fix. The defect was the register
asserting blanket protection across all three files when it holds for two —
overclaiming a guarantee is how the next person stops looking.

**Remediation:** the claim is scoped correctly in both the register and the
reproducer's own header, and the two out-of-band checks that *do* protect the
driver are recorded above as standing obligations. The row for
`run_certification.sh` is kept: it catches accidental edits and makes deliberate
ones show up in a diff, which is worth having even though it cannot be
self-enforcing.

---

## CERT-FINDING-13 — the fourth instance, and why there is no fifth correction

**Status: CLOSED — structurally, not by correction.** Low (documentation). In a
pinned file.

The framing sentence of the KMS rationale said the first draft *"claimed **a
latency and an availability advantage that this system, AS BUILT, does not
have**"*. The relative clause governs both nouns, so it asserts the system has no
latency advantage. The LATENCY bullet three lines later says it has one — one KMS
`decrypt` where KMS-native makes two `asymmetricSign` calls.

**Same pattern, same block, fourth occurrence**, and in the most load-bearing
position yet: the sentence a reader meets *before* the bullets, introducing the
correction where this whole sub-chain began.

### The pattern, and the decision to stop correcting it

| Finding | Instance | Found in |
| --- | --- | --- |
| CERT-FINDING-3 | the three grounds as originally written | round 1 |
| CERT-FINDING-5(a) | *"at a cost of roughly halving"* — sign inverted | round 2 |
| CERT-FINDING-5(b) | *"the only ground that survives scrutiny"* | round 2 |
| CERT-FINDING-11 | *"destroys the latency and availability arguments"* | round 4 |
| CERT-FINDING-13 | *"a latency and an availability advantage … does not have"* | round 5 |

**Four passes, and each one found exactly one more.** Every instance is a
**summary sentence contradicting the bullets it summarises**, and the bullets have
been correct and unchanged since `da5b5d0`. Every correction introduced the next
instance.

The mechanism is worth naming because it is not carelessness: **a summary of a
three-way verdict wants to collapse into a two-way one.** One ground stands, one
is reduced, one is withdrawn — and "reduced" is the one that keeps getting
swallowed into "withdrawn", always in the same direction. The collapse is
invisible to whoever writes it, because they know what they meant.

**So the fifth correction was not written.** On the certifier's recommendation,
both summary paragraphs were **deleted** and the bullets left to speak for
themselves. The file now carries an explicit instruction not to add one back, with
this history as the reason — because the next person to read that section will
feel it is missing a summary, and they will be wrong.

**The general lesson:** when four independent passes each find exactly one more
instance of one pattern, the next pass is not evidence of convergence. **Change
the structure that keeps generating the instances**, not the instance. The
certifier proposed this and was right to; correcting a fifth time would have been
the obvious move and the wrong one.

### One thing this closure does NOT claim

The certifier's sweep extracted every docstring sentence containing any of sixteen
keywords and checked each against the three bullets. **A contradiction phrased
without those words would have been missed** — which is exactly why the summaries
were deleted rather than audited again. The structural fix is what bounds this,
not the sweep.


---

## CERT-FINDING-14 — the gate found this one on itself

**Status: CLOSED.** Low (CI accounting). **Fail-closed** — which is the whole
point of the entry. Found by the gate, not by a certifier and not by the author.

Writing CERT-FINDING-13 added a table to this register listing the five instances
of the contradiction pattern. Its rows begin `| CERT-FINDING-3 |`,
`| CERT-FINDING-11 |`, `| CERT-FINDING-13 |` — finding-shaped, but they are prose,
and their third column is an *Instance* description, not a Status.

The gate matched a finding-shaped row **anywhere in the document**, read those
three as status rows carrying an unrecognised status, and — under the
CERT-FINDING-7 fail-closed rule — counted them **OPEN**:

```
register: 16 finding rows, 3 OPEN, 13 CLOSED
::error::EXPECTED_FAILURES=0 in this file, but ... lists 3 OPEN finding(s):
         CERT-FINDING-3 CERT-FINDING-11 CERT-FINDING-13
```

**The build went red on a register that was entirely correct.** A false positive,
caught immediately, on the first commit where the register discussed findings in
a table of its own.

**Why it is recorded rather than quietly fixed.** This is the fail-closed rule
paying for itself in the direction nobody tests. Every earlier finding in this
chain — 7, 9, 10, 12 — was a check that would have gone **green** when it should
have gone red. This is the mirror: a parse bug that went **red** when it should
have gone green. Under the old prefix-and-anywhere parser the same rows would
have been read as unrecognised and silently counted CLOSED, and the defect would
have sat in the parser undetected. **A check that fails in the noisy direction
reports its own bugs; a check that fails in the quiet direction accumulates
them.** That trade was chosen deliberately in CERT-FINDING-7 and this is the
first evidence it was the right choice.

**Remediation:** the parse is scoped to the table containing the `Status` header
— it begins at that header and ends at the first non-table line. A finding-shaped
row elsewhere in the document is now ignored, which is what lets this register
discuss findings in tables without confusing the gate that reads it. Verified
with a new control: a prose table carrying an `**OPEN**` row, placed after the
status table, must be **ignored** (exit 0), while every in-table adversarial
state still fails. Fourteen states, all correct.


---

## CERT-FINDING-15 — the remedy was applied where the defect was last seen, not everywhere it can occur

**Status: CLOSED.** Low (documentation / record accuracy). In a pinned file.
Raised in round 6, against CERT-FINDING-13's own remediation.

**The pattern did not recur.** That is the first thing to say, because it is what
the deletion was for: the certifier ran a *structural* sweep this time — every
docstring sentence that names one of the three grounds **and** carries a
verdict-bearing construct, which does not depend on guessing the words a
contradiction might use — and **found no sixth contradiction.** The GROUNDS
section is clean. The remedy worked where it was applied.

**It was not applied everywhere summaries live.** Two parts:

**(a) A surviving summary, in the section where CERT-FINDING-5(b) already lived
once.** `THE HONEST SECURITY STATEMENT` still carried a full summary of the same
three-way verdict:

> *"…the only ground substantial enough to be decisive is the provisioning and
> IAM work above. (Two grounds survive scrutiny, not one — latency is retained
> above in reduced form…)"*

It was **accurate when found.** It was deleted anyway, and that is the point of
the finding: *being accurate today is not the property that matters.* The
argument for deleting the other two summaries applies to this one unchanged — a
summary of a three-way verdict wants to collapse into a two-way one, and the
collapse is invisible to whoever writes it. This paragraph had already collapsed
once.

**(b) A false claim about the file's own contents, introduced by the commit that
did the deleting.** The GROUNDS section was left reading *"no sentence in this
**file** summarises them"* — disproven by (a), in the same file, at the moment it
was written. That is the CERT-FINDING-1 harm class: **the record telling a future
engineer something untrue about itself**, in the sentence a reader meets
immediately before the bullets.

**Remediation:** the surviving summary is replaced by a pointer to the bullets;
the claim is scoped to *"this SECTION"*; and the deletion rule now states
explicitly that it **governs the whole docstring, not the section where it was
first applied**.

### The lesson, which is not the same as CERT-FINDING-13's

CERT-FINDING-13 was about a *defect* recurring. This is about a *remedy* being
scoped too narrowly — and the two look identical from the outside, because both
present as "the same problem, again". They need opposite responses. The defect
recurring means the fix was wrong. The remedy under-scoped means the fix was
right and was applied to one instance of its own domain.

**A structural fix inherits the blast radius of wherever you happened to be
looking.** Both summaries deleted in round 6 were in the section where the last
four findings had been found; the third summary was two headings away and had
been wrong there before. **After applying a structural remedy, the question is
not "did I fix the instances?" but "where else does this construct occur?"** —
and the honest way to answer it is a sweep, not a recollection.

**A note on the sweep's own limits**, recorded because the certifier stated them:
a sentence qualifies if it names a ground *and* carries a verdict construct, and
"verdict construct" is still a hand-written list. **A sentence implying a verdict
without naming any of the three grounds would be missed.** The structural fix —
no summaries anywhere in this docstring — is what bounds the risk. The sweep only
confirms it landed.


---

## CERT-FINDING-16 — three verifier gates had ZERO coverage, and the record said otherwise

**Status: CLOSED.** Medium–High (certification integrity). **Not a product
defect** — gates 7, 9 and 11 are correct and always were. The defect is that the
certification's central evidence did not test them while the record implied it
did. Present since the original certification, 2026-08-20; untouched by every
commit in this chain until round 7 went looking.

### The mechanism

Four of the ten adversarial mutations edit the **signed claims**. Integrity is
checked at step 4, *before* gates 6–12. So they die as `bad_signature` and never
reach the control they are named for. Measured:

| mutation | denial reason | reaches its gate? |
| --- | --- | --- |
| `env_kind=prod` | `bad_signature` | **no** |
| budget escalated in claims | `bad_signature` | **no** |
| tenant swapped | `bad_signature` | **no** |
| crawl swapped | `bad_signature` | **no** |
| signature forged / unknown kid / alg / revocations stripped / wrong origin / expired | their own | yes |

The harness asserted only `not authorized`, which is true either way.

### The falsification — what the harness did NOT notice

Each gate deleted from `attest.py` in isolation, `attest.py` restored and its
digest re-verified after every case:

| Gate deleted | Before the fix | After the fix |
| --- | --- | --- |
| 7 — production isolation (`env_kind != disposable`) | **152 / 0** | **161 / 3** |
| 9 — tenant binding | **152 / 0** | **161 / 1** |
| 9 — crawl binding | 152 / 1 | 161 / 2 |
| 11 — replay guard | **152 / 0** | **161 / 1** |

**Delete production isolation entirely — the control the whole milestone turns on
— and the certification reported a perfect 152 / 0.** Same for tenant binding.
Same for the replay guard. With gate 7 and both gate-9 bindings removed, one
tenant's proof replayed onto another tenant and crawl returned
`authorized=True`, and the harness was one check short of clean.

### Why the record is the harm, not the harness

`A11_INDEPENDENT_CERTIFICATION.md` §3.1 says *"Adversarial mutations confirmed
DENIED for every case: `env_kind=prod`, budget escalated inside signed claims,
tenant swapped, crawl swapped…"*. Every clause is **literally true** — they were
denied. The implication a reader takes, that these confirm the named controls, is
**false for four of them**.

This is this repository's own **blind-verifier class**, named in `CLAUDE.md` §5
and carried in the register since CERT-FINDING-1: *a check that would still pass
if its subject were absent.* It is the same defect the certification was built to
catch, sitting inside the certification.

### Remediation — reaching the gate is the load-bearing half

Asserting the *reason* rather than `not authorized` is the obvious half and the
cheaper one. It is not sufficient alone: a reason-assertion on a torn proof would
demand `not_disposable` and get `bad_signature` **on correct code**. The proof
must be **validly signed and hostile**, which only the issuer half can mint.

`issue_side.py` now mints a second set of attestations signed with the same fresh
key, carrying claims the issuer should never have emitted — `env_kind` of `prod`,
`staging` and blank; a swapped tenant; a swapped crawl; budgets above both
ceilings. That is not a contrived shape: it models the threat the record already
names — **a compromised or over-generous issuer** — which is exactly what gates
6–12 exist for and the only way past step 4.

### Two corrections to the remediation as proposed, both found by reading the contracts

The certifier's proposed remediation was right in substance and wrong in two
specifics. Both were caught by checking the code before implementing, and both
would have shipped assertions that fail on correct behaviour:

1. **The replay guard's contract is "one proof_id → one crawl_id", NOT "one
   use".** `admit()` ends `return bound == cid`: re-verifying the same proof on
   the *same* crawl is **deliberately admitted**. The proposed "replay onto the
   same crawl" check asserts something the guard never promised and goes red on
   correct code. Reaching gate 11 requires **two validly signed proofs sharing a
   `proof_id` but naming different crawls** — claims internally consistent, so
   gate 9 passes and the guard is what refuses. That is minted issuer-side, which
   is precisely why a verifier-side mutation could never test this gate.
2. **A signed budget of 999 is not clamped — it is refused as
   `malformed_claims`.** `HARD_MAX_MUTATIONS_PER_STEP` is 10 and `ProofClaims`
   enforces it at the schema, which is a *stronger* refusal than the clamp and a
   *different* control. Both are now pinned separately: 999 → `malformed_claims`,
   and 10 → authorised but clamped to the fleet ceiling of 3.

**A remediation is a hypothesis about the code, and it needs the same
verification the finding did.** Both of these were plausible, both were stated by
the party that had been right every round, and both were wrong.

### The check count 152 → 161

Nine checks: three `env_kind` forms, tenant, crawl, the hard-max schema refusal,
the fleet clamp, and the replay pair with its control. Every one of them can go
red — that is the table above. **This is the largest single rise in the harness's
history and the only one that closed a hole rather than describing one.**

### What this does not claim

`attest.py` has thirteen numbered steps. Steps 1, 2, 3, 5, 6, 8 and 10 were not
isolated and falsification-tested; four gates were. **The coverage of the
coverage is itself unmeasured**, and this finding exists because nobody had
measured it for seven rounds.
