# Working in this repository

Everything below was verified in this repository, not assumed. Each item cost
someone real time or produced a wrong result before it was written down.

---

## 1. This checkout is shared by many concurrent agent sessions

At times ~9 sessions have worked in this single working tree at once. Files
appear, change and revert under you mid-task, and `HEAD` moves while you read.
Two consequences you must plan for:

**The git INDEX is shared too.** `git add <paths> && git commit` does **not**
commit only your paths — another session can stage its work between the two
commands, and your commit takes everything staged. This happened four times in
one day across independent sessions, each time discovered afterwards by the
victim or a bystander. It is a property of the setup, not a mistake.

Use a **pathspec commit**, which ignores the index entirely:

```bash
git commit -m "msg" -- <paths>          # flags BEFORE the separator
git commit -F - -- <paths> <<'EOF'      # message on stdin also works
...
EOF
```

Everything after `--` is a pathspec, so `git commit -- <paths> -m "msg"` fails.

A **brand-new file** must be made known to git first — the pathspec still bounds
what the commit takes, so this is safe:

```bash
git add <new-file>
git commit -F - -- <new-file> <other-paths>
```

For a file **another author is also editing**, take only your hunks with
`git apply --cached` into a private index rather than staging the whole file.

**Verify what you committed**: `git show --stat --oneline HEAD`.

**Do not rewrite history.** Other sessions have already built on it. Correct a
mis-attribution in the record, not with a rebase.

**Land work early.** Do not hold a large change to the end of a task — an
uncommitted tree here is one `git clean -fd` away from gone.

---

## 2. Running tests

Match CI's plugin set, or collection dies before any test runs:

```bash
python -m pytest tests --ignore=tests/browser -q -p no:cacheprovider -p no:randomly
```

`pytest-randomly` is present in some global site-packages and is **not** a
project dependency. Via `thinc`'s reseed hook it aborts collection with
`ValueError: Seed must be between 0 and 2**32 - 1`. CI installs only
`pytest pytest-asyncio`, so `-p no:randomly` is the faithful configuration.

**The two services cannot share an interpreter.** `qe-explorer` and `qe-central`
both ship a top-level `app` package. Run each from its own service root
(`PYTHONPATH` set to that root); a single `pytest tests/` across both cannot
work and never could. Cross-service contracts are frozen as data under
`Nexus_power/contracts/` and each side asserts against them in its own process.

**The browser lane is slow and order-sensitive.** `tests/browser` is ~900 tests
and over an hour; `tests/browser/test_coverage.py` alone is ~10 minutes. A
golden test that fails in the lane but passes alone is usually not a stale
golden — check whether another session is rewriting goldens on disk right now
(`git status --short -- .../tests/browser/golden/`).

A **skip is not automatically a hole** here: fixtures declare which lanes can
adjudicate them and each skip states why. `tests/_infra_gate.py` turns a
missing-infrastructure skip into a failure when `QEC_REQUIRE_*` is set, so CI
cannot go green on tests it never ran.

---

## 3. Line endings will corrupt your evidence

`core.autocrlf=true` on every Windows checkout here. This has produced two
real, opposite failures:

* **Fails loudly, misleadingly.** `sha256sum -c` treats a trailing CR as part of
  the *filename*, so a CRLF digest manifest makes every entry fail to open —
  reported identically to genuine drift.
* **Passes misleadingly.** A CRLF `squid.conf` was `docker cp`'d into Squid
  while the evidence claimed to run the repository's bytes. It passed. Squid
  tolerating CRLF is luck, not a verified property.

If you ship a file that is read as *bytes* by a tool or copied into a container,
pin it in `.gitattributes` **by extension, not by filename** — pinning the one
file that already broke leaves the next one broken — and scope the rule to your
own directory. Better still, have the consumer assert on the bytes it actually
loaded and record their digest in the evidence.

---

## 4. There are two different `develop` branches

```
local  develop   merge-base with feature branches: yes   layout: Nexus_power/, QECentral/
origin/develop   merge-base: NONE                        layout: application at repo root
```

`origin/develop` is a flattened single "Initial commit" sharing no history with
the working branches and a different directory layout. GitHub therefore refuses
to open a PR against it, which is why `ci.yml` carries a bootstrap push trigger.
**Do not treat `origin/develop` as a merge target** without a deliberate history
reconciliation — that reconciliation is still open.

Before proposing any merge, measure what it would actually carry:

```bash
git rev-list --count develop..HEAD
git log --format="%s" develop..HEAD | sort | uniq -c
```

The long-lived feature branch is a shared trunk for several gates at once, so
"merge my work" usually means "merge everyone's".

---

## 5. Evidence standards this repository holds to

These are not style preferences; work has been reverted for failing them.

* **A green test proves nothing until you know it can go red.** If an assertion
  is an *absence* ("blocked", "no write", "refused"), include a control that
  removes the guard and requires the thing to happen. Otherwise an unrelated
  failure — a rejected `fetch`, a dead socket — satisfies the test while the
  gate is wide open.
* **Do not certify by re-running the author's tests.** That proves their tests
  agree with their code. Independent verification targets what their design
  *structurally cannot* test.
* **Prove a claim, don't accept it** — including a claim about your own
  artefacts. "Only line endings changed" is checkable: re-hash and compare.
* **Say what is not claimed.** "Implemented and tested, NOT deployed, NOT
  live-proven" is a normal and expected way to close a milestone here.
