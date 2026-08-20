# M3.3 — Fleet Concurrency: Evidence Report

**Date:** 2026-08-19 · **Branch:** `feat/qec-dynamic-catalog-p0-p6`
**Status:** code-complete, locally proven. **NOT deployed, NOT cluster-proven.**

The first engineering requirement of this milestone was:

> Do not increase concurrency until tenant isolation and egress isolation are proven.

That ordering was followed. Isolation (T-FL-04, T-FL-05) was fixed and proven
before the queue and registry (T-FL-01, T-FL-02) were allowed to raise
concurrency.

---

## 1. Re-grounding: what already existed

Two components were found **already implemented and correct**, and were wired
rather than rewritten:

| Component | Finding |
|---|---|
| `controlplane/scheduling/crawl_queue.py` | Pure core (`plan_admission`, `fair_drain_order`, `queue_positions`, `CLAIM_QUEUED_SQL` with `FOR UPDATE SKIP LOCKED`) — fully tested, **zero importers**. Not rewritten. |
| M0.5 T-SEC-03 reserve-then-fence ordering | Already correct in `_dispatch_explorer`. Preserved and re-pinned, not redesigned. |
| Reaper queue support | `ACTIVE_STATUSES` already included `queued`/`claimed`, and a queue-timeout reason existed. Reused. |
| Reaper per-tenant RLS scan | The correct RLS-safe pattern already existed in `reaper.py`. Extracted and shared rather than reinvented. |

---

## 2. Defects found and closed

| # | Defect | Where | Evidence |
|---|---|---|---|
| D1 | **A busy fleet marked crawls `failed`** — indistinguishable from a broken application, and the work was lost with no retry | `routers/explorations.py` | T-FL-01 |
| D2 | **`_scan_fleet` was blind under production RLS** — GUC-less fleet query returned **0 rows**; cycle daemon discovered no work, silently | `controlplane/cycle/driver.py` | T-FL-05 |
| D3 | **Two workers could share one egress allowlist file** — a config-only cross-tenant egress leak, with no code defect and no error anywhere. Nothing enforced the "own file" rule the docstring stated | worker pool config | T-FL-04 |
| D4 | **Evidence handoff assumed a shared filesystem** — pod-local `emptyDir`; a completed crawl on a different pod read as `no manifest produced`, and a pod restart destroyed it | `routers/internal.py`, `completion_recovery.py` | T-FL-03 |
| D5 | **Journeys API N+1** — ~5 queries/journey; 136 queries for 25 journeys | `routers/journeys.py` | T-FL-06 |
| D6 | **No evidence GC** — unbounded disk growth, which concurrency multiplies | (new) `controlplane/evidence_gc.py` | T-FL-07 |
| D7 | **Static pool scheduling** — no liveness, capacity, or least-loaded; worker[0] absorbed every attempt; dead workers kept being offered work | `clients/config.py` | T-FL-02 |

---

## 3. Measured evidence

### 3.1 RLS fleet scan (T-FL-05)

Database posture verified before any conclusion: role `qec` is
`rolsuper=f`, `rolbypassrls=f`; `client_apps`, `app_cycles`, `change_events`,
`qe_explorations` all `relrowsecurity=t`, `relforcerowsecurity=t`.

```
no GUC set (what _scan_fleet did)  ->  0 apps visible
GUC = tenant_a                     ->  1 app visible
tenant_a sees app_a1 / tenant_b sees app_b1   (isolation holds)
```

**Fix:** enumerate tenants once, then scan each under its own
`nexus.current_tenant_id`. **RLS was not weakened** — every read still happens
under an enforcing policy. The shared helper (`controlplane/tenant_scope.py`) is
now used by both the reaper and the driver so the rule cannot drift again.

### 3.2 Journeys API N+1 (T-FL-06)

Query count measured by hooking SQLAlchemy `before_cursor_execute`:

| Journeys | Before | After |
|---:|---:|---:|
| 1 | ~13 | **13** |
| 2 | 21 | **13** |
| 5 | ~34 | **13** |
| 10 | ~60 | **13** |
| 25 | 136 | **13** |
| 50 | ~260 | **13** |

Flat. A separate test asserts the batched rollup returns values **identical** to
the per-journey implementation — a faster endpoint returning different numbers
would be a regression, not an optimisation.

### 3.3 Database under load (T-FL-06)

Harness: `tests/fleet/loadtest_db.py`. 4 tenants × 40 journeys, 16 concurrent
× 4 rounds (152 ops), measured from **PostgreSQL's own statistics views**, not a
client stopwatch.

```
peak connections        : 16 / max_connections 100     (84 headroom)
peak waiting locks      : 0
longest transaction     : 1.141s
connections after load  : 11 idle-in-pool, 0 leaked

journeys_list       n=64  p50=2232ms  p95=4346ms
pool_checkout_wait  n=64  p50= 782ms  p95=3226ms   <-- dominant cost
queue_drain_plan    n=16  p50=2840ms  p95=4599ms
rls_fleet_scan      n=8   p50=2923ms  p95=4643ms
```

**Honest finding:** the bottleneck at this concurrency is the **application-side
SQLAlchemy pool** (`pool_size=10, max_overflow=5`), not PgBouncer
(`default_pool_size=100`) and not Postgres (84 connections of headroom). Pool
checkout is ~35% of `journeys_list` p50. The remedy is more replicas or a larger
per-replica pool — **not** a bigger PgBouncer pool.

**A first run of this harness was discarded**: it inherited
`QEC_TEST_DB_NULLPOOL=1`, which opens a fresh connection per checkout, and
reported p50 = 11.5s / 35 peak connections. Those numbers were almost entirely
TCP + auth setup, not query cost. They are recorded here because reporting them
would have been exactly the "claim scale from the wrong measurement" this
milestone forbids.

Absolute latencies are inflated by a Dockerised Postgres on Windows; the
**shapes** (flat connection ceiling, zero lock contention, pool-wait dominance)
are the load-bearing results.

### 3.4 Worker capacity under concurrency (T-FL-02)

10 concurrent acquisitions against a worker of capacity 3 → **exactly 3 win**.
The decision is a single conditional `UPDATE` (`in_flight < capacity` evaluated
under the row lock the UPDATE takes), so there is no check-then-write window.

### 3.5 Egress isolation red-team (T-FL-04) — the stop condition

Real files, real concurrency, no mocked writer:

- 8 tenants dispatching **simultaneously** to 8 workers: every crawl saw only
  its own destination in the fence **at dispatch time**, and each file held
  exactly one tenant's hosts afterwards.
- Directed attack: a tenant hammering its own worker 20× while a victim crawl is
  live — the victim's fence was byte-unchanged, no `evil.example` reached it.
- Reserve-then-write: a worker that refuses the reservation has its allowlist
  file **never opened**; the incumbent's fence is byte-identical.
- **Shared-fence guard (new):** when N workers share an allowlist path, **all N**
  are refused work — not N-1, because whichever was kept would still have its
  fence rewritten by the others. Capacity loss is an incident; a shared fence is
  a breach.

### 3.6 Object-storage handoff (T-FL-03)

**Correction applied after first delivery.** The first implementation hand-rolled
a boto3 client and a private `QEC_EVIDENCE_*` env contract inside qe-central. That
was wrong, and the re-grounding step should have caught it — my search scope was
`platform/ engines/` and therefore excluded `sdk/`.

`nexus_sdk.storage` already provides this (`create_storage` + `ArtifactStore` over
s3/gcs/azure/local) and is used by five engines, platform-api, **and qe-central
itself** in `app/substrate/assets.py`, whose docstring states the requirement:
config comes from the same `NEXUS_STORAGE_BACKEND` contract "so assets are
co-readable across services — design §3.1 hard requirement".

The consumer half now goes through `ArtifactStore`. Two consequences beyond
tidiness:

- keys are **tenant-scoped** via `ArtifactStore.build_key(tenant, "eyes",
  "crawls", crawl_id)` — the hand-rolled layout keyed on `crawl_id` alone and had
  no such property, a strictly weaker position on the exact axis this milestone
  defends;
- one deployment's variables now configure every service, instead of qe-central
  needing a second, private storage convention.

The **producer** (explorer) still hand-mirrors the layout, deliberately: the
contained Playwright image carries no `nexus_sdk` dependency (it is absent from
`requirements.txt`; the one SDK touch in `emit.py` is an import-guarded optional
with a local fallback), and that minimalism is a security property of the service
that runs a browser against customer applications. A contract test compares the
producer's keys against the **real** `ArtifactStore.build_key` so the hand-copy
cannot drift.

Two latent dependency gaps surfaced and were closed: qe-central installed the SDK
without the `storage-s3` extra (so `aiobotocore` was missing — which would also
have broken `assets.py`'s existing S3 path), and the explorer never declared
`boto3`.

Proven against **real MinIO**, not a mock. "Different pods" is reproduced
literally as *the producer and consumer share no filesystem*:

- producer writes to dir A → publish → consumer reads from **dir B** → manifest
  and all frames arrive byte-identical;
- **producer's directory deleted entirely** before the consumer runs (what a pod
  restart does to an `emptyDir`) → evidence still recovered;
- manifest is uploaded **last**, so a consumer racing the upload can never
  ingest a manifest whose frames are missing;
- a cross-service contract test asserts the producer's key layout equals the
  SDK's `build_key` output and that both read the house env contract — they share
  no library, and a drift would mean the producer publishes where the consumer
  never looks;
- tenant scoping is proven: the same crawl id under a different tenant resolves
  to a different prefix, and the second tenant reads nothing.

### 3.7 Disk GC (T-FL-07)

All four "never delete" guarantees proven individually, each in the fail-closed
direction: active crawl, audit evidence (separate 30-day clock), configured
retention, incomplete ingestion (`completion.json` present, `completion.ack`
absent). Unknown crawl, unrecognised directory name, unrecognised status, and
missing finish time all resolve to **keep**.

Sustained-load test: 6 rounds × 5 finished crawls with GC running — directory
stayed at **≤ 1 MiB**, zero accumulation.

### 3.8 Concurrency red-team (T-FL-08)

24 concurrent crawls, 4 tenants, overlapping domains, 3 workers × capacity 2
(6 slots):

| Assertion | Result |
|---|---|
| No crawl marked `failed` because the fleet was busy | ✅ zero |
| Every crawl accounted for (`completed` or honestly `queued`) | ✅ |
| Worker capacity never exceeded | ✅ peak ≤ 2 per worker |
| No egress fence violation | ✅ zero foreign hosts observed at dispatch |
| Fence topology sound throughout | ✅ |
| Tenant cannot read another's evidence (predicate **and** primary key) | ✅ |
| Queue fairness under concurrent submission | ✅ lone crawl beats flooder's 2nd |
| Stale worker stops receiving work; work reassigned | ✅ |
| 8 concurrent claims of one crawl → exactly one wins | ✅ |
| Concurrent evidence publish, no cross-contamination | ✅ |

---

## 4. Test totals

```
tests/fleet/                          97 passed     (the M3.3 proofs)
tests/unit + security + contract    1948 passed, 25 skipped, 3 failed
```

The three failures are **not M3.3**:

| Failure | Cause |
|---|---|
| `test_hmac_rotation_inflight_m34.py` (×2) | An **M3.4** test file, untracked in this working tree (`git status` = `??`), exercising `app/security/hmac_auth.py`. It references none of the M3.3 modules. Concurrent in-flight work, not a regression from this milestone. |
| `test_rls_isolation.py::test_page_visits_isolates_tenants` | My local substrate DB is a **stub** (`tenants` only). The real proof needs `page_visits` / `canonical_artifacts` from the VKPower alembic chain. |

On that last one: I deliberately did **not** hand-create those tables to turn the
test green. Its purpose is to prove the SHIPPED migration's policy isolates; an
approximation I wrote myself would prove only that my approximation isolates —
the exact green-wash this codebase exists to refuse. It needs the real substrate
schema (`scripts/qec_ci_db_setup.sh`), which CI builds and my local rig does not.

Three defects in my own delivery were found and fixed while verifying this:
removing `substrate_engine` from `reaper.py` broke `test_reaper_db.py`'s access
path; the new `explorer_workers` table had no entry in the schema-drift gate's
`_NO_ORM_MODEL`; and the storage refactor is described in §3.6.

Running the DB-gated suites locally requires `QEC_TEST_DB_NULLPOOL=1` and
`NEXUS_DATABASE_URL_SUBSTRATE` — without them the reaper contract tests fail for
environmental reasons that look like logic failures (each test drives its own
`asyncio.run`, and a pooled connection binds to the first event loop).

---

## 5. Autoscaling (T-FL-07)

`infrastructure/keda/qe-explorer-scaledobject.yaml` — validated YAML, 3 documents.

| Parameter | Value | Reason |
|---|---|---|
| scale-up trigger | `qec_crawl_queue_depth`, threshold **2** | CPU is the wrong signal: a browser crawl waits on the network and can saturate a pod at 15% CPU, so a CPU HPA scales *down* a full fleet |
| second trigger | `qec_crawl_queue_oldest_wait_seconds` > **300** | Depth alone under-reacts to a small queue that is not moving |
| min replicas | **1** | Scale-to-zero makes the first crawl after any quiet period pay a browser cold start |
| max replicas | **20** | Bounds the blast radius; unbounded scaling moves the bottleneck to the DB, which fails far less gracefully |
| scale-down stabilisation | **300s**, 1 pod / 120s | A removed pod may hold a live crawl worth tens of minutes |

Alerts: `QecEgressFenceConflict` (**critical** — security, not capacity),
`QecCrawlQueueStalled`, `QecFleetNoLiveWorkers`.

---

## 6. What is NOT proven — read this before deploying

1. **No Kubernetes cluster was involved.** The pod boundary is proven as its
   defining property (no shared filesystem), not by scheduling real pods on real
   nodes. The KEDA manifest is reviewed and YAML-valid, **never applied**.
2. **No real browser.** T-FL-08's explorer is a coroutine. Every *control-plane*
   property is exercised for real; Chromium against a live application is not.
3. **Squid was not in the loop.** Fence *files* are proven correct; that squid
   re-reads them is inherited from the existing deployment, unverified here.
4. **Nothing is deployed.** No migration applied outside the local test DB, no
   image built, no config shipped.
5. **`_scan_fleet` is O(tenants) round trips.** Correct and RLS-safe, but at
   ~1000+ tenants the per-tenant loop needs batching (a narrow `SECURITY
   DEFINER` enumerator, as `qec_resolve_webhook_app` already does for webhooks).
   Fine at current fleet size; a known bound, stated rather than hidden.
6. **Load numbers are from a Windows Docker Postgres.** Shapes are meaningful;
   absolute milliseconds are not a production SLO.

---

## 7. Ship gates (all inert by default)

Every M3.3 behaviour is env-gated and **off** unless explicitly enabled, so this
milestone ships without changing a running deployment:

| Variable | Unset behaviour |
|---|---|
| `QEC_QUEUE_DRAIN_TICK_SECONDS` | drainer returns immediately; no DB work |
| `QEC_EVIDENCE_GC_TICK_SECONDS` | nothing is ever deleted |
| `QEC_TENANT_CONCURRENCY_CAP` / `QEC_HOST_CONCURRENCY_CAP` | unlimited — un-provisioned tenants behave exactly as before |
| `QEC_EVIDENCE_STORE` | `filesystem` — today's behaviour byte-for-byte |
| worker registry (empty) | falls back to static `QEC_EXPLORER_POOL` |

Migration `qec_022` is purely additive (`explorer_workers`), with its tenant-free
status registered in the standing RLS coverage gate's allowlist with a written
reason.
