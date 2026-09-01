# Branch reconciliation — scope, not a plan to execute yet

**Status: SCOPING ONLY. Nothing here has been done, and none of it should be
started without the decision in §3 being made first.**

> **UPDATE 2026-08-31 (Team H · H1).** §4's four "safe to do NOW" items are now
> done — see **§6**. They found a method (`-s ours`, §6.2) that needs **no
> force-push and no protection change**, and destroys nothing; and they found
> that §2 Option B **cannot execute as written**, because `develop` refuses
> force-pushes even to an admin (§6.1). §3's layout question is measured in
> §6.3 and is not evenly balanced. What remains is consent, not risk.

Written because "merge both branches" appears inside T2 as though it were a
step, and it is not one. It is a separate piece of work with a prerequisite
decision, and Gate 1's own exit record says the same obstacle *"blocks every
other gate on this branch and is currently undocumented."*

---

## 1. The two facts that make this not-a-step

### 1.1 There is no commit range that means "one gate's work"

```
$ git rev-list --count develop..HEAD            ->  107
$ git diff --name-only develop...HEAD | wc -l   ->  447
$ git log --format="%s" develop..HEAD | cut -d: -f1 | sort -u
    gate0  gate1  gate2  gate3  gate4  A11  A11e  cert  ci  feat  fix  test

(Measured 2026-08-22. Gate 1's exit record quotes 55/429 — that was true when
written and the branch has since grown by ~50 commits. Re-measure before
quoting: on this branch these numbers move daily.)
```

The branch is the shared trunk for **five gates and ~9 concurrent sessions**.
Any single gate's commits are a minority of the total, interleaved with work
from squads who have not signed off — some of it explicitly in flight.

> **Merging this branch does not merge A6–A10, or A11, or any other gate. It
> merges everyone.**

### 1.2 The remote branch of record is not a merge target

```
$ git merge-base develop HEAD           ->  ede6bf2…    (shares history)
$ git merge-base origin/develop HEAD    ->  (empty)     NO COMMON ANCESTOR
```

| | merge-base with HEAD | top-level layout |
| --- | --- | --- |
| local `develop` | `ede6bf2` | `Nexus_power/`, `QECentral/` |
| `origin/develop` | **none** | application at repository **root** |

`origin/develop` is a single flattened "Initial commit" that shares no history
with the working branches and carries the application at a different path.
GitHub refuses to open a PR against it at all — which is why `ci.yml` carries a
bootstrap push trigger with a comment saying the histories are unreconciled.

`CLAUDE.md` states the standing rule: *"Do not treat `origin/develop` as a merge
target without a deliberate history reconciliation — that reconciliation is
still open."*

---

## 2. What "merge" could mean — three different jobs

These are not variants of one task. They have different risk, different
approvers and different failure modes. **Exactly one must be chosen before any
work starts.**

### Option A — merge the feature branch into LOCAL `develop`

*Shares history, so this is an ordinary merge.*

* **Carries:** all 107 commits, all five gates, everyone's work.
* **Needs:** sign-off from **every** squad with commits in the range, not just
  the certified ones. Gate 2, 3 and 4 work is included whether or not it is
  ready.
* **Risk:** moderate and reversible (a merge commit can be reverted).
* **Does NOT solve:** `origin/develop` remains unreachable, so this does not
  make anything releasable — it relocates the same 107 commits.
* **Honest verdict:** cheap, and mostly cosmetic. It does not unblock a release.

### Option B — reconcile `origin/develop` with the working history

*The real blocker, and the only option that unblocks a release.*

The two histories are unrelated **and** differently laid out
(`Nexus_power/…` vs application-at-root), so this is a structural exercise, not
a merge:

1. establish which layout is canonical going forward — that is a **product**
   decision about the repository, not an engineering preference;
2. if the working layout wins: `origin/develop` is replaced (force-push to a
   branch nobody has built on, or a new default branch) — **destructive to the
   remote's history**, needs the repository owner;
3. if the root layout wins: every path in 447 files moves, every CI workflow,
   Dockerfile and script path changes, and every open branch conflicts;
4. either way `--allow-unrelated-histories` produces a tree containing both
   layouts unless one side is deliberately emptied first.

* **Risk:** HIGH and partly irreversible. Touches the remote's default branch.
* **Approver:** repository owner (`KVRMtech`), not a squad.
* **Prerequisite:** §3's decision.

### Option C — do neither yet; land gates on the shared branch

* Gates continue to certify and land on `feat/qec-dynamic-catalog-p0-p6`, which
  is where CI already runs green.
* **Cost:** the branch keeps growing; reconciliation gets harder, not easier.
* **Honest verdict:** this is the current de-facto state. It is defensible for
  now and indefensible as a permanent answer.

---

## 3. The decision that must be made first

> **Which `develop` is the branch of record, and which top-level layout is
> canonical?**

Nothing in Option B can be scoped, estimated or safely started until that is
answered, because the answer determines whether 447 files move.

It is not an engineering call. It needs whoever owns `KVRMtech/nexusqa`.

**Second decision, only if Option A:** which squads must sign off, given that
merging carries all five gates. The certified ones (Gate 1 / A11) cannot consent
on behalf of the uncertified ones.

---

## 4. What can be done safely NOW, before any decision

These reduce risk under every option and are independently useful:

- [x] **Measure the layout delta precisely** — **DONE, §6.3.** 2675 files sit
      under `Nexus_power/`; 107 hard-coded path references across 22 files, plus
      `working-directory` defaults in 6 workflows. The estimate Option B lacked.
- [x] **Inventory sign-off state per gate** — **DONE, §6.4.** 894 commits, all
      five gates. Option A's approver list is now known, and it is everyone.
- [x] **Confirm branch protection on the target** — **DONE, §6.1.** It fails
      exactly as this line predicted: `allow_force_pushes: false` with
      `enforce_admins: true`, so a force-push is refused even to the owner.
      Option B cannot execute as written.
- [x] **Rehearse in a scratch clone** — **DONE, §6.2**, and better than asked:
      rehearsed with `git commit-tree`, which touches no branch, no index and no
      working file, so the shared checkout was never at risk. It found the
      method in §6.2 that needs no force-push at all.

**Explicitly NOT safe to do now:** any push to `origin/develop`, any
force-push, any `--allow-unrelated-histories` merge, any path migration.

---

## 5. Recommendation

**Do the four items in §4, then take the §3 decision to the repository owner.**
Do not attempt Option B before that conversation, and do not treat Option A as
progress toward it — Option A moves commits between two branches that are both
already unreachable from the remote's default.

And a sequencing note that applies whichever option wins: **do not promote the
A11e advisory CI jobs to required during the merge sequence.** A newly-required
check reddening mid-merge will be attributed to the merge, and the debugging
will start in the wrong place.

---

## 6. The §4 checklist, done — and a method that changes the risk

**Added 2026-08-31 (Team H · H1). All four items §4 called "safe to do NOW" have
been carried out. They produced a method the sections above did not consider,
and it is neither destructive nor irreversible.**

### 6.1 · §4 item 3 — branch protection on the target: CHECKED, and it bites

`gh api repos/KVRMtech/nexusqa/branches/develop/protection`:

| setting | value |
|---|---|
| `allow_force_pushes` | **false** |
| `allow_deletions` | **false** |
| `enforce_admins` | **true** |
| `required_pull_request_reviews` | **enabled** (0 approvals) |
| `required_status_checks` | 18 contexts, `strict: false` |

§2 Option B assumed a force-push. **A force-push to `develop` will be rejected**,
and `enforce_admins: true` means the repository owner cannot bypass it either.
Option B as written therefore cannot execute at all without first *weakening*
protection on the default branch — opening a window in which the branch is
unprotected. That window is a worse risk than the migration it enables, and
§4 was right to say "check first".

`required_pull_request_reviews` also blocks **direct** pushes. Whatever lands on
`develop` must arrive through a pull request that passes the 18 required checks.

### 6.2 · §4 item 4 — rehearsed, and it needs no force-push at all

A merge commit's *tree* need not be a combination of its parents. `git merge
-s ours` (equivalently `git commit-tree <trunk-tree> -p <trunk> -p <old>`)
records the old snapshot as a **second parent** while keeping the working tree
exactly as it is. Rehearsed with `commit-tree`, which touches no branch, no
index and no working file:

```
trunk tip   : ed5c489209a0e6d69a229dc8553083758185b5b0
old develop : ba4fd8ff572b47eb9a10dd842bf46eeaa0e329e2  (2026-04-14 "Initial commit")
rehearsal   : c3ab76c59706bcfe6b009995a2b7f13c0ddf91c6

1. tree identical to trunk?          YES - zero file differences.
                                     The root-layout snapshot contributes NO files.
2. fast-forward for develop?         YES - old develop is an ancestor.
                                     A NORMAL push. No force-push. No protection change.
3. do feature branches then share
   history with develop?             gate4/phase3-proofs        -> yes (59daad08de2c)
                                     gate5/ceremony             -> yes (bc0f6652a12a)
                                     phase4/entry-gate-remediation -> yes (2fbea5324360)
4. old snapshot preserved?           YES - ba4fd8f stays reachable as an ancestor.
```

This is **Option D**, and it dominates Option B on every axis §2 scored:

| | Option B (force-push) | **Option D (`-s ours` merge)** |
|---|---|---|
| destroys remote history | yes | **no** — old snapshot kept as an ancestor |
| needs protection weakened | yes, a real window | **no** |
| produces a dual-layout tree | only if done carelessly | **no** — tree is byte-identical to trunk |
| reversible | poorly | **yes** — `develop` can be reset; nothing was deleted |
| unblocks pull requests | yes | **yes** — all five branches gain a merge-base |

§2's "**Risk:** HIGH and partly irreversible. Touches the remote's default
branch" was accurate for the method it described. It is not accurate for this one.

### 6.3 · §4 item 1 — the layout delta, measured

§3's question ("which top-level layout is canonical") is answerable on cost now:

| | |
|---|---|
| files under `Nexus_power/` | **2675** |
| files under `QECentral/` | 46 |
| other files at the root | 21 |
| tracked total | **2742** |
| hard-coded `Nexus_power/` references | **107 occurrences across 22 files** (64 in `.github/workflows`, 14 in `scripts/`, 29 in `Nexus_power/scripts/`) |
| plus | `defaults: run: working-directory: Nexus_power` in 6 workflows |

Adopting the **root** layout moves 2675 files, rewrites 107 path references, and
conflicts every open branch. Adopting the **nested** layout moves nothing and
rewrites nothing, because it is what every branch, workflow, Dockerfile and
script already assumes. The only thing carrying the root layout is a single
commit from 2026-04-14 that nothing has ever built on.

**The layout question is not evenly balanced and should not be presented as
though it were.** Nested wins on cost by roughly 2700 files to zero.

### 6.4 · §4 item 2 — what a merge to `develop` would carry

```
$ git rev-list --count ed5c489 --not ba4fd8f
894

feat 302 · fix 245 · docs 53 · gate3 33 · gate4 25 · gate2 22 · test 18
phase4 12 · chore 12 · diag 10 · A11 10 · gate0 9 · ci 9 · gate1 6 · cert 6
```

§1.2's warning stands unchanged and is the **only** remaining blocker: this
carries every gate, certified and uncertified alike. What has changed is that it
is no longer *also* a destructive act — so the decision is now purely about
consent, not about risk to the remote.

Worth stating plainly, because it narrows the consent question: `develop` is
**not a release branch**. Nothing deploys from it today, and since Team H's
`[0/4]` gate landed, nothing can deploy from any branch without a green CI run.
Merging to `develop` therefore ships nothing to anyone; it makes the branch of
record describe reality.

### 6.5 · Recorded method, and what is still owed to §3

**Method of record: Option D.** A branch carrying the `-s ours` reconciliation
commit, pushed to origin, merged into `develop` **through a pull request** so
the 18 required checks adjudicate it. No force-push, no protection change, no
path migration, nothing deleted.

**Canonical layout of record: nested (`Nexus_power/`, `QECentral/`)**, on the
cost measured in §6.3.

Still owed to §3, and still not an engineering call:

- [ ] **Consent to carry 894 commits onto `develop`.** The certified gates
      cannot consent for the uncertified ones (§3, second decision).
- [ ] **Confirmation that `develop` remains the default branch** afterwards.

Sequencing note from §5 still applies: do **not** promote the A11e advisory jobs
to required during the merge sequence.
