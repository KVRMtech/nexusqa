# Branch reconciliation — scope, not a plan to execute yet

**Status: SCOPING ONLY. Nothing here has been done, and none of it should be
started without the decision in §3 being made first.**

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

- [ ] **Measure the layout delta precisely** — how many of the 447 files would
      move under Option B, and which CI/Docker/script paths hard-code
      `Nexus_power/`. Read-only; produces the estimate Option B currently lacks.
- [ ] **Inventory sign-off state per gate** — which of the five gates on this
      branch are certified, which are in flight. Option A's approver list is
      unknown until this exists.
- [ ] **Confirm branch protection on the target** — a reconciliation that
      force-pushes a protected branch fails late and loudly. Check first.
- [ ] **Rehearse in a scratch clone.** Whichever option is chosen, do it once in
      a throwaway clone and verify CI green there before touching the remote.
      Costs an hour; the alternative is discovering the layout problem on the
      default branch.

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
