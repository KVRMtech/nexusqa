# A34 — `_scan_fleet` scalability: the measured bound, and the decision

**Status: DECIDED — Option B, bound formally accepted, with a measured trigger.**
**Decision date:** 2026-08-21 · **Recorded by:** Gate 4 (Phase 3 proofs)
**Evidence:** `Nexus_power/evidence/gate4/a34_scan_fleet_scale.json`
**Reproducer:** `platform/qe-central/tests/fleet/a34_scan_fleet_scale.py`

---

## 1. What was actually asked

A34 forbids the outcome "the behaviour stays because nobody changed it". Either
batch the scan and prove it under load, or accept the bound **explicitly**, with
a measurement, a growth expectation, an operational threshold, and the trigger
that will force batching later.

The pre-existing position was M3.3 §6.5:

> `_scan_fleet` is O(tenants) round trips. Correct and RLS-safe, but at ~1000+
> tenants the per-tenant loop needs batching … Fine at current fleet size; a
> known bound, stated rather than hidden.

That is an honest statement and it is **not a decision**, because it contains no
number. "Fine at current fleet size" cannot be checked, defended, or alerted on.

---

## 2. The shape being measured

`_scan_fleet` enumerates the tenant registry once, then per tenant opens **one
transaction** containing **three queries** — active cycles, unprocessed change
events, and active apps — each scoped by `nexus.current_tenant_id` so RLS
admits exactly that tenant's rows.

    tenants T  ⇒  T transactions, 3T round trips, on every discovery tick

RLS is the reason it is shaped this way, and RLS is not negotiable: the fix for
the T-FL-05 blindness was to scan *inside* each tenant's scope rather than to
weaken the policy.

---

## 3. The measurement

Real PostgreSQL 15, the real migration-built schema, the real `_scan_fleet`
imported from the driver, under the **production RLS posture** — every statement
runs as `qec`, which is `NOSUPERUSER` / `NOBYPASSRLS`, with
`relforcerowsecurity=t` on `client_apps`, `app_cycles`, `change_events`. The
harness refuses to record a single timing unless that posture is verified first,
because a scan measured as a superuser does not run the plan production runs.

Each synthesised tenant owns one active app with a non-empty `latest_artifact_id`,
so every tenant is visible to the scan. 5 repeats per step, median reported.

| tenants enumerated | p50 scan | per-tenant | round trips |
|---:|---:|---:|---:|
| 9 | 0.088 s | 88.2 ms | 27 |
| 16 | 0.165 s | 20.6 ms | 48 |
| 40 | 0.445 s | 13.9 ms | 120 |
| 72 | 0.765 s | 12.0 ms | 216 |
| 136 | 1.626 s | 12.7 ms | 408 |
| 264 | 2.715 s | 10.6 ms | 792 |
| 520 | 6.200 s | 12.1 ms | 1 560 |
| 1 032 | **11.98 s** | 11.7 ms | 3 096 |

**The cost is linear at ≈ 11.7 ms per tenant** once the fixed connection
overhead amortises (the 88 ms at 9 tenants is that overhead, not per-tenant
work). ≈ 3.9 ms per round trip on loopback.

> **These are laptop-Docker numbers.** The *shape* — clean linearity — is the
> transferable result. The absolute milliseconds are not a production SLO, and a
> managed Postgres across a real network link will have a materially larger
> per-round-trip constant, which makes the threshold in §5 **conservative in the
> wrong direction**. Re-measure there before relying on the headroom.

### 3.1 The first dataset was invalid, and why that matters

The first sweep reported per-tenant cost falling from 32,175 ms to 13.6 ms — an
"efficiency curve" that was pure artefact. An earlier run had been killed
mid-sweep by a Docker outage, so its `finally` cleanup never ran and 1 024
harness tenants stayed in the registry. Every subsequent step therefore scanned
1 024 tenants while being labelled 1, 8, 32 …

The guard in place at the time asserted only `apps_returned >= n` — it caught
*under*-reaching and was blind to *over*-reaching. The harness now purges its
namespace **before** seeding and asserts the enumerated fleet is exactly
`baseline + n`, so a timing can no longer be attributed to a fleet it was not
taken against. The invalid table is recorded here rather than deleted, because
the failure mode — a plausible curve from a contaminated fixture — is the one
worth remembering.

---

## 4. The decision: **Option B — accept the bound**

Batching is **not** implemented now. The reasoning, in order of weight:

1. **The consumer is switched off.** `_scan_fleet` runs only from the cycle
   daemon, which is inert unless `QEC_CYCLE_TICK_SECONDS > 0`. That variable is
   set in no compose file, no env template, and no deployment in this
   repository. Optimising a disabled code path is speculative work.

2. **The current fleet is three orders of magnitude below the bound.** The
   substrate registry holds **8** tenants. At 8 tenants the scan costs ~0.09 s.

3. **The batched design touches the RLS boundary.** The named fix — a narrow
   `SECURITY DEFINER` enumerator, as `qec_resolve_webhook_app` already does — is
   a function that reads *across* tenants by design. That is exactly the shape
   whose previous absence caused T-FL-05, and exactly the shape that, written
   carelessly, reintroduces cross-tenant reads. Taking that risk to save 90 ms
   on a disabled daemon is a bad trade.

4. **The bound is linear, not quadratic.** Linear growth with a small constant
   is a scheduling problem, not an architectural one. There is no cliff.

### Trigger — the condition that makes batching mandatory

Batching becomes **required work, not a suggestion**, when *any* of:

* **T1 — Fleet size.** The tenant registry exceeds **250 tenants**. At the
  measured constant that is a ~3 s scan; at 250 the linear model is still well
  inside any plausible tick, and 250 leaves room to schedule the work before it
  is urgent.
* **T2 — Duty cycle.** The p50 scan exceeds **25 % of `QEC_CYCLE_TICK_SECONDS`**
  on the deployed database. A daemon that spends a quarter of every tick
  discovering work has no headroom for a slow tenant or a failover.
* **T3 — The daemon is enabled in production at all** with more than 100
  tenants — i.e. the first time this code path carries real load, it gets
  re-measured on that database rather than on a laptop.

At a 60 s tick, T2 is reached at ≈ 1 280 tenants; at a 30 s tick, ≈ 640. **T1
fires first by design**, which is the point: the trigger is meant to arrive
before the pain.

---

## 5. Two findings the measurement surfaced

### 5.1 The per-tenant `LIMIT` does not bound fleet-wide work — the sharper bound

The documented concern is round trips. The materialisation is worse, and it is
not what §6.5 describes.

Per tenant, the scan issues `LIMIT :lim` with `lim = limit * 4` for apps and
`limit * 20` for change events (`limit` defaults to 50). The **fleet-wide** cap
is applied only afterwards, in `_discover_due_work`, as `out[:limit]`.

So at the default limit, per tenant the scan may materialise up to 200 app rows
and 1 000 change-event rows — and it accumulates them into Python lists across
*every* tenant before anything is truncated:

| tenants | worst-case rows accumulated | rows actually used |
|---:|---:|---:|
| 8 | ~9 600 | ≤ 50 |
| 250 | ~300 000 | ≤ 50 |
| 1 000 | ~1 200 000 | ≤ 50 |

Memory, not round trips, is the first thing that will break, and it will break
as an OOM in the daemon rather than as a slow tick. **This does not change the
decision** — at 8 tenants the worst case is ~9 600 rows — but it sharpens T1:
whoever implements batching must push the fleet-wide cap *into* the query, not
just reduce the number of round trips.

### 5.2 An unreadable tenant registry silently collapses the fleet to one scope

`fleet_tenant_ids()` catches its own failure and returns `[PLATFORM_SCOPE]`
rather than propagating. That is a deliberate, documented choice — an empty list
would be indistinguishable from "the fleet is idle" — and it logs
`qec.controlplane.tenant_enum_failed` at WARNING.

But the *caller* cannot tell the difference: `_scan_fleet` then completes
normally, scans one scope, discovers almost nothing, and returns successfully.
The daemon reports a healthy tick.

This is not hypothetical. It happened during this milestone: the harness pointed
the substrate engine at a role that does not exist in the development database,
and the sweep produced perfectly plausible timings for a "fleet" of one. It was
caught only because the harness cross-checks the enumerated count.

**Recommendation (not blocking):** have the discovery tick emit a metric for
`len(tenants)` and alert when a fleet that had N tenants suddenly enumerates 1.
The fail-soft is right; the silence about it is not.

---

## 6. Reproducing this

```bash
QEC_TEST_QEC_DATABASE_URL=postgresql+asyncpg://…/qecentral \
QEC_TEST_SUBSTRATE_DATABASE_URL=postgresql+asyncpg://…/nexus \
python -u platform/qe-central/tests/fleet/a34_scan_fleet_scale.py \
        --steps 1,8,32,64,128,256,512,1024 --repeats 5 --out evidence.json
```

The harness refuses to produce numbers unless `current_user = qec`, `BYPASSRLS`
is off, `FORCE RLS` is on, and the enumerated fleet matches the step it is
labelled with. Every one of those refusals exists because the corresponding
mistake was actually made while producing this document.

---

## 7. Review

This decision is recorded, not ratified. It should be revisited by whoever owns
fleet architecture, and **must** be revisited when any trigger in §4 fires. If
the daemon is turned on in production before then, §4 T3 applies immediately.
