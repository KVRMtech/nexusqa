# Gate 5 — Final Certification (A37)

**Status: CERTIFICATION REFUSED. No commit SHA is signed.**

Published dossier: https://claude.ai/code/artifact/e2311716-fc28-4160-ab88-7968dc8a63e2

A37.4 is the final authority and it refuses. Six of the brief's own stop
conditions are triggered. Two of the three A37 drills were executed against live
production infrastructure and **passed**; they carry forward.

| item | verdict |
|---|---|
| A37.1 KMS provisioning + KEK re-wrap | **PASS** — 9/9 production credentials decrypt through Cloud KMS, before *and* after a full service rebuild |
| A37.2 rotate the exposed credential | **CLOSED 2026-09-01** — the owner checked both accounts and no such token is listed; it was revoked or expired. §A37.2 below records what was and was not verified, and the exposure window that stands regardless |
| A37.3 live three-service rollback drill | **PASS** — executed against verdict-box, 19 checks passed / 0 failed |
| A37.4 final ARB certification | **REFUSED** |

---

## §0 · Why certification stops

The certification target could not be frozen. HEAD moved **four times** during
the audit:

```
0124a53  gate0(A4): the baseline, recorded          <- candidate at audit start
a9a4e19  gate3(A20): qec_019 round-tripped in CI
c0770d4  (reported by a concurrent Gate 4 session)
3778c1a  gate3: four defects the first CI run in three days uncovered
```

All authored by **concurrent sessions** in this same checkout. In the same
window the working tree went from 7 modified / 4 untracked to **20 modified /
28 untracked**. This is the condition Gate 0 §0 escalated and it has not been
granted: the tree does not hold still.

### Stop conditions triggered

1. **Exposed credential remains valid** — §3. Disk copies removed, token alive.
2. **Evidence cannot be traced to the certified SHA** — Gate 1's A11 and A12 are
   untracked/unstaged working-directory files (§2).
3. **Deployed build differs from the certified SHA** — `verdict-box` serves
   `ede6bf2`, 13+ commits behind HEAD, predating every Gate 0 and Gate 1 commit.
4. **Required gate evidence is missing** — Gate 2 entirely, Gate 3 in progress,
   Gate 4 has implementation with zero acceptance (§2).
5. **Certification depends on a moving branch** — above.
6. **CI does not correspond to the SHA** — improved mid-audit but not met (§1).

**Not** triggered, and worth stating plainly: *credentials cannot decrypt after
KMS migration*. The failure mode the ARB warned about did not occur.

Step 3 of the brief — non-author reproduction — has **no register for any
gate**. The two A37 proofs below are packaged as one-command reruns, which is
the only Step 3 evidence this gate can currently supply.

---

## §1 · Gate 5 was convened ahead of its inputs

The decisive finding is not a defect. It is sequencing.

| gate | commits | gate evidence | state |
|---|---|---|---|
| Gate 0 | `0a91cea` … `0124a53` | `GATE_0_DURABILITY.md` | 3 of 5 closed |
| Gate 1 | tag `gate-1` → `3420d88` | `GATE1_F_REGISTER.md` | tagged, but A11/A12 not in the commit |
| Gate 2 | **none at audit close** | **none** | never executed — **see §7, this changed after the audit** |
| Gate 3 | `a9a4e19`, `3778c1a` | none | **in progress during this audit** |
| Gate 4 | **none** | **none** | implementation only, no acceptance |

**One blocker cleared mid-audit.** Another session resolved the push (an
`origin-https` remote now exists), so `a9a4e19` reached `origin` and CI ran on
this branch **for the first time in three days**. It returned four defects, and
the current HEAD is unpushed again — so A5 is closer but still not met, and
there is no pushed, green, immutable object to sign.

---

## §2 · The evidence register

The full indexed register — every A-item, its state and its anchor — is in the
published dossier. Two entries change what the `gate-1` tag means:

**A11 attestation issuer — in no commit.**

```
attestation_keys.py                   untracked
attestation_models.py                 untracked
qec_023_attestation_issuer.py         untracked
walk_attestation.py                   modified, unstaged      (A12)
```

The tag `gate-1` points at `3420d88`, which **does not contain any of them**.
Certifying that SHA would certify a system without its attestation issuer while
the gate record claims otherwise — the same class of defect Gate 0's A2 caught
and reverted (`0052ab7`): a claim shipping in a different commit from the thing
that makes it true.

Gate 4's artifacts (MinIO, the KEDA manifest, the crossing journal, a live Squid
egress proxy on the VM) all exist. *Implementation present* is not *acceptance
proven*, and the register keeps the two apart deliberately.

---

## §3 · A37.2 — PARTIAL: exposure removed, credential not yet dead

A fine-grained GitHub PAT sat in cleartext in **two** places on `verdict-box`:
the `origin` remote URL in `/home/srika/nexus-src/.git/config`, and three lines
of `/home/srika/.bash_history`. Presented to `GET /user` it returned **HTTP
200** as `Venkatareddy2012`. The credential flagged at M0 closure had never been
revoked.

**Done — the exposure is gone.**

```
remote origin  ->  https://github.com/Venkatareddy2012/nexus-power-snapshot.git   (token stripped)
.bash_history  ->  3 PAT lines -> 0
sweep of /home /root /etc /opt /srv /var/lib + every container env  ->  NONE
```

**CLOSED 2026-09-01 — but read what that does and does not mean.**

The repository owner checked both `KVRMtech` and `Venkatareddy2012` on
2026-09-01 and no such fine-grained token is listed. It was revoked or it
expired; either way there is nothing left to revoke.

**What is NOT claimed.** This is an absence-from-listing check by the owner, not
a revocation receipt, and nothing records WHEN the token stopped being valid. So
the exposure window is not zero: it runs from 2026-08-20, when the exposure was
found, to at latest its stated expiry of 2026-09-05. Anything captured in that
window was usable during it. A credential incident closes by rotation on a
known date, not by the credential quietly ageing out, and this one closed the
second way.

**Verified separately, and this part is good news.** No full-length token
literal is recoverable from git history: every commit matching `github_pat_` or
`ghp_` merely documents the incident and cites the suffix. The scrub was done
correctly, so the exposure never extended to anyone with repository access.

The original finding, kept because it is why this section exists:
Scrubbing is not rotation, and the
brief is explicit that overwriting the secret is not the deliverable. Anyone who
already captured it can still use it.

Identifiers captured *before* the scrub so the right token can be found:

```
type      fine-grained PAT (github_pat_)
account   Venkatareddy2012
ends      ...p0Ox
expires   2026-09-05 01:32:15 UTC
```

**Action for the account owner:** revoke at `github.com/settings/tokens`, mint a
replacement into KMS-backed custody, then re-test the old token and confirm the
failure.

**Operational consequence:** the VM no longer carries an embedded pull
credential for that remote. Deploys that fetch from it need a deploy key or a
credential helper.

---

## §4 · A37.1 — PASS

The ARB warning is that flipping to KMS without executing the migration makes
existing data undecryptable. It was tested, not asserted.

**The documented blocker is gone.** `6acedfd` recorded the KMS half as
UNEXERCISED because the box returned `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT`. The
instance now carries `cloud-platform` scope and an encrypt/decrypt round-trip
against the production key succeeds from its own identity.

```
key       projects/project-8d85a07a-396c-40aa-9b6/locations/asia-southeast1/
          keyRings/verdict/cryptoKeys/kek     ENABLED
identity  69711394512-compute@developer.gserviceaccount.com
```

**The migration was executed.** All 9 `client_apps.creds_blob` rows carry
`provider=gcp_kms` under that key. Metadata is not decryptability, so every row
was decrypted through a real KMS unwrap by
`Nexus_power/scripts/a37_verify_kms_decrypt.py`:

```
app_id         decrypt   bytes    sha256[:12]
24c0aea7-c08   OK        281      86e23b0fd1e5
626681a0-ee0   OK        62       646dd8aa2a04
72b5b3bb-6f9   OK        281      294d60e9b04b
86203785-1fe   OK        596      d88138b4d929
8951e1e9-367   OK        595      fb9c8c9aac33
9b5aa07e-ef8   OK        281      c891dcd25315
a20943b3-a40   OK        1604     3c19873f6eca
c0adfd94-9fa   OK        281      494faa6c034e
e4befee6-341   OK        1426     ceed086223d8

A37.1 DECRYPT-UNDER-KMS: 9 OK / 0 FAIL
```

Lengths fall in the 62 B – 1604 B range the pre-migration preflight recorded.
Plaintext is never printed, written or logged — only length and digest. The
probe is read-only: no UPDATE, no INSERT, no key mutation.

**Operational drill.** The probe was re-run *after* the A37.3 rollback drill
tore down and rebuilt all three services from an older commit. Every digest came
back **byte-identical**. The system continues to function on KMS-backed
credentials across a full service rebuild.

**Named, not absorbed:** no gated crawl was driven end-to-end to perform a real
application login from a KMS-backed credential. The decryption path is proven;
the journey that consumes it is not exercised here.

---

## §5 · A37.3 — PASS: live rollback drill, 19/0

Fired against the real VM. Not a dry run, not local containers. The drill built
a real multi-service manifest and invoked `gate_rollback.sh` — the same
executable a red deploy gate calls — rolling `qe-central`, `qe-explorer` and
`platform-api` back to the green anchor `9dc00d9`.

```
1. inventory        rollback set == deployment set, count, reverse order   3/3 PASS
2. red verdict      GATE_VERDICT=REGRESSION, exit 3                        2/2 PASS
3. baseline         gate run leaves tracked baseline byte-identical        2/2 PASS
4. rollback         all three restored, tree at green commit, exit 0       2/2 PASS
5. running          every service running and named in the report          6/6 PASS
6. partial          unbuildable service -> non-zero, names survivors       2/2 PASS
7. corrupt manifest refuses with exit 2, ROLLBACK IMPOSSIBLE               2/2 PASS

checks passed: 19   failed: 0
DRILL PASSED
```

The two failure-injection stages matter most: they prove the drill can still
fail. A rollback that reports success while a rejected container keeps serving
is worse than no rollback, because it ends the investigation.

**Final state verified independently of the drill's own report:**

```
repo HEAD    ede6bf2  (develop)   tree clean
qe-central   running / healthy
qe-explorer  running / no healthcheck defined
platform-api running / healthy
NEXUS_KEK_PROVIDER=gcp_kms        survived the rebuild
```

Functional validation went beyond health checks: the A37.1 probe was re-run
against the rebuilt containers and all nine credentials decrypted to identical
digests.

Executable by someone other than the implementer — one command, self-restoring
by construction. Log retained on the VM at `/tmp/a37_drill.log`.

---

## §6 · What must happen before Gate 5 can reconvene

1. **Revoke the PAT at GitHub** (fine-grained, `Venkatareddy2012`, `...p0Ox`,
   expires 2026-09-05), replace into KMS-backed custody, re-test the old one and
   confirm it fails. The disk exposure is already cleared; this half needs
   account access.
2. **Give the VM a deploy credential** — the scrubbed remote has no embedded
   token.
3. **Finish Gates 2, 3 and 4.** The long pole.
4. **Commit A11 and A12**, and re-point or re-cut the `gate-1` tag.
5. **Get CI green and make the lanes required** — the first run in three days
   returned four defects; `gate0_require_ci_lanes.sh` refuses until all three
   lanes report, correctly.
6. **Freeze the tree.**
7. **Reconcile the deployment** with the candidate SHA (13 commits apart today).
8. **Stand up the reproduction register.** A37.1 and A37.3 are already packaged
   as one-command reruns for a non-author.
9. **Reconvene the ARB** against one pushed, green, immutable SHA. A37.1 and
   A37.3 carry forward on re-run; A37.2 needs its revocation evidence attached.

---

## Final certification record

```
Certified Repository:   KVRMtech/nexusqa
Certified Commit SHA:   NONE — refused
Build Artifact:         n/a
Deployment:             verdict-box @ ede6bf2 (does NOT match any candidate)

Gate 0:                 PARTIAL  (A5 not met; push cleared mid-audit, CI red)
Gate 1:                 PARTIAL  (A11, A12 not in the tagged commit)
Gate 2:                 NO EVIDENCE
Gate 3:                 IN PROGRESS
Gate 4:                 NO ACCEPTANCE EVIDENCE

A37.1:                  PASS
A37.2:                  PARTIAL — exposure cleared, credential still valid
A37.3:                  PASS — 19 checks, 0 failures, all 3 services restored

Independent Proof Reproduction:  NOT PERFORMED — no register exists
ARB Certification:      REFUSED
Certification Date:     2026-08-20/21
ARB Signatories:        none — the exit criteria are not met
```

The brief forbids waiving these conditions to meet schedule. They are not
waived.

---

## §7 · Post-audit movement (recorded 2026-08-21)

This section exists so the register does not go stale and silently mislead. It
records what changed *after* the audit closed. **It does not reopen
certification** — nothing here was audited, and Gate 5 remains REFUSED.

**Gate 2 is no longer empty.** At audit close it had zero commits. It now has
**14**, with live evidence bundles for all three applications
(`Nexus_power/evidence/gate2/{acme-life,summit-life-carrier,vkpower-life}`):

```
A14   8 commits    live crossing work, incl. an evidence collision reconciled at 5c0f511
A16   1 commit     a login the crawler declared failed on an app it had already signed into
A17   3 commits    three applications; lane registered as required but deliberately NOT armed
A19   1 commit     three depth fields computed, stored, and read by nothing
```

No commit is labelled **A15** or **A18**, so on the face of the log those two
remain unclosed. Not verified by me.

Notably, `8eaf38e` records that the live vkpowerlife journey **does not
complete**, naming two blockers. That is the honest direction of travel and is
consistent with what Gate 2 exists to find — but "the gate ran and reported a
negative result" is not the same as "the gate's exit criteria passed", and only
Gate 2's own owners can make that call.

**Authorship cannot be established from git in this repository.** All 40 most
recent commits carry an identical author, committer and `Co-Authored-By`
trailer. Any ownership or non-author-reproduction claim that leans on git
metadata is unfounded here — which makes Step 3 of the brief (independent
reproduction) *harder* than the main record states, not easier. A reproduction
register must record the reproducing session explicitly; the commit log will
never show it.

**Unchanged and still blocking:** Gate 4 has no acceptance evidence, Gate 3 is
in progress, A11/A12 remain uncommitted, the deployment still lags HEAD, and the
PAT is still valid until revoked.
