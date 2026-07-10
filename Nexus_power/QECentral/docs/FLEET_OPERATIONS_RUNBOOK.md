# VKPower Verdict — Fleet Operations Runbook (Phase 7)

**Audience:** the on-call operator and the platform lead running VKPower Verdict
for **20+ clients / thousands of apps**.
**Scope:** how to turn on multi-replica operation, scale it, keep it polite and
cheap (the change-triggered flywheel), and respond to every alert. Everything
here is grounded in code that already exists — the distributed admission
limiter, Postgres leader election, incremental regression, the cost meter, and
the `/metrics` exposition. This runbook adds no new runtime behaviour; it tells
you which knobs to turn and which signal to trust.

Companion artifacts:
- Observability bundle: `infrastructure/observability/verdict/`
  (`prometheus-verdict.yml`, `alerts-verdict.yml`, `grafana-verdict-fleet.json`, `README.md`).
- Capacity calculator: `scripts/verdict_capacity_model.py` (pure calc + `--selftest`).
- Deploy: `docker-compose.qec.yml`, `scripts/verdict_box_bootstrap.sh`.
- Backups: `scripts/verdict_pg_backup.sh` (nightly + `--restore-drill`).

---

## 1. Fleet topology at a glance

One Verdict "box" per client (or per isolation boundary) runs the self-contained
stack from `scripts/verdict_box_bootstrap.sh`:

```
Postgres (nexus + qecentral)   <- evidence system-of-record + control plane
Redis (db 3)                   <- distributed admission limiter store  [HA required]
platform-api + engines         <- the UNCHANGED VKPower factory (over HTTP)
qe-central  (:8093)            <- THE control plane: cycle driver, admission, cost, harness
qe-explorer / repo-intel       <- recorders/analysers behind qe-central
verdict-portal (:5273)         <- operator UI
prometheus + grafana           <- this bundle (scrape :8093/metrics)
node_exporter / redis_exporter / postgres_exporter
```

Multi-tenancy is enforced by Postgres RLS (`nexus.current_tenant_id` GUC set in
`tenant_scoped_qec_session`). The `/metrics` exposition is deliberately
**fleet-aggregate and PII-free** — no `tenant_id`/`app_id`/URL is ever a label —
so per-tenant numbers come from the tenant-scoped API/DB, not from Prometheus.

---

## 2. Turning on multi-replica operation

A single qe-central instance is the default (every scale-out knob defaults to
today's behaviour). To run **N replicas** of qe-central safely you flip exactly
two flags — both are already wired into `docker-compose.qec.yml` and generated
into `.env.production` by the bootstrap:

| Env var | Default | Fleet value | Effect |
|---------|---------|-------------|--------|
| `QEC_ADMISSION_BACKEND` | `memory` | `redis` | N replicas share **one** atomic, fail-closed admission limiter instead of N independent ones (otherwise the fleet would admit at N× the per-host rate — an attack on the customer's app). |
| `QEC_REDIS_URL` | `redis://redis:6379/3` | (same) | DSN for the shared limiter. |
| `QEC_DAEMON_LEADER_ELECTION` | `none` | `advisory_lock` | Exactly **one** replica runs the fleet-scanning cycle daemon (Postgres `pg_advisory_lock`); on leader death the session lock auto-releases and a follower takes over. |

Nothing else changes: the cycle driver, admission decisions, and cost metering
are byte-for-byte identical — only the *backend* of the limiter and the
*who-scans* question move from process-local to shared.

**Why both flags, not one:**
- Redis limiter without leader election ⇒ N daemons all scan and race to fire
  cycles (the `app_cycles` partial-unique index stops a duplicate *active* cycle,
  but you still pay N× the scan and thrash).
- Leader election without the Redis limiter ⇒ only one daemon *scans*, but any
  replica serving an API-triggered cycle still admits against its own local
  buckets ⇒ per-host politeness is under-enforced across replicas.

Verify after rollout:
- `sum(up{job="qe-central"})` equals your replica count.
- Exactly one replica logs `qec.leader.acquired`; the rest log `qec.leader.follower`.
- `qec_admission_decisions_total{reason="limiter_unavailable"}` stays at **0**
  (any non-zero rate ⇒ Redis is unreachable and the fleet is stalling — see
  [Redis HA](#redis-ha)).

---

## 3. Scaling replicas and workers

Sizing is driven by the [capacity model](#capacity), not by guesswork.

1. Run the model for your fleet:
   ```bash
   python scripts/verdict_capacity_model.py \
     --clients 20 --apps-per-client 100 --changes-per-app-per-day 0.5
   ```
2. Apply its outputs:
   - `recommended_max_global_cycles` → `QEC_MAX_GLOBAL_CYCLES` (global concurrency cap).
   - `recommended_replicas` → number of qe-central replicas.
   - Keep `QEC_MAX_PER_TENANT_CYCLES` **strictly below** `QEC_MAX_GLOBAL_CYCLES`
     so no single tenant can occupy every global slot (starvation guard — with
     `max_per_tenant < max_global`, at least `max_global - max_per_tenant` slots
     are always reachable by other tenants).
3. Scale **out** (more replicas), not just up — the shared Redis limiter means
   replicas are horizontally interchangeable and the admission cap is enforced
   globally regardless of replica count.

Scaling signals (Grafana "Admission depth" row / alerts):
- `qec_admission_in_flight` pinned at `QEC_MAX_GLOBAL_CYCLES` for 15m
  (`VerdictAdmissionInFlightSaturated`) ⇒ the cap is the bottleneck: raise the cap
  and add replicas.
- `>75%` of admissions denied on capacity reasons (`VerdictAdmissionDeniedSurge`)
  ⇒ contention; cycles are queueing (safe) but throughput is capped.
- p95 cycle duration climbing toward the 1800s ceiling ⇒ capacity or factory
  latency; check `qec_factory_call_duration_seconds` before adding replicas.

Do **not** raise `QEC_HOST_BURST_FACTOR` to push throughput — that loosens
customer-facing politeness. Add replicas and raise the global cap instead; the
per-host token bucket is a customer-protection contract, not a throughput dial.

---

## 4. The change-triggered incremental-regression flywheel

**The economic thesis: fleet cost scales with _change_, not with _app count_.**

A cycle fires when an app actually changes (`trigger=webhook_repo` on a repo
push, `probe_drift` when the live UI drifts, `schedule` for the cadence floor)
and it re-verifies **only the affected slice** — not a full re-crawl. So:

- Adding a client whose apps rarely change adds ~0 incremental cost; you pay only
  the cheap scheduled "full floor" safety re-crawl (`trigger=full_floor`).
- Daily browser-seconds track the number of **changes**, not the number of apps.

The capacity model computes this explicitly and reports a `savings_ratio` = (cost
of re-crawling every app nightly) ÷ (actual incremental + floor cost). For a
representative fleet it is several-fold; as change-rate rises toward "every app
changes every day at full cost", the ratio correctly converges to ~1× (there is
nothing left to save). The model's self-test pins this property.

Operational implications:
- Watch `qec_cycles_started_total{trigger}` — a healthy fleet is dominated by
  `webhook_repo`/`probe_drift` (real change), with a steady low `full_floor`.
- A sudden collapse of `webhook_repo` to zero usually means the repo webhooks or
  `repo-intel` change detection broke, not that development stopped — the fleet
  will silently stop regressing. `VerdictNoCyclesProgressing` catches the
  fleet-wide case; per-app, check the app's last-cycle timestamp.
- Cost per app per cycle is a **measured** number: `qec_cost_units_total{unit}`
  (fleet) and the `cost_ledger` (per tenant/app). Compare measured
  browser-seconds to the model's projection to catch cost regressions early.

---

## 5. Per-tenant politeness and rate story

Two independent protections, both enforced by the admission gate
(`app/controlplane/scheduling/admission.py`, and the shared Redis version in
`distributed.py`):

1. **Fairness across tenants** — a global concurrency cap
   (`QEC_MAX_GLOBAL_CYCLES`) plus a per-tenant cap (`QEC_MAX_PER_TENANT_CYCLES`).
   No tenant can starve the fleet.
2. **Politeness toward each customer's app** — a per-`canonical_host` token
   bucket (rate = the app's `max_rps` fence, burst 2×) plus a per-host mutex (one
   active cycle per host at a time). Buckets **start empty**, so a process
   restart or a fresh replica can never manufacture a burst against a customer.

**Fail-closed by design:** an app with no positive `max_rps`
(`reason=rate_unconfigured`) or no `canonical_host` (`reason=host_unconfigured`)
is **refused** — we never crawl a customer host we cannot rate-key. These are
onboarding gaps, not transient (see [Onboarding fences](#onboarding-fences)).

The distributed limiter runs the whole decision (host mutex → global cap →
per-tenant cap → token bucket, token consumed **last**) inside one atomic Redis
Lua script, so two replicas racing the same host can never both be admitted.

---

<a id="redis-ha"></a>
## 6. Redis HA

The distributed admission limiter is **fail-closed**: if Redis is unreachable it
**denies every admit** (`reason=limiter_unavailable`) rather than fall open and
burst a customer's app. This is the correct safety direction — a deferred cycle
costs latency; an un-rate-limited crawl costs the customer — **but it means a
Redis outage stalls the entire fleet.**

Therefore, at fleet scale **Redis must be highly available**:
- Run Redis with replication + Sentinel (or a managed HA Redis), not a single
  container. The existing `infrastructure/helm/nexus-qa/templates/redis-sentinel.yaml`
  is the pattern to mirror for a k8s deploy; on the compose box use a managed HA
  Redis endpoint in `QEC_REDIS_URL`.
- Alert `VerdictLimiterStoreDown` (redis_exporter `up==0`) and
  `VerdictAdmissionLimiterUnavailable` (`limiter_unavailable` denials) both page.
- The lease/mutex safety TTL (`QEC_ADMISSION_LEASE_TTL_SECONDS`, default 7200s)
  must exceed the max cycle wall-clock so a crashed replica's slot self-heals but
  a live cycle is never reaped.

Recovery: restore Redis, confirm `limiter_unavailable` returns to 0, confirm
`qec_cycles_started_total` resumes advancing. No data is lost — the limiter state
is ephemeral by design (buckets re-fill from empty, leases self-heal).

---

<a id="capacity"></a>
## 7. Capacity

Use `scripts/verdict_capacity_model.py` — a pure calculator (stdlib only, no
deps) that turns fleet inputs into a daily plan and sizing recommendations. It is
self-testing: `python scripts/verdict_capacity_model.py --selftest`.

What it computes (per day, from clients × apps × change-rate + per-cycle costs):
- **Cycles/day** — incremental (change-triggered) + full-floor (scheduled).
- **Browser-seconds/day** — the primary metered cost unit
  (`UNIT_BROWSER_SECONDS`), plus the full-re-crawl baseline and the flywheel
  `savings_ratio`.
- **DB growth** — `substrate_rows/day × bytes/row` → GiB/day and GiB/month for
  Postgres sizing of the evidence system-of-record.
- **Concurrency + sizing** — average and peak concurrent cycles (Little's law ×
  a peak-to-average burst factor), then `recommended_max_global_cycles` (the
  admission cap) and `recommended_replicas` (peak concurrency ÷ per-replica
  concurrency).
- **Estimated USD** — only when unit prices are supplied; otherwise it publishes
  raw units and says `UNPRICED` (it never invents dollars — same honesty rule as
  the cost meter).

Cost-per-app-per-cycle, browser-seconds budget, and DB sizing all fall out of
this model. Feed its outputs into `QEC_MAX_GLOBAL_CYCLES`, the replica count, per-
app/per-tenant budgets, and your Postgres disk plan. Re-run it whenever a client
onboards or change-rate shifts, and compare its browser-seconds projection to the
**measured** `qec_cost_units_total{unit="browser_seconds"}` to catch drift.

Example (20 clients × 100 apps, 0.5 changes/app/day):
```
python scripts/verdict_capacity_model.py --clients 20 --apps-per-client 100 \
  --changes-per-app-per-day 0.5
# -> ~1,286 cycles/day, ~96 browser-hours/day, ~1 GiB/day DB growth,
#    peak ~14 concurrent cycles -> QEC_MAX_GLOBAL_CYCLES=15, ~4 replicas,
#    savings ~5x vs nightly full re-crawl.
```

---

## 8. Alert response playbook

Alerts live in `infrastructure/observability/verdict/alerts-verdict.yml`. Every
rule binds to a real `qec_*` metric family (or `up` / the backup textfile
gauges). Severity: **page** = wake up; **ticket** = one business day; **info** =
awareness. Each subsection below is the deep-link target used in the alert
annotations.

### Cycle failure
`VerdictCycleFailureRateFast` (page, >20%/10m) / `...Slow` (ticket, >5%/1h).
Fleet-wide failure, not one flaky app. Triage in order:
1. Factory health — `qec_factory_calls_total{status=~"5..|transport_error"}`
   (see [Factory](#factory)). A factory outage fails most cycles.
2. Admission fail-closed — `qec_admission_decisions_total{reason=~"rate_unconfigured|host_unconfigured"}`
   (see [Onboarding fences](#onboarding-fences)).
3. qe-central logs by correlation id (`X-Request-ID` echoed on every response;
   `request_id` on every structured log line). Break down failures by
   `trigger`/`mode` to localise.

### Budget
`VerdictBudgetBreachSurge` (ticket, >5 budget_stopped/1h). A budget breach is an
**honest** terminal `budget_stopped`, never a partial `done`. Causes:
- Budgets too tight for current change volume → raise per-app/per-tenant budgets
  (client_apps fences/budgets) using the capacity model's cost figures.
- A cost regression → inspect the `cost_ledger`; watch for `unit=unmetered_run`
  gap flags (runs whose duration could not be correlated — the meter under-counts
  rather than invent seconds, so a spike of gap flags hides real spend).

### Factory
`VerdictFactoryErrorRateHigh` (page, >10%/10m) / `VerdictFactoryLatencyP95High`
(ticket, p95>60s). The Verdict plane cannot generate/heal/run without the
UNCHANGED VKPower factory (consumed over HTTP). Check platform-api health, the
`qec-egress` path, and downstream model latency. Break down by
`endpoint`/`method`/`status`. A slow synchronous `/generate` compile drags cycle
wall-clock toward the ceiling.

### Green wash
`VerdictGreenWashDetected` (page, any occurrence). The REFUSE harness caught a
false-heal (a dishonest pass). The product's category promise is
proof-of-behavior; this is **trust-breaking**. Freeze promotion of the affected
app/ruleset, capture the harness run + dossier (hash-chained verdict evidence),
and root-cause before anything ships. Zero tolerance.
> Requires `record_harness_outcome(...)` wired to the verdict; until then
> `qec_harness_outcomes_total` has no series and this cannot fire — do **not**
> read a silent panel as "no green-wash".

### False heal SLO
`VerdictFalseHealRateHigh` (page, >1%/30m). The explicit false-heal SLO
(green-wash ÷ all harness outcomes). Above 1% is a *systematic* honesty
regression, not a one-off — treat as a release-blocking incident and bisect the
heal/verify path. Same wiring caveat as [Green wash](#green-wash).

### Refuse rate
`VerdictRefuseRateAnomalyHigh` (ticket, >60%/1h) and `VerdictRefuseRateCollapsed`
(ticket, <2% at healthy volume). Refusing when it lacks positive proof is
**correct**; the anomaly is the signal:
- **Spike** ⇒ an upstream break is starving the oracle of evidence (anchor-less
  ambiguity, vision tier off, factory degraded). Fix the upstream, not the
  refusal.
- **Collapse to ~0** at healthy volume ⇒ a gate may have stopped refusing
  (green-wash risk). Confirm the oracle/evidence path is intact and this is
  genuine improvement, not a disabled check.

### Evidence
`VerdictEvidenceSubstrateStalled` (page). Cycles reaching `done` while
`qec_substrate_rows_written_total` is flat = the silent-extraction-break
signature (the Phase-2 fresh-artifact failure mode: HTTP 200, 0 rows). A `done`
cycle that wrote no evidence is a green-wash of coverage. Check the substrate
writer and the `ground_truth_events` table/migration.

### Onboarding fences
`VerdictAdmissionFailClosedMisconfig` (ticket). Cycles refused for
`rate_unconfigured` (no positive `max_rps`) or `host_unconfigured` (no
`canonical_host`). This will **not** clear without operator action — fix the
app's fences in `client_apps` (env_attestation/fences/budgets). We never crawl a
customer host we cannot rate-key.

### Liveness
`VerdictQECentralDown` (page, `up{job="qe-central"}==0`). If the safety spine
**refused** an unsafe boot (dev KEK / default secret in a deployed env), this is
fail-closed working — check the container logs; the fix is real secrets/KMS, not
a restart loop. Otherwise the control plane is down and no cycles run.

---

<a id="leader-and-liveness"></a>
## 9. Leader and liveness

The cycle daemon scans the whole fleet each tick and fires cycles; with
`QEC_DAEMON_LEADER_ELECTION=advisory_lock` exactly one replica does this. Two
failure shapes:

- **Leader dead / fleet idle** — `VerdictNoCyclesProgressing` (ticket): no
  `qec_cycles_started_total` advance for 1h while qe-central is up. This is
  *ambiguous* — it can be a genuinely quiet fleet (no change, empty schedule) OR
  a dead/flapping leader OR a fail-closed limiter. Distinguish by:
  - `qec_admission_decisions_total{reason="limiter_unavailable"}` > 0 ⇒ Redis
    stall ([Redis HA](#redis-ha)).
  - Leader logs: exactly one `qec.leader.acquired` and no repeated
    acquire/release churn.
  - Pending schedules / recent change events for at least one app.
- **Leadership flapping** — `VerdictLeaderElectionFlapping` is **pending metric
  wiring** (an optional `qec_leader_is_leader` gauge, 1 on the leader / 0 on
  followers; `sum` should equal 1). Until that gauge is emitted, detect flapping
  from logs — e.g. a Loki rule on repeated `qec.leader.acquired`:
  ```logql
  sum(count_over_time({container="nexus-qe-central"} |= "qec.leader.acquired" [15m])) > 3
  ```
  and rely on `VerdictNoCyclesProgressing` as the observable symptom. Advisory-
  lock leadership is bound to the DB session, so brief flaps usually mean
  Postgres connection churn — check DB connectivity/pgbouncer before the app.

---

<a id="backups"></a>
## 10. Backups

The evidence system-of-record **is** the product; an un-backed-up SoR is the
single highest live risk. `scripts/verdict_pg_backup.sh` dumps both `nexus` and
`qecentral` (custom format, compressed, parallel-restorable) and ships them to
`GCS_BACKUP_BUCKET`. The bootstrap installs a nightly cron (03:17).

**Fleet observability wiring (opt-in):** set `NODE_EXPORTER_TEXTFILE_DIR` for the
backup job and point node_exporter's `--collector.textfile.directory` at the same
dir. On each success the script atomically writes:
- `verdict_backup_last_success_timestamp_seconds`
- `verdict_restore_drill_last_success_timestamp_seconds`

so Prometheus can alert on staleness. When the env var is unset the script is
byte-for-byte unchanged (hard no-op).

Alerts:
- `VerdictBackupStale` (page, >36h) — nightly cadence + grace missed.
- `VerdictBackupMetricsMissing` (ticket) — the freshness gauges are **absent**,
  so the staleness alerts can't protect you. Absent = *no signal*, which must not
  read as "healthy" — wire node_exporter + `NODE_EXPORTER_TEXTFILE_DIR`.

### Restore drill
"Having backups" is not "recovery works". `verdict_pg_backup.sh --restore-drill`
restores a fresh `qecentral` dump into a throwaway database and compares table
counts — recovery **proven**, not assumed. Run it at least weekly.
`VerdictRestoreDrillMissed` (page) fires at >8 days since the last pass. A
regulated buyer requires proven recovery before scale, so treat a failed or
missing drill as a go/no-go blocker for onboarding the next client.

---

## 11. Onboarding a client onto the fleet

1. Provision the client's box (`scripts/verdict_box_bootstrap.sh` with real
   `NEXUS_KEK_GCP_KEY` + `GCS_BACKUP_BUCKET`; the safety spine refuses a dev KEK
   or default secret in a deployed env).
2. Register the client's apps in `client_apps` with **fences**: a positive
   `max_rps` and a `canonical_host` (else admission fail-closes — see
   [Onboarding fences](#onboarding-fences)) plus budgets sized from the
   [capacity model](#capacity).
3. Turn on multi-replica if the box serves many apps (Section 2).
4. Confirm the observability bundle is scraping (`up{job="qe-central"}==1`, the
   dashboard populates) and a first backup + restore-drill have passed.
5. Re-run the capacity model with the new totals and adjust
   `QEC_MAX_GLOBAL_CYCLES` / replicas / budgets.

---

## Appendix A — env-var quick reference (fleet-relevant)

| Env var | Default | Fleet note |
|---------|---------|------------|
| `QEC_ADMISSION_BACKEND` | `memory` | `redis` for N replicas sharing one limiter. |
| `QEC_REDIS_URL` | `redis://redis:6379/3` | Shared limiter DSN; empty ⇒ fail-closed. |
| `QEC_ADMISSION_LEASE_TTL_SECONDS` | `7200` | Must exceed max cycle wall-clock. |
| `QEC_DAEMON_LEADER_ELECTION` | `none` | `advisory_lock` for single-scanner. |
| `QEC_LEADER_LOCK_KEY` | `qec-cycle-driver-leader` | One logical lock per fleet. |
| `QEC_MAX_GLOBAL_CYCLES` | `8` | Global concurrency cap = model's `recommended_max_global_cycles`. |
| `QEC_MAX_PER_TENANT_CYCLES` | `2` | Keep `< QEC_MAX_GLOBAL_CYCLES` (starvation guard). |
| `QEC_HOST_BURST_FACTOR` | `2.0` | Politeness burst; do **not** raise for throughput. |
| `QEC_METRICS_ENABLED` | on | `0` disables `/metrics`. |
| `QEC_METRICS_PATH` | `/metrics` | Scrape path (outside `/api`, no JWT). |
| `NODE_EXPORTER_TEXTFILE_DIR` | unset | Set on the backup job to emit freshness gauges. |

## Appendix B — metric reference (the fleet signal)

Emitted by `platform/qe-central/app/observability/metrics.py` (counters render
with a `_total` suffix in PromQL):

| Metric | Labels | Reads as |
|--------|--------|----------|
| `qec_cycles_started_total` | `trigger,mode` | work started (flywheel mix by trigger) |
| `qec_cycles_completed_total` | `terminal_state` | terminal outcomes (`done`/`failed`/`budget_stopped`) |
| `qec_cycle_duration_seconds` | `terminal_state` | cycle wall-clock (histogram) |
| `qec_admission_decisions_total` | `decision,reason` | admission (politeness/caps/fail-closed) |
| `qec_admission_in_flight` | — | live concurrency depth (gauge) |
| `qec_factory_calls_total` | `endpoint,method,status` | VKPower factory health over HTTP |
| `qec_factory_call_duration_seconds` | `endpoint,method` | factory latency (histogram) |
| `qec_substrate_rows_written_total` | `table` | evidence coverage produced |
| `qec_harness_outcomes_total` | `outcome` | verdict bands / false-heal / refuse (needs wiring) |
| `qec_cost_units_total` | `unit` | metered cost (`browser_seconds` etc.; `unmetered_run` = gap) |

Per-**tenant** breakdowns are intentionally **not** Prometheus labels
(low-cardinality, PII-free). Query the tenant-scoped API / `cost_ledger` /
evidence tables for per-tenant cost and coverage.
