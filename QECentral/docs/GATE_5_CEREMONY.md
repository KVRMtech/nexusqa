# Gate 5 — the certification ceremony (A37.4)

**Status: the ceremony apparatus is built and tested. The ceremony has NOT been
held, because it needs people this repository cannot appoint.**

The Gate 5 brief requires three things that a document alone cannot supply: a
**Release Director**, **ARB signatories**, and **clean-clone verification on
hardware no author controls**. Exactly one of those three is mechanizable, and
it is built. The other two are seats, and a seat is a person.

| requirement | state |
|---|---|
| Clean-clone verification on unowned hardware | **BUILT** — `.github/workflows/gate5-clean-clone-attestation.yml`, runs on an ephemeral GitHub-hosted runner and refuses to run anywhere else |
| Release Director | **VACANT** — must be named by the programme owner |
| ARB signatories | **VACANT** — quorum of 3, none appointed |
| Ceremony record is validatable, not decorative | **BUILT** — `scripts/gate5_verify_ceremony.py`, with an 18-case negative-control suite |

**No name in the ceremony record may be written by an agent.** A signature is a
person asserting they personally checked the thing beside their seat.
Manufacturing one would be the precise green-wash this programme exists to
prevent, and the validator cannot detect a fabricated human — only the absence
of one. That check is social, and it lives here rather than in code.

---

## §1 · Why "hardware no author controls" is a real requirement

Every recorded run of `scripts/gate0_verify_clean_clone.sh` happened on an
author's machine. The script is correct — it caught a genuine defect during
Gate 0, when a golden was committed before its producer and passed twice locally
while failing in a clean clone. But *the commit is complete* and *the commit is
complete and nobody who wrote it could have tilted the result* are different
claims, and only the second can anchor a certification.

`gate5-clean-clone-attestation.yml` closes that gap:

* **It asserts the hardware first, before verifying anything.**
  `RUNNER_ENVIRONMENT` must be `github-hosted`. A self-hosted runner is a
  machine somebody in this programme owns; it would look identical in the logs
  and would silently void the entire property. Without that assertion the
  workflow would still go green on author-controlled hardware — which is the
  failure it exists to prevent, so the assertion **is** the negative control.
* **It anchors to one 40-hex SHA.** Branches, tags and prefixes are refused.
* **It clones from the remote**, not `actions/checkout` of the triggering ref,
  because the claim under test is "a second party, starting from the commit
  alone, gets the same answer".
* **It re-asserts the SHA three times** — after the outer clone, and again
  inside the throwaway clone the verifier makes. A certification that drifted
  one commit during its own proof would be worthless, and in this repository
  HEAD has moved four times inside a single audit.
* **It reuses the repository's own verifier rather than re-typing it.** Drilling
  a re-typed copy proves the copy works; this is the same script an author runs
  locally, so the two cannot diverge.
* **A failing attestation cannot hide behind a green check.** If
  `CLEAN_CLONE` does not pass, the job fails.

Run it:

```bash
gh workflow run gate5-clean-clone-attestation.yml -f sha=<40-hex>
# or push any branch under gate5/** and it fires on that commit
```

The artifact `clean_clone_attestation.json` is what gets pasted into the
ceremony record. **Do not hand-write it.**

---

## §2 · The seats

### Release Director — one person, vacant

Attests:

> I attest that the deployed artifact corresponds to the certified SHA, that the
> three-service rollback drill was executed against the real VM, and that no
> undocumented manual intervention was required.

This seat owns the two release stop conditions: *deployed build differs from
certified SHA*, and *undocumented manual intervention was required*. Neither is
checkable from inside the repository — the first needs somebody to look at the
live estate, the second needs somebody who was there.

### Proof Guild — one person, vacant

Attests:

> I attest that every critical acceptance proof has been reproduced by a named
> person who did not author the implementation, and that each reproduction is
> recorded with its SHA, environment and artifact.

### ARB signatories — quorum of 3, none appointed

Each attests:

> I have reviewed the complete Gate 0–5 evidence package against the certified
> SHA and find every exit criterion satisfied.

### Independence

The Release Director and the Proof Guild may **not** also sit as ARB
signatories, and no person may occupy two signatory seats. One person
certifying their own release is not a board. The validator enforces all three
rules.

---

## §3 · The authorship problem, and why names must be recorded live

Every commit in this repository carries an **identical** author, committer and
`Co-Authored-By` trailer:

```
author    srika <reddy.sepd@gmail.com>              40/40 of the last 40 commits
committer srika <reddy.sepd@gmail.com>              40/40
trailer   Co-Authored-By: Claude Opus 5 (1M context) 40/40
```

Many sessions commit under one identity. **"Who wrote this commit" is therefore
unanswerable from git by anyone, including the session that wrote it.** A peer
session attempting to route ownership by authorship during this programme would
have mis-routed had it not been told.

The consequence for Gate 5 is sharper than an inconvenience: **non-author
reproduction cannot be reconstructed from the log after the fact.** There is no
archaeology that recovers it. The implementer and the reproducer must be named
in `reproductions[]` at the moment the reproduction happens, or the claim is
unfalsifiable forever.

This makes Step 3 of the brief *harder* than the certification record originally
implied, not easier — and it is why the record carries names rather than commit
references.

Two proofs are already packaged as one-command reruns for a non-author:

| proof | command | properties |
|---|---|---|
| A37.1 credentials decrypt under KMS | `Nexus_power/scripts/a37_verify_kms_decrypt.py` | read-only; emits digests only, never plaintext; treats 0 rows as FAIL |
| A37.3 three-service rollback | `Nexus_power/scripts/gate_rollback_drill.sh` | self-restoring; injects failures to prove it can still fail |

---

## §4 · Order of operations

The ceremony is the **last** step, not a parallel one. Running it early is what
produced a refused Gate 5 the first time.

1. Every gate closes. Gates 2, 3 and 4 are the outstanding ones.
2. **Freeze the tree.** A checkout that moves during certification cannot be
   certified; this is Gate 0's still-ungranted escalation.
3. Pick the SHA and push it. No branch, no tag, no "latest".
4. CI goes green on that SHA.
5. Deploy that SHA, or certify what is deployed — they must match.
6. Run `gate5-clean-clone-attestation.yml` against it; paste the artifact in.
7. Non-authors reproduce each critical proof; record names, SHA, environment,
   result, artifact.
8. Fill the seats. Real people, real timestamps.
9. `python scripts/gate5_verify_ceremony.py QECentral/certification/gate5_ceremony_record.json`
10. Only if that exits 0 does the ARB sign.

---

## §5 · The validator, and proof that it discriminates

`scripts/gate5_verify_ceremony.py` refuses a record on any of: a SHA that is not
40-hex; a deployment that differs from it; a missing, failed, wrong-SHA or
author-hardware clean-clone attestation; any gate or A37 item not `PASS`; an
empty or self-signed reproduction; an unfilled or placeholder seat; an unmet
quorum; a duplicated signatory; or a Release Director who also signs as ARB.

A validator that refuses everything is indistinguishable from one that works,
and the live record is currently refused — so refusal alone proves nothing.
`scripts/gate5_verify_ceremony_selftest.py` settles it:

```
== positive control: a complete, honest ceremony must PASS
   PASS  the validator is capable of saying yes

== negative controls: each break must be caught, and named
   PASS  SHA is a branch name
   PASS  deployment differs from certified SHA
   PASS  clean clone ran on a self-hosted runner
   PASS  clean clone admits author-controlled hardware
   ... 14 more ...

SELFTEST: PASS -- 1 positive control, 18 negative controls
```

Run against the live record today it prints **17 unmet conditions** and exits 1.
That refusal is the honest state of the programme, not a defect in the record.

---

## §6 · What this does not do

* It does not appoint anyone, and it cannot detect a fabricated signatory —
  only a missing one.
* It does not verify that a signatory actually read the evidence.
* It does not close Gates 2, 3 or 4.
* It does not revoke the exposed PAT (see `GATE_5_CERTIFICATION.md` §3).

The ceremony is now blocked on people and on the gates, which is the correct
place for it to be blocked.
