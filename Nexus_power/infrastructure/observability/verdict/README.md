# VKPower Verdict — fleet observability bundle (Phase 7)

Drop-in Prometheus + Grafana config for operating the Verdict control plane at
20+ clients. Everything here binds to the **real** metric families emitted by
`platform/qe-central/app/observability/metrics.py` (import-guarded
`prometheus_client`, a dedicated registry, low-cardinality labels — no
`tenant_id` / `app_id` / URL is ever a label).

| File | What it is | Where it goes |
|------|------------|---------------|
| `prometheus-verdict.yml` | Prometheus scrape config + `rule_files` + alerting seam | `/etc/prometheus/prometheus.yml` |
| `alerts-verdict.yml` | 22 fleet alert rules (cycles, admission, factory, honesty, evidence, backups, scrape) | `/etc/prometheus/alerts-verdict.yml` |
| `grafana-verdict-fleet.json` | Fleet-overview dashboard (uid `verdict-fleet-overview`, schemaVersion 39) | Grafana import / provisioning |

## Wire it (docker-compose box)

The Verdict box runs `docker-compose.qec.yml`. Add a Prometheus + Grafana pair on
the same `nexus` / `qec-internal` network and mount this bundle:

```yaml
prometheus:
  image: prom/prometheus:latest
  volumes:
    - ./infrastructure/observability/verdict/prometheus-verdict.yml:/etc/prometheus/prometheus.yml:ro
    - ./infrastructure/observability/verdict/alerts-verdict.yml:/etc/prometheus/alerts-verdict.yml:ro
  networks: [nexus, qec-internal]

grafana:
  image: grafana/grafana:latest
  # provision a Prometheus datasource with uid "prometheus", then import
  # grafana-verdict-fleet.json (the panels reference datasource uid "prometheus").
  networks: [nexus]
```

`qe-central` exposes `/metrics` on `:8093` **outside** `/api`, so the JWT gate
skips it (same public posture as `/health`) — no scrape auth needed. Metrics are
on by default; `QEC_METRICS_ENABLED=0` disables them.

## Exporters the alerts depend on

Deploy these standard exporters on the box (targets are pre-wired in
`prometheus-verdict.yml`); otherwise the corresponding alerts have no source:

- **node_exporter** (`:9100`, `--collector.textfile.directory=$DIR`) — host
  metrics **and** the backup/restore-drill freshness gauges written by
  `scripts/verdict_pg_backup.sh` when `NODE_EXPORTER_TEXTFILE_DIR` is set.
- **redis_exporter** (`:9121`) — the distributed admission limiter store. Redis
  is fail-closed, so a Redis outage stalls the fleet by design → keep it HA.
- **postgres_exporter** (`:9187`) — evidence system-of-record + `qecentral` DB
  sizing/connection saturation.

## Metric → panel/alert map (grounding)

- `qec_cycles_started_total{trigger,mode}` / `qec_cycles_completed_total{terminal_state}` → cycles-by-state, failure-rate, budget-breach, liveness.
- `qec_admission_decisions_total{decision,reason}` / `qec_admission_in_flight` → admission depth, denied-reason breakdown, fail-closed (limiter_unavailable / *_unconfigured).
- `qec_factory_calls_total{endpoint,method,status}` / `qec_factory_call_duration_seconds` → VKPower factory health (unchanged, over HTTP).
- `qec_harness_outcomes_total{outcome}` → autonomy bands, false-heal (`GREEN_WASH_DETECTED`), refuse rate (`REFUSED_CORRECTLY`). **Requires `record_harness_outcome(...)` wired to the verdict.**
- `qec_cost_units_total{unit}` / `qec_substrate_rows_written_total{table}` → fleet cost by unit, evidence coverage. Per-**tenant** cost is DB-sourced (`cost_ledger`), never a Prometheus label.

## Validate locally

```bash
python -c "import yaml; yaml.safe_load(open('prometheus-verdict.yml')); yaml.safe_load(open('alerts-verdict.yml')); print('yaml OK')"
python -c "import json; json.load(open('grafana-verdict-fleet.json')); print('json OK')"
```

See `QECentral/docs/FLEET_OPERATIONS_RUNBOOK.md` for scaling, the flywheel, the
capacity model, and the per-alert on-call playbook.
