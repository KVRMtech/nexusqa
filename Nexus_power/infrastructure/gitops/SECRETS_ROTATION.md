# Secrets Rotation Runbook

This runbook is the operator's contract for rotating every credential
class the platform consumes. It assumes the M0.5 ESO + cloud-KMS path is
fully wired (see [`README.md`](./README.md) §Secrets and
[`resources/external-secrets/`](./resources/external-secrets/)).

The platform never reads secrets directly from cloud KMS. Pods read a
Kubernetes Secret that ESO populates from the cluster's
`ClusterSecretStore`. Rotation is therefore always two-step:

1. **Update the value in the upstream backend** (Secrets Manager / Secret
   Manager / Key Vault / Vault).
2. **Wait for ESO to sync** (or force a sync via `kubectl annotate
   externalsecret <name> force-sync=$(date +%s)`).

Pods do **not** auto-restart when the Secret is updated. For credentials
that are read once at startup (JWT signing key, DB password), trigger
a rollout (`kubectl rollout restart deploy/<name>`). For credentials read
on every request (cloud SDK creds), no restart is required.

---

## Secret classes

Each class has its own cadence and procedure. The cadences below are the
maximum interval; emergency rotations follow the same procedure on
demand.

| Class                       | Cadence  | Reload required          |
|-----------------------------|----------|--------------------------|
| JWT signing key             | 90 days  | Yes — rolling restart    |
| Database (Postgres)         | 180 days | Yes — rolling restart    |
| Redis password              | 180 days | Yes — rolling restart    |
| Neo4j password              | 180 days | Yes — rolling restart    |
| Object-storage IAM (S3 key) | 90 days  | No — SDK refreshes       |
| Object-storage IAM (Azure)  | 90 days  | No                       |
| Object-storage IAM (GCS)    | 90 days  | No                       |
| Grafana admin password      | 365 days | Yes — restart pod        |
| Shield encryption key       | 365 days | Yes — see §Shield        |
| Ears HF token               | as-needed| Yes — restart eyes/ears  |
| Heart/Brain LLM API keys    | 90 days  | No — engine re-reads     |
| KMS keys (provider-managed) | annual   | None — opaque rotation   |

---

## Per-secret procedures

### JWT signing key

The JWT signing key is read once when the auth-service starts and
embedded in every token issued during that pod's lifetime.

```
NEW_KEY=$(openssl rand -hex 64)

# 1. Push the new value (procedure depends on cloud)
aws secretsmanager put-secret-value \
  --secret-id nexus-platform/${ENV}/jwt-secret \
  --secret-string "$NEW_KEY"

# 2. Force ESO sync
kubectl -n nexus-qa annotate externalsecret nexus-qa-secrets \
  force-sync=$(date +%s) --overwrite

# 3. Verify the in-cluster Secret picked it up
kubectl -n nexus-qa get secret nexus-qa-secrets \
  -o jsonpath='{.data.jwt-secret}' | base64 -d | sha256sum

# 4. Rolling restart auth-service (token-issuers)
kubectl -n nexus-qa rollout restart deploy/nexus-qa-auth-service

# 5. Rolling restart every engine + platform pod (token-validators).
#    All pods read the same JWT_SECRET env, so all need to reload.
for d in $(kubectl -n nexus-qa get deploy -l app.kubernetes.io/part-of=nexus-qa -o name); do
  kubectl -n nexus-qa rollout restart "$d"
done
```

Tokens issued by the **old** key keep validating until every
auth-service replica has been replaced. After step 5 they are rejected.
Tokens have a 1-hour expiry so the overlap window is bounded.

### Database password (Postgres)

The postgres-password is shared between the auth-service, platform-api,
backbone-engine, spine-engine, and orchestrator.

```
# 1. Provision the new password ON the database first. NEVER swap the
#    Secret before the database itself has the new password.
NEW_PG=$(openssl rand -hex 32)
psql -h ${PG_HOST} -U postgres -c "ALTER USER nexus_app WITH PASSWORD '$NEW_PG';"

# 2. Push to KMS
aws secretsmanager put-secret-value \
  --secret-id nexus-platform/${ENV}/postgres-password \
  --secret-string "$NEW_PG"

# 3. Force ESO sync (see JWT for the full snippet)

# 4. Roll every consumer (in this order — auth-service goes last so the
#    user-facing surface fails over only after backends recover).
kubectl -n nexus-qa rollout restart deploy/nexus-qa-platform-api
kubectl -n nexus-qa rollout restart deploy/nexus-qa-backbone-engine
kubectl -n nexus-qa rollout restart deploy/nexus-qa-spine-engine
kubectl -n nexus-qa rollout restart deploy/nexus-qa-orchestrator
kubectl -n nexus-qa rollout restart deploy/nexus-qa-auth-service

# 5. Drop the old password if Postgres role retention is on (it isn't
#    by default; ALTER USER replaces the credential, no leftover).
```

If a consumer is stuck on the old password (won't reach the new DB),
look at the pod's `terminationGracePeriodSeconds`; long graces (legs at
60 s) may keep old pods in service longer.

### Redis password

```
NEW_REDIS=$(openssl rand -hex 32)

# 1. Push to KMS
aws secretsmanager put-secret-value \
  --secret-id nexus-platform/${ENV}/redis-password \
  --secret-string "$NEW_REDIS"

# 2. Apply on the Redis side. Redis-Sentinel rotation is more involved;
#    update each replica's requirepass + masterauth in lock-step.
#    See deploy/redis-sentinel-rotate.sh for the live drill.

# 3. Force ESO sync, then rolling restart every pod (all engines + the
#    platform set read REDIS_PASSWORD on startup).
```

If the rotation script lags between the Sentinel mass-update and the
ESO sync, pods will fail readiness probes (auth-required). Run
`redis-sentinel-rotate.sh` AFTER step 3 to keep the window short.

### Neo4j password

Only backbone-engine uses it.

```
# Online ALTER:
cypher-shell -u neo4j -p ${OLD} \
  "ALTER CURRENT USER SET PASSWORD FROM '${OLD}' TO '${NEW}'"

aws secretsmanager put-secret-value \
  --secret-id nexus-platform/${ENV}/neo4j-password \
  --secret-string "$NEW"

kubectl -n nexus-qa annotate externalsecret nexus-qa-secrets \
  force-sync=$(date +%s) --overwrite
kubectl -n nexus-qa rollout restart deploy/nexus-qa-backbone-engine
```

### Object-storage IAM credentials

**Cloud-native rotation is preferred over per-pod credentials.** When
the cluster uses IRSA / Workload Identity / Azure Workload Identity,
the platform's pods carry zero static credentials — the IAM role
attached to the engine ServiceAccount is what S3 / GCS / Blob trusts,
and the cloud rotates the underlying STS / GSA token automatically.

Per-cloud static-key rotation (only when IRSA/WI is NOT available):

```bash
# AWS — rotate the IAM access key behind the engines' SA.
aws iam create-access-key --user-name nexus-${ENV}-engines
# Push the new AccessKeyId + SecretAccessKey to KMS:
aws secretsmanager put-secret-value \
  --secret-id nexus-platform/${ENV}/s3-access-key \
  --secret-string "<new-access-key-id>"
aws secretsmanager put-secret-value \
  --secret-id nexus-platform/${ENV}/s3-secret-key \
  --secret-string "<new-secret-access-key>"
kubectl -n nexus-qa annotate externalsecret nexus-qa-secrets \
  force-sync=$(date +%s) --overwrite
# Engine SDK rebuilds the boto session on next upload — no restart.
# Wait ~1 min for boto's existing in-flight uploads to drain, then:
aws iam delete-access-key --user-name nexus-${ENV}-engines \
  --access-key-id <old-access-key-id>
```

GCS and Azure follow the same pattern; the operative knob is whether
the cluster runs Workload Identity (none of the above is needed) or
static-key auth (rotate as shown).

### KMS keys (envelope rotation)

Provider-managed encryption keys (AWS CMK, GCP CMEK, Azure Key Vault)
rotate transparently — no platform-side action required. The platform
Secret remains the same; only the **envelope** changes.

Verify rotation via:

```
aws kms describe-key --key-id alias/nexus-platform-${ENV}-secrets \
  --query 'KeyMetadata.{Rotation:KeyRotationEnabled,LastRotation:LastRotatedDate}'
```

GCP equivalent:

```
gcloud kms keys describe nexus-secrets-${ENV} \
  --keyring=nexus-platform --location=${REGION} \
  --format='value(rotationPeriod,nextRotationTime)'
```

### Shield encryption key

The Shield engine encrypts PII at rest with a Fernet key. Rotation
requires **dual-key** mode for a window:

1. Generate the new key:
   ```
   NEW=$(python -c 'import cryptography.fernet as f; print(f.Fernet.generate_key().decode())')
   ```
2. Push to KMS (use a versioned secret).
3. Set `SHIELD_DECRYPTION_KEYS` to `<old>,<new>` (comma-separated) and
   `SHIELD_ENCRYPTION_KEY` to `<new>` via an env override; restart
   shield-engine. The engine reads both for decryption, encrypts with
   only the new one.
4. Re-encrypt the historical store with a background job (separate
   runbook).
5. After re-encryption completes, drop the old key from
   `SHIELD_DECRYPTION_KEYS` and restart shield-engine.

The chart-managed `shield-encryption-key` Secret field is **the active
encryption key**; the previous-key list is held outside the chart in a
dedicated ConfigMap so rotation doesn't churn the main release.

### LLM tier API keys (Heart / Brain)

Heart and Brain read `HEART_TIER1_API_KEY` / `HEART_TIER2_API_KEY` /
`BRAIN_TIER1_API_KEY` / `BRAIN_TIER2_API_KEY` from env. The engines
rebuild the per-request HTTP client on every call from `os.environ`, so
no pod restart is required after the Secret is updated:

```
aws secretsmanager put-secret-value --secret-id nexus-platform/${ENV}/heart-tier1-api-key \
  --secret-string "$NEW_KEY"
kubectl -n nexus-qa annotate externalsecret nexus-qa-secrets \
  force-sync=$(date +%s) --overwrite
```

If a request lands on a pod between the env reload and the next API
call, it falls through to tier 2 — graceful degradation.

---

## ESO refresh behaviour

`ExternalSecret.spec.refreshInterval` (default `1h` from
[values.yaml](../helm/nexus-qa/values.yaml)) is the routine interval.

`ClusterSecretStore.spec.refreshInterval` (default `10m`) controls how
often the store revalidates the upstream connection. A store that lost
permissions takes at most 10 minutes to flip the
`ClusterSecretStoreUnavailable` alert.

To force an immediate sync of one ExternalSecret:

```
kubectl annotate externalsecret <name> -n <ns> \
  force-sync=$(date +%s) --overwrite
```

To force a re-evaluation of the store credential (e.g. after an IAM
policy change):

```
kubectl -n external-secrets rollout restart deploy/external-secrets
```

---

## Validation

After ANY rotation, verify in this order:

1. `kubectl get externalsecret nexus-qa-secrets -n nexus-qa -o yaml`
   — check `status.conditions[type=Ready].status == True` and the
   `refreshTime` is recent.

2. `kubectl get secret nexus-qa-secrets -n nexus-qa -o yaml | head -30`
   — verify the b64-decoded value matches what you pushed upstream.

3. End-to-end smoke: log in via auth-service, hit a platform-api
   endpoint that touches the database, upload a tiny artifact via eyes
   and confirm the engine logs `eyes.frames_uploaded backend=<store>
   count=...`. Three signals from three different code paths.

If step 1 or 2 fail, check the ESO controller logs:

```
kubectl -n external-secrets logs -l app.kubernetes.io/name=external-secrets \
  --tail=200 | grep -i error
```

If a particular key is missing in the upstream backend, ESO surfaces
that as `SecretSyncedError`. The `ExternalSecretSyncFailing` alert
catches this within 10 minutes (see
[`common/00-prometheus-rules.yaml`](./resources/external-secrets/common/00-prometheus-rules.yaml)).

---

## Emergency rotation (compromise)

When a credential is suspected to be compromised, rotate it first, ask
later. The procedure is the same as scheduled rotation; the only
addition is auditing the access history:

1. Rotate per the per-secret procedure above.
2. AWS: `aws cloudtrail lookup-events --lookup-attributes \
   AttributeKey=ResourceName,AttributeValue=nexus-platform/${ENV}/<key>`
3. GCP: `gcloud logging read 'protoPayload.resourceName:"nexus-platform/${ENV}/<key>"' --limit=200`
4. Azure: `az monitor activity-log list --resource-group <rg> \
   --offset 7d`
5. File an incident ticket with the access timeline.

The blast-radius from a compromised platform secret is bounded by
network policies (NetworkPolicy deny-by-default; only the platform's
own egress allowlist is reachable) and by the ClusterSecretStore IAM
scope (tag-based; the role can only read `nexus:env=${ENV}` secrets).
That bound is what gives operators 10–15 minutes of safety margin
during a rotation.

---

## Out of scope

- Cluster-CA and certificate rotation — owned by cert-manager (see
  [`resources/cert-manager/`](./resources/cert-manager/)).
- Linkerd trust-anchor rotation — see
  [`resources/linkerd/00-trust-anchor.yaml`](./resources/linkerd/00-trust-anchor.yaml);
  the 10-year root rotates manually with a separate runbook.
- Argo CD admin password — set via the Argo CD operator, not this
  pipeline.
