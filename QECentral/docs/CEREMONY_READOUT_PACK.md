# Gate 5 — ceremony read-out pack and evidence roll-call

**Rehearsal completed 2026-08-24 at HEAD `bc05f26`. This is the pack that gets
read aloud and accepted in writing; the roll-call below turns the evidence walk
into a checklist rather than a hunt.**

---

## 1 · Clean-clone dry-run — done, and the trap is proven not assumed

A fresh clone of the pushed SHA on Windows, then the gates:

| step | result |
| --- | --- |
| `git -c core.longpaths=true clone …` then `checkout bc05f26` | **exit 0**, `HEAD == bc05f26`, working tree clean |
| `scripts/gate5_verify_ceremony_selftest.py` | **PASS** — 1 positive control, 18 negative controls |
| `scripts/gate5_verify_ceremony.py` on the live record | **REFUSED** (correct — seats vacant) |
| `pytest tests --ignore=tests/browser` | **2247 passed, 2 xfailed** — identical to the working tree |

### ⚠️ `core.longpaths` is REQUIRED, and here is the proof

The same clone with `core.longpaths=false`:

```
CLONE_EXIT=128
warning: Clone succeeded, but checkout failed.
        --- files present: 17 ---   dirty=2765
```

**It does not fail cleanly — it half-lands.** A partially-checked-out tree with
2765 dirty entries reads as a corrupt repository, not as a path-length limit,
and the natural reaction on signing day is to suspect the commit. This cost the
R3 non-author reproduction real time before it was diagnosed.

**Ceremony instruction:** run `git config --global core.longpaths true` **before**
the clean-clone step, on any Windows verifier. Gate 5's own attestation workflow
runs on an ephemeral Linux runner and is unaffected — this is for the humans.

---

## 2 · The read-out — four items, each accepted in writing

### ① The lexical-gate residual — OPEN, owned, next arc

The refuse pack's destination matching is **lexically incomplete and cannot be
made complete**. The payment vocabulary is open *and* section-colliding at both
ends: `/refunds` browses, `/payments/42/refund` commits, and the same word names
both. Seven non-author red-team rounds converged on one fail-closed polarity with
one shared section vocabulary, which makes every miss a **visible over-block**
rather than a silent crossing — the incompleteness is now consistent and loud,
not absent.

**What must be said aloud:** in `Phase.WALK`, once `walk_attested` is true, a
mutating request is gated by `classify_action_verb(name, url)` **and nothing
else** — the mutation-signal rules adjudicate reads only. The URL string is
therefore still load-bearing for money movement in the one phase whose own code
comment says it "has no human in the loop".

**The fix, named and not built:** move the guarantee to request-observation so
WALK never adjudicates money by reading a URL string. **Owner: peer session
`nexusqa-2d`** (which found the defect class and has offered to verify the fix
fresh). **Trigger: next arc.**

### ② A11e cross-interpreter matrix — advisory leg landed, promote later

`CONVERGENCE OK: 24 vectors × 2 copies × 2 interpreters — agree within each and
across all.` CPython 3.10.11 (host) and 3.11.16 (`python:3.11-slim`), with the
**cross-version** comparison actually run — not a within-version assertion
re-labelled, which the design names as the thing most likely to be got wrong.

Negative control included: one tampered value fails on **both** the WITHIN and
ACROSS axes, exit 1. Both the passing run and the negative control are committed.

**Status: advisory by design, and it stays advisory** — no project rule requires
promotion. Promote-later is a decision, not an oversight.
Evidence: `Nexus_power/evidence/a11e_interpreter_matrix/`.

**Correction, read aloud 2026-08-27:** `CERT_FINDING_REGISTER.md` states A11e
carries its own separate trigger — *"Trigger: GATE 5 ENTRY — must be closed, or
accepted in writing by a named owner, before Gate 5 certification is convened."*
That trigger is about the finding, not about promoting the CI jobs to blocking,
and it is now checked live rather than recalled: on `.github/workflows/a11-attestation-certification.yml`,
run at the certified SHA itself (`7d7408b`, run
[33039740426](https://github.com/KVRMtech/nexusqa/actions/runs/33039740426)),
job-level conclusions are `success` for all three —
`A11e convergence sweep (advisory, py3.10)`, `A11e convergence sweep (advisory, py3.11)`,
and `A11e convergence: agree within AND across (advisory)`. The remediation the
register calls for has landed and is green on the SHA being certified; the
trigger's "closed" branch is satisfied. The jobs remain `continue-on-error: true`
and outside `a11-gate`'s `needs:` — advisory-by-design and the register's
Gate-5-entry trigger are two different questions, and both are answered.

### ③ Phase-1 exit re-scope — the item that changes the claim

`QECentral/docs/PHASE_1_EXIT_RESCOPE.md`. Phase 1 exits at **one admissible
crossing plus two measured capability gaps**, not three crossings. Four gaps
(B1–B4) go to the Phase-5 Fill-Engine backlog, triggered at Phase-5 entry.

**The sentence to read verbatim:** *the t3 admissibility gate is unchanged; the
summit bundle remains refused; no application was modified and no crossing was
fabricated.*

### ④ Egress fence — ACCEPT `capacity = 1` as a shipped constraint

`QECentral/docs/ARB_EGRESS_FENCE_DECISION_RECORD.md`. Option (A) was rejected on a
measured finding: the producer takes no crawl id, the consumer reads one fixed
path, and one proxy mounts one shared volume — so a producer-side change would
make the code *look* fixed while the browser stayed fenced by whichever write
happened last.

**Blast radius, to be read aloud:** above capacity 1, tenant A's browser may
egress to tenant B's approved domains, silently. Reachable **only** by
configuration — an operator or migration raising `qec_022`'s `server_default="1"`.
Bounded to one worker; A32 proves the fence holds across workers.

**Not closed:** nothing in the running system refuses `capacity > 1`. The (b+)
runtime refusal is endorsed and unimplemented, and it carries a landing hazard —
`T-FL-08` is `xfail(strict=True)` at capacity 2, so the refusal must land together
with that test's rewrite or CI turns red. **Owner seat: VACANT.**

---

## 3 · Evidence roll-call inventory

Every bundle carrying `produced_by`, scanned from the tree rather than recalled.
**Read the CITED rows; the others are listed so nobody cites them by accident.**

### ✅ Cited by the certification

| bundle | `produced_by` head | dirty | crossed | confirmed |
| --- | --- | --- | --- | --- |
| `evidence/gate2/r3_acme_reproduced/journey.json` | `e24bcf54d088` | **false** | **2** | **true** |
| `evidence/gate2/r3_acme_reproduced/NON_AUTHOR_REPRODUCTION.md` | reproduced at `24da99f` by `nexusqa-21` | — | 2 | true |
| `evidence/gate2/r1r2_task1_task2/summit_seeded_journey.json` | `6aedd6a4f42f` | false | 1 | false |
| `evidence/gate2/r1r2_task1_task2/vkpower_carddriver_journey.json` | `6aedd6a4f42f` | false | 0 | false |
| `evidence/a11e_interpreter_matrix/` (5 artifacts + RESULTS.md) | `d6af7c4` | false | — | — |
| `evidence/gate2/r7_refuse_pack_redteam/NON_AUTHOR_REDTEAM.md` | 7 rounds, `d3ed533` → `2b7604c` | — | — | — |
| `evidence/gate2/T3_GATE_ROLLCALL.txt` | gate output at `e24bcf5` | — | — | — |

The summit and vkpower rows are cited **as measured gaps**, not as crossings.
summit's bundle is refused by the t3 gate and that refusal is part of the claim.

### ⛔ Present but NOT citable — do not read these into the record

| bundle | why |
| --- | --- |
| `evidence/gate2/acme-life/journey.json` | `dirty: true`, head `5c0f511d` — the original inadmissible bundle whose dirty path was **the harness itself**. Superseded by `r3_acme_reproduced`. |
| `evidence/gate2/r1r2_urlpath_overblock/finding.json` | `dirty: true`, head `2164ac3` — probe for a root cause that was **retracted**. |
| `evidence/gate2/r2_summit_counterfactual/` | marked `NOT_ADMISSIBLE.md` — ran against a refuse pack injected from outside the tree; it is stamped clean and **proved the opposite of its claim**. |
| `evidence/gate2/r1_vkpower_live/`, `r2_summit_live/` | head `8c443f2`, superseded by the `6aedd6a` bundles above. |
| `evidence/gate2/vkpower-life-LIVE-seeded/` | head `bbcb6f9e`, predates this arc. |

**Roll-call rule being applied:** any bundle whose provenance is dirty, or whose
head predates the certified SHA, is struck rather than explained. Two of the
strikes above are this programme's own earlier work.

---

## 4 · Ceremony order (from `GATE_5_CEREMONY.md §4`)

1. Freeze the branch.
2. Name **one** 40-hex certification SHA.
3. Clean-clone verification on hardware no author controls —
   `gate5-clean-clone-attestation.yml`, GitHub-hosted runner asserted **first**.
4. Evidence roll-call — §3 above, as a checklist.
5. Read-out — the four items in §2, each accepted in writing.
6. Sign in `GATE_5_CERTIFICATION.md`.
7. Tag the SHA.
8. Unfreeze.

## 5 · Still blocking, and not ours to clear

Four human acts, unchanged and unfillable by any agent: **revoke the exposed
GitHub PAT** · **replace the OpenAI key** (re-measured this run: plain `curl` →
`HTTP 401 invalid_api_key`) · **name the signatories** · **approve the R5′ deploy
window**. `GATE_5_CEREMONY.md` is explicit that no name in the record may be
written by an agent, and the validator can detect an absent human but never a
fabricated one.
