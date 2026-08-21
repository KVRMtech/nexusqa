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
| A21 | Two real crawls, three deliberate changes, three correct classifications | **real crawl** | ✅ producer 8/8 ×2, consumer 3/3 vs real Postgres |
| A22 | A really-discovered journey compiles and protects behaviour | — | not started |
| A23 | Real-application network trace with correct action joins | **live deployment** | ✅ 10/10 on 68 real events, 2 defects fixed |
| A24 | M2.6 capture against a live tenant | — | not started |
| A25 | M2.1 passes on the deployed artifact | — | not started |

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

Defect 3 is a product defect, not a test defect: a generated persona would fill a
real application with an age and a date of birth that contradict each other, and
the carrier's rejection would then be reported as the application's fault —
precisely the failure `fill_engine/persona.py` exists to prevent.

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

*(pending — see status table)*

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

*(pending — see status table)*

---

*Sections for A22, A24 and A25 are appended as each milestone produces its
evidence.*
