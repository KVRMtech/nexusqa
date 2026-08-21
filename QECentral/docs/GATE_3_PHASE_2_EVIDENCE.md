# Gate 3 — Phase 2 Evidence

Fixture proof → real-crawl proof → deployed-build proof.

Every claim below answers the four evidence questions: **what was tested**,
**was it real**, **what proves it**, **who reproduced it**. Where a milestone is
incomplete or a claim is narrower than the brief asks for, this document says so
in those words rather than rounding up.

**Branch:** `feat/qec-dynamic-catalog-p0-p6`
**Reality note:** this checkout is shared with several concurrent sessions
landing Gate 1, Gate 2, Gate 4 (A26/A27) and Gate 5 work. Where an obstacle or a
failure belongs to one of those, it is named as theirs rather than absorbed.

---

## Status at a glance

| WP | Claim | Evidence class reached | State |
|---|---|---|---|
| A20 | `qec_019` round-trips in the CI database | **CI** | ✅ green |
| A21 | Two real crawls, three deliberate changes, three correct classifications | **real crawl + CI** | ✅ producer 8/8; **CI re-crawled and the stamp matched byte-for-byte**; consumer 3/3 |
| A22 | A really-discovered journey compiles and protects behaviour | real crawl attempted | ⛔ **BLOCKED** — no app in the repo is both walkable and backend-calling |
| A23 | Real-application network trace with correct action joins | **live deployment** | ✅ 10/10 on 68 real events, 2 defects fixed |
| A24 | M2.6 capture against a live tenant | **live tenant** | ✅ 9/9 capture + 2/2 persisted; 1 defect pinned |
| A25 | M2.1 passes on the deployed artifact | — | ⛔ **NOT ATTEMPTED** — A22 blocked, CI not green, and M2.1 has no deployed-services variant |

---

## A20 — Migration `qec_019` beyond a local test DB ✅

### What was tested

Migration `qec_019` (`M2.2 / T-BR-01..05 — the catalogue becomes a reviewable
evidence artifact`), which adds five columns to `catalog_questions`:
`depends_on`, `locator`, `options_total`, `business_rule_state`,
`business_rule_evidence`.

### Was it real

**CI.** GitHub Actions run
[`32441252806`](https://github.com/KVRMtech/nexusqa/actions/runs/32441252806),
job *QE-Central database & tenant-isolation contract*, against the job's
`postgres:16-alpine` service — the same minor version the deployed stack runs.
Driven by the real `python -m alembic` command line, not the programmatic API,
so what CI proves is the command an operator types.

Local reproduction was on a `postgres:16-alpine` container for parity; the
accepted evidence is the CI run.

### What proves success

Named CI step **“A20 — qec_019 up → down → up, in the CI database”**:

```
platform/qe-central/tests/contract/test_qec019_round_trip.py::test_qec019_round_trips_with_its_table_still_standing PASSED
platform/qe-central/tests/contract/test_qec019_round_trip.py::test_qec019_downgrade_is_not_a_no_op                  PASSED
platform/qe-central/tests/contract/test_migration_roundtrip.py::test_head_revision_round_trips                      PASSED
platform/qe-central/tests/contract/test_migration_roundtrip.py::test_full_chain_round_trips_to_base                 PASSED
platform/qe-central/tests/contract/test_migration_roundtrip.py::test_every_revision_declares_a_downgrade            PASSED
============================== 5 passed in 12.73s ==============================
```

The full contract suite in the same job: **217 passed, 2 skipped**. This is also
the first time `qec_018`…`qec_022` have ever been applied in CI — the branch had
not been pushed since `qec_017` was head.

The round trip is the one A20 specifies, at `qec_019`’s own revision with
`catalog_questions` still standing:

```
qec_018 (before state) → seed a row
   ↓ qec_019 UP        → validate: 5 columns, types, nullability, server
                          defaults; row intact; RLS still FORCEd
   ↓ qec_019 DOWN      → validate restoration: columns gone, fingerprint
                          IDENTICAL to the before state, row still intact
   ↓ qec_019 UP        → final validation: fingerprint identical to the first
                          UP, row still intact
```

### Two defects this milestone found

**1. The head round-trip gate had been red since `qec_018`.**
`test_head_revision_round_trips` named `ix_qe_explorations_status_updated` and
asserted “exactly one index removed” — the objects `qec_017` owns, correct when
`qec_017` *was* head. Five revisions later `downgrade -1` steps back over
`qec_022`, the `qec_017` index is still present, and the assertion fails. It went
unseen because CI had only ever seen the chain that made the pin accidentally
true.

The module's own docstring warned about this failure mode and `_chain_head()`
was written to avoid it *for the revision*; the objects were pinned anyway. They
are derived now — `upgrade` and `downgrade` must be inverses — which is
head-agnostic and stronger than counting one index.

**2. `qec_019` is invisible to both existing round trips.** It stopped being head
at `qec_020`, and the walk to base cannot see it: a later revision's downgrade
dropping `catalog_questions` hides an empty `qec_019.downgrade()` completely.

### Anti-green-wash: both gates were mutation-probed

| mutation | result |
|---|---|
| a scratch migration whose `downgrade()` is `pass`, at head | **RED** — names the revision |
| `qec_019.downgrade()` replaced with `pass` | **RED** — names all five orphaned columns |
| `qec_019.downgrade()` that drops the columns correctly but destroys the rows | **RED — and only the seeded-row assertion catches it** |

The third is why the data validation earned its place: the structural check
passes a downgrade that loses every tenant's catalogue.

### Who reproduced it

Author ran it locally on `postgres:16-alpine`; **GitHub Actions reproduced it
independently** on a clean runner with a freshly bootstrapped database. The CI
run is the acceptance evidence, not the local one.

### Reproduce it yourself

```bash
docker run -d --name pg16 -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=postgres -p 55416:5432 postgres:16-alpine

cd Nexus_power/platform/qe-central
QEC_TEST_ADMIN_DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:55416/postgres" \
  python -m pytest tests/contract/test_qec019_round_trip.py \
                   tests/contract/test_migration_roundtrip.py -v
```

---

## Defects found outside A20's scope, fixed because they blocked its CI job

Pushing twelve commits at once put this branch through CI for the first time in
three days. Four independent things fell out. None is A20; all four were red in
the pipeline A20's evidence has to be green in.

| # | Defect | Where |
|---|---|---|
| 1 | `INSERT INTO tenants (tenant_id)` against a `NOT NULL` `name` column — **19 M3.3 fleet tests** had never met a schema built from the migration chain | `tests/fleet/` ×5 |
| 2 | Two `select(JourneyBranchRow)` calls with **no tenant filter** in a shared CI database reached through a superuser DSN (RLS bypassed), so a dict keyed on option label took another tenant's row — passes alone, fails in the suite | `tests/test_journey_graph.py` |
| 3 | **A leap-day persona contradicts itself**: `_reference_date_for` fell back to 28 February, the day *before* the birthday, so `age` and `date_of_birth` disagreed. Reads as flake and is not — the birth date comes from `date.today()`, so a seed only lands on 29 February on some days. Green 18 Aug, red 21 Aug, no code change | `app/fill_engine/persona.py` |
| 4 | The jsdom lane installed pytest but not the service; `-m jsdom` deselects modules but pytest still **imports** them, so one module reaching `pydantic` killed collection for the whole lane | `.github/workflows/ci.yml` |

**And a fifth, which defect 1 uncovered by moving the failure.** Once the fleet
seed could insert a tenant, those 19 tests ran far enough to reach their own
autouse cleanup fixture — which does `DELETE FROM tenants` through the
**substrate** role. The production bootstrap grants that role `SELECT, INSERT`
only, with an explicit *“No UPDATE/DELETE — existing tenants are never touched.”*
So Phase 6 failed with 98 `InsufficientPrivilegeError: permission denied for
table tenants`.

It is ordering-dependent, which is why it hid: the fixture only deletes `if
tenants:`, so the dedicated single-file steps pass (no `tfl%` tenant exists yet)
and only the combined Phase 6 run trips it. Running the fleet file alone will not
reproduce it.

Fixed **test-side**, not with a grant — at the second attempt.

> **The first fix swapped one failure for another, and my verification could not
> see it.** I routed the purge through `QEC_TEST_ADMIN_DATABASE_URL`. Privilege
> errors went 294 → 0 and I called it done. But that DSN is superuser on the
> **maintenance** database (`.../postgres`) — its documented job is CREATE/DROP of
> throwaway databases — and `tenants` lives in `nexus`. Ample privilege, wrong
> database: 490 `relation "tenants" does not exist`.
>
> The verification is the real lesson. I checked *absence of the old error*:
>
> ```
> admin DSN unset → InsufficientPrivilegeError
> admin DSN set   → 0 privilege errors        ← true, and the fixture still failed
> ```
>
> A check scoped to "no `InsufficientPrivilegeError`" is satisfied by any *other*
> exception. **Would this still pass if the subject broke in a new way?** Here,
> yes — which is the same vacuity class as the nine passes below, this time in my
> verification method rather than in a test.
>
> The assertion that cannot be fooled is *presence of success* — the pre-existing
> row is actually gone:
>
> ```
> substrate role (original)     tfl_probe still present = 1   purge did NOT run
> superuser, WRONG db (v1)      tfl_probe still present = 1   purge did NOT run
> superuser on substrate (v2)   tfl_probe still present = 0   PURGE COMPLETED
> ```
>
> Corrected to `QEC_TEST_DATABASE_URL` — superuser **on nexus**. The constant is
> named `SUPERUSER_SUBSTRATE_DB_URL` because the name has to say which *database*
> it must point at; privilege was never the part that was hard to get right.
> Caught by nexusqa-e3. Widening `qec_substrate` to DELETE on
`tenants` — the table tenant isolation is anchored on — to make a cleanup fixture
convenient would undo a boundary someone drew deliberately. The purge now uses
the admin DSN that already exists for exactly this class of work, falling back to
the old path when it is unset. Reproduced against a role holding the production
grant verbatim (`permission denied`), and verified through the real fixture: 0
privilege errors with the admin DSN set. Diagnosed by nexusqa-e3.

Defect 3 is a product defect, not a test defect: a generated persona would fill a
real application with an age and a date of birth that contradict each other, and
the carrier's rejection would then be reported as the application's fault —
precisely the failure `fill_engine/persona.py` exists to prevent.

---

## A caveat on the committed evidence, found by auditing my own commits

**The recorded coverage accounts contain a key that no committed code emits.**

`evidence/a21_catalog_diff/*.json`, `evidence/a22_generation/coverage.json` and
`evidence/a24_live_capture/coverage.json` all carry `unblock_irreversible`. That
field comes from an **uncommitted** change to `app/coverage.py` in this shared
checkout (a concurrent session's Gate-1 work). The crawls that produced this
evidence ran the working tree, so they recorded it.

Consequences, stated exactly:

* **No gate is affected, and one of them proves it.** A21's guard compares only
  the substance fingerprint — questions, types, answer sets — so when CI
  re-recorded the crawl from a *clean clone of HEAD* (which does not emit the
  key) the stamp still matched byte for byte. Had that guard compared the
  coverage bytes, as it originally did, it would have failed on this alone.
* **A23/A24's sha256 pins are self-consistent**: they hash the committed
  artefact and are checked against the committed artefact.
* **But "reproduce it yourself" will not byte-match.** Re-running a producer
  from a clean clone of HEAD yields coverage *without* `unblock_irreversible`.
  The substance is identical; the bytes are not. Once the Gate-1 coverage change
  lands, this resolves itself.

---

## Provenance: three of my commits carry other squads' work

Recorded because a reader of this document will otherwise take these commits to
be what their messages say. The shared checkout has ONE git index, so
`git add <paths> && git commit` picks up whatever a concurrent session has
already staged. Nothing was lost or reverted; it landed under the wrong message.

| my commit | carries | whose |
|---|---|---|
| `099a597` | A27.1 skip-reason hunk in `test_t_fl_01_durable_queue.py` | nexusqa-e3 |
| `3778c1a` | same hunk in `test_t_fl_06`, `test_t_fl_08` | nexusqa-e3 |
| `7d79739` | the `gate2-journeys` job + the summit-life-carrier port work in `browser-harness.yml` | nexusqa-b3 |

Both owners were told directly and both chose **not** to rewrite pushed history in
a tree nine sessions are writing to; e3 has corrected their write-up to record
those three files as landed rather than pending.

Two consequences that are live rather than cosmetic:

* **`7d79739` is not only what its message says.** The proving-ground matrix
  gained an explicit per-image `port` field and a new `gate2-journeys` job, and
  neither is Gate-3 work.
* **`gate2-journeys` is at HEAD but has never reported.** It must not be promoted
  to a required check until it has a green run — a required check that has never
  run blocks every PR forever waiting for it.

Note that `git commit -- <paths>` does **not** prevent this when the peer's hunk
is in a file you are legitimately committing; only a private index
(`GIT_INDEX_FILE` + `git read-tree HEAD`) or a separate worktree does. Every
commit here from `2c27a67` onward used the private index.

> **The private index is only half the recipe, and the missing half is what arms
> the trap for everyone else.** A private index protects *your* commit and leaves
> the SHARED index stale — read from a HEAD that no longer exists. Once HEAD
> advances, a file present in HEAD with no shared-index entry reads as
> **deleted**, and a stale entry reads as **revert this**. The next plain
> `git add … && git commit` then silently carries those deletions inside a commit
> about something else.
>
> Measured by nexusqa-e3 immediately after landing A26/A27 through a private
> index: the shared index held their four new files staged as deletions and five
> modified files at pre-commit content. Nobody staged that; it is mechanical.
>
> So the recipe ends with a bare **`git reset`** — no pathspec, and never
> `--hard`, which would destroy other sessions' uncommitted work. Index only,
> working tree untouched. That step is load-bearing, not housekeeping, and
> anyone following the recipe without it arms the trap on every commit.

---

## A cross-tenant egress finding, surfaced by making the fleet suite runnable

**Not Gate 3, not fixed here, and not a flake.** Recorded because it is a
tenant-isolation defect that only became visible once these tests could run, and
because the obvious response — rerun it — would bury it.

`test_t_fl_08_concurrency_redteam.py::test_n_concurrent_crawls_multi_tenant_overlapping_domains`:

```
EGRESS FENCE VIOLATION on w0_1104b771: a crawl for tfl08_t0_1104b771 was
fenced with another tenant's destination(s) ['tfl08_t2_1104b771.example']
— concurrent dispatch clobbered a live fence
```

### The mechanism, read out of the production path

```python
# routers/explorations.py:1487
_write_egress_allowlist(allowed_hosts, worker["allowlist_path"])   # per-WORKER file
result = await explorer_client.dispatch_crawl(...)                 # await → yields
```

* the egress allowlist is a **per-worker file**, overwritten at every dispatch;
* `acquire_slot` admits **`capacity`** concurrent crawls on one worker;
* the write is followed immediately by an `await`, and **no lock** serialises
  write-then-dispatch per worker.

So two crawls for different tenants dispatched to the same worker interleave:
A writes its allowlist, yields, B overwrites the same file, and the browser
running A's crawl is fenced by **B's** destinations.

### Severity, stated precisely

`capacity` has a schema default of **1** (`qec_022`), and at 1 the window never
opens. The defect is **latent at the default and live the moment any worker is
registered with `capacity > 1`** — which the schema, the registry API and the
scheduler all support as first-class configuration. Nothing refuses it, and
nothing warns.

This is adjacent to M3.3's recorded *“config-only cross-tenant egress leak
(shared allowlist file)”*: that fix moved the fence from one shared file to one
file **per worker**. Per-worker is still shared across concurrent crawls **on**
that worker.

### Why it is not fixed here

Every repair is an M3.3 architecture decision with a matching change on the
explorer side — per-crawl fence files (the right fix, needs a protocol change so
the worker reads the right file for the right crawl), a per-worker dispatch lock,
or refusing `capacity > 1` until the fence is per-crawl. Re-architecting another
milestone's isolation model from an evidence gate is not this gate's call, and it
is the same restraint applied to the next-action re-key and the catalogue
duplication.

### The sharpest part: the docstring asserted the guarantee

`_write_egress_allowlist`'s own docstring said:

> *“Each worker has its OWN file (per-worker egress isolation); a shared file
> would be raced by concurrent crawls and break the fence.”*

It **names the exact hazard** — raced by concurrent crawls — and then concludes
per-worker files prevent it. Per-worker prevents racing *across* workers. It does
nothing about concurrent crawls *on* one worker, which is the case the sentence
literally describes, because that file is shared between them. The code reasoned
about the right hazard in the wrong dimension and documented the conclusion as a
property it does not have.

This is the day's defect class in its most expensive form. A blind check reports
green; **a comment asserting a guarantee is what the next reader trusts instead of
re-deriving it.** Anyone auditing egress isolation would have read that sentence
and stopped.

**Corrected in place** — behaviour untouched, because correcting a false claim is
not choosing between the three repairs.

> **And “comment-only” turned a CI job red — because a check read comments as
> source.** `test_crawl_quota_enforcement_m34` asserts structurally that every
> dispatch route funnels through the guarded choke point. It called `ast.parse`
> and then discarded the structure, substring-matching `ast.dump(node)` — which
> renders docstrings, since a docstring is an `ast.Constant` in the body. My
> corrected docstring illustrates the defect with the line
> `await explorer_client.dispatch_crawl(...)`, and the test read that **mention**
> as a **route**.
>
> One weakness, both directions: a real dispatch reached through an alias also
> never puts the literal text where a callee-match can see it. Too loud and too
> quiet for the same reason.
>
> **My first fix made it worse in the quiet direction, and only a mutation probe
> showed it:**
>
> | probe | old (substring) | fix v1 (callees) | fix v2 (references) |
> |---|---|---|---|
> | direct `client.dispatch_crawl(...)` | caught | caught | **caught** |
> | aliased `_f = client.dispatch_crawl` | caught *by accident* | **MISSED** | **caught** |
> | mention in a docstring | **false positive** | ok | **ok** |
>
> Matching `ast.Call` callees fixed the false positive and lost the aliased case
> the old version caught incidentally — one blindness traded for another. The
> landed version matches **name references**: an `ast.Attribute` or `ast.Name` is
> code however it is later used, and a mention in a string is an `ast.Constant`,
> which is neither.
>
> The general lesson is narrower than “verify your fixes”: **“comment-only” is
> only true if nothing downstream treats comments as source.** Here something
> did. *(Caught by nexusqa-e3.)* The docstring now states what per-worker
files do and do not prevent, that the defect is latent at `capacity=1` and live
above it, and which test proves it. *(Spotted by nexusqa-e3.)*

### The cheapest honest interim, if the owner wants one

Refuse `capacity > 1` at registration with a message naming this defect. A few
lines, reversible, removes the live path without pretending the fence is fixed,
and cannot be mistaken for the real repair. **Not done here** — it removes a
documented feature, which is the owner's call. *(nexusqa-e3's suggestion.)*

**What it does need is a person, not a retry.** A green rerun is not evidence the
isolation holds — it is evidence the race did not fire, and an intermittent
cross-tenant leak is worse than a deterministic one because isolation then depends
on timing. Diagnosed with nexusqa-e3, who flagged it rather than absorbing it.

---

## Known red, and NOT mine — stated rather than absorbed

**`qe-explorer-characterization` — 28 stale browser goldens.** Commit `3420d88`
added `journey_crossings` and `journeys_walked` to the manifest and re-recorded
the `f1` characterization goldens but not the 28 `tests/browser/golden/manifest_*.json`
files. The committed tree cannot pass its own characterization gate.

Not re-recorded here on purpose: a concurrent session is mid-flight on
`app/coverage.py` and `app/walker.py` (adding `unblock_irreversible` to the
coverage payload, with four `.golden` files already re-recorded in the working
tree). Re-recording now would produce goldens that are wrong the moment that
session commits.

**`test_t_fl_03_object_storage_handoff.py::test_producer_key_layout_matches_the_sdk_build_key`** —
the A26/A27 owner has an uncommitted fix in the working tree.

**`Crawl summit-life-carrier` — the crawl reaches only the sign-in screen.**
Surfaced by this gate rather than caused by it: `browser-harness.yml` had never
run on this branch at all (see A21), so this lane's first-ever execution is what
found it.

```
AssertionError: [summit-life-carrier] the crawl discovered 1 page state(s)
but only 2 form signals and 0 actions — fewer than the 5 required.
```

One page state is the sign-in screen. This is an **auth** defect, root-caused by
nexusqa-b3, whose fix is uncommitted — so CI runs without it. `acme-life` and
`vkpower-life` pass in the same matrix, on the same runner, in the same run.

> ### CORRECTION — I recorded the wrong cause here, and the mistake is instructive
>
> This section previously said the application *“never served on :8099”* within
> its 60-second wait. **That is false. It serves in two seconds.** I read this
> line out of the CI log as the failure firing:
>
> ```
> ::error::summit-life-carrier never served on :8099
> ```
>
> GitHub Actions **echoes every line of a `run:` block before executing it**, so
> both branches of a wait loop appear in the log whichever one actually ran. The
> tell is in the escape codes, and it was in output I printed myself and read
> straight past:
>
> ```
> ^[[36;1m    echo "summit-life-carrier is serving after ${i}s"^[[0m   <- echo: escapes, ${i} unexpanded
> ^[[36;1mecho "::error::summit-life-carrier never served..."^[[0m     <- echo
> summit-life-carrier is serving after 2s                            <- REAL: no escapes, expanded
> ```
>
> I had both facts — the echoed error and the crawl assertion — and combined them
> into a causal story that was wrong: *the app never started, therefore the crawl
> only saw the login page.* The app started fine. The crawl stops at sign-in for
> an unrelated reason.
>
> This is the companion to the vacuous-pass findings below, pointed at **reading**
> evidence instead of **writing** checks. Those asked *“could this check pass on
> an absent subject?”*; this asks *“could this line be something that merely
> RESEMBLES evidence of failure?”* Same rule, two activities. Caught by
> nexusqa-b3, verified independently by nexusqa-9e, re-verified here from the log
> before correcting.

---

## A21 — Real catalog diff

### What was tested

`catalog_diff.diff_catalogs`, reached through the whole production path
(`journey_fold.fold_crawl` → `build_app_master_catalog` →
`persist_catalog_version` → `diff_latest_versions`), against **two real crawls of
one application that was deliberately changed in three ways between them**.

### Was it real

**Real crawl.** Two crawls of `proving-grounds/acme-life` in real headless
Chromium, through the production `Crawler` and `PlaywrightBrowserPort`, with a
real verified walk authorization and the production refuse pack. No fixtures.
The recorded coverage accounts are byte-for-byte what the crawler built.

The consumer half deliberately has **no fixture fallback**: if the recording is
absent it fails and tells you to run the producer.

### The three deliberate changes

| classification | change made to the application | why this one |
|---|---|---|
| `removed` | deleted the **“SSN (synthetic)”** field | nothing in the app reads `#ssn`, so removing it leaves a *working* application — a broken app is not a changed app |
| `added` | added an optional **“Occupation”** field | optional on purpose: a new *required* field changes what the funnel must fill, making a later failure ambiguous |
| `changed` | **“State”** gains a fourth option (`NY`) | the only one that tests the catalogue's identity model |

`changed` is the hard one, and its size was chosen from a **measurement**, not a
guess:

```
3 options -> option_shape "few"  -> signature 60f388bc5306ec74
4 options -> option_shape "few"  -> signature 60f388bc5306ec74   SAME
7 options -> option_shape "many" -> signature 90590b6cc2763894   DIFFERENT
```

`field_signature._option_shape` buckets an answer set by size so a 12-country
dropdown and a 195-country one are the same field. One extra option stays inside
the bucket, so the `question_id` survives and the diff can say
`options_changed`. Four more would cross into `many`, mint a new id, and report
the same real-world change as a removal plus an addition. That is a real,
defensible property of the identity model — recorded because a future reader
adding a fifth option would otherwise turn `changed` into `added` with no idea
why.

### What proves success

**Producer — 8/8 on two real crawls** (`7m43s`, real Chromium):

```
test_the_baseline_crawl_reached_all_three_questions            PASSED
test_the_second_crawl_no_longer_sees_the_removed_question      PASSED
test_the_second_crawl_sees_the_added_question                  PASSED
test_the_changed_question_gained_its_option_and_kept_its_name  PASSED
test_the_surviving_questions_are_still_asked                   PASSED
test_exactly_three_questions_moved                             PASSED
test_both_crawls_are_conclusive_enough_to_diff_on              PASSED
test_the_evidence_is_written_for_the_consumer_half             PASSED
```

**The classification, read out of the real evidence** through the catalogue's own
`extract_controls` + `question_id_for`:

```
baseline questions : 15
after questions    : 15
ADDED   : q_a9e65476  'Occupation'
REMOVED : q_deb713bd  'SSN (synthetic)'
CHANGED : q_8790b5be  'State'  ['CA','FL','TX'] -> ['CA','FL','NY','TX']
unchanged: 13
```

The `State` question carries **the same `question_id` on both sides** — that is
the `changed` claim, not an inference about it. `q_deb713bd` is also the id the
independent M2.3 crawls gave “SSN (synthetic)”, so identity is stable across
separate crawls of the same application.

**Consumer — 3/3 against a real Postgres**, through the whole production chain
(`fold_crawl` → `build_app_master_catalog` → `persist_catalog_version` →
`diff_latest_versions` → `diff_catalogs`):

```
baseline crawl  : a21-baseline       (15 questions)
second crawl    : a21-after-change   (16 questions)

ADDED:    q_a9e6547640cd56f6  'Occupation'
REMOVED:  q_deb713bdc6328a09  'SSN (synthetic)'
              lifecycle        : retired
              retired_in_crawl : a21-after-change
              last seen in     : a21-baseline
CHANGED:  q_8790b5befc3bdbc0  'State'  kinds=['options_changed']
              options: ['CA','FL','TX'] -> ['CA','FL','NY','TX']
UNCHANGED (13)
```

### The control group

Five questions — one from each page the funnel walks — are asserted to survive
all three changes, and the consumer additionally requires every non-target
question to land in `unchanged`. Without this, `removed` could be produced by a
crawl that simply saw less and `added` by one that saw more: a diff over two
crawls of *different depth* is not a diff of two versions of an application.

### A finding the real evidence produced, kept rather than smoothed over

**Changing a page's FORM re-keys that page's *button-space* question.**

Besides the questions an application asks, the catalogue carries one derived
pseudo-question per page — `"Next action"`, the classified button space at a
decision node. Its identity is

```python
digest = sha256(sorted(option labels) + "@" + node_fingerprint)   # crawl_constants.py
```

The node fingerprint moves when the page's controls move. So removing
“SSN (synthetic)” and adding “Occupation” changed the application page's
fingerprint, and **the same five buttons arrived under a new `question_id`**:

```
nextaction:20b8a7c3bd99a82  ->  nextaction:1779cec30bf7044
options (identical on both sides):
  ['Apply', 'Review & bind', 'Optional riders',
   'Policy replacement disclosures', 'Continue to review']
```

The diff therefore reports an `added` question the application never gained,
whose answer set is indistinguishable from one already in the catalogue — and it
is added without a matching `removed`, so the catalogue keeps both.

The binding to the fingerprint is deliberate (“keeps two different pages'
identical button sets distinct”), and changing it is a decision about state
identity across every milestone — **not A21's to make unilaterally**, especially
with concurrent sessions working in exactly that area.

What A21 does instead of hiding it: the three classifications are asserted over
the **application's own questions**, and the next-action movement is asserted
*separately and exactly* — `added <= 1`, `removed == 0`, `changed == 0`, with the
observed re-key printed in the CI log. The test goes red if that noise ever grows
beyond the one re-key measured here, rather than quietly absorbing more of it.

**Recommended follow-up (not done here):** key the next-action digest to the
page's *route identity* rather than its full node fingerprint. The stated intent —
distinguishing two different pages with the same buttons — is served by route
identity; binding to the form's contents buys nothing and costs a phantom row in
every change report that touches a form.

### A defect this milestone found: an evidence guard that could never pass

M2.3's CI step diffed the whole `evidence/m23_retirement` directory and failed if
anything moved. Measured: **re-running that producer against an unchanged
application with an unchanged crawler rewrote 117 lines.** Every one was an
ephemeral `FixtureServer` port or a wall-clock `*_ms` field.

A third class showed up too, and it is the more interesting one:
`state_fingerprint_after` changed between runs (`submit_43f2cb0b…` →
`submit_3fccfcfa…`) — **state fingerprints are port-dependent**, so they are not
reproducible across machines either.

A byte-equality guard over those files can therefore only ever fail, and a guard
that always fails is a guard someone deletes. Both A21's and M2.3's guards now
compare `stamp.json`, which carries the substance: for A21, the questions each
crawl asked with their types and answer sets. It is stable across runs and still
moves the instant the proving ground or the crawler produces different coverage —
which is all the guard was ever for.

### CI placement, and a second defect

The proving-ground producers each drive two real crawls (M2.3 ~7 min, A21
~8 min). They belong to `browser-harness.yml`, whose Chromium lane budgets for
them (raised 45 → 75 minutes here).

Left in `ci.yml`'s fast `qe-explorer-browser` lane they do not merely slow it
down — **they silence it**. On run `32441252806` that step was *cancelled* at the
job's 25-minute limit, and a cancelled job reports nothing about the ~600 fast
browser tests it exists to run. That lane now runs
`-m "not characterization and not proving_ground"`.

⚠️ **`browser-harness.yml` does not trigger on this branch.** Its `push` filter
is `[main, develop]`. The M2.3 producer step has therefore never run in CI, and
neither has A21's, until dispatched manually via `workflow_dispatch`.

### Who reproduced it

**The crawl** was run once from the author's machine — it needs the network and a
credential, so CI cannot run it.

**The verification was reproduced independently.** `test_a23_live_network_evidence.py`
lives in the fast `qe-explorer-tests` job, so GitHub Actions re-executed all ten
assertions against the committed manifest on a clean Linux runner
([run `32446412929`](https://github.com/KVRMtech/nexusqa/actions/runs/32446412929),
`1967 passed, 4 skipped`) and printed the endpoint map into its own log.

That is verification of the *evidence*, not a second live crawl — stated as such.
The manifest's sha256 is pinned in `stamp.json` and checked on every run, so the
artifact CI verified is provably the artifact the crawl produced.

### Reproduce it yourself

```bash
cd Nexus_power/engines/qe-explorer
python -m pytest tests/browser/test_a21_catalog_diff_regression.py -v    # ~8 min, real Chromium

cd ../../platform/qe-central
QEC_TEST_DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/nexus" \
  python -m pytest tests/contract/test_a21_catalog_diff_regression.py -v -s
```

---

## A23 — Real-application network evidence

### What was tested

The M2.5 network-evidence stack — capture, `endpoint_inventory.build_inventory`,
and the action↔endpoint join — against a **live deployed application**, not
fixture `30-network-retry-poll-ratelimit`.

### Was it real

**Live deployment, over the public internet.**
`https://vkpowerlife.136-85-106-73.sslip.io/` — a deployed Next.js application on
a real VM, reached over real HTTPS. Not a proving ground, not localhost, not a
fixture. Crawled through the production `Crawler` and `PlaywrightBrowserPort`
with **no boundary approvals and no walk attestation** (read-only posture,
because it is a live deployment).

```
target       : https://vkpowerlife.136-85-106-73.sslip.io/
explorer     : qe-explorer/1.0+inv-js-v12
events       : 68        endpoints: 7
attribution  : {'navigate': 61, 'click': 7}
```

Evidence preserved at `Nexus_power/evidence/a23_live_network/`
(`manifest.jsonl` + `stamp.json` carrying its sha256).

### What proves success

`tests/test_a23_live_network_evidence.py` — **10 assertions, green**, run in the
fast `qe-explorer-tests` job so it gates on every push. The M2.5 instrument
already crawled a live app but **asserts nothing by design**; an instrument
nobody runs proves nothing next month.

The endpoint map, printed as the attached artifact:

```
seq[ 1..66] GET /life-insurance/quote/start/index.txt      seen=15  {200:15}
seq[ 2..35] GET /login/index.txt                           seen= 8  {200: 8}
seq[ 3..65] GET /portal/dashboard/index.txt                seen=15  {200:15}
                triggered by: click 'Verify & Sign In'
seq[ 4..56] GET /portal/beneficiaries/index.txt            seen=15  {200:15}
                triggered by: click 'Verify & Sign In'
seq[30..64] GET /index.txt                                 seen=11  {200:11}
seq[48..67] GET /life-insurance/quote/coverage/index.txt   seen= 2  {200: 2}
                triggered by: click 'Continue'
seq[49..68] GET /life-insurance/quote/personal/index.txt   seen= 2  {200: 2}
                triggered by: click 'Continue'
```

### The four properties A23 requires

| requirement | result |
|---|---|
| endpoints correctly identified | inventory ↔ raw events agree on the endpoint **set** in both directions, and per-endpoint counts sum to 68 |
| requests not incorrectly attributed | every `actions` entry is present on an event **of that endpoint** — no invented attribution |
| unrelated requests not attached to actions | **61 navigation-time requests carry no click label; 7 click-attributed events all carry theirs. Zero borrowed labels.** |
| joins deterministic | identical inventory across **5 different shuffles** of the same event set |

### Two defects this milestone found — both invisible to a fixture

**1. Every endpoint's sequence and timing were `None` on stored evidence.**
`build_inventory` read `sequence` and `timestamp_ms` with
`isinstance(value, int)`. That is true for an event handed straight over by the
port and **false for the identical event read back off a manifest**, whose
network-event fields are typed `dict[str, str]` — `sequence` comes back as
`"1"`, `timestamp_ms` as `"5983"`.

Measured: **7 of 7 endpoint rows, across 68 live events, carried
`first_sequence=None last_sequence=None first_timestamp_ms=None`.**

It cost twice over: the inventory's own ordering keys on `first_sequence`, so
every row fell through to its `1<<30` fallback; and M2.4's generation reads this
inventory, so a compiled spec could not know when an endpoint was first observed.

This is the **same defect class the function already documents and fixes for
`request_body_keys`** — “an event re-read from a written manifest carries the
flattened string … an inventory that looked complete and had lost the API
contract”. Two more fields were missed. A fixture cannot catch any of them,
because a fixture passes Python ints.

**2. The action↔endpoint join was not deterministic.** The same 68 events in a
different order produced a different inventory. Endpoint identity, counts and
statuses were stable; `actions` was not, and for **three of the seven endpoints a
shuffled run kept a different SET** — `MAX_ACTIONS_PER_ENDPOINT` is a prefix cap,
and a prefix of an unordered stream is arbitrary. `build_inventory` now
aggregates in `sequence` order, the ordinal already assigned at capture for
exactly this purpose.

### Anti-green-wash: mutation-probed

Reverting both fixes turns **3 of the 10 tests RED**, each naming what it saw
(`7 of 7 endpoint rows carry no first_sequence…`, `the inventory differs when the
same events arrive in a different order (seed 1)`). The other 7 stay green — they
test properties that were already correct, so the separation is real rather than
a blanket.

Regression check: 2016 explorer tests + 83 endpoint/network + 69 qe-central
M2.4/endpoint tests, all green.

### What this application CANNOT prove — stated, not worked around

Every one of the 68 requests is a `GET` that returned `200`: the app is a static
export behind a catch-all, and a direct probe confirmed even a nonexistent route
answers 200. So on this application there is **no auth pattern** to observe
(`auth_pattern: none` on all 68), **no request bodies**, and the 5xx oracle
correctly stays **silent** — a real no-false-positive result over 68 events of
genuine traffic, and *not* evidence that the oracle fires. Those axes are proven
on the M2.5 fixture and the frozen-data contract test. The A23 test file says so
in its own docstring so its green cannot be read as covering them.

### Who reproduced it

**The crawl** was run once from the author's machine (network + credential).

**Both gates were reproduced independently by CI.** The capture half runs in
`qe-explorer-tests` — GitHub Actions re-executed its nine assertions on a clean
runner and printed the live-tenant capture table into its log
([run `32446412929`](https://github.com/KVRMtech/nexusqa/actions/runs/32446412929)).
The persistence half runs in the `qec-database` job against that job's own
`postgres:16-alpine`, so the fold, the catalogue and the 52-option durable row
are rebuilt from scratch on infrastructure the author does not control.

`coverage.json`'s sha256 is pinned in `stamp.json` and re-checked every run.

---

## A24 — Live-tenant capture

### What was tested

The M2.6 capture fixes, and the capture → catalogue → **persistence** chain, on a
live tenant application.

### Was it real

**Live deployed tenant, over the public internet.**
`https://vkpowerlife.136-85-106-73.sslip.io/`, crawled read-only (no boundary
approvals, no walk attestation) by `record_live_capture.py` through the
production `Crawler` and `PlaywrightBrowserPort`.

```
stop_reason : completed
states      : 9      flows: 2      distinct controls: 19
inventory_failures : 0
```

This matters because M2.6's existing proof is first-party in a specific way:
`proving-grounds/acme-life` **was edited for M2.6** — it grew an accordion and a
`<details>` so the expansion pass would have something to open. A24's question is
whether the fixes hold on an application nobody shaped for them.

Evidence at `Nexus_power/evidence/a24_live_capture/` (`coverage.json`,
`manifest.jsonl`, `stamp.json` with the coverage sha256).

### What proves success

* **`tests/test_a24_live_tenant_capture.py` — 9/9** (explorer, no DB, fast job)
* **`tests/contract/test_a24_live_tenant_catalog.py` — 2/2** (qe-central, real Postgres)

**T-CAP-01, the option ceiling, on real data.** The defect M2.6 verified was a
stack of private ceilings — browser snippet 300, Python refiner 60, **catalogue
48**. This tenant's `State of residence` offers **52 options**:

```
State of residence     carried= 52  counted= 52   <-- over the old catalogue ceiling of 48
Branch of Service      carried= 14  counted= 14
Coverage Amount        carried= 13  counted= 13
Relationship           carried= 11  counted= 11
Military Affiliation   carried= 10  counted= 10
Term Length            carried=  7  counted=  7
```

and it survives **all the way into the durable row**: the persisted
`catalog_questions` row carries 52 options with `options_total = 52`. One live
control that the catalogue ceiling alone would have clipped, proven end to end.

**Persistence.** `fold_crawl` → 6 nodes, 4 edges, 2 traversals, 147 branches,
23 catalogue questions → **23 durable rows read back through the ORM**, each
carrying a `locator` and a `business_rule_state` (the qec_019 columns A20
round-tripped).

**T-CAP-03, proven NEGATIVE and stated as such.** `expansions_opened`,
`expansions_skipped` and `tab_views_recorded` are all **0**. That is only
evidence if the application really has nothing shut — so it is not assumed:
capture emits `disclosure` for every recorded action, and across **all 40**
recorded actions the value is `""`. No `<details>`, no `aria-expanded`, no
`role=tab`. The counters read zero because the pages had nothing collapsed, not
because the pass failed to look.

> **T-CAP-03's positive path is NOT proven on a live tenant.** It is proven on
> acme-life and on the M2.6 fixtures. Finding a live tenant with a real accordion
> is remaining work, named rather than papered over.

### The defect this milestone found: one control, two catalogue questions

23 durable rows for **16 distinct labels**. Seven labels appear twice, and the
two populations are not the same thing:

| label | locators | verdict |
|---|---|---|
| `First name`, `Last name`, `Date of birth` | `q_first`/`b_first`, … | **correct** — two genuinely different controls that share a label, on the quote form and the beneficiary form, differing in required-ness |
| `Branch of Service`, `Coverage Amount`, `Military Affiliation`, `Term Length` | one locator each | **defect** — one control, catalogued as two questions |

**Root cause, established rather than guessed.** `question_id_for` prefers the
control SIGNATURE and falls back to the normalised NAME. `extract_controls`
merges the signature in from the field ledger keyed by **`(url, name)`** — so a
control gets its signature-derived id on the page where the walk *filled* it and
its name-derived id on a page where the walk only *observed* it.

The proof is arithmetic:

```
question_id_for({'name': 'Branch of Service'})    -> q_58aea59e5b186336
                the second row's id               == q_58aea59e5b186336   ✓
field ledger signatures for that name, crawl-wide : exactly ONE

… and for the legitimate pairs:
field ledger signatures for 'First name'          : TWO   (neither id is the fallback)
```

This is M2.1's Δ2 failure resurfacing on a live tenant — the same shape as “three
questions had produced seven catalogue rows”. It inflates this application's
question count by 4 in 16.

**Not fixed here, deliberately.** The repair is small in shape — resolve the
signature crawl-wide when a name has exactly one — but it **re-keys
`question_id`**, which is the join key for the catalogue, the diff, retirement,
and the committed M2.3 and A21 evidence, while two other sessions are working in
catalogue and state-identity code. Re-keying the catalogue as a side effect of an
evidence gate is not this milestone's decision to take alone.

So it is **pinned**: exactly these four, by id. A fifth duplication, or one of
these disappearing, turns the test red and puts a human in the loop instead of
letting the number drift.

### Who reproduced it

**GitHub Actions, on a clean Linux runner** — this is the strongest reproduction
in the gate, because CI did not verify a recording, it *made its own*.

[Run `32447553270`](https://github.com/KVRMtech/nexusqa/actions/runs/32447553270),
*Chromium lane*, both steps green (and this run was not cancelled — it ran the
lane to completion):

```
tests/browser/test_a21_catalog_diff_regression.py ....... 8 passed in 304.00s
✅ A21 — three real application changes, recorded by two real crawls
   "A21 crawl evidence is unchanged in substance."
✅ Fail if the A21 crawl evidence changed in substance
```

The first re-ran the whole producer: two fresh Chromium crawls of acme-life, the
three deliberate surgeries between them, and all eight assertions — on a
different machine, a different OS, and different ephemeral ports.

The second is the one that matters. It re-recorded `stamp.json` from those fresh
crawls and diffed it against the copy committed from a Windows box. **It matched
byte for byte.** The questions each crawl asked, their types and their answer
sets are identical across machines.

That is also the substance-fingerprint decision vindicated: the raw coverage
accounts could never have matched (ephemeral ports, wall-clock `*_ms`,
port-dependent `state_fingerprint`), so a byte guard over them would have failed
here while nothing about the application had changed.

The M2.3 recording reproduced identically in the same run, under the same
corrected guard.

**The consumer half** was reproduced by CI too, in the `qec-database` job's own
`postgres:16-alpine` — the fold, both catalogue versions and the diff are rebuilt
from scratch on infrastructure the author does not control.

*(The job as a whole is RED — `Characterization — pass 1` FAILS on the 28 stale
goldens owned by another milestone. Every step Gate 3 owns runs BEFORE it and
reports for itself; that reordering is what made this evidence obtainable at
all. See "Known red, and NOT mine".)*

---

## A22 — Real journey → executable specification ⛔ BLOCKED

**Not delivered. The blocker is a property of the application inventory, and it
is measured rather than asserted.**

### What A22 asks for

A journey **actually discovered by the crawler**, compiled into a specification
that executes against the real application, carries **network assertions** and
hard outcome assertions, PASSES on the healthy app, and goes RED under a seeded
regression.

### What already existed, and what was fixture

M2.4's proof does the hard end of this and does it well: 21 tests, real
`@playwright/test`, a real HTTP application, two orthogonal seeded regressions,
green on healthy and red on both. Verified locally in this gate — **21 passed in
96s**.

What it does **not** do is discover the journey.
`m24_generation/crawl_evidence.py` says so in its own first paragraph:

> FIXTURE: the raw network events and the journey graph rows — i.e. what a crawl
> of the quote application **WOULD** have recorded.

That hand-built account is exactly the fixture A22 exists to replace.

### What was built and run

`engines/qe-explorer/tests/browser/test_a22_generation_crawl.py` — a real crawl,
real Chromium, production `Crawler` and `PlaywrightBrowserPort`, real walk
authorization, against the **same** application the M2.4 proof executes its
generated spec against. Evidence recorded to
`Nexus_power/evidence/a22_generation/`.

### The blocker, measured — and it is two layers, not one

The crawl **actuated the funnel**. The application's own server log — an
independent record kept by the app, not by the crawl — shows it happened:

```
server saw : GET /   GET /api/config   POST /api/quote   GET /result.html
```

**The manifest recorded the walk correctly.** It has the click, the edge and the
result page:

```
page_state /             actions=1   click 'Get Quote' -> navigation /result.html
edge       4d4c877… -> 160c7560…     target_label 'Get Quote'
page_state /result.html
crawl_meta stop_reason=completed
```

**The coverage account — which is what the fold consumes — does not:**

```
states : 1      (the entry page only; /result.html absent)
flows  : 0
forms_found : 0     journeys_completed : 0
```

#### Layer 1 — the bare-button wizard gate, so no journey exists

Already known. M2.1's own *architectural concerns discovered* names it and
explicitly leaves it as somebody else's gap:

> A page whose only questions are bare buttons is never walked. `discovery.py`'s
> wizard gate requires `fill.filled or fill.has_unanswered_decisions`, and a step
> made of nothing but `<button>` answers commits nothing — so
> `_answer_questionnaire` never runs on it.

`forms_found == 0` is that gate declining, measured on a real crawl of an
application with no inputs at all: one button, everything else in JavaScript.

#### Layer 2 — the OUTCOME PAGE is dropped from the account (new)

This one was not known, and it is the more interesting half.

The manifest's `/result.html` record carries **exactly what a hard outcome
assertion needs**, already located and already classified:

```json
"displayed_values": [{
  "label": "Your monthly premium", "selector": "#premium-value",
  "text": "42.50", "value_type": "number",
  "value_reason": "number value under an outcome label"
}]
```

It never reaches `coverage["states"]`, because of one line in
`state_identity.note_state_signals`:

```python
if not signals and not controls:
    return
```

A funnel's **result page is by construction a page with neither** — nothing to
ask, nothing to press. So the one page whose VALUE a generated specification has
to assert on is the one page the account is designed to discard. The rule is
deliberate and defensible for hub pages (“a page that asks nothing can still
REFUSE everything”); its effect on outcome pages appears to be unintended.

This is very likely why `crawl_evidence.py` had to hand-write `outcome_values`
into its traversal fixture in the first place: **a real crawl cannot supply them
through this path.** Fixing Layer 1 alone would give A22 a journey with no
outcome to assert on.

### Why choosing a different application does not solve it

| application | crawler walks it? | calls a backend? |
|---|---|---|
| `m24_generation/fixture_app.py` (quote funnel) | ❌ bare-button gate | ✅ real `POST /api/quote` |
| `proving-grounds/acme-life` | ✅ (A21: 15 questions, real flows) | ❌ `grep -c 'fetch('` = **0** |
| `proving-grounds/vkpower-life` (deployed) | ✅ (A24: 9 states, 19 controls) | ⚠️ static export — A23 measured 68 requests, **all GET, all 200** |
| `proving-grounds/summit-life-carrier` | ❌ does not start in CI (see below) | unknown |

**No application currently in the repository can produce a real discovered
journey and real endpoint traffic at the same time.** A22 needs both in one
crawl. That is a fact about the inventory, not a shortfall of effort.

### How it is left

The producer is committed and green, with the milestone's stop condition kept as
a **strict xfail** rather than deleted, plus a companion test that pins the
*shape* of the blocker (server saw the POST; crawl recorded `forms_found=0`, one
coverage state; the result page in the manifest and not in the account).

**It runs in CI, and the blocker reproduces there** — run
[`32447553270`](https://github.com/KVRMtech/nexusqa/actions/runs/32447553270),
step *A22 — the generation blocker is still exactly where it was*:

```
test_the_crawl_walked_the_funnel_to_its_result_page        XFAIL
test_the_blocker_is_exactly_the_bare_button_wizard_gate    PASSED
test_the_backend_really_answered_the_crawl                 PASSED
test_the_crawl_captured_the_call_the_backend_answered      PASSED
```

That matters beyond bookkeeping: both layers of the blocker are properties of the
**code**, not of one developer's machine. A clean Linux runner reaches the same
two disagreeing records.

Two consequences, both deliberate:

* the day the bare-button gate closes, the xfail **XPASSes** and CI goes red
  until someone finishes A22 — the gap cannot be forgotten;
* if the crawl starts failing for a *different* reason, the companion test goes
  red instead of the xfail silently absorbing it.

### What would unblock it

Either of these, in preference order:

1. **Close the bare-button wizard gate** (relax `discovery.py` so a step whose
   only control is a commit-shaped button is still walked). This is the real fix
   and it benefits every SPA-shaped application, not just this one. M2.1 declined
   it because *“it alters what a crawl clicks”* — a deliberate, reviewed change,
   not a Gate-3 side effect.
2. **Give the quote funnel one real input** (an age field, which the page already
   hard-codes as “35” in JavaScript). Cheaper, but it edits a fixture another
   milestone's 21 passing tests are pinned to, and it makes A22's proof depend on
   the application having been reshaped to be crawlable — the same criticism this
   gate levels at M2.6's use of acme-life.

---

## A25 — Deployment & deployed-build proof ⛔ NOT ATTEMPTED

**Not delivered, and deliberately not attempted.** A25's own rule is that it must
be last, and its preconditions are not met:

* **A22 is blocked** (above), so Phase 2 is not complete to deploy.
* **CI is not green on this branch.** Three jobs are red for reasons owned by
  other sessions — 28 stale browser goldens from commit `3420d88`, the
  `test_t_fl_03` object-storage handoff, and the `summit-life-carrier` proving
  ground, whose crawl reaches only the sign-in screen — an auth defect whose fix
  is uncommitted (`acme-life` and `vkpower-life` both pass in the same lane; the
  app itself serves in two seconds). See *Known red, and NOT mine*, including the
  correction to the cause I originally recorded.
* Deploying a feature branch to the VM would run migrations `qec_018`…`qec_023`
  against the database currently serving the live `vkpowerlife` and
  `summitlife-admin` demos — the same deployment A23 and A24 just used as their
  real-application evidence.

A second, independent obstacle is worth recording because it is not a scheduling
problem and will not go away when the others do:

> **M2.1 as it exists cannot execute against deployed services.**
> `tests/browser/test_questionnaire_catalog_e2e.py` drives an **in-process**
> `Crawler` and imports qe-central's pure functions directly. Nothing in it
> reaches a deployed qe-explorer or a deployed qe-central. Pointing it at the VM
> is not configuration — it needs a variant that dispatches a crawl through the
> deployed explorer's API and reads the catalogue back out of the deployed
> database. That work does not exist yet, and A25 cannot be honestly closed
> without it, because *“M2.1 executes against deployed services”* is the
> acceptance criterion.

Verified reachable and healthy, so the target is not the obstacle:

```
https://vkpowerlife.136-85-106-73.sslip.io/        200
https://summitlife-admin.136-85-106-73.sslip.io/   200
https://136.85.106.73/                             200
gcloud: authenticated, project project-8d85a07a-396c-40aa-9b6
```

---

*A22 and A25 remain open. Everything above is complete and reproducible.*
