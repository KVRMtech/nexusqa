# Nexus QA — Operator Runbook

Single source of truth for "how do I run this system" and "what do I do when it breaks." Audience: an on-call operator who has never seen the codebase.

---

## 1. Topology at a glance

```
                                gateway (8080)
                                    │
                       ┌────────────┴────────────┐
                       │                         │
              platform-api (8091)        orchestrator (8100)
                                                 │
                  Redis Streams ───────── dispatches per (engine, kind) lane
                  nexus:queue:<engine>.<cpu|gpu>
                                                 │
                  ┌─────────┬─────────┬──────────┼──────────┬─────────┐
                shield     eyes     ears       spine    backbone     ...
                (8001)    (8003)   (8002)     (8009)    (8005)
                                                                  │
                       ┌──────────────────────────────────────────┤
                       │                                          │
                    Neo4j (graph)               Milvus (vector store, full profile only)
                                                                  │
                                                        etcd + minio + milvus
```

Stateful dependencies: **Postgres** (workflow state + metadata), **Redis** (queue lanes + cache), **Neo4j** (knowledge graph), **Milvus** (vector store; full profile).

---

## 2. Bringing the stack up

### Dev (docker compose)

```bash
# Full canonical stack, including Milvus + backbone
docker compose --profile full up -d

# Lighter (no backbone / no Milvus)
docker compose up -d

# Same with rebuild after code changes:
bash scripts/rebuild_canonical.sh
```

Smoke checks after up:

```bash
# 1. All services healthy
docker compose --profile full ps

# 2. Workflow lanes have consumers attached
for lane in shield.cpu eyes.cpu eyes.gpu ears.cpu ears.gpu spine.cpu spine.gpu backbone.cpu; do
  docker compose exec redis redis-cli -n 3 XINFO GROUPS "nexus:queue:${lane}" \
    | awk '/^consumers/{getline; print "  " ENVIRON["lane"] " consumers=" $1; exit}' \
    lane="${lane}"
done

# 3. Backbone using real Milvus (not degraded)
curl -s http://localhost:8005/health | python -m json.tool | grep vector_store
# Expect: "milvus + sentence-transformers (all-MiniLM-L6-v2)"
```

### Production (Helm)

Pre-flight (operator team installs once per cluster):
1. **CloudNativePG operator** in `cnpg-system`
2. **Prometheus Operator** (kube-prometheus-stack) for ServiceMonitor / PrometheusRule CRDs
3. **External Secrets Operator** + cloud secret store (AWS Secrets Manager / GCP Secret Manager / Vault)
4. **KEDA** for queue-driven autoscaling
5. **Argo Rollouts** for canary deploys

Verify each:
```bash
kubectl get crd clusters.postgresql.cnpg.io
kubectl get crd prometheusrules.monitoring.coreos.com
kubectl get crd externalsecrets.external-secrets.io
kubectl get crd scaledobjects.keda.sh
kubectl get crd rollouts.argoproj.io
```

Deploy:
```bash
helm upgrade --install nexus infrastructure/helm/nexus-qa \
  -n nexus-platform --create-namespace \
  -f infrastructure/helm/nexus-qa/values-production.yaml \
  --set images.digestOverride="$IMAGE_DIGEST" \
  --set images.registry="$IMAGE_REGISTRY"
```

The chart will refuse to render if `IMAGE_DIGEST`/`IMAGE_REGISTRY` placeholders aren't replaced — that's intentional.

---

## 3. The five things you'll be paged for

### 3.1 `NexusWorkflowQueueBackup` (warning, paged at critical)

**What it means.** A specific Redis Stream (e.g. `nexus:queue:eyes.gpu`) has more than 1000 pending entries for >5 minutes. KEDA isn't scaling out fast enough, or downstream is stuck.

**First 90 seconds.**
```bash
# 1. Which lane is backed up?
kubectl get prometheusrule -n monitoring nexus-nexus-qa-workflow-alerts -o yaml | grep -A2 NexusWorkflowQueueBackup
# Look at the alert label `key=nexus:queue:<engine>.<kind>`.

# 2. How deep is the queue right now?
ENGINE=eyes  # replace
KIND=gpu     # replace
kubectl exec -it deploy/redis -- redis-cli -n 3 XLEN "nexus:queue:${ENGINE}.${KIND}"

# 3. How many consumers are attached?
kubectl exec -it deploy/redis -- redis-cli -n 3 XINFO GROUPS "nexus:queue:${ENGINE}.${KIND}"
# `consumers=0` → no engine pod is reading. `consumers=N>0` → pods are reading but not fast enough.

# 4. Are the engine pods alive?
kubectl get pods -l engine=${ENGINE}
```

**Decision tree.**
- 0 consumers → engine pods crashed. Inspect `kubectl logs -l engine=${ENGINE} --tail=200`.
- N>0 consumers, but XLEN keeps growing → KEDA cap is too low or the step itself is slow. Check the canonical-pipeline Grafana dashboard for that step's p95.
- KEDA replica count == maxReplicas → bump `autoscaling.keda.components[engine].maxReplicas` in [values-production.yaml](Nexus_power/infrastructure/helm/nexus-qa/values-production.yaml).

### 3.2 `NexusWorkflowDLQGrowing` (critical, pages)

**What it means.** Workflows have exhausted retries and landed in the dead-letter lane.

**First 90 seconds.**
```bash
# Find DLQ entries
kubectl exec -it deploy/redis -- redis-cli -n 3 XRANGE "nexus:dlq:<lane>" - +

# Pull a sample workflow_id from the entry, then look up the row
WORKFLOW_ID=...
kubectl exec -it deploy/postgres -- psql -U nexus -d nexus -c \
  "SELECT workflow_id, status, current_step, attempt, error FROM workflow_state WHERE workflow_id='${WORKFLOW_ID}';"
```

**Decision tree.**
- Same step failing repeatedly across many workflows → bug or upstream config issue. Block new ingest while investigating.
- One bad workflow that ran out of attempts → operator may manually requeue OR mark as cancelled.

### 3.3 `NexusBackboneVectorStoreDown` / `NexusBackboneInDegradedMode` (critical, pages)

**What it means.** Milvus is unreachable, OR backbone is running but its vector mode isn't `milvus`. Search will return empty results without warning.

**First 90 seconds.**
```bash
# Verify backbone's view of the world
kubectl exec deploy/backbone -- curl -s http://localhost:8005/health | grep vector_store
# Healthy: "milvus + sentence-transformers (...)". Degraded: "in-memory (...)"

# If degraded, check Milvus health directly
kubectl exec -it sts/nexus-nexus-qa-milvus -- curl -sf http://localhost:9091/healthz
kubectl get pods -l app.kubernetes.io/component=milvus -o wide
```

**Decision tree.**
- Milvus pod healthy but backbone in degraded mode → restart backbone. The startup `production_guard` will refuse to start if Milvus is still unreachable, which is the correct behavior.
- Milvus pod crashlooping → check etcd + minio underneath. Milvus depends on both.
- **Do NOT set `NEXUS_ALLOW_DEGRADED_MODE=true` as a workaround in prod.** It silently breaks search. The alert exists specifically to catch that.

### 3.4 `NexusServiceHighRestartRate` (warning)

**What it means.** A pod has restarted 3+ times in 15 minutes.

**First 60 seconds.**
```bash
kubectl get events --sort-by=.lastTimestamp -n nexus-platform | tail -20
kubectl logs <pod-name> --previous --tail=100
```

Common causes: OOMKilled (bump memory limits), failed health check (engine startup taking too long — usually model load), missing config (ESO secret not synced).

### 3.5 `NexusWorkflowStepFailureRate` (warning)

**What it means.** A specific `(engine, step)` is failing >10% of the time over 10 minutes.

**First 90 seconds.**
Open the `Nexus — Canonical Pipeline` Grafana dashboard. The "Step failure rate" panel + "Step duration p95" heatmap pinpoint the bad cell.

Then drill in:
```bash
kubectl logs -l engine=<engine> --tail=200 | grep -i "error\|traceback\|failed"
```

---

## 4. Routine ops

### Adding a new tenant

Tenants are seeded into Postgres by the platform-api. Operators don't normally provision them by hand; if you must:

```sql
INSERT INTO tenants (tenant_id, display_name, tier, created_at)
VALUES ('acme-corp', 'ACME Corp', 'pilot', now());
```

To change metric bucketing (so a tenant shows up as `enterprise` instead of `unknown` in Grafana):

```yaml
# values-production.yaml
extraEnv:
  NEXUS_TENANTS_ENTERPRISE: "acme-corp,beta-inc"
  NEXUS_TENANTS_PILOT: "gamma-llc"
```

### Draining the workflow queue (e.g. before a schema migration)

1. Stop accepting new uploads at the gateway:
   ```bash
   kubectl scale deploy/gateway --replicas=0
   ```
2. Wait for queue lanes to drain:
   ```bash
   for lane in shield.cpu eyes.cpu eyes.gpu ears.cpu ears.gpu spine.cpu spine.gpu backbone.cpu; do
     kubectl exec -it deploy/redis -- redis-cli -n 3 XLEN "nexus:queue:${lane}"
   done
   ```
3. Wait for `nexus_workflow_in_flight` gauge to hit 0 on the Grafana dashboard.
4. Apply the change.
5. Scale gateway back up.

### Tuning per-step deadlines

Per-step `deadline_seconds` lives in [plans.py](Nexus_power/sdk/nexus-sdk/nexus_sdk/workflows/plans.py). Bump if `NexusWorkflowDeadlineBreaches` fires for the same `(kind, step)` repeatedly.

Be cautious: the workflow-level `deadline_seconds` must be ≥ sum of step deadlines + 10–20% safety margin.

### KEDA scale-out tuning

Per-engine caps in [values-production.yaml](Nexus_power/infrastructure/helm/nexus-qa/values-production.yaml) under `autoscaling.keda.components`. Common pattern:

- If `NexusWorkflowQueueBackup` fires repeatedly on the same lane, raise `maxReplicas` for that engine.
- If pods are stuck `Pending` for >2 min, the node pool is saturated — bump the node-group capacity, not the KEDA cap.

---

## 5. Disaster recovery

### Postgres failover (CNPG-managed)

Automatic. CNPG promotes a sync replica within ~30s of detecting primary failure. Verify:

```bash
kubectl get cluster -n nexus-platform nexus-nexus-qa-postgres
kubectl describe cluster -n nexus-platform nexus-nexus-qa-postgres | grep "Primary\|Ready"
```

No operator action needed unless the cluster reports `cluster in failure-recovery state`.

### Redis failover (Sentinel-managed)

Automatic. Sentinel promotes a replica within ~10s. Client retries on reconnect via Sentinel discovery.

### Restoring from Postgres backup (CNPG + Barman)

Only relevant if `postgres.cnpg.backup` was enabled when the cluster was created.

```bash
kubectl get backups -n nexus-platform
kubectl create -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: nexus-postgres-restore
spec:
  instances: 1
  bootstrap:
    recovery:
      source: nexus-nexus-qa-postgres
      recoveryTarget:
        targetTime: "2026-05-15 12:00:00"
  externalClusters:
    - name: nexus-nexus-qa-postgres
      barmanObjectStore:
        destinationPath: s3://nexus-pg-backups/production
EOF
```

### Milvus rebuild from scratch (corruption / etcd loss)

Milvus IS not the source of truth — embeddings are derivable from canonical artifacts in object storage. If Milvus is unrecoverable:

1. Drain the workflow queue (see §4).
2. Delete the Milvus PVC + etcd PVC.
3. Recreate.
4. Re-run a rebuild job that walks `canonical_artifacts` and dispatches `backbone.canonicalize` for each.

This is not a tested-in-prod path yet; treat it as a last resort and engage engineering.

---

## 6. What we know is rough (be honest with the client)

| Known gap | Impact | Workaround | Owner |
|---|---|---|---|
| Eyes pipeline is monolithic | A failure mid-video retries the whole video (cost + latency) | None today. Phase 5 split is queued. | Engineering |
| No load-test number for "100 clients × 1000 req" | Capacity caps are estimates | Pilot with ≤10 friendly tenants first; measure; tune KEDA caps before opening to 100. | Platform |
| CNPG / Redis Sentinel HA is config-only verified | Failover *behavior* not yet drilled against a real cluster | Stage a failover drill in pre-prod before first client. | Operator team |
| Milvus dev compose uses non-HA standalone | Acceptable for dev; production Helm chart deploys real Milvus | None needed; production path is correct. | — |

Don't hide these. The client is better served knowing what's solid and what's queued than discovering it during an incident.
