# Gate 0 — Durability and an honest CI

**Status: A1 and A2 CLOSED. A3 and A4 in measurement. A5 blocked on repository access.**

> **2026-08-31 — Team H / H1 appended as §9.** A5's stated blocker (no push
> access) no longer exists; see the correction row below. The deploy now refuses
> a commit CI has not passed, proven by refusal rather than by configuration.

The tree freeze that §0 asked for arrived on its own: by 12:18 no other squad had
written for twenty minutes and no competing `pytest` process was running. That
window was all A1 ever needed. `git status` now reports **working tree clean**, and
a clean clone runs **2003 passed / 0 failed** — up from 1712, closing the whole
reproducibility gap.

| # | Action | Verdict |
|---|---|---|
| A1 | Commit the unversioned work; working tree clean | **DONE** — 255 files / 46,087 insertions in `3420d88`; working tree clean; clean clone 2003 passed |
| A2 | Re-record `f1_public_discovery` as a *reviewed* diff | **DONE** — landed *inside* `3420d88` with its producer, the only way it could ship; determinism reconfirmed hours apart at the same sha256 |
| A3 | Resolve the browser failures permanently | **MOSTLY DONE** — 2 failures measured (not 10), both root-caused and fixed, one guarded; whole-suite repetition and parallel not proven |
| A4 | Record a crawl-wide performance baseline | **INSTRUMENT DELIVERED + RUN; baseline NOT committed** — no quiet machine; the run found a reproducible 61 s stall |
| A5 | Require *both* CI lanes before merge | **BLOCKED ON ACCESS** — script and workflow fixes delivered; the push that would let any lane report is denied to this machine |
| A5 · *correction 2026-08-31* | — | **THE ACCESS BLOCK IS GONE.** The `gh` token now reports `admin: true` on `KVRMtech/nexusqa` and pushes have been landing over the `origin-https` remote since 2026-08-27. §5's "Step 1 is the gate" no longer holds; steps 2–5 of that handover are unblocked and still not done |
| H1 | Nothing deploys without a green CI run | **BUILT AND DEMONSTRATED** — §9. `deploy.ps1` refuses a red sha before it pushes (three live refusals + a positive control); reconciliation and bootstrap-trigger removal NOT done |

---

## §0 · The finding that governs everything below

**This working tree is being written to by other squads while Gate 0 runs.**

That is not an inconvenience to note in passing; it invalidates two of Gate 0's
five exit criteria as written, and it manufactured a defect that does not exist.

Files that appeared *during* this session, none of them mine:

| file | last written | Closure-Plan item |
|---|---|---|
| `app/completion.py` (modified) | 09:19:11 | A8 |
| `app/fill_engine/repair.py` (modified) | 09:26:06 | A10 |
| `tests/test_journey_completion.py` | 09:18:07 | A8 |
| `tests/test_answer_to_unblock_radio.py` | 09:16:08 | A7 |
| `tests/test_fill_engine_retry.py` | 09:28:20 | A10 |
| `tests/browser/fixtures/27-wizard-20-step-samefingerprint/` | 09:30:50 | A9 |

By the end of the session **four** `pytest` processes belonging to other squads
were running against this same checkout.

### The phantom defect this produced

Running the engine lane under randomised order produced, in sequence:

```
seed=4711   1914 passed
seed=1234   5 failed, 1909 passed
seed=9090   5 failed, 1909 passed
```

Read alone that is a textbook order-dependence signature, and it is wrong.
Re-running the *same seeds* minutes later passed. Two more runs then produced:

```
run1  seed=23496   99 failed, 1815 passed   — a cascade of NameError
run3  seed=27691    5 failed, 1936 passed   — all five in test_fill_engine_retry.py
```

The collected test count moved **1870 → 1914 → 1941** inside one session.

Both failures resolve against the mtime table above:

* `run3`'s five failures are all in `test_fill_engine_retry.py`, whose mtime is
  **09:28:20** — it was being saved while pytest imported it.
* `run1`'s ninety-nine `NameError`s span every file that imports
  `app/completion.py`, mtime **09:19:11**. A half-written module produces
  exactly that cascade.

With the tree momentarily still, three further randomised orders were clean:

```
seed=555   1941 passed        seed=777   1941 passed        seed=999   1941 passed
```

**The engine lane is order-independent. The tree is not stable.** Reporting the
first result as an order-dependence bug would have sent someone hunting a race
that does not exist — the exact failure mode Gate 0 exists to prevent, pointed
at itself.

### What this costs

* **A1's "working tree clean" is unattainable by Gate 0 acting alone.** The
  moment another squad saves a file the tree is dirty again. A single commit
  cannot make a statement true about a directory somebody else is writing to.
* **A4's baseline cannot be honestly recorded here.** A performance number
  measured against four competing pytest processes and a browser suite measures
  the contention, not the crawler. Publishing it as "the permanent reference for
  future regression detection" would poison every future comparison.

### What is actually required

1. **Freeze the tree** for the final Gate 0 commit — a announced window in which
   no squad writes to `feat/qec-dynamic-catalog-p0-p6`. Gate 0 is one commit
   long; the window is minutes.
2. **Record A4 on a dedicated machine**, or on a CI runner, with nothing else
   scheduled. The instrument is committed and takes one command.

Neither is a code change and neither is in my gift to grant, which is why they
are escalated rather than absorbed.

---

## §1 · A1 — Repository inventory

### Method

Every changed path was enumerated at file granularity
(`git status --porcelain --untracked-files=all`), then reviewed along four axes
before staging: secrets, generated artefacts, package structure, and syntax.

### Counts — and the correction that matters

The Closure Plan's "209 unversioned files (87 untracked + 121 modified)" counts
untracked *directories* as one entry each. At file granularity:

| | session start (08:20) | session end (10:40) |
|---|---|---|
| modified (tracked) | 121 | 118 |
| untracked (files, not dirs) | 133 | 164 |
| **total unversioned** | **254** | **282** |

**The backlog grew by 28 files while Gate 0 was closing it.** That single row is
the honest summary of A1: this is not a queue that can be drained by one commit,
because it is being refilled faster than a review can empty it.

### What was actually committed

Three scoped commits, twenty files, all of them Gate 0's own work:

| commit | files | what |
|---|---|---|
| `0a91cea` | 9 | A2 golden re-record, A5 `paths:` trap fix + CI-coverage guard, `.gitattributes` LF pin, `_measure_live/` ignore, A4 instrument |
| `faecf78` | 8 | A3 fixture-30 migration + `where`-clause contract guard + jsdom slack fix |
| `6da62fb` | 3 | A5 protection script, A4 instrument fixes, this document |

### What was NOT committed, and why that is the right answer

**The ~250-file milestone backlog remains unversioned.** Committing it was the
literal instruction, and I did not do it. The reason is in §0 and is not a
scheduling excuse:

* Other squads hold **uncommitted edits inside the same files** — `crawler.py`,
  `walker.py`, `forms.py`, `completion.py`, `fill_engine/repair.py`. A commit
  cannot take the M2.5 half of `walker.py` and leave the in-progress A6 half; the
  separation is intra-file, and file-level exclusion cannot express it.
* Their work is demonstrably mid-flight. Fixture 27's `expected.json` expects a
  `css_hint` its own `index.html` does not emit — it fails right now. Twelve
  minutes before the first commit, `test_fill_engine_retry.py` did not import.
* `evidence/m23_retirement/*` was **rewritten at 09:52** by another squad's
  browser run, and `coverage_report.json` at 10:06. Both are CI-asserted
  artefacts. Committing a file while another process is regenerating it risks
  capturing a partial write into the record CI compares against.

Sweeping all of that into a durability commit would attribute another team's
half-finished Gate 1 work to Gate 0 and could version a state that does not run.
That is a worse outcome than leaving it unversioned for one more hour, and it is
the opposite of what this gate is for.

### What was removed rather than committed

| path | files | why |
|---|---|---|
| `engines/qe-explorer/_measure_live/` | 5 (368 KB) | Instrument OUTPUT, not source: `frame_*.png` screenshots of a **live third-party deployment** plus its `manifest.jsonl`, regenerated on every run. Now `.gitignore`d. The instrument that writes it stays in version control; what it emits does not. |
| `tests/browser/golden/{inventory,manifest}_23-network-retry-poll-ratelimit.json` | 2 | Orphans of a fixture renumber — fixture 23 is now `23-canvas-app` and the network fixture is `30-`. No fixture directory, and **no reference anywhere in the tree**. Deleted. |

### The review the backlog did get

Every one of the 254 files present at session start was reviewed along the four
axes below before I concluded it was *safe* to commit — the blocker is
concurrency, not content.


### Review results

* **Secrets — clean.** Every non-binary changed file scanned for
  `ghp_`/`github_pat_`/`sk-`/`AKIA`/PEM private keys/Slack tokens: zero hits. A
  broader sweep for `password|secret|token|api_key|credential` assigned a literal
  returned exactly one match, `"not-a-real-password"` in fixture 30's HTML.
* **Structure — clean.** All seven new `app/` modules import; new package dirs
  carry `__init__.py`; `tests/fleet/` correctly has none, matching every sibling
  test directory.
* **Syntax — clean.** 132 changed `.py` files byte-compile. All 69 changed JSON
  and 3 YAML files parse.
* **Size — clean.** 4.6 MB, no binaries, largest file 542 KB of Python.
* **`ruff check .` — passes** on the full tree, the same invocation `ci.yml`'s
  blocking `lint` job makes.

### A durability defect found while doing this

`.gitattributes` pinned `*.sh`, `*.py`, `*.yml` and friends to LF but said
nothing about `*.golden`. With `core.autocrlf=true` — the setting on this
checkout — a fresh Windows clone gets CRLF goldens, and the first re-record
rewrites every line back to LF. `git status` then reports the whole file dirty
with no behaviour change behind it, and CI's own "a golden was rewritten" guard
cannot tell that apart from a real capture regression. Pinned to LF; verified to
cause zero renormalisation churn (all four goldens are already 0 CR bytes).

---

## §2 · A2 — Golden review

### The instruction was "a reviewed diff, not a bulk overwrite", and that distinction did real work

Only `f1_public_discovery` was failing. `QEC_UPDATE_GOLDENS=1` would have
rewritten all four. **One file was re-recorded.** f2, f3 and f4 were left
untouched because they already passed, and were then reviewed anyway, because
they are modified-but-uncommitted in this tree and therefore land in the same
commit.

### The whole of the f1 failure

One field, on one record:

```
.network_calls[0].capture_window_start_ms   ADDED   "0"
```

Produced by [`state_identity.py:624`](../../Nexus_power/engines/qe-explorer/app/state_identity.py),
M2.5's nav-prefetch window, which always emits the key and defaults it to `"0"`.
The characterization harness drives a `ScriptedBrowser`, which supplies no
capture window, so the default is what appears.

**Why only f1.** It is the only fixture whose crawl makes a network call at all
— f2, f3 and f4 have zero `network_calls` entries, so the new field has nowhere
to appear and they could not have diverged.

### Determinism, measured before re-recording

Five consecutive captures, identical:

```
sha256 = ace2f509557c7bad7ec28a0d2ab9a14f074c38cb917373d5d6e2bbf1c0ee7425   × 5
```

After re-recording, `tests/test_characterization.py` is green **twice in
succession** — the same two-pass discipline `browser-harness.yml` applies to the
browser goldens.

### All four goldens, HEAD → committed

A line-by-line diff misleads here: record counts change, so every subsequent line
reads as different. Compared instead by **key-path set**, which is alignment-
independent:

| golden | records | key-paths added | key-paths removed |
|---|---|---|---|
| f1_public_discovery | 10 → 10 | 39 | **0** |
| f2_auth_wizard | 11 → 11 | 40 | **0** |
| f3_questionnaire_submit | 6 → 8 | 17 | **0** |
| f4_guard_refusal | 7 → 7 | 16 | **0** |

**Zero key-paths were removed from any golden.** No captured field was lost, so
no capture capability regressed. Every change is additive, and every added field
belongs to a named milestone:

| added | milestone |
|---|---|
| `actions[].qec.{capture_scope, disclosure, frame_origin, shadow_scope}` | M3.2 frames/shadow + M2.6 disclosure |
| `form_snapshot_signals.<field>.locator.{strategy,value,role,verified,bindable}`, `.options_total` | M2.1 / M1.6 locator evidence |
| `network_calls[].{…19 fields…}` | M2.5 network evidence stream (f1 only) |
| f3: two new `type: crossing` records + `status`, `confirmed`, `reserved_at_ms`, `completed_at_ms`, `refusal_reason`, `state_fingerprint`, `url` | M1.2 crossing ledger + M1.4 completion |

f3 is the questionnaire-**submit** fixture — the only one that crosses a
boundary — so it is the only one that could gain crossing records. The diff is
consistent with the milestones that shipped, in every case.

### The clean clone refused the re-record, and it was right to

The re-recorded golden was committed in `0a91cea`, passed twice in this working
tree, and then **failed in a clean clone of that very commit**:

```
== HEAD: 1cc9878…
   dirty entries in a FRESH clone: 0
FAILED tests/test_characterization.py::…[f1_public_discovery]
   golden 317 lines,  actual 224 lines
```

The clone produces the OLD 224-line shape, and every one of the 93 missing lines
is a field from the M2.1 / M2.5 / M3.2 families:

```
.actions[].qec.{capture_scope,disclosure,frame_origin,shadow_scope}   REMOVED
.network_calls[].{action_label,action_token,…}                        REMOVED
```

**The golden records behaviour that only exists in an uncommitted working tree.**
`state_identity.py` — the producer — is modified-uncommitted, and it opens with
`from . import network_evidence`, a module that is **untracked**. The producer
chain does not terminate before it reaches the whole ~250-file backlog.

So committing the golden by itself did not fix the failure — it **inverted** it:

| | working tree | clean clone |
|---|---|---|
| before `0a91cea` | f1 FAILS | f1 passes |
| after `0a91cea` | f1 passes | **f1 FAILS** |

A golden and the code that produces it are one atomic change. Splitting them
moves the red build from the author's machine to everyone else's, which is
strictly worse — the author stops seeing it.

**The re-record is therefore reverted** (`f1_public_discovery.golden` is back to
the shape the committed code actually produces, and the clean clone is green
again). The reviewed 317-line version is kept at
`scratchpad/f1_reviewed_NEW.golden` and reproduced by one command once the
backlog lands:

```
QEC_UPDATE_GOLDENS=1 pytest tests/test_characterization.py -k f1_public_discovery
```

**A2 depends on A1.** The Closure Plan lists them as siblings; they are not. That
dependency is the single most useful thing this section found, and only a clean
clone could have found it — which is exactly why the Definition of Done asks for
one.

### Accepted, recorded, not fixed

`capture_window_start_ms` defaults to the string `"0"`, so "window unknown" and
"window opened at t=0" are indistinguishable. In the scripted harness that is
harmless. In `test_network_stream_gate.py` the window check
`start <= event <= last_seen` becomes vacuously true rather than failing when the
value is missing — a weaker assertion than it appears. Not changed under Gate 0:
it is a live-crawl semantics question for M2.5's owner, and the field is
correctly *present*, which is what that gate's other assertion requires.

---

## §3 · A3 — Browser test stability

*(see §0 first — the "10 failures" headline did not survive measurement)*

### The headline did not survive measurement

The brief says *"Resolve the 10 browser failures"*. A full serial run of all 797
browser tests produced:

```
2 failed, 503 passed, 291 skipped, 1 xfailed   in 4337s (1:12:17)
```

**Two**, not ten. The number is not disputed for its own sake — it matters
because the two have *different* causes and only one of them is a defect in this
repository. Treating them as one population of ten would have produced one fix
for two unrelated problems.

### Failure 1 — a timing assumption, not a capture bug

```
test_jsdom_execution.py::test_jsdom_capability_probe
subprocess.TimeoutExpired: ['node', 'jsdom_runner.js'] timed out after 45.0 seconds
```

Passes alone in **3.87 s**. The 45 s ceiling is `timeout_ms/1000 + 30` in
`_harness.run_jsdom` — a 15 s in-jsdom budget plus a hard-coded 30 s of process
slack for node startup, module resolution and jsdom construction, none of which
the in-page timeout can observe. That constant is a statement about how busy the
machine is, not about the code under test, and this machine was running four
other squads' pytest processes.

**Fixed** by making the slack an env-tunable (`QEC_JSDOM_PROC_SLACK_S`, default
raised 30 → 90) and by converting `TimeoutExpired` into a named error that says
it is a wall-clock failure, names contention as the usual cause, and tells the
reader to re-run alone before investigating capture. **Deliberately not
retried**: a retry would convert a genuine runner hang into an intermittent
failure, which is strictly worse than a slow one.

### Failure 2 — a real defect, reproducible in isolation

```
test_playwright_execution.py::test_expected_controls_in_chromium[30-network-retry-poll-ratelimit]
KeyError: 'where'   at _harness.py:487
```

Fixture 30's four `expect_controls` entries were authored in the wrong schema —
flat `{"name": …, "kind": …}` instead of `{"where": {…}, "fields": {…}}`.
`assert_control` opens with an unguarded `spec["where"]`, so the fixture's own
error surfaced as a bare `KeyError` from inside the harness, in the Chromium
lane, forty minutes into a seventy-minute run, pointing at the harness rather
than at the fixture that was wrong.

**Fixed** by migrating fixture 30 to the harness schema. The expectations are
anchored on `css_hint` (the ids the fixture's own HTML declares) and *assert*
`name`/`role`/`tag`, rather than matching on `name`. Matching on the name would
have been tautological — a capture regression that lost the accessible name
would simply match nothing and read as "the fixture changed".

### Why the contract did not catch it, and the guard that now does

`test_every_fixture_declares_a_valid_contract` checks that a fixture names its
purpose, its lanes, its snippet, and that it asserts *something*. It never looked
at the **shape** of the entries. The inverse rule already existed one function
away — `describes_runtime_behaviour` is checked for *not* carrying a control
shape — so only the positive half was missing.

`test_control_expectations_carry_a_where_clause` now runs in the fast
fixture-library lane and answers in milliseconds instead of forty minutes.
Verified in both directions: green on all 26 fixtures; revert fixture 30 and it
fires by name.

It also caught its own author. The first version carried an allow-list of
permitted keys that I had **guessed rather than read**, and it red-flagged
eighteen correct fixtures over `why`, `href_suffix` and `list_lengths` — all
three of which `assert_control` genuinely consumes. The shipped list is derived
from the harness source and says so.

### Confirmation

| run | order | result |
|---|---|---|
| fixture 30 + fixture library | declaration | 146 passed |
| fixture library (all 26) | declaration | 173 passed |
| fixture-library + jsdom + playwright (526 tests) | **randomised** | 298 passed, 274 skipped, 1 failed |

The one remaining failure is
`test_expected_controls_in_jsdom[27-wizard-20-step-samefingerprint]`: another
squad's fixture, created at **09:30:50** and last edited at **09:31:54**, whose
`expected.json` expects `css_hint='#opt-yes'` that its own `index.html` does not
yet emit. It did not exist during the baseline run and is A9 work in progress.

### What is NOT proven

A3's exit criteria ask for the full suite green **in parallel** and across
**repeated** whole-suite executions. Neither is delivered:

* One full serial pass costs **72 minutes** on this machine, and the tree changes
  underneath it — the run that produced the baseline started before fixture 27
  existed and finished after.
* CI does not run the suite serially either: it shards by marker across two
  jobs, and `ci.yml` records that `pytest-xdist` was **measured and rejected**
  (`-n 2` took 28 min against a 23 min serial baseline) because both lanes are
  session-scoped — every extra worker re-pays the whole Chromium launch. So
  "passes in parallel" is not a property this suite is built to have, and
  asserting it would require redesigning the lane fixtures, which is well
  outside Gate 0.

What *is* established: both measured failures are root-caused, both are fixed,
one has a regression guard, and the affected files pass in randomised order.
Whole-suite repetition needs the same tree freeze §0 asks for.


---

## §4 · A4 — Crawl performance baseline

### The gap is real

`measure.py` runs the **fill engine** against a `FakeApplication` port: twenty
fields, no browser, no navigation, no network. Nothing in this repository has
ever recorded what a crawl costs end to end, so "slower" has never been a
falsifiable claim.

### Delivered: `measure_crawl_performance.py`

Runs the production `Crawler` through the production `PlaywrightBrowserPort` in
real Chromium against real proving-ground applications, and records:

* **wall clock** — crawl duration, browser startup, artifact generation, timed
  separately so a slow launch cannot hide inside a slow crawl
* **throughput** — states/s, pages/s, network requests/s, port calls/s
* **phase attribution** — navigation, DOM extraction, screenshot, interaction,
  network drain, via a `TimedPort` proxy around the real port. A proxy rather
  than a subclass, so a port method added next month is still measured (it lands
  in `other`) instead of silently counting as free.
* **resources** — peak/mean RSS and CPU sampled every 250 ms across this process
  **and its children**, because the browser is a child process and sampling only
  the driver would omit the largest consumer in the run
* **spread** — median, mean, P95 (nearest-rank, never interpolated, so the number
  reported was actually observed) and worst case over `--reps` repetitions

Recorded alongside: OS, CPU count, RAM, Python, Playwright and Chromium
versions, the crawl budget, and every stand-in.

### Named, not hidden

* `summit-life-carrier` is **excluded**: Next.js SSR with no `index.html`, so it
  needs a Docker lane. Benchmarking it here would time a container boot as part
  of a crawl. Recorded as a gap (Closure Plan A16), not quietly dropped.
* The tier-3 advance oracle is a **deterministic stand-in**. A real model call
  would make the wall-clock number measure someone's API latency.
* `psutil` is now pinned in `requirements.txt`; without it the instrument
  degrades to timings-only rather than failing.

### Run, and deliberately not committed as the baseline

The instrument was executed across all four static proving grounds, two
repetitions each, and produced every metric the gate asks for. The output is
committed as `perf/crawl_contended_2026-08-20.json` and is explicitly **not** the
baseline; `perf/README.md` says so beside it. `perf/crawl_baseline.json` does not
exist yet, on purpose.

Environment: Windows 11 (10.0.26200), 12 logical CPUs, 15.7 GB RAM, Python
3.10.11, Playwright 1.49.0, Chromium 131.0.6778.33.

| app | states | navs | port calls | wall (rep1 / rep2) | peak RSS |
|---|---|---|---|---|---|
| acme-life | 6 | 16 | 685 | 98 845 / **147 132** ms | 517 MB |
| vkpower-life | 5 | 11 | 318 | 75 055 / 75 470 ms | 531 MB |
| questionnaire-life | 2 | 3 | 114 | 22 872 / 23 818 ms | 526 MB |
| catalog-evidence | 2 | 3 | 127 | 101 531 / 98 778 ms | 537 MB |

Browser startup 622–921 ms. Artifact generation 0.07–0.23 ms — negligible, and
worth knowing precisely because it was a candidate suspect.

### Why this is not the baseline, measured rather than asserted

`acme-life` did **identical work** in both repetitions — same 6 states, same 16
navigations, same 685 port calls — and took 98.8 s then 147.1 s. **A 49% spread
with no behavioural difference whatsoever.** `vkpower-life`, minutes later,
varied 0.6%. The noise is bursty rather than a constant tax, which is the worse
case: an average over it would look plausible and mean nothing, and every future
comparison against it would inherit the lie.

### What the instrument found on its first real run

Two things worth the exercise on their own:

**1. Interaction dominates, everywhere.** Not DOM extraction, which is the
intuitive suspect and is cheap:

| app | interaction | dom_extraction | navigation |
|---|---|---|---|
| acme-life | 57 940 ms (n=99) | 6 480 ms (n=326) | 11 743 ms |
| vkpower-life | 40 806 ms (n=38) | 10 190 ms (n=138) | 11 422 ms |
| catalog-evidence | 89 857 ms (n=25) | 4 896 ms (n=55) | 3 833 ms |

DOM extraction runs 4–6× as often as interaction and costs a fraction as much.
Any future optimisation aimed at capture would be aimed at the wrong thing.

**2. A reproducible 61-second stall.** `catalog-evidence` spends 61 s inside a
single `select_option`:

| rep | worst `select_option` | its median | crawl wall | share of crawl |
|---|---|---|---|---|
| 1 | 61 637 ms | 1 272 ms | 101 531 ms | **61%** |
| 2 | 61 117 ms | 1 164 ms | 98 778 ms | **62%** |

Half a second apart across two reps — contention does not reproduce like that
(see the 49% acme-life spread above). One call runs ~50× its own median and is
more than half the crawl. The shape, ≈61 s ≈ two 30 s Playwright waits, points at
a locator that never resolves and is retried once rather than at slow work.

**Not fixed here** — it is crawler/fill-engine behaviour, not durability — but it
is the first thing this instrument found, it was invisible while the only
benchmark was a browserless fill-engine harness, and it should be triaged before
anyone tunes anything else.

### To produce the real baseline

One command, on a machine nobody else is using:

```
python measure_crawl_performance.py --reps 3 --baseline
```


## §5 · A5 — CI enforcement

### Measured starting position

`develop` is protected: 18 required contexts, `enforce_admins` on, force-push and
deletion off, conversation resolution required. Every required context belongs to
`ci.yml` or `security-m05.yml`.

**Neither browser-harness lane is required, and neither is `integrity-proof`.**

| context | workflow | required today |
|---|---|---|
| `integrity-proof` | ci.yml | **no** |
| `jsdom execution lane` | browser-harness.yml | **no** |
| `Chromium lane + characterization + coverage` | browser-harness.yml | **no** |
| `Crawl {acme-life, summit-life-carrier, vkpower-life}` | browser-harness.yml | **no** |

`integrity-proof` is the job M1.7 shipped and documented as **BLOCKING**, whose
stated purpose is destroying claims that outrun their evidence. It is advisory.

### The trap that had to be fixed first

`browser-harness.yml`'s `pull_request` trigger carried a `paths:` filter. **A
required status check that never runs is never reported, and GitHub blocks the
pull request forever waiting for it.** Making those lanes required with the
filter in place would not have gated merges — it would have prevented them, on
every PR that did not touch `qe-explorer/`.

The filter is removed from `pull_request` and kept on `push`: pushes to
main/develop arrive through a PR that has already run these lanes, so re-running
them on the merge commit buys nothing and costs 45 minutes. The consequence —
every PR now pays the Chromium lane — is the point of A5, not a side effect.

### The second escape, and its guard

`browser-harness.yml` names its test files one at a time. Seven files carrying
the M1.5, M2.2, M2.5, M2.6, M3.1 and M3.2 browser proofs appear in **no step of
it**. They are executed today only because `ci.yml:497` happens to pass the whole
directory:

```
pytest tests/browser -m "not characterization" -q --tb=short
```

That job is otherwise well built — it installs Chromium and sets
`QEC_REQUIRE_BROWSER_LANES=1`, so a lane that cannot start fails rather than
skips. But the property "every browser test runs somewhere" rests on one
unquoted directory argument, and nothing said so.

[`tests/test_ci_executes_every_browser_test.py`](../../Nexus_power/engines/qe-explorer/tests/test_ci_executes_every_browser_test.py)
now says it, in the fast engine lane. Verified in both directions: green on the
real tree; narrow that one argument and it fires, naming all seven files.

Writing it produced a defect worth recording, because it is the same class of
error the guard exists to catch: the first version read
`pytest tests --ignore=tests/browser` — the engine lane **excluding** the
directory — as proof that the directory runs. The single invocation that
guarantees those files do *not* execute was being counted as coverage. Fixed with
a lookbehind, and the negative control is now part of the test's own docstring.

### Delivered as a script, deliberately not applied

`scripts/gate0_require_ci_lanes.sh` — dry-run by default, idempotent, and it
refuses on unmet preconditions. Branch protection is the one part of this
programme with no diff and no review, so it ships executable rather than as a
click-path in a README.

It is **not applied**, on the release owner's instruction and for a reason the
script itself demonstrates. Its dry run against the live repository says:

```
-- would ADD (3):
     + integrity-proof
     + jsdom execution lane
     + Chromium lane + characterization + coverage

-- checking each context has reported on a recent commit (the never-runs trap):
     NEVER RAN  integrity-proof
     NEVER RAN  jsdom execution lane
     NEVER RAN  Chromium lane + characterization + coverage

REFUSING: 3 context(s) have not reported on develop's head commit.
```

That is the trap from the previous section, confirmed live: **none of the three
has ever reported on `develop`.** Requiring them today would not gate merges, it
would stop them — for the four squads currently working in this tree, on a branch
whose browser lane is red for a reason belonging to none of them (fixture 27,
mid-authoring).

Sequence, in order:

1. Push a commit that runs both workflows; let them finish.
2. `bash scripts/gate0_require_ci_lanes.sh` — dry run; confirm all three report.
3. `bash scripts/gate0_require_ci_lanes.sh --apply`.
4. Prove the refusal. Configuration is not evidence; a refused merge is. The
   script's header carries the exact commands, ending in a `gh pr merge` that
   must fail **405**.

One implementation note worth keeping: the script **PATCHes** the
`required_status_checks` sub-resource rather than PUTting `/protection`. A PUT
replaces the whole protection object, so one that omits `enforce_admins` or
`required_pull_request_reviews` silently switches them off — turning a hardening
step into a regression nobody would notice.

`Crawl <app>` (proving-ground) is **not** proposed for the required set here.
Making a job required before its status is known blocks all merges, and those
three jobs' green-ness on this commit is unmeasured. That is Closure Plan **A17**
(Gate 2), and conflating it with A5 would be the same over-claim this gate exists
to stop.

---

### The one thing Gate 0 cannot do for itself

Every remaining A5 step is downstream of a single `git push`, and this machine
cannot make it:

```
$ git push origin HEAD:feat/qec-dynamic-catalog-p0-p6
ERROR: Permission to KVRMtech/nexusqa.git denied to Venkatareddy2012.
```

`origin` is `git@github.com:KVRMtech/nexusqa.git` over **SSH**, and the SSH key on
this box belongs to `Venkatareddy2012`, who has no write access. The `gh` CLI *is*
authenticated as `KVRMtech` with `repo` and `workflow` scope, so the same push over
HTTPS would succeed — but that is a credential decision for the repository owner,
not something to route around.

Without a push, no workflow runs on this branch; without a run, no context
reports; without a report, no context can be required. A5 stops there.

**A second, structural fact governs the refusal proof.** A pull request from this
branch into `develop` cannot be opened at all:

```
$ git merge-base HEAD origin/develop
(nothing — no common ancestor)
```

`develop` is a single flattened "Initial commit" sharing no history with this
branch, exactly as `ci.yml`'s own header comment documents. GitHub refuses the PR
outright. The refusal proof must therefore be run from a branch cut **from
`origin/develop`**, not from this one.

### Handover, in order

| # | Step | Who |
|---|---|---|
| 1 | Push the branch — `git push origin HEAD:feat/qec-dynamic-catalog-p0-p6`, or allow the equivalent HTTPS push | **repository owner** |
| 2 | Let `ci.yml` finish, then run `browser-harness.yml` via `workflow_dispatch` on the branch — its `push` trigger is scoped to main/develop, so it will not fire on its own | either |
| 3 | `bash scripts/gate0_require_ci_lanes.sh` — dry run; confirm all three contexts now report | either |
| 4 | `bash scripts/gate0_require_ci_lanes.sh --apply` | either |
| 5 | Refusal proof, from a **develop-based** branch: break one required lane, open the PR, and record the `gh pr merge` 405 | either |

Steps 3–5 are minutes. Step 1 is the gate.


## §6 · Evidence index

| lane | command | result |
|---|---|---|
| engine | `pytest tests --ignore=tests/browser -q` | **1870 passed, 0 failed** (31 s) at session start; **1941 passed** after other squads' tests landed |
| engine, randomised | `--randomly-dont-reset-seed --randomly-seed={555,777,999}` | **1941 passed × 3, 0 failed** |
| characterization | `pytest tests/test_characterization.py` ×2 | **6 passed, 6 passed** |
| qe-central | `pytest platform/qe-central/tests -q` | **2263 passed, 146 skipped, 0 failed** (82 s) |
| platform-api | `bash ci/run_platform_api_tests.sh` | **PASS** — 108 files, all green in isolation |
| lint | `ruff check .` | **All checks passed** |
| compile | 132 changed `.py` | **all OK** |
| browser | `pytest tests/browser -q` | see §3 |
| **clean clone** | `bash scripts/gate0_verify_clean_clone.sh` | **PASS** — 0 dirty entries in a fresh clone, all `.py` compile, ruff clean, **1712 passed / 0 failed**, characterization 6 passed twice, no golden or evidence rewritten by the run |

The clean-clone check is the one that earned its keep: it caught a defect in this
gate's own A2 work that four green runs in the working tree had hidden (§2). It
is committed as `scripts/gate0_verify_clean_clone.sh` and takes one argument-free
command.

Note the clean clone runs **1712** tests against the working tree's **1988**. The
276-test gap is the unversioned backlog and the other squads' in-flight work —
i.e. a second party reproducing from the commit today sees 86% of the suite that
exists. Closing that gap is A1.

The 146 qe-central skips are DB-gated and are covered by the `qec-database` job,
which runs the same suite against real Postgres with `QEC_REQUIRE_DB` set, where
a skip is a failure.

---

## §7 · Risks and technical debt

| risk | severity | note |
|---|---|---|
| Shared tree has no freeze protocol | **high** | Manufactured a phantom defect in this very session; will do so again, and the next reader may believe it |
| A4 baseline uncommitted | **high** | Every future performance claim stays unfalsifiable until one quiet run happens. One command. |
| `catalog-evidence` 61 s `select_option` stall | **high** | Reproducible across reps; 61% of that crawl's wall time. Found by the new instrument, owned by nobody yet. Triage before tuning anything else. |
| `proving-ground` lanes unmeasured and unrequired | medium | Closure Plan A17; three real-app crawls gate nothing today |
| `requirements.txt` pins `playwright==1.48.0`, this machine ran 1.49.0 | medium | Local/CI drift; the browser results above were produced on 1.49.0 |
| `capture_window_start_ms` sentinel `"0"` | low | Weakens the network-window assertion to vacuously-true when absent |
| `pytest-randomly` + numpy | low | `_reseed` overflows past 2³²−1 and errors; any randomised run needs `--randomly-dont-reset-seed`. Not in `requirements.txt`, so CI is unaffected |

---

## §8 · Sign-off

**Gate 0 is not signed off.**

Five commits landed — `0a91cea`, `faecf78`, `6da62fb`, `1cc9878`, and the A2
revert — and the repository is verifiably better than it was: the clean clone
passes, two real browser defects are fixed and one is guarded, a CI trap that
would have blocked every pull request is closed, and the crawl has a measuring
instrument for the first time.

None of that is Gate 0. Gate 0's exit criterion is *"CI is green on a named
commit, from a clean clone, on hardware the author does not control"*, and the
work that commit must contain is still sitting in a working directory that four
other squads are writing to.

What this gate learned, that the Closure Plan did not know:

* **A2 depends on A1.** A golden cannot land before the code that produces it.
  Committing it alone does not fix the red build, it relocates it to everyone
  else's machine. Proven by a clean clone, on my own work.
* **A1 is not a task, it is a scheduling problem.** The backlog grew 254 → 282
  while being reviewed. No amount of care empties a queue that refills faster.
* **A3's "10 failures" was 2**, with two unrelated causes, one of which was not a
  defect in this repository at all.
* **A5's two lanes could not have been made mandatory** as the plan assumed —
  both a `paths:` filter and a never-reported context would have converted the
  gate into an outage.

Three things are needed, and none of them is engineering:

1. **A tree freeze** long enough for one commit — minutes, announced.
2. **One quiet machine** for one benchmark command.
3. **A decision on who owns the 61-second `select_option` stall** the baseline
   found. It is 61% of a crawl and it belongs to no milestone.

Given (1), A1, A2 and A5 close in a single sitting. A3's whole-suite repetition
closes with them. A4 closes with (2).

---

## §9 · H1 — the deploy gate (Team H · CI & Release Engineering)

**Status: the deploy gate is BUILT and its refusal is DEMONSTRATED. The history
reconciliation and the bootstrap-trigger removal are NOT done — they are blocked
on the decision recorded in `BRANCH_RECONCILIATION_SCOPE.md` §3.**

### §9.1 · The finding: CI and the deploy had no connection at all

There are two remotes, and until now only one of them was ever asked anything:

```
laptop -- git push --> mine (Venkatareddy2012/nexus-power-snapshot) -- git pull --> VM
laptop -- git push --> origin (KVRMtech/nexusqa) --> GitHub Actions runs HERE
```

`deploy.ps1` pushed to `mine` and the VM pulled from `mine`. `mine` has no
Actions. Nothing in the deploy path consulted `origin`, and `gh run list`
appeared nowhere in the repository. The golden crawl gate is a good gate, but it
runs *after* the swap — it detects a bad build by serving it to clients first.

Measured on the real repository, 2026-08-31:

| | |
|---|---|
| commits on trunk (`feat/qec-dynamic-catalog-p0-p6`) at origin | **826** |
| …with a `Nexus QA CI` run of any conclusion | **92** |
| …with a **successful** `Nexus QA CI` run | **21** (2.5%) |
| last 100 ci.yml runs on trunk | **53 cancelled, 18 failure, 21 success** |
| local commits ahead of origin at session start | **68** (all dated 2026-08-31) |

Those 68 were pushed as the first act of this work (`d5130e4..ed5c489`) — the
first time that day's output was compiled by anything other than a laptop.

### §9.2 · Built

* **`scripts/require_green_ci.ps1`** — adjudicates one sha. Exit 0 green, 1 red,
  2 never-ran, 3 still-running, 4 unknowable. **Every non-zero code is a
  refusal**; there is no "could not tell, carry on" path.
* **`scripts/deploy.ps1`** — a new `[0/4]` stage calls it **before the push**, so
  a commit CI has not passed never reaches the deploy remote at all. This also
  covers `-PushOnly`, which previously left an unverified sha one `git pull`
  away from the fleet. New exit code **3 = refused, nothing pushed, nothing
  deployed**. `NEXUS_DEPLOY_BRANCH` was added (same shape as `GATE0_BRANCH`) so
  the refusal can be proven against a named commit without moving `develop`,
  which nine concurrent sessions share.
* **`scripts/gate0_require_ci_lanes.sh --audit-runs [since]`** — the instrument
  for this gate's own exit criterion. One API call, intersected with
  `git rev-list`; a cancelled run counts as *no* run.

There is deliberately **no bypass flag**. `-NoGate` skips the golden *crawl*
gate and always did; it does not skip this one.

### §9.3 · Why the gate does not trust "`gh run list` said success"

Four workflows fire per push and they are not comparable: `Nexus QA CI` ~30 min,
`Browser Test Harness` ~60 min, `M0.5 Security Gate` ~45 s, `A11 Attestation
Certification` ~50 s. `Nexus QA CI` also runs under `cancel-in-progress: true`,
so the next push kills it.

On commit `36adb1f` the honest picture was:

```
M0.5 Security Gate              success   (42s)
A11 Attestation Certification   success   (1m0s)
Nexus QA CI                     CANCELLED (1m31s)   <-- the actual suite
```

A gate written as `gh run list --commit <sha> | grep success` **passes that
commit** — it would report green on precisely the commits whose suite never
finished. The gate therefore demands a verdict from each *named gating
workflow*, and treats `cancelled` as red rather than as absent.

### §9.4 · The refusal, demonstrated

Configuration is not evidence. Three runs of the **real** `deploy.ps1`:

**Proof A — a real commit whose suite genuinely failed** (`2b7604c`, "R7(7): the
two branches were running opposite polarities", 2026-08-23). Not a synthetic
red: `ci.yml` has no `workflow_dispatch` and fires only on four named branches,
so a scratch branch cannot be made to go red. A real historical failure is the
stronger artefact anyway.

```
[0/4] CI gate - has origin already passed this commit?

== CI GATE ===============================================
   repo     : KVRMtech/nexusqa
   commit   : 2b7604ccad565f690585b3054016eed581425273
   requires : Nexus QA CI + M0.5 Security Gate == success

-- every run on this commit (4):
     Browser Test Harness (M0.2)      completed    failure
     Nexus QA CI                      completed    failure
     M0.5 Security Gate               completed    success
     A11 Attestation Certification    completed    success

   Nexus QA CI                      FAILURE
        https://github.com/KVRMtech/nexusqa/actions/runs/32648665802
   M0.5 Security Gate               success

DEPLOY REFUSED - this commit has not passed CI.
   * Nexus QA CI concluded failure

NOTHING WAS PUSHED AND NOTHING WAS DEPLOYED. The fleet is untouched.

DEPLOY REFUSED BY THE CI GATE (require_green_ci exit 1).
No push to 'mine'. No pull on the VM. No build. No swap.
>>> deploy.ps1 EXITCODE = 3
```

Note the two green workflows in that transcript: this is the §9.3 trap caught in
the act, on a genuinely failed commit.

**Proof B — the commit `deploy.ps1` would have shipped today**, with no override
at all (`develop` @ `a07cb59`):

```
   commit   : a07cb5919317226b835aa4b6a363fab2e77f6f55
REFUSED (2) - this commit has NO CI run of any kind.
Nothing has ever built or tested a07cb5919317226b835aa4b6a363fab2e77f6f55.
>>> deploy.ps1 EXITCODE = 3
```

The branch the deploy script points at by default has **never been tested by
anything**. That was true before this gate existed, and nothing said so.

**Proof C — the falsification control: the gate script itself removed.** An
absent check is indistinguishable from a passing one, so its absence must
refuse. Run with `GIT_SSH_COMMAND=false` as a safety net, so no push could reach
a remote even if the guard failed:

```
[0/4] CI gate - has origin already passed this commit?
DEPLOY REFUSED - the CI gate script is missing:
  C:\Users\srika\nexusqa\scripts\require_green_ci.ps1
A deploy does not proceed past a gate that is not there.
>>> deploy.ps1 EXITCODE = 3
```

`[1/4] Pushing…` never printed in any of the three.

**Positive control — the gate can also say yes** (`d5130e4`, all workflows
green). Without it the three refusals above would prove only that the script
always refuses:

```
   Nexus QA CI                      success
   M0.5 Security Gate               success
CI GATE PASSED - every gating workflow is green on d5130e4843ff...
>>> EXITCODE=0
```

### §9.5 · Three defects found in this gate *while building it*

Recorded because each one would have shipped a gate that looked like it worked.

1. **The gate green-washed a red commit.** `ConvertFrom-Json` does not enumerate
   an array down the pipeline in PowerShell 5.1 — it emits the whole `Object[]`
   as one item. `$latest.conclusion` therefore returned *every* conclusion, and
   `@("success","success","cancelled") -eq "success"` is PowerShell's filter
   operator, which returns two matches: a non-empty, therefore **true**, value.
   The gate reported GREEN on `36adb1f`, whose suite was cancelled. Found only
   because a **known-red commit was used as a control** rather than a run of
   known-green ones.
2. **`gh run list --commit <short-sha>` returns `[]` and exit 0.** It silently
   matches nothing — no error, no warning. Here that failed closed, but it would
   have refused every legitimate deploy while printing "this commit has NO CI
   run" about a fully green commit. The gate now resolves to a full
   40-character sha first.
3. **A missing gate script reported success.** The invocation path was corrupted
   to `"$PSScriptRoot" + CR + "equire_green_ci.ps1"`; PowerShell raised
   CommandNotFound and left `$LASTEXITCODE` at **0**, so the deploy aborted on an
   unhandled exception while telling its caller it had succeeded. Two halves of
   CLAUDE.md §3 in one line: a lone backslash read as an escape by the writing
   tool, then a later *text-mode* read of this CRLF file promoting that CR into
   a real line break. Fixed with `Join-Path` (no backslash in the source at all)
   plus the explicit `Test-Path` refusal that Proof C exercises.

### §9.6 · The exit criterion, measured rather than asserted

```
$ bash scripts/gate0_require_ci_lanes.sh --audit-runs 2026-08-20
   commits   : 238
-- 17/238 commits have a SUCCESSFUL 'Nexus QA CI' run
AUDIT FAILED — 221 commit(s) in the window were never tested green.
(exit 1)
```

**"Every trunk commit has a CI run" is not reachable under the current trigger
design, and that is structural rather than a backlog.** A push of N commits
fires **one** run, on the tip — GitHub does not build per commit — and
`cancel-in-progress: true` kills that one when the next push lands. Pushing the
68-commit backlog produced exactly one run, not 68.

Closing it requires a decision, not more work: push per commit, drop
`cancel-in-progress` on trunk, or state plainly that **the tested unit is the
pushed tip, not the commit**. The third is honest and nearly free, and is the
recommendation. Until one is taken the audit will keep failing, correctly.

### §9.7 · What is NOT claimed

* **The reconciliation of `origin/develop` is NOT done.** It remains
  `BRANCH_RECONCILIATION_SCOPE.md` §3's open owner decision. New evidence for
  that conversation, measured 2026-08-31: `origin/develop` is a single commit
  `ba4fd8f` dated **2026-04-14**, and **no branch on the remote shares any
  history with it** — all five working branches return an empty `merge-base`.
  It has zero dependents, so replacing it destroys no one's work.
* **The bootstrap `push:` trigger in `ci.yml` is NOT removed.** Removing it
  before pull requests can be opened would stop CI running on the working branch
  altogether. It is the last step of this chain, not the first.
* **`deploy.ps1`'s green path was NOT executed live.** Running it to completion
  means deploying. The pass verdict is proven at the `require_green_ci.ps1`
  level (positive control, §9.4); `deploy.ps1`'s propagation of exit 0 is
  proven by inspection only.
* **The required-contexts set was NOT changed.** `--apply` was not run;
  protection on `develop` still carries the same 18 contexts, and A5's three
  lanes remain advisory.
* **Nothing was deployed to the VM in this session.** The fleet still runs
  whatever it ran before.
