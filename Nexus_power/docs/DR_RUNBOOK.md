# Disaster Recovery Runbook (Phase 10)

Companion to [scripts/dr_drills.sh](Nexus_power/scripts/dr_drills.sh) and the production [OPERATOR_RUNBOOK.md](Nexus_power/docs/OPERATOR_RUNBOOK.md).

This document defines:
1. Recovery Time Objectives (RTOs) per failure class
2. Recovery Point Objectives (RPOs) per data class
3. Drill procedures + acceptance criteria
4. How to populate `DR_DRILL_REPORT.md` after each drill

---

## Recovery Time Objectives

| Failure | Target RTO | Mechanism | Verification |
|---|---|---|---|
| Postgres primary failure | **< 60s** | CNPG operator promotes a sync replica | `kubectl get cluster` shows new primary |
| Postgres replica failure | **< 30s** | Replica rejoins after restart | `cluster.status.readyInstances` returns to N |
| Redis master failure | **< 30s** | Sentinel promotes a replica | `redis-cli -h sentinel sentinel master` returns new IP |
| Engine pod kill mid-step | **< 60s** | Sweeper orphan-recovery (2 × heartbeat) | `nexus_workflow_orphans_recovered_total` increments |
| Milvus pod failure | **< 2min** | StatefulSet restart + backbone reconnect | backbone `/health.modes.vector_store` says `milvus...` |
| Node drain | **< 5min** | Pod eviction + reschedule | Workloads re-Ready on other nodes |
| Zone outage (1 of 3) | **< 5min** | TopologySpread reschedules in-zone replicas | No workflow loss; `failed` rate < 0.5% during incident |
| Object storage 60s blackout | **Graceful pause + resume** | Workflows back off + retry | No workflows quarantined; in-flight resumes on reconnect |

---

## Recovery Point Objectives

| Data class | Target RPO | How preserved |
|---|---|---|
| Workflow state (Postgres) | **0 (zero loss)** | Synchronous replication (CNPG `minSyncReplicas: 1`) |
| Queue state (Redis Streams) | **< 30s** | AOF + Sentinel; up to 30s of unflushed writes may be lost on master kill |
| Canonical artifacts (object storage) | **0** | S3 / GCS / Azure native durability (11 9s) |
| Vector index (Milvus) | **0** for committed; re-derivable for in-flight | Milvus persists to MinIO/S3; in-flight embeddings can be re-derived from canonical artifacts |
| Knowledge graph (Neo4j) | **< 60s** | Causal cluster (3-node); 60s of writes may roll back on majority loss |

For data classes with non-zero RPO (Redis, Neo4j) the recovery procedure includes a "replay" step — see §3 below.

---

## Drill procedures

### Quarterly cadence

Run all 6 drills against pre-prod every quarter. Real production cluster is OUT OF SCOPE — never inject chaos in the customer-serving cluster.

```bash
KUBE_NAMESPACE=nexus-pre-prod \
DRILL_REPORT=docs/drill_reports/$(date +%Y-%m-%d)_quarterly.md \
bash scripts/dr_drills.sh --commit all
```

Each drill prints what it will do BEFORE `--commit`. Always run without `--commit` first to verify the plan.

### Pre-production cutover (one-time, blocks first client)

Drill the full set before any client traffic hits the cluster.

| Drill | When |
|---|---|
| `postgres-primary-kill` | Day 1 |
| `redis-master-kill` | Day 1 |
| `milvus-kill` | Day 1 |
| `engine-pod-kill` | Day 2 (requires manual k6 orchestration) |
| `node-drain` | Day 2 |
| `object-storage-blackout` | Day 3 (requires NetworkPolicy setup) |

If any drill fails, **block client cutover** until fixed.

---

## 3. Recovery procedures (when a drill or real incident triggers)

### 3.1 Postgres primary failure

**Expected behavior.** CNPG promotes a sync replica within 60s. In-flight queries fail with `terminating connection`; the engine retry logic (pgbouncer + asyncpg reconnect) catches them automatically.

**Operator action.** None if RTO met. If primary doesn't come back:
```bash
kubectl get cluster -n nexus-platform nexus-nexus-qa-postgres -o yaml | grep -A5 phase
# If phase=Failed: file ticket with CNPG team
kubectl logs -n cnpg-system -l app.kubernetes.io/name=cloudnative-pg --tail=200
```

**Replay needed?** No — sync replication guarantees zero workflow-state loss.

### 3.2 Redis master failure

**Expected behavior.** Sentinel promotes a replica in <30s. Engines reconnect via the Sentinel-aware client. Up to 30s of unflushed writes may be lost.

**Operator action.** Check the workflow queue:
```bash
kubectl exec -n nexus-platform deploy/redis-sentinel -- \
  redis-cli -p 26379 sentinel master mymaster
```

**Replay needed?** Sometimes. Workflows whose queue entry was in the unflushed window get re-dispatched by the sweeper within 2× heartbeat (60s). No operator action needed unless `nexus_workflow_orphans_recovered_total` keeps incrementing.

### 3.3 Milvus failure

**Expected behavior.** Backbone refuses to start if Milvus is unreachable (via `production_guard`). Existing canonical workflows complete (frames/transcripts already canonicalized). New `backbone.canonicalize` steps stall in the queue until Milvus returns.

**Operator action.**
```bash
# Check Milvus + its deps
kubectl get pods -n nexus-platform -l app.kubernetes.io/component=milvus
kubectl get pods -n nexus-platform -l app.kubernetes.io/component=milvus-etcd
kubectl get pods -n nexus-platform -l app.kubernetes.io/component=milvus-minio

# Drain backbone queue if it backed up severely
kubectl exec -n nexus-platform deploy/redis -- redis-cli -n 3 XLEN nexus:queue:backbone.cpu

# Verify reconnect after Milvus returns
kubectl exec -n nexus-platform deploy/backbone -- \
  curl -s http://localhost:8005/health | jq .modes.vector_store
```

**Replay needed?** If Milvus PVC was destroyed: yes, full re-canonicalize from `canonical_artifacts`. See §3.7 below.

### 3.4 Engine pod kill mid-step

**Expected behavior.** Sweeper detects stale heartbeat (older than `max_idle_seconds`, default 120s) and orphan-recovers the workflow. Step retries from its checkpoint.

**Operator action.** None if RTO met. Watch:
```bash
kubectl exec -n nexus-platform deploy/postgres -- \
  psql -U nexus -d nexus -c "
    SELECT workflow_id, current_step, attempt, last_heartbeat
    FROM workflow_state
    WHERE status='running' AND last_heartbeat < now() - interval '2 minutes';
  "
```

**Replay needed?** No — checkpoint state in Postgres is authoritative. The retry resumes from the last completed step.

### 3.5 Node drain (planned)

**Expected behavior.** Pods evict gracefully (60s grace period), reschedule on other nodes within ~3 min. `PodDisruptionBudget` prevents more than one replica being unavailable at a time.

**Operator action.**
```bash
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --grace-period=60
# After maintenance:
kubectl uncordon <node>
```

**Replay needed?** No.

### 3.6 Zone outage

**Expected behavior.** `TopologySpread` constraint distributes replicas across 3 zones. Loss of 1 zone leaves 2/3 of replicas serving traffic. KEDA scales up to compensate within 5 min if backlog grows.

**Operator action.**
```bash
# Confirm pods are now in remaining zones only
kubectl get pods -n nexus-platform -o wide | awk '{print $7}' | sort | uniq -c

# If KEDA isn't scaling, manual override:
kubectl scale deploy/eyes-engine --replicas=20 -n nexus-platform
```

**Replay needed?** Workflows in-flight on the lost zone's pods orphan-recover via §3.4.

### 3.7 Catastrophic data loss (Milvus PVC destroyed)

**Expected behavior.** None — this is destructive.

**Operator action.**
1. Drain workflow queue: `kubectl scale deploy/gateway --replicas=0`.
2. Wait for in-flight to complete: `nexus_workflow_in_flight` gauge → 0.
3. Delete Milvus + etcd PVCs.
4. Recreate Milvus via Helm: `helm upgrade --install nexus ...`.
5. Run the re-canonicalize job (NOT YET WRITTEN — Phase 17):
   ```bash
   kubectl create job recanonicalize-vectors --from=cronjob/recanonicalize-vectors
   ```
   This job walks `canonical_artifacts` and dispatches `backbone.canonicalize` for each row, rebuilding the vector index from the source-of-truth artifacts.
6. Verify completion: `nexus_workflow_completed_total{kind="canonical_recanonicalize"}` matches `canonical_artifacts` row count.

**RPO.** 0 for canonical artifacts (they're durable in object storage). Vector index is re-derived; the index is not the source of truth.

---

## 4. Drill report format

After each drill, append to `docs/drill_reports/YYYY-MM-DD_<drill_type>.md`:

```markdown
# DR Drill — <date>

Operator: <name>
Cluster: <pre-prod | production>
Drill: <name>

## Result
| Field | Value |
|---|---|
| Target RTO | <e.g. <60s> |
| Measured RTO | <e.g. 47s> |
| Measured RPO | <e.g. 0 (zero workflows lost)> |
| Status | <PASS / MISS / FAIL> |

## Observations
<free text — what did and didn't go to plan>

## Action items
<follow-up tickets if any>
```

Commit the report to the repo. Quarterly compliance review reads from `docs/drill_reports/`.

---

## 5. What this runbook does NOT cover

- **Data corruption** (logical, not infrastructure): handled by PITR — see [OPERATOR_RUNBOOK §5](Nexus_power/docs/OPERATOR_RUNBOOK.md#5-disaster-recovery) on Postgres recovery.
- **Security incident response**: separate `docs/SECURITY_RUNBOOK.md` (not yet written; Phase 15 deliverable).
- **Customer-facing communications**: separate `docs/INCIDENT_COMMS.md`.
- **Cost-runaway events** (sudden GPU-cost spike): separate cost-anomaly runbook.
