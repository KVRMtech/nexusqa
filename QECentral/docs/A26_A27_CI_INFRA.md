# A26 / A27 — CI object storage, and the end of the silent infrastructure skip

**Branch:** `feat/qec-dynamic-catalog-p0-p6`
**Scope:** A26.1, A26.2, A27.1, A27.2
**Status:** implemented and locally proven; the CI run itself is the remaining
proof and is described in [§9](#9-what-is-proven-and-what-is-not).

> ### ⚠️ LANDING THIS: three files must go in ONE commit
>
> `ci.yml` imports two files that are still **untracked**. Committing the
> workflow without them turns the qec-database job red on a missing import —
> and the failure will look like a broken gate rather than a missing file:
>
> * `.github/workflows/ci.yml`
> * `Nexus_power/platform/qe-central/tests/_infra_gate.py` *(untracked)*
> * `Nexus_power/platform/qe-central/tests/contract/test_infra_skip_gate_canary.py` *(untracked)*
>
> plus `scripts/qec_ci_minio_setup.sh` *(untracked)*, which Phase 3b shells out to.
>
> **Do not commit with `git add … && git commit` in this checkout.** The index is
> shared between concurrent sessions and the commit sweeps whatever else is
> staged in it — that is how part of A27.1 landed under someone else's message.
>
> `git commit -m "…" -- <paths>` is *not* sufficient here, for two reasons: it
> does nothing for **untracked** files (three of the four above), and it takes the
> whole file, so it still sweeps a concurrent session's hunk in a file you both
> touched. Use a **private index**:
>
> ```bash
> export GIT_INDEX_FILE="$(mktemp)"
> git read-tree HEAD
> git add .github/workflows/ci.yml >         Nexus_power/scripts/qec_ci_minio_setup.sh >         Nexus_power/platform/qe-central/tests/_infra_gate.py >         Nexus_power/platform/qe-central/tests/contract/test_infra_skip_gate_canary.py >         Nexus_power/platform/qe-central/tests/fleet/test_t_fl_03_object_storage_handoff.py >         Nexus_power/sdk/nexus-sdk/nexus_sdk/storage/base.py >         Nexus_power/sdk/nexus-sdk/nexus_sdk/storage/s3.py >         QECentral/docs/A26_A27_CI_INFRA.md
> git diff --cached --name-only HEAD    # confirm ONLY these appear
> git commit -m "…"
> unset GIT_INDEX_FILE
> ```
>
> Everything up to and including `write-tree` was verified in this checkout: it
> stages exactly those eight paths, untracked ones included, and leaves the shared
> index untouched. The `commit` step itself was not run.
>
> **A FINAL `git reset` IS PART OF THE RECIPE, NOT TIDYING.** Committing through a
> private index leaves the SHARED index pointing at the old HEAD. Once HEAD
> advances, every file your commit ADDED reads as a staged *deletion* there, and
> every file it MODIFIED reads as a staged *revert* — so the next plain
> `git add … && git commit` by any session silently removes your work inside a
> commit about something else. This was observed, not theorised: immediately
> after `0c26853` the shared index held all four new files as `D`.
>
> Resync it **path-scoped**, not wholesale:
>
> ```bash
> git reset -q HEAD -- <the paths you just committed>
> ```
>
> A bare `git reset` also unstages every *other* session's legitimate entries.
> It was safe here only because the index was checked first and held nothing but
> these nine files; that check is not something to rely on in a nine-session
> tree. To test a single entry before touching anything,
> `git show :<path>` against `git show HEAD:<path>` tells you whether that entry
> is a silent revert waiting to happen. Neither form touches the working tree.
>
> **Part of A27.1 has already landed**, swept into two other sessions' commits
> from that shared index: the skip-reason fix to `test_t_fl_01` is in `099a597`,
> and `test_t_fl_06` / `test_t_fl_08` are in `3778c1a`. Those three are done and
> have been through CI — do not re-apply them.

---

## 1. The defect, stated exactly

T-FL-03 is the object-storage manifest handoff that lets an Explorer instance
publish a crawl's evidence and a *different* qe-central pod ingest it. It is what
makes the Explorer fleet horizontally scalable: without it, producer and consumer
must share a filesystem, and in Kubernetes they do not.

Its proof is `platform/qe-central/tests/fleet/test_t_fl_03_object_storage_handoff.py`.
Six of its fourteen tests are gated on a real S3-compatible endpoint. CI provided
PostgreSQL and Redis and **no object storage**, so all six skipped — in every CI
run ever made.

The M0.x no-silent-skip gate existed precisely to stop this, and did not, because
it understood exactly one category of infrastructure:

```python
_DB_SKIP_SIGNATURES = ("QEC_TEST_DATABASE_URL", …, "QEC_TEST_REDIS_URL", "BYPASSES RLS")
```

An S3 skip says `QEC_TEST_S3_ENDPOINT not set`. No signature matched. The gate
looked straight past it and the build stayed green.

So the failure was not "we forgot to add MinIO". It was that **a gate written for
one dependency reads, to everyone downstream, as a guarantee about all of them.**
A26 provisions the service; A27 makes the gate structurally incapable of having
that blind spot again.

---

## 2. What changed

| File | Change |
|---|---|
| `.github/workflows/ci.yml` | MinIO provisioning, S3 env, `QEC_REQUIRE_REDIS`/`QEC_REQUIRE_S3`, `boto3`, named T-FL-03 + canary steps, evidence artifact, teardown |
| `Nexus_power/scripts/qec_ci_minio_setup.sh` | **new** — deterministic object-storage bootstrap and teardown |
| `platform/qe-central/tests/_infra_gate.py` | **new** — the extensible infrastructure-skip registry and its pytest hooks |
| `platform/qe-central/tests/conftest.py` | now a shim that installs the registry's hooks |
| `platform/qe-central/tests/fleet/test_t_fl_03_object_storage_handoff.py` | the S3 gate became two-state |
| `platform/qe-central/tests/contract/test_infra_skip_gate_canary.py` | **new** — 15 arms proving the gate fires, does not over-fire, and cannot be bypassed by an unnamed skip reason |
| `…/tests/fleet/test_t_fl_{01,06,08}*.py` | skip reasons now NAME their DSN variables — 18 tests were invisible to the gate (§5). **Already landed** in `099a597` / `3778c1a` |
| `sdk/nexus-sdk/nexus_sdk/storage/base.py`, `s3.py` | bounded S3 failure — a production defect the first execution exposed (§6) |

Nothing in the PostgreSQL or Redis provisioning was modified. See §8.

---

## 3. A26.1 — provisioning MinIO

### Why it is not a `services:` block

GitHub Actions service containers accept an image, `env`, `ports` and `docker
create` options. They **cannot supply a command**. `minio/minio` ships
`ENTRYPOINT=/usr/bin/docker-entrypoint.sh` and `CMD=["minio"]`, so with no
arguments it prints its help and exits — the server needs `minio server <dir>`.

The common workaround is the floating `minio/minio:edge-cicd` tag, whose `CMD` is
baked to `server /data`. That was rejected. `docker-compose.yml` pins
`minio/minio:RELEASE.2023-03-20T20-16-18Z`, and this repository's CI already
holds the line that CI services must match what is deployed — the Postgres pin
carries the reasoning verbatim: *"a CI that silently floats to 17 would prove the
schema against a server nobody runs."* Proving the object-storage handoff against
a rolling edge build while shipping a 2023 release is that same mistake on a new
axis.

So MinIO is started explicitly by `scripts/qec_ci_minio_setup.sh`, from the same
pinned release, with the same `/minio/health/live` probe the compose healthcheck
uses.

### Readiness is measured, never assumed

There is no `sleep` anywhere in the path. A fixed sleep is either too short (a
flake) or too long (wasted minutes), and it never reports *why* a service is
missing. Three escalating, bounded, fail-loud checks:

1. **the container is running** — otherwise its last 40 log lines are printed and
   the step aborts;
2. **`/minio/health/live` answers** — bounded to 60 one-second attempts;
3. **the S3 API answers with the credentials, and a real `PUT`/`GET`/`DELETE`
   round trip succeeds on the evidence bucket.**

(3) is the one that matters. A live health endpoint proves only that a process is
up. The tests need working credentials and a writable bucket, and discovering a
credential typo as six confusing test failures is strictly worse than discovering
it in one named step.

### The bucket is created explicitly

`nexus_sdk`'s S3 backend lazily creates a missing bucket on first use — and
**swallows the error if that fails**, logging a note and carrying on. Depending
on that would make provisioning invisible and make its failure surface as
something else entirely. The bucket is created by the bootstrap script, verified
with `head_bucket`, and a failure there is fatal there.

### Teardown

`services:` containers are reaped by the runner; a container the job started is
not. `scripts/qec_ci_minio_setup.sh --down` runs under `if: always()`, so a
failing test still cleans up. On a hosted runner the machine is discarded anyway
— this is what makes the same script safe to run repeatedly on a laptop or a
self-hosted runner.

---

## 4. A26.2 — executing T-FL-03 for the first time

### Configuration

Set at job level:

```yaml
QEC_TEST_S3_ENDPOINT: http://localhost:9000
QEC_TEST_S3_BUCKET:   qec-evidence
AWS_ACCESS_KEY_ID:    minioadmin
AWS_SECRET_ACCESS_KEY: minioadmin
QEC_REQUIRE_S3:       "1"
```

Deliberately **not** set at job level: `NEXUS_STORAGE_BACKEND` and the `S3_*`
variables. Exporting those job-wide would repoint the house storage layer for the
entire ~1400-test suite — every screenshot, asset and artifact write in tests
that expect the local backend. The T-FL-03 fixture monkeypatches them per test,
which is the only scope at which they are correct.

### A dependency the first execution exposed

`test_the_manifest_is_published_last` uses a synchronous **boto3** client to read
object `LastModified` timestamps. The service depends on `aiobotocore` (the SDK's
`storage-s3` extra), which pulls `botocore` but **not** `boto3`. On a clean runner
that test would have died at import with `ModuleNotFoundError` — which reads as a
broken test rather than a missing dependency. `pip install boto3` is now in the
job, as a test-only dependency (production is aiobotocore), so it does not enter
`platform/qe-central/requirements.txt`.

This is the kind of thing that only ever surfaces on first execution, and it is
the reason "the tests exist" was never the same claim as "the tests run".

### Execution is *proven*, not inferred

A step that exits 0 is not evidence of execution — **a suite in which every test
skipped also exits 0**, which is the entire defect. So the run writes
`--junitxml` and a following step parses it and asserts:

* all six S3-gated tests were **collected** (matched by name, not by count — a
  count still passes if one test is renamed away and another added);
* all six **passed**;
* **nothing** in the file was skipped.

The XML is uploaded as the `tfl03-first-execution-junit` artifact.

This verification was itself tested in both directions locally: against a real
MinIO run it printed `14 tests collected, 0 skipped` and exited 0; against a
junit file from an endpoint-less run it exited 1, naming all six as `skipped`.

---

## 5. A27 — the generalized gate

### A27.1 — a registry, not a longer `if`

`platform/qe-central/tests/_infra_gate.py` declares infrastructure categories:

```python
InfraCategory(
    key="s3",
    label="S3 / MinIO object storage",
    require_env="QEC_REQUIRE_S3",
    skip_signatures=("QEC_TEST_S3_ENDPOINT", "QEC_TEST_S3_BUCKET"),
    remedy="wire QEC_TEST_S3_ENDPOINT to the CI MinIO service, or unset QEC_REQUIRE_S3",
)
```

Registered today: **Database (PostgreSQL)** `QEC_REQUIRE_DB`, **Redis**
`QEC_REQUIRE_REDIS`, **S3 / MinIO** `QEC_REQUIRE_S3`. Adding a fourth is a
`register_infra_category(...)` call — the detection, the reporting and the canary
are category-agnostic.

Two behaviours are worth calling out explicitly:

**Redis enforcement is a union, not a hand-off.** `QEC_TEST_REDIS_URL` was a
*database* signature before A27.1, and it still is, *as well as* being the Redis
category's signature. Moving it would have quietly weakened the existing gate:
the qec-database job sets `QEC_REQUIRE_DB`, so a Redis skip there would have
stopped being a failure the moment Redis moved to a flag that job did not set.
Either flag catches it.

**Call-phase skips are now caught.** The M0.x gate watched only the `setup`
phase, so a `pytest.skip()` raised inside a test *body* walked straight through
it. Both phases are watched now. A search of the suite confirmed no existing
call-phase skip names an infrastructure variable, so this adds coverage without
changing any current outcome.

The one documented exemption is preserved: a skip naming
`QEC_TEST_PLATFORM_API_URL` is **not** a database failure. `QEC_REQUIRE_DB`
promises the database services, not a live platform-api HTTP server beside them.
A gate that cries wolf gets switched off.

### The two-state test gate

`infra_gate(...)` / `require_infra(...)` mirror the existing `_dbgate` pattern:

* flag unset (laptop) — a missing endpoint **skips**, naming the variable;
* flag set (CI) — the mark stops skipping, the test runs, and `require_infra`
  fails it with a message naming the variable and the remedy.

All six S3-gated tests take the `s3_env` fixture, so one `require_infra` call in
that fixture covers all six.

### A27.2 — the canary

`platform/qe-central/tests/contract/test_infra_skip_gate_canary.py`, mirroring the
RLS coverage canary: deliberately create the exact violation the detector exists
to catch, and fail if the detector stays quiet.

The violation cannot be staged in the session itself — a session that fails
itself is a broken build, not a test. So each arm runs a throwaway pytest session
in a **subprocess**, loading the *real* gate module with `-p _infra_gate`:

```
a synthetic S3-gated test → no endpoint → it skips
    → the REAL gate module
        → inner session exits NON-ZERO
            → the outer test passes because it did
```

`-p _infra_gate` matters: a canary that exercised a *copy* of the detection logic
would only prove that the copy works.

Fourteen arms:

| Arm | Condition | Expected |
|---|---|---|
| 1 | `QEC_REQUIRE_S3=1` + an S3 skip | **RED** — and the report names the category and the test |
| 2 | no flag + the same skip | GREEN — a laptop may still skip |
| 3 | all flags set + an unrelated skip (`opt-in slow test`) | GREEN — no crying wolf |
| 4 | `QEC_REQUIRE_DB=1` + a `QEC_TEST_PLATFORM_API_URL` skip | GREEN — the documented exemption survives |
| 5 | a `rabbitmq` category registered *at runtime* + its skip | **RED** — the registry really is the seam |
| 6 | the same, flag unset | GREEN |
| 7–11 | `QEC_REQUIRE_DB=1` + each of the five DSN signatures | **RED** — M0.x enforcement unchanged |
| 12 | `QEC_REQUIRE_REDIS=1` + a Redis skip | **RED** — the new axis works alone |
| 13 | `QEC_REQUIRE_S3=1` + a **call-phase** `pytest.skip()` | **RED** — the old blind spot is closed |
| 14 | drift: every DSN in `_dbgate.DB_ENV_VARS` is a registered signature | pass |

Arm 1 also asserts the inner session still reports `1 passed, 1 skipped` — the
gate *observes*, it does not rewrite outcomes.

### The audit, and the hole it found

"No silent infrastructure skips remain possible" is a claim that has to be
measured, not asserted. So every skip in the suite was enumerated from a junit
run and classified against the registry: **148 skips, 42 distinct reasons.**

| Class | Count | Verdict |
|---|---|---|
| seen by the gate (database) | 122 | correct |
| seen by the gate (S3) | 7 | correct — six T-FL-03 plus one in T-FL-08 |
| the documented platform-api exemption | 2 | correct — deliberately exempt |
| benign (`prometheus_client is installed`) | 1 | correct |
| **infrastructure-shaped but INVISIBLE to the gate** | **18** | **a hole** |

The 18 were three fleet modules gated like this:

```python
reason="T-FL-01 needs the qecentral + substrate test DSNs"
```

Prose, naming no variable. The gate matches skip reasons by the environment
variable in them, so these were invisible: had the CI database failed to start,
T-FL-01, T-FL-06 and T-FL-08 would have skipped silently under `QEC_REQUIRE_DB`
and the build would still have gone green. **The same defect as T-FL-03, on the
database axis, still open after the gate that was supposed to close it.**

All three reasons now name their variables. Re-running the audit: **0
unclassified infrastructure skips.**

Fixing three strings fixes today. `test_no_skip_reason_describes_infrastructure_without_naming_it`
fixes tomorrow: it parses every test module's AST, finds every literal `skipif` /
`skip` reason, and fails if one talks about infrastructure (DSN, Postgres, Redis,
S3, MinIO, bucket, object storage) without naming a signature the gate can see.
Reasons built by `infra_gate()` / `db_gate()` are exempt by construction — those
build the reason *from* the variable name and cannot omit it. Reverting one of
the three reasons makes this test fail, which is how it was verified.

### A seventh S3-gated test

The audit also found that T-FL-03 is not the only S3-gated module.
`test_t_fl_08_concurrency_redteam.py::test_evidence_handoff_survives_concurrency_and_is_tenant_isolated`
is gated on the same endpoint and had likewise never executed. With MinIO
provisioned it now runs in Phase 6.

It could not be proven through pytest here — its module sits behind a DB gate
needing a live qecentral + substrate database — so its object-storage half was
run standalone against real MinIO: eight concurrent publishes with no
cross-contamination, and a second tenant provably unable to read the first
tenant's evidence under the same crawl id. **Its DB-backed half remains unproven
by this milestone** and belongs to the fleet-suite-in-CI work (A20 / Gate 3).

### A false pass inside the handoff proof itself

`core.autocrlf=true` is set for every checkout here, which means a file without a
`.gitattributes` rule has different bytes on Windows than in git. Every file this
milestone touches is pinned `text eol=lf` (`git check-attr text eol` confirms),
so the *sources* are safe. The **fixture** was not.

`_write_crawl_evidence` built the manifest with `write_text()`, which applies the
platform's newline translation — so the fixture wrote **CRLF on Windows and LF on
Linux**. The laptop proof and the CI proof were running against different bytes,
and whichever one nobody looked at is the one that mattered.

Worse, the headline assertion compared `read_text()` on **both** sides. Both go
through newline translation on the way in, so it could not see a line-ending
change at all — in a **JSONL** file, where the newline *is* the record delimiter.

Measured, not argued. A surgical mutation makes `fetch_crawl_dir` write only the
manifest in text mode — the exact CRLF regression:

| assertion | result under the mutation |
|---|---|
| the original `read_text() == read_text()` | **14 passed** — completely silent |
| the hardened `read_bytes() == read_bytes()` | **1 failed**, naming `b'…

…' != b'…
…'` |

The fixture now writes bytes, the comparison is byte equality, and a second
assertion rejects any CR in the manifest. Nothing else in the file fires on that
mutation, so this assertion is the only thing standing between the repo and a
silently CRLF-corrupted manifest.

The general lesson pairs with §9: **an outage produces false failures; a platform
byte-difference produces false passes.** Only the second kind is silent.

### The same question, turned on this milestone's own checks

Six instances of one class turned up across three squads in a day, and the tell
was identical every time: **would this check still pass if the subject were
absent?** Applied to the checks *this* document is arguing for, it found three:

| check | if the subject were absent | fixed by |
|---|---|---|
| the AST skip-reason guard | a wrong root or a broken glob leaves `offenders` empty → **green having parsed nothing** | assert ≥50 modules parsed, ≥20 literal reasons extracted, ≥10 matching a registered signature |
| the `_dbgate` drift test | an empty `DB_ENV_VARS` makes `missing` empty → **green having compared nothing** | assert the list is real and contains a known DSN |
| the category well-formedness test | every assertion is a loop over `INFRA_CATEGORIES`; an empty registry satisfies all of them | assert `{db, redis, s3}` are present |

Both fixes were mutation-tested: pointing the scan at `NOTHING_*.py` now fails
with *"the scan only parsed 0 test modules"*, and emptying `DB_ENV_VARS` fails
with *"too few to be the real list, so this drift check has no subject"*. Before
the fix each mutation passed green.

Worth stating plainly: this milestone spent a day proving other people's checks
were blind, and its own guard was blind in the same way. The question is cheap
enough to be a habit; not asking it is what makes it expensive.

### The second question: the WRONG subject, not an absent one

"Would it pass if the subject were absent?" does not catch a check that fires on
something merely *resembling* its subject. Asking the companion question —
**could this pass on the wrong object?** — found two more here:

* **`scanned >= 50` is satisfied by any fifty modules.** The guard now anchors on
  a file that must be in scope (`test_t_fl_03_object_storage_handoff.py`), so a
  scan pointed at a directory that resembles this suite fails instead of passing.
* **`read_bytes() == read_bytes()` is satisfied by two EMPTY files.** A handoff
  delivering nothing on both sides is byte-equal. The proof now pins what the
  manifest must *be* — two JSONL records, starting `{"type"` — before comparing.

The anchor earned its place within seconds of being written, by catching a defect
in the guard itself. The scanner did:

```python
except SyntaxError:
    continue          # "a broken file fails elsewhere"
```

**That comment was wrong, and the line was a silent skip of exactly the kind the
guard exists to detect.** A module that will not parse was simply dropped from
the scan, leaving the guard green having never looked at it. It surfaced because
a syntax error was accidentally introduced into the T-FL-03 proof and the scan
reported 158 modules examined *without* the one file this milestone is about.
Unparseable modules are now collected and fail loudly.

Both are mutation-proven: appending `def broken(:` to the T-FL-03 module now
fails with *"a module the scanner cannot read is a module the scanner is silently
skipping"*, and emptying the manifest fixture fails with *"the fixture no longer
produces the 2-record JSONL this proof assumes: b''"*.

### A correction to the CRLF verification rule

The "delta equals line count" heuristic — worth recording because it is now
written down elsewhere — is **wrong as stated**. The invariant is
`disk_size − blob_size == the number of CR bytes on disk`, which is not the line
count whenever a file is already LF or has mixed endings. Measured here:

| file | CR bytes | delta | lines |
|---|---|---|---|
| `qec_ci_minio_setup.sh` | 191 | 191 | 191 |
| `_infra_gate.py` | 0 | 0 | 282 |
| `test_infra_skip_gate_canary.py` | 350 | 350 | 438 |

The canary file is mixed because it was created with one tool and appended to
with another — an ordinary thing to do, and enough to make the line-count form
raise a false alarm on a perfectly healthy file.

---

## 6. The production defect the first execution exposed

`test_a_configured_but_unreachable_store_fails_loudly` passed on its first ever
run — and took **53.6 seconds** to do it.

That is not a test-speed problem. `object_store.ensure_local` sits on the crawl
**ingestion** path and on the reaper's completion-recovery sweep. An
object-storage outage therefore stalled each affected crawl for the better part
of a minute before failing; serialised across a fleet, that is an outage
amplifier rather than an outage.

Two causes, both fixed in the SDK:

1. **No client configuration at all.** botocore's defaults are tuned for AWS over
   the public internet (60 s connect, 5 attempts). A private MinIO/S3 endpoint
   that is up answers in milliseconds, so the long tail buys nothing.
   `StorageConfig` gained `s3_connect_timeout` (10 s), `s3_read_timeout` (60 s)
   and `s3_max_attempts` (3), all env-overridable, and `s3.py` now passes an
   `AioConfig`. **`read_timeout` is deliberately unchanged** — it bounds a single
   read of a *large* object, and shortening it would break big evidence uploads
   to fix a problem those uploads do not have.

2. **`_get_client` tried to CREATE a bucket on a store it could not REACH.**
   `head_bucket` failing for want of a network is a connectivity fault, not a
   missing bucket, and the `create_bucket` that followed bought nothing except a
   second full retry cycle — and a `"bucket creation note"` log line naming the
   wrong problem. A genuine 404/403 still falls through to the create.

Measured on the same box, same endpoint: **53.6 s → 29.7 s**, and the log now
names connectivity. The whole T-FL-03 file went from 62.6 s to 26.6 s. The
absolute numbers are Windows-specific (a refused loopback connect returns
instantly on Linux; there most of the cost is retry backoff) — the CI run will
record the Linux figure. The unbounded-by-default behaviour was real on every
platform.

---

## 7. Why the job was not renamed

`"QE-Central database & tenant-isolation contract"` is a **required status check**
on `develop`. Renaming it would silently retire that check: every future PR would
wait forever on a context that no longer reports.

Object storage was added to that job rather than to a new one for the same
reason — a new job would not be a required check, so the six T-FL-03 tests would
run without gating anything, which is most of the way back to not running. The
widened scope is recorded in the job's header comment and here.

---

## 8. Regression report

| Guarantee | How it was checked | Result |
|---|---|---|
| PostgreSQL provisioning unchanged | `services.postgres` block byte-identical; `git diff` touches no line of it | unchanged |
| Redis provisioning unchanged | `services.redis` blocks in `test` and `qec-database` byte-identical | unchanged |
| `QEC_REQUIRE_DB` enforcement preserved | canary arms 7–11, one per DSN signature | RED as before |
| The platform-api exemption preserved | canary arm 4 | still exempt |
| Existing skip handling still correct | canary arms 2, 3, 6 | still green |
| The gate can still fire | three mutations (§9) | all three caught |
| qe-central suite unaffected | full suite, no services — the `qe-central-tests` job's exact command | see §9 |
| SDK storage change is safe | full T-FL-03 run against real MinIO before and after | 14/14 both times |
| Infrastructure startup deterministic | bootstrap script run twice back to back | idempotent, no sleeps |

---

## 9. What is proven, and what is not

**Proven locally, against a real `minio/minio:RELEASE.2023-03-20T20-16-18Z`:**

* `scripts/qec_ci_minio_setup.sh` starts, health-probes, bootstraps the bucket and
  verifies a `PUT`/`GET`/`DELETE` round trip — and is idempotent across runs.
* All **14** T-FL-03 tests execute and pass, including all **six** S3-gated ones.
* With `QEC_REQUIRE_S3=1` and no endpoint, those six **fail loudly** by name
  (exit 1) instead of skipping.
* Without the flag, they skip cleanly (exit 0) — the laptop path still works.
* The junit evidence step exits 0 on a real run and exits 1, naming all six as
  `skipped`, when fed a junit file from an endpoint-less run.
* All 14 canary arms pass, and the canary catches deliberate sabotage of the gate:

  | Mutation | Result |
  |---|---|
  | remove the S3 category from the registry | 2 arms RED |
  | make `pytest_sessionfinish` report but never set `exitstatus` | 9 arms RED |
  | watch only the `setup` phase (the M0.x blind spot) | 1 arm RED |

**Re-verified after a machine-wide Docker outage.** Midway through this work a
concurrent session created a kind cluster on the same 8GB box and Docker Desktop
failed, killing every container with exit 255. The whole path above was re-run
after recovery and reproduces exactly: 14/14, evidence gate 0, 15/15 canary.

The results were never at risk, and the reason is worth keeping: **an outage
produces false failures, not false passes.** Five of the six S3-gated tests do
real PUT/GET/LIST against MinIO — with the container dead they error, they cannot
pass — so a 14-passed run is itself evidence the service was up.

What the outage *did* corrupt was **timing**. The canary took 408 s during the
contention window against 15–32 s before and 15.0 s after. Anyone reading a slow
canary in a CI log should suspect a loaded runner before suspecting the canary:
its ~17 subprocess pytest sessions cost about a second each on a quiet machine.

**Committed as `0c26853`** (9 files), through the private-index recipe, verified
to contain exactly those files and nothing swept from concurrent sessions. The
committed tree was checked for self-consistency: every file `ci.yml` references
is present in the same commit, the shell script's committed blob begins
`#!/usr/bin/env bash
` with no CR, and the committed `conftest.py` imports the
gate.

**Not pushed.** `git push origin` returns *Permission to KVRMtech/nexusqa.git
denied to Venkatareddy2012* — the SSH key belongs to an account without write
access. An `origin-https` remote and a credential manager exist, but trying a
second identity after an explicit access denial is an owner decision, not an
automatic one. Six commits are unpushed on this branch (three squads').

**Not reproducible from a clean clone yet.** The Gate 4 defect register
(`b533a6a`, `6292518`) cites several findings from this milestone — the
silent-skip scanner, the anchor-over-count rule, the empty-manifest hole. Those
citations are accurate about what was measured, but the guards they describe live
in this working tree and not in any commit, so anyone reproducing from the
register against a fresh checkout will not find them. That is the same shape as
the problem it warns about: **a record that is true only where its subject
happens to live.** Landing the four files in §0 resolves it; until then, this
caveat is the honest form of the claim.

**PROVEN IN CI.** Run `32485861007`, commit `f512fda`, job *QE-Central database
& tenant-isolation contract*:

| step | result |
|---|---|
| Phase 3b — start MinIO and bootstrap the evidence bucket | **success** |
| A27.2 — no-silent-skip canary | **success** |
| A26.2 — T-FL-03 object-storage handoff (first execution in CI) | **success** |
| A26.2 — prove the six S3-gated tests were collected and executed | **success** |
| Teardown — remove the MinIO container | **success** |

**The six tests have now executed in CI.** The evidence step confirmed all six
collected by name, passed, and nothing skipped.

### What the first run found — the point of running it

The previous run (`32483911098`) was red, on a defect no laptop could produce:

```
PermissionError: [Errno 13] Permission denied: '/app'
  LocalStorage.__init__ -> self._root.mkdir(parents=True)
```

`settings.nexus_storage_path` ships as the **container** path
`/app/service/data`. On a Linux runner creating it needs root; on a Windows
laptop the same string resolves to a drive-relative path the developer can
create. It passed locally every time and could only ever fail on the machine
that matters. Fixed in `f512fda`, verified by reproducing an unwritable storage
root locally and confirming the counterfactual — reverting just those lines
reproduces CI's exact error.

**And a false pass underneath it.** Two sibling tests hit the *same*
`PermissionError` and passed, because `backend_name()` swallows construction
errors and answers `"local"`. For a local deployment that is correct; for one
configured `s3` it means `publish_crawl_dir` becomes a no-op returning 0 and every
crawl's evidence is silently never published — this module's own defect, wearing
the costume of a working local install. `is_object_backed()` is now fail-closed
in that one direction, with both directions pinned by tests.

### Still red, and not this milestone's

Phase 6 (the whole suite against live infrastructure) fails with 98
`permission denied for table tenants` errors. `qec_db_bootstrap.sql:103` grants
`SELECT, INSERT ON tenants TO qec_substrate` with the deliberate comment *"No
UPDATE/DELETE — existing tenants are never touched"*, while
`tests/fleet/conftest.py::_clean_fleet` **deletes** tenants through that role. It
is ordering-dependent — an `if tenants:` guard means the DELETE only fires once a
`tfl%` tenant exists, which is why Phase 5 and the dedicated A26.2 step are green
in the same job. The T-FL-03 tests appear among the 98 only because the shared
fleet fixture errors at setup and takes the whole directory with it. Handed to
the fleet-suite-in-CI lane with the recommendation to route the purge through
`QEC_TEST_ADMIN_DATABASE_URL` rather than widen a least-privilege grant on the
table tenant isolation is anchored to. Everything above is the CI
recipe executed step for step on a developer machine, which is the strongest
evidence obtainable before the workflow is pushed — but Linux runner behaviour
(image pull time, loopback connect semantics, the `/tmp` junit path) is confirmed
by the first CI run and not before it. That run is the last acceptance step.

---

## 10. Production readiness

The class of defect this milestone closes is *"a required infrastructure feature
whose tests silently never run."* It is closed by construction rather than by
vigilance:

* every infrastructure category CI provisions has a `QEC_REQUIRE_*` flag set in
  the job that provisions it;
* under that flag a skip is a session failure, listed by name and remedy;
* both skip phases are watched, so neither a mark nor a runtime `pytest.skip()`
  is silent;
* the canary proves the detector fires, and three mutations prove it can still
  detect sabotage;
* a drift test fails if a DSN is added to `_dbgate` and not to the registry;
* the T-FL-03 evidence step asserts *collection and execution* by name, so a
  green step cannot mean "everything skipped".

**The one structural weakness, and what closes it.** The gate matches skip
*reasons* by substring, so a reason that does not name its variable is invisible
to it. That is not theoretical — it was live in three fleet modules and hid 18
tests (§5). Two things now hold it shut: `infra_gate()` builds the reason *from*
the variable name so anything using it cannot omit it, and
`test_no_skip_reason_describes_infrastructure_without_naming_it` reads the
suite's own source and fails on any literal reason that discusses infrastructure
without naming a signature.

**What that guard still cannot catch**, stated plainly: a skip reason that
describes its dependency in vocabulary the guard does not know (`"needs the blob
service"`), or a reason assembled at runtime from non-literal parts. Both are
narrower than what was open this morning, and both would be caught the next time
this audit is run. The stronger form — refusing collection of a hand-rolled
`skipif` in any module that imports an infrastructure client — is the follow-up
if this class recurs a third time.
