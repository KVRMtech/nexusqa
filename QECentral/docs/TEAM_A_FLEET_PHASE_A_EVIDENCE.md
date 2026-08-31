# Team A · Fleet & Scheduling — Phase A evidence record

Prepared 2026-08-31 against branch `feat/qec-dynamic-catalog-p0-p6`
(entry tip `fa74285`; contracts frozen first at `517430e`). Author: Team A
session. Status per claim is stated per section; the one-line summary is:

> **A1, A2, A3 implemented and test-proven, including the T-FL-08 strict
> xfail XPASSING and being removed, and a LIVE squid+Chromium proof of the
> per-crawl fence. NOT deployed. The VM exit measurements (two registered
> workers, three back-to-back gates, capacity-2 dispatch) have NOT been run.
> Nothing in this record is CI-verified on origin (gap G8 stands).**

---

## 0. Contracts frozen before code (entry condition)

`Nexus_power/contracts/fleet_heartbeat_v1.json` and
`Nexus_power/contracts/fleet_egress_fence_v1.json`, committed alone at
`517430e` before any implementation. Each service asserts them in its own
process (the two `app` packages cannot share an interpreter):

| half | file |
| --- | --- |
| qe-central heartbeat | `tests/fleet/test_a_worker_announces_itself_and_stays_announced.py` |
| qe-explorer heartbeat | `tests/test_heartbeat_contract.py` |
| qe-central fence (producer) | `tests/unit/test_egress_fence_per_crawl.py` |
| qe-explorer fence (consumer) | `tests/test_fence_identity.py` |

The Team F dependency is carried honestly: `worker_identity: "fleet-secret"`
in the contract — a worker id is a claim signed with the shared fleet secret,
the same trust level as a completion callback. A per-worker credential
replaces it in a later schema version.

## A1 — worker heartbeat protocol

**Built.** `qe-explorer/app/heartbeat.py` (FleetAnnouncer: register with
backoff → beat on the interval the RESPONSE advertises → 404 ⇒ re-register),
started from the lifespan; config `QEC_WORKER_ID/_URL/_ALLOWLIST_PATH`,
`QEC_FLEET_REGISTER`. qe-central routes in `app/routers/fleet.py`
(`worker_router`), mounted under **/internal**/fleet/workers/… — deliberately
not a bare `/fleet`: only the /internal prefix gets the T-SEC-02 boundary
token check before any handler; handlers verify the scope-bound v2 signature
on top. Registry writes through the existing `worker_registry` store;
`validate_registration` is pure and fail-closed (a worker whose fence path
qe-central cannot write is refused 422 — degraded to the static pool, never a
half-registered worker).

**Staleness retires a row, visibly**: `retire_stale_workers` (status →
`stale` at TTL, guarded on `active` so an operator's `draining` survives),
`reap_stale_workers` (delete at retention), both under the new leader-elected
`worker_registry_sweeper_daemon`, env-gated on
`QEC_WORKER_REGISTRY_TICK_SECONDS` per the house daemon convention. Compose
sets tick 15s / retention 60s < TTL 90s so the A1 done-when
(`SELECT count(*)` drops to 0 within one TTL of stopping the explorer) holds
by arithmetic; the retirement WARNING is the operator's corpse-for-debugging.

**Proven** (runner lane, migrated qecentral DB @ qec_025):
`test_a_worker_announces_itself_and_stays_announced` — the full lifecycle:
announce → schedulable; heartbeat carries the worker's own `in_flight` (heals
slot drift); silence ⇒ retired at TTL (unschedulable even at TTL=∞); a
heartbeat revives it (carries its own status); silence past retention ⇒ row
GONE; unknown ⇒ 404 naming re-register; re-registration resets `in_flight`.
Auth negatives: unsigned ⇒ 401 before validation; a signature scoped to
another worker id ⇒ 401; path/body worker mismatch ⇒ 400.

**Proven, live, two processes** (2026-08-31, `live_announce_proof.py` +
`announce_worker_side.py`): qe-central's REAL `worker_router` served by
uvicorn on a real port against a migrated qecentral database, driven by the
EXPLORER's own `FleetAnnouncer` — its signing, its payloads — running in its
own interpreter, because the two `app` packages cannot share one (the same
constraint that makes the frozen contract necessary; a first attempt to do it
in-process was caught by an assert and restructured rather than fudged). All
17 checks PASS:

```
forged_secret_refused ← CONTROL: a wrong fleet secret is refused
                        (unknown_key_id) and writes NO row
registered · row_exists · identity_declared · capacity_landed
allowlist_path_landed · schedulable
heartbeat_ack · liveness_advanced · worker_own_in_flight · interval_advertised
retired_at_ttl · row_kept_for_debugging · retired_row_unschedulable
count_drops_to_zero          ← the A1 done-when, at TTL 6s / retention 9s
unknown_worker_404 · re_registered
```

The control is what makes the rest mean anything: without it, every "the row
appeared" check would pass equally against a route that accepted anything.

**Still not claimed**: the same measurement ON THE VM, against the deployed
containers — that needs a deploy this session did not perform.

## A2 — the queue drainer, on and honest

**Built.** Compose now sets `QEC_QUEUE_DRAIN_TICK_SECONDS` (default 5).
`drain_once` **refuses the pass while no worker has registered** — the
scheduling source is the static pool ⇒ `{"refused": "registry_empty"}`, a
paced WARNING naming the cause and the `QEC_QUEUE_DRAIN_STATIC_POOL=1`
override, and a WARNING on the empty→populated transition so the log records
when draining began. The reaper's queue-timeout remains the backstop.

**Proven**: `test_a_queue_with_no_workers_says_so.py` — refusal + loud log,
the registry-source falsification control (plans a drain), the override, the
log pacing (≤2 warnings per 61 ticks), the recovery announcement. Plus the
whole pre-existing T-FL-01 durable-queue suite green on the DB lane.

**Not claimed**: three golden gates back-to-back with no APP_UNHEALTHY — a VM
measurement.

## A3 — per-crawl egress fence; the clamp removed

**The consumer-side fix the ARB record said was the only real one.** squid
6.13 ships `basic_fake_auth`; the crawl's PROXY LOGIN is the per-request
identity:

* producer — `_write_egress_allowlist(domains, allowlist_path, *, crawl_id)`
  (REQUIRED kwarg) → `egress_fence.py`: atomic per-crawl dstdomain file
  `crawls/allowlist.<crawl_id>.txt`, regenerated `crawls.conf` (ACL pair +
  allow per live crawl, naming squid's view of the paths), content-compared
  `reload.stamp`; add-order file→conf→stamp, remove-order conf→stamp→file so
  a HUP never reads a config naming a missing file; age-GC backstop; released
  at completion (`internal._settle_worker_accounting`), on dispatch failure,
  and by the reaper.
* consumer — new `squid.conf`: `proxy_auth REQUIRED` challenge before the
  generated `include`, final deny-all; the legacy `allowed_domains.txt` allow
  rule is GONE (an old qe-central against the new conf reaches nothing —
  fail-closed in every mixed-version direction). Parsed clean by the real
  `ubuntu/squid:6.13` image. The `%un` access-log field now records the crawl
  id per egress line.
* explorer — `app/fence.py`: each browser context is created with
  `proxy={server: egress_proxy, username: crawl_id, password: "fenced"}`
  (password documented as a constant, not a secret — `basic_fake_auth` does
  not verify it and pretending otherwise would be theatre). JobManager takes
  `capacity` (default 1 = the proven single-flight posture;
  `QEC_EXPLORER_CAPACITY`). Health reports capacity/in_flight — the same
  numbers the heartbeat carries.

**The ordered sequence the pairing test dictated, executed and logged:**

1. writer made per-crawl → the strict xfail **XPASSed** on the DB lane:

   ```
   [XPASS(strict)] KNOWN CROSS-TENANT EGRESS DEFECT — the fence is per-WORKER and
   concurrent dispatches on one worker overwrite each other. … the day the fence
   becomes per-crawl this XPASSes and the marker must be removed.
   ```

2. marker removed — `test_the_egress_fence_survives_concurrent_dispatch_on_one_worker`
   now stands as the permanent green regression proof;
3. `FENCE_IS_PER_WORKER = False` — capacity means capacity; fleet totals and
   `explain_unavailable` quote registered room;
4. the pairing survives **inverted**: writer-loses-crawl-id ⇒ build fails
   until the clamp returns (`test_a_shared_fence_admits_one_crawl_per_worker.py`),
   and the old latent-to-live tripwire is replaced by
   `tests/contract/test_egress_fence_per_crawl_tripwire.py` — capacity may be
   unclamped only while the shipped squid.conf still selects fences per crawl
   (asserted as bytes against the contract). The revert path is exercised by
   forced-clamp controls so it cannot rot.

**Proven, live, on this machine** (2026-08-31, `live_fence_proof.py`): real
squid 6.13 (shipped image + the repository's shipped squid.conf,
compose-identical boot incl. the /dev/stdout chown), fences written by the
production `egress_fence` module, real Chromium via Playwright per-context
proxy credentials — **on one worker root**:

```
a_own 200 · a_foreign 403 · b_own 200 · b_foreign 403 · no_credentials 407
a_after_release 403 · b_still_fenced_control 200        → all 7 checks PASS
```

squid's own access log records the crawl identity per request — the
per-crawl egress evidence line the bundle can join on:

```
TCP_DENIED/407 … GET http://example.com/ -      (no identity: reaches nothing)
TCP_DENIED/403 … GET http://example.com/ cid-a  (a's login, b's host: denied)
TCP_MISS/200   … GET http://example.org/ cid-b  (b's login, b's host: allowed)
```

Every negative had its positive control in the same run, so a dead proxy or
dead DNS could not have faked a pass.

**Proven, control-plane** (runner DB lane, post-flip): 118/121 → after the
three clamp-era t_fl_02 tests were converted to explicit revert-path
controls, the full Team A suite is green — T-FL-08 unmarked (24 crawls / 4
tenants / 3 workers × capacity 2: no fence violation, no over-subscription,
no failed-because-busy, tenant isolation by predicate and PK), T-FL-04
rewritten to per-crawl semantics incl. the new
`test_two_crawls_on_one_worker_keep_independent_fences`, R4 dispatch
ordering, quota-gate ordering.

**Capacity > 1 per worker**: admission, fleet totals, JobManager and the
fence are all capacity-true and proven at capacity 2 in T-FL-08 and the
JobManager tests. Compose ships `QEC_EXPLORER_CAPACITY` default **1** —
raising it on the VM is an ops decision gated on this build being deployed
end-to-end (writer + squid.conf + explorer together; `deploy.ps1` now
recreates `qec-egress-proxy` whenever qe-explorer deploys so the consumer
cannot lag the producer). A `fleet2` compose profile adds
`qe-explorer-2` + `qec-egress-proxy-2` with their own fence volume for the
two-worker exit measurement.

### Who else goes through the proxy? (the "what did this break" check)

Requiring a proxy login makes every un-credentialed request 407, so the
question is whether anything OTHER than the crawl context egresses through
squid. Measured, not assumed:

* `grep new_context(` across the whole explorer app → **one** site
  (`main.py`, the crawl context) — the one that now carries the credentials.
  `playwright_port.py` mentions it only in a docstring.
* `grep egress_proxy|proxy=` across the app → the launch flag (unchanged, and
  it must stay: Chromium routes a per-context proxy through the launch proxy),
  the health field, and the new per-context call.
* The service-root harness scripts (`gate2_journey.py`, `measure_*.py`,
  `record_live_capture.py`) reference no proxy at all — they launch plain
  Chromium and were never fenced by squid. Confirmed independently by the
  session running the live summit journeys on that harness.

So the blast radius of the auth requirement is exactly the production crawl
path, which is the path that carries the identity.

## Worker accounting completes the loop

Dispatch stamps `worker_id` onto the exploration row (`jsonb_set`, not a
wholesale stats replace — a fast completion cannot be clobbered); the
completion callback releases the registry slot and retires the fence exactly
once (past the idempotency gate, so a duplicate callback cannot double-free a
slot another crawl now holds); the reaper does the same for crawls that will
never call back; heartbeats reconcile `in_flight` from the worker's own count
as the standing healer. `crawl_liveness` now unions REGISTERED workers with
the static pool — without this, worker 1's definitive 404 would have read as
"dead" for a crawl on worker 2 and the reaper would kill healthy crawls the
moment the fleet grew.

## What is NOT claimed

* **NOT deployed; NOT live-proven on the VM.** Every "Exit (measured)" item —
  two registered workers, capacity 2 each, two tenants concurrently on one
  worker on the VM, three back-to-back gates with no APP_UNHEALTHY — awaits a
  deploy this session did not perform.
* **Not CI-verified on origin** (G8): this machine cannot push to origin;
  the runner-lane results above are the CI-shaped substitute (same bootstrap
  script, same DSN roles, migrated chain @ qec_025).
* **Worker identity is the fleet secret** (Team F seam, in the contract).
* The explorer **browser lane** (~900 tests) was not run for this change; the
  non-browser explorer suite was (2751 passed; 3 unrelated walker-area tests
  failed in-lane and pass in isolation while Team B rewrites those subjects —
  re-run before certifying their area).
* Helm still has no explorer/proxy manifests (T3.3 remainder; G6 postgres
  emptyDir untouched — other owners).
* `QEC_QUEUE_DRAIN_STATIC_POOL=1` exists as a documented operator override;
  it re-enables blind static-pool draining and is off by default.

## Fleet suite tally (runner DB lane, post-everything)

```
tests/fleet/test_t_fl_02_worker_registry.py          (extended: validation, retirement, revival)
tests/fleet/test_a_worker_announces_itself_and_stays_announced.py   (new, A1)
tests/fleet/test_a_queue_with_no_workers_says_so.py  (new, A2)
tests/fleet/test_t_fl_01_durable_queue.py
tests/fleet/test_t_fl_04_egress_isolation.py         (rewritten per-crawl)
tests/fleet/test_t_fl_08_concurrency_redteam.py      (xfail marker REMOVED)
tests/unit/test_egress_fence_per_crawl.py            (new, producer contract)
tests/unit/test_explorer_pool.py
tests/test_a_shared_fence_admits_one_crawl_per_worker.py  (rewritten, both directions)
tests/contract/test_egress_fence_per_crawl_tripwire.py    (replaces latent-to-live)
tests/security/test_r4_dispatch_ordering.py
tests/test_crawl_quota_enforcement_m34.py

    122 passed, 1 skipped, 61.71s
```

The 1 skip is the T-FL-08 object-storage test (needs `QEC_TEST_S3_ENDPOINT`;
MinIO was not provisioned in this lane — its property is untouched by Phase A).
Explorer side: `test_heartbeat_contract.py` + `test_fence_identity.py` +
`test_no_import_cycles.py` green (24 tests); the whole non-browser explorer
suite **2774 passed, 2 xfailed, 0 failed** on the post-commit tree.

(An earlier run of that suite showed 3 walker-area reds — `test_crawler_logic`,
`test_decision_points`, `test_e2e_advance`. They passed in isolation at the
time and are green in this full run: another session was landing Team B's
walker/submit rewrite into this shared checkout mid-lane. Recorded because a
red on a shared tree is not evidence until it survives a rerun.)

DB lane provenance: throwaway `postgres:16-alpine` + the production
`scripts/qec_ci_db_setup.sh` (same bootstrap SQL, same least-privilege
`qec`/`qec_substrate` roles, both alembic chains at head — qec_025), driven
from a `python:3.11-slim` runner with the service's own requirements — the
same shape as the CI job this branch cannot currently reach (G8).
