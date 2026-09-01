# Nexus QA — Client Handover Checklist

Use this list **before** you hand the system off. Every box should be a green tick or a documented exception.

---

## A. Cluster pre-flight

- [ ] **CloudNativePG operator** installed in `cnpg-system`. Verify: `kubectl get crd clusters.postgresql.cnpg.io`.
- [ ] **Prometheus Operator** (kube-prometheus-stack) installed. Verify: `kubectl get crd prometheusrules.monitoring.coreos.com`.
- [ ] **External Secrets Operator** installed + a `ClusterSecretStore` named `nexus-platform-production` pointing at the client's secret backend (AWS SM / GCP SM / Vault).
- [ ] **KEDA** installed. Verify: `kubectl get crd scaledobjects.keda.sh`.
- [ ] **Argo Rollouts** installed. Verify: `kubectl get crd rollouts.argoproj.io`.
- [ ] **ingress-nginx** (or the client's ingress controller of choice) is the one referenced in `values-production.yaml.ingress.className`.
- [ ] **cert-manager** installed with a `ClusterIssuer` named `letsencrypt-prod` (or the client's equivalent, override in values).
- [ ] **A namespace** `nexus-platform` exists with the right labels for PodSecurity (`restricted`).
- [ ] **Node groups** with the right taints/labels for GPU engines (`nvidia.com/gpu: present`). Either real GPUs or the chart's CPU fallback is acceptable for the pilot, **document which**.

---

## B. Secrets

Every value below comes from the **client's** secret backend, not Git. Owner: the operator team.

- [ ] `nexus-platform-production-jwt`: `secret-key` ≥ 32 bytes random.
- [ ] `nexus-platform-production-postgres`: `username`, `password`, `superuser-password`.
- [ ] `nexus-platform-production-redis`: `password` (only if `redis.auth.enabled`).
- [ ] `nexus-platform-production-neo4j`: `password`.
- [ ] `nexus-platform-production-storage`: `s3-access-key`, `s3-secret-key`, or cloud-equivalent.
- [ ] (Optional) `nexus-platform-production-pg-backup`: `ACCESS_KEY_ID`, `ACCESS_SECRET_KEY` for the Postgres CNPG → S3 backup path.
- [ ] An ExternalSecret for each lands in `nexus-platform` and shows `synced=True`.

```bash
kubectl get externalsecrets -n nexus-platform
# All entries should show SyncedSecret=True
```

---

## C. Image registry

- [ ] CI publishes engine images to the client's registry (`infrastructure/helm/nexus-qa/values-production.yaml.images.registry`).
- [ ] Images are **digest-pinned**, not tag-pinned. `images.digestOverride` is set on each deploy.
- [ ] Image pull secret exists in `nexus-platform` namespace OR IRSA / Workload Identity is wired so pods don't need an explicit pull secret.

---

## D. Storage

- [ ] **Object storage bucket** for canonical artifacts exists (`storage.bucket` in values).
- [ ] Bucket has the right IAM for the engines (read + write under `tenants/<tenant_id>/...`).
- [ ] **Storage class** for PVCs (`global.storageClass`) supports the requested access modes (RWO for Postgres, RWX for model cache).
- [ ] Model cache PVC (~100 GiB) provisioned; Ollama can write to it.

---

## E. Render + dry-run

```bash
helm template nexus infrastructure/helm/nexus-qa \
  -f infrastructure/helm/nexus-qa/values-production.yaml \
  --set images.digestOverride="$IMAGE_DIGEST" \
  --set images.registry="$IMAGE_REGISTRY" \
  | kubectl apply --dry-run=server -f -
```

- [ ] Above command exits clean (no schema errors).
- [ ] No `REPLACE_WITH_*` placeholders left in the rendered YAML: `helm template ... | grep REPLACE_WITH_` should return nothing.

---

## F. First deploy

- [ ] Engines reach `Ready=True` within 5 minutes (Whisper / pyannote / LLaVA need to download or come from a warm cache).
- [ ] All 8 canonical workflow lanes show ≥1 consumer:
  ```bash
  for lane in shield.cpu eyes.cpu eyes.gpu ears.cpu ears.gpu spine.cpu spine.gpu backbone.cpu; do
    kubectl exec -it deploy/redis-master -- redis-cli -n 3 XINFO GROUPS "nexus:queue:${lane}"
  done
  ```
- [ ] Backbone reports `vector_store: "milvus + sentence-transformers (...)"` — **not** `in-memory`.
- [ ] Alembic is at head:
  ```bash
  kubectl exec deploy/platform-api -- python -m alembic -c /app/alembic.ini current
  # Expect the latest revision in alembic/versions/.
  ```

---

## G. Observability

- [ ] ServiceMonitor is scraping engine metrics:
  ```bash
  kubectl get servicemonitor -n nexus-platform
  # Targets show as UP in Prometheus.
  ```
- [ ] Grafana dashboards are loaded:
  - `Nexus — Canonical Pipeline`
  - `Nexus — Canonical Queues`
  - `Nexus — Canonical Engines`
  - Plus the existing HTTP/SLO dashboards (gateway, engine-overview, infrastructure, slo).
- [ ] Alertmanager has a route for `severity: critical` that pages the on-call. Test with a fake alert:
  ```bash
  amtool --alertmanager.url=$AM_URL alert add NexusServiceDown severity=critical
  ```

---

## H. End-to-end smoke

Run **one** real workflow through the system to confirm every hop works.

- [ ] Pick a small test audio file (5–10 min) AND a small test video (1–2 min).
- [ ] Upload through the gateway with a valid JWT.
- [ ] Watch the workflow row in Postgres go from `pending` → `running` → `completed`:
  ```bash
  kubectl exec deploy/postgres -- psql -U nexus -d nexus -c \
    "SELECT workflow_id, kind, status, current_step FROM workflow_state ORDER BY created_at DESC LIMIT 5;"
  ```
- [ ] Verify the same payload uploaded again returns `completed` in <500ms (cache-hit path).
- [ ] Verify search via backbone returns the freshly-ingested content with similarity > 0.4.

---

## I. Failover drills (pre-prod only)

Don't open to first real client until these have been done **once** in a staging environment that mirrors production.

- [ ] **Postgres primary kill.** `kubectl delete pod -l postgresql=primary --grace-period=0`. CNPG should promote a replica in <60s. Workflow plane resumes.
- [ ] **Redis master kill.** Sentinel should promote a replica in <30s. Queue lanes resume.
- [ ] **Engine pod kill mid-step.** `kubectl delete pod <engine-pod>`. Sweeper should orphan-recover the workflow within 2× `heartbeat_seconds`.
- [ ] **Milvus pod kill.** Backbone refuses to start until Milvus is back — verify the degradation guard fires, then verify backbone recovers when Milvus returns.

---

## J. Capacity calibration

The KEDA caps in values-production.yaml are **estimates**. Before the first real load:

- [ ] Run the load test (Phase 9, queued separately): 100 simulated tenants, mixed video/audio, 10 minutes.
- [ ] Measure p50/p95/p99 of `nexus_workflow_duration_seconds` per kind.
- [ ] Tune `autoscaling.keda.components.*.maxReplicas` based on measured backlog.
- [ ] Document the resulting numbers in `LOAD_TEST_REPORT.md`.

**If the load test hasn't happened yet, this is the largest unknown of the handover.** Be explicit with the client.

---

## K. Documentation handed over

- [ ] [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md) — alert playbook + routine ops.
- [ ] This file (`HANDOVER_CHECKLIST.md`).
- [ ] Architecture diagram (one-pager).
- [ ] An on-call rotation schedule for the first 2 weeks post-handover.
- [ ] Escalation contact list (platform team, engineering, security).
- [ ] **A list of known gaps and what's queued to fix them.** Pull from [OPERATOR_RUNBOOK.md §6](OPERATOR_RUNBOOK.md#6-what-we-know-is-rough-be-honest-with-the-client).

---

## L. Sign-off

```
Client:                  ___________________________
Pilot tenants approved:  ___________________________
Go-live date:            ___________________________
First on-call (us):      ___________________________
First on-call (client):  ___________________________

Signed (Operator):       ___________________________
Signed (Engineering):    ___________________________
Signed (Client):         ___________________________
```
