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
