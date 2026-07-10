# VKPower Verdict — On-Prem / Air-Gapped Install Runbook (Phase 8)

**Goal:** install VKPower Verdict inside a **regulated client's own Kubernetes
cluster**, where **all tenant data and all proof-of-behavior evidence stay in
that cluster** and never leave it. This is the Helm-packaged counterpart to the
dedicated-box deploy (`QECentral/docs/VERDICT_DEPLOY_RUNBOOK.md`): same
fail-closed safety spine, now installable + certifiable for buyers who require
on-prem, air-gapped, KMS/Vault-fronted operation.

Chart: `infrastructure/helm/verdict/` (see its `README.md` for the resource +
values map). This runbook is the operator's step-by-step.

> **Bounded context — VKPower stays untouchable.** The Verdict chart deploys the
> **Verdict plane only** (qe-central, qe-explorer + its egress sandbox,
> repo-intel, the portal, and a dedicated qecentral Postgres). The VKPower
> **factory** (`platform-api` + engines + the `nexus` DB) is an **external**
> dependency the Verdict plane talks to over HTTP + a least-privilege DB role —
> deploy it with the `nexus-qa` chart (or the existing compose) and point
> Verdict at it with `factory.platformApiUrl` / `factory.substrate.*`.

---

## 0. Data-residency guarantee (what "stays in the cluster" means)

| Data class | Where it lives | Leaves the cluster? |
|---|---|---|
| qecentral tables (apps, cycles, verdicts, dossiers, waivers, cost ledger) | the chart's dedicated Postgres (or the client's external PG) | **No** |
| Evidence substrate (sessions, page_visits, frames, ground-truth) | the factory `nexus` DB + object store, both in-cluster | **No** |
| Client app credentials (`client_apps.creds_blob`) | encrypted at rest with a **customer-managed KEK**; ciphertext in the DB | **No** (KEK never leaves the client's KMS/Vault) |
| The operator's JWT | the browser's `sessionStorage` only | **No** |
| **Outbound**, during a crawl | the **egress proxy → the allowlisted target app only** | the single, audited, host-allowlisted hop |

The only egress the platform ever makes is the quarantined explorer reaching the
**one onboarded target app** through the squid proxy's hard host allowlist. The
NetworkPolicies make every other outbound path impossible by construction
(§1.1): the explorer can reach neither the databases, nor the factory, nor the
internet directly.

---

## 1. Prerequisites

* A Kubernetes cluster (v1.24+) with a **NetworkPolicy-enforcing CNI**
  (Calico / Cilium / Weave) — the isolation invariants are enforced by policy.
* A default (or named) **StorageClass** for the dedicated Postgres + evidence
  volumes (`global.storageClass`).
* The **VKPower factory** reachable in-cluster (namespace, e.g. `nexus-qa`),
  with its `nexus` DB and `platform-api` Service up.
* A **customer-managed KMS key** (GCP CryptoKey or AWS CMK) **or** a
  KMS-API-compatible endpoint the client controls (Vault AWS-KMS engine / an HSM
  proxy). `local` KEK is **dev/pilot only** and is refused in a deployed env.
* Optionally the **External Secrets Operator** + a `ClusterSecretStore`/
  `SecretStore` bound to the client's KMS/Vault (recommended — no plaintext
  secret ever lives in the release).
* Optionally the **Prometheus Operator** (kube-prometheus-stack) for the
  `ServiceMonitor`.
* `helm` v3.14+ and `kubectl` on the operator's workstation (a jump host with
  cluster access; it needs no internet in the air-gapped case).

---

## 2. Load the images into a private registry (air-gapped)

Verdict ships four images: `qe-central`, `qe-explorer`, `repo-intel`,
`verdict-portal` (plus the third-party `postgres` and `squid` images). On a host
**with** internet, pull + save them; move the tarball across the air gap; load +
push into the client's **private registry**.

```bash
# ── On a connected host: pull + save (retag to the client's registry path) ──
REG=registry.airgap.internal/vkpower           # the client's registry path
TAG=2.0.0
for img in qe-central qe-explorer repo-intel verdict-portal; do
  docker pull ghcr.io/nexus-qa/$img:$TAG        # or build from the repo
  docker tag  ghcr.io/nexus-qa/$img:$TAG $REG/$img:$TAG
done
# Third-party deps (pin the digests your security team approves):
docker pull postgres:16-alpine && docker tag postgres:16-alpine $REG/postgres:16-alpine
docker pull ubuntu/squid:latest && docker tag ubuntu/squid:latest $REG/squid:airgap

docker save \
  $REG/qe-central:$TAG $REG/qe-explorer:$TAG $REG/repo-intel:$TAG \
  $REG/verdict-portal:$TAG $REG/postgres:16-alpine $REG/squid:airgap \
  -o verdict-images-$TAG.tar

# ── Move verdict-images-$TAG.tar across the air gap, then on an internal host ──
docker load -i verdict-images-$TAG.tar
for ref in qe-central:$TAG qe-explorer:$TAG repo-intel:$TAG \
           verdict-portal:$TAG postgres:16-alpine squid:airgap; do
  docker push $REG/$ref
done
```

Create the pull secret the pods reference (`global.imagePullSecrets`):

```bash
kubectl -n verdict create secret docker-registry verdict-registry \
  --docker-server=registry.airgap.internal \
  --docker-username=<user> --docker-password=<token>
```

Point the chart at the private registry (already set in
`values-airgapped.yaml`): `global.image.registry=registry.airgap.internal`,
`global.image.repository=vkpower`, `global.image.tag=2.0.0`,
`global.image.pullPolicy=IfNotPresent`. Override the squid image with
`--set egressProxy.image=registry.airgap.internal/vkpower/squid:airgap` and the
Postgres image with `--set postgres.image=registry.airgap.internal/vkpower/postgres:16-alpine`.

---

## 3. KMS / KEK options on-prem (the client's own key)

Credentials are **envelope-encrypted**: a per-secret DEK is wrapped by a KEK the
**client owns and controls**. Pick one:

| Provider | `kek.provider` | Config | Notes |
|---|---|---|---|
| **GCP KMS** | `gcp_kms` | `kek.gcpKey=projects/…/cryptoKeys/…` | customer-managed CryptoKey; the node/SA needs `cloudkms.cryptoKeyVersions.useToEncrypt/Decrypt` |
| **AWS KMS** | `aws_kms` | `kek.awsArn=arn:aws:kms:…:key/…`, `kek.awsRegion=…` | customer-managed CMK |
| **Vault / HSM (air-gapped)** | `aws_kms` | `kek.awsArn=<key on the endpoint>`, `kek.awsRegion=<label>` + point the AWS SDK at the client's **KMS-API-compatible endpoint** (Vault AWS-KMS secrets engine, or an on-prem HSM proxy) | the KEK never leaves the client's boundary |

> **Fail-closed by design.** In a deployed env (`env.nexusEnv` ∈
> {`staging`,`production`}) the qe-central boot gate **and** the SDK
> `EnvelopeService` both **refuse** `kek.provider=local`. There is no way to run
> a client's real credentials on the development KEK — an unsafe posture is
> impossible to start. `local` is available only for a throwaway pilot on
> `env.nexusEnv=development`.

The `/health` endpoint reports the live posture without leaking anything:
`kek.is_production_grade=true` only for `gcp_kms`/`aws_kms` with a working
envelope; a degraded KEK makes credential writes refuse (503) rather than run
unprotected.

---

## 4. Secrets — from the client's KMS/Vault (no plaintext)

Verdict needs five secrets: `jwt-secret` (must match the factory's
`NEXUS_JWT_SECRET`), `explorer-token` (per-fleet HMAC), `qec-db-password`,
`substrate-db-password`, and `postgres-password` (the chart-managed PG
superuser). **Never** commit them.

**Recommended — External Secrets Operator.** Store the five under a prefix in
the client's backend, then:

```bash
# values-onprem.yaml already sets externalSecrets.enabled=true, remoteKeyPrefix=verdict
helm ... --set externalSecrets.secretStoreRef.name=<client-ClusterSecretStore>
# ESO then syncs verdict/{jwt-secret,explorer-token,qec-db-password,
#   substrate-db-password,postgres-password} into the release Secret. secret.yaml
#   is skipped so there is no plaintext-vs-ESO collision.
```

**Alternative — `--set` at install** (values stay out of git; use a private,
short-lived override file or a CI secret store):

```bash
--set secrets.jwtSecret=$JWT --set secrets.explorerToken=$EXPLORER \
--set secrets.qecDbPassword=$QECPW --set secrets.substrateDbPassword=$SUBPW \
--set secrets.postgresPassword=$PGPW
```

Generate strong values (`openssl rand -hex 32`). If any secret is empty/dev in a
deployed env, qe-central **refuses to boot** — that is the safety spine, not a
bug.

---

## 5. One-time DB role bootstrap (outside the chart — VKPower untouchable)

The `qec_substrate` role and its **least-privilege** grants live on the
**factory `nexus` DB** and require superuser + touch a VKPower-owned database, so
the chart never performs them. Run the repo's bootstrap once against the factory
Postgres (it also creates the `qec` role + `qecentral` DB if you are NOT using
the chart-managed Postgres):

```bash
# from a pod/host with psql to the factory postgres:
psql -h <factory-pg-host> -U <superuser> -d nexus \
  -v qec_password="$QECPW" -v qec_substrate_password="$SUBPW" \
  -f scripts/qec_db_bootstrap.sql
```

If you use the **chart-managed** Postgres (`postgres.enabled=true`), the chart's
first-init hook already creates the `qec` role RLS-safely (LOGIN, NOSUPERUSER,
NOBYPASSRLS) and hands it ownership of `qecentral` — so on the factory side you
only need the `qec_substrate` grants (the SQL above is idempotent and applies
just those when the `qecentral` parts already exist).

> **Why NOSUPERUSER/NOBYPASSRLS matters:** the qecentral tables use `FORCE ROW
> LEVEL SECURITY` for tenant isolation. A superuser or a `BYPASSRLS` role would
> silently defeat it. The chart's role model guarantees the app role is subject
> to RLS.

---

## 6. Install

```bash
kubectl create namespace verdict

helm install verdict infrastructure/helm/verdict \
  -n verdict \
  -f infrastructure/helm/verdict/values.yaml \
  -f infrastructure/helm/verdict/values-onprem.yaml \
  # air-gapped: ALSO layer the air-gapped overlay
  # -f infrastructure/helm/verdict/values-airgapped.yaml \
  --set kek.provider=gcp_kms \
  --set kek.gcpKey=projects/<p>/locations/<l>/keyRings/verdict/cryptoKeys/verdict-kek \
  --set factory.platformApiUrl=http://nexus-qa-platform-api.nexus-qa.svc:8091 \
  --set factory.substrate.host=nexus-qa-postgres.nexus-qa.svc \
  --set externalSecrets.secretStoreRef.name=<client-store> \
  --set 'egressProxy.allowedDomains={app.staging.client.internal}' \
  --set 'egressProxy.excludeCidrs={10.0.0.0/8,172.16.0.0/12}'
```

The migration Job (`alembic upgrade head`) runs automatically as a post-install
hook. `helm install` blocks until the hook completes.

**Set the egress allowlist to the ONE onboarded target app.** An empty
`egressProxy.allowedDomains` is intentionally **fail-closed** (squid denies all
real egress). `egressProxy.excludeCidrs` must list the cluster's pod + service
CIDRs so the proxy can never reach in-cluster services.

---

## 7. Verify (the on-prem exit gates)

```bash
# a) all pods healthy
kubectl -n verdict get pods

# b) qe-central health + KEK is production-grade (not degraded)
kubectl -n verdict port-forward svc/verdict-qe-central 8093:8093 &
curl -s localhost:8093/health | python -m json.tool
#    expect: status "healthy", kek.is_production_grade true, envelope true

# c) fail-closed proof — the boot gate refuses a dev secret. Try a bad install
#    in a scratch namespace and confirm qe-central CrashLoops with
#    "qe_central.boot_safety.REFUSED" in its logs (do NOT do this in prod ns).

# d) network isolation proof (§1.1) — the explorer cannot reach the DB:
kubectl -n verdict exec deploy/verdict-qe-explorer -- \
  sh -c 'timeout 5 sh -c "</dev/tcp/verdict-postgres/5432" && echo REACHED || echo BLOCKED'
#    expect: BLOCKED (or a timeout) — the NetworkPolicy denies it.

# e) the REFUSE matrix on the real stack (same proof as the box deploy)
kubectl -n verdict exec deploy/verdict-qe-central -- \
  sh -lc 'QE_HARNESS_ENABLED=true python -m app.harness.runner'
#    expect: R1..R8 REFUSED_CORRECTLY, baseline PASS_BASELINE, exit 0

# f) metrics scrape (if the ServiceMonitor is enabled)
curl -s localhost:8093/metrics | grep -c '^qec_'
```

Portal: `kubectl -n verdict port-forward svc/verdict-verdict-portal 8080:80`,
then open `http://localhost:8080` (or the ingress host).

---

## 8. Upgrade path

Verdict is upgraded like any Helm release; the schema migration re-runs as a
hook and is a no-op at head.

```bash
# 1. Load the new image tag into the private registry (§2).
# 2. Roll the release forward:
helm upgrade verdict infrastructure/helm/verdict \
  -n verdict \
  -f infrastructure/helm/verdict/values.yaml \
  -f infrastructure/helm/verdict/values-onprem.yaml \
  --set global.image.tag=<new-tag> \
  --reuse-values
# 3. The post-upgrade Job runs `alembic upgrade head` against the qecentral DB.
# 4. Verify (§7). Rollback if needed:
helm rollback verdict -n verdict
```

Notes:
* **Rolling, zero-downtime** for the stateless services (`maxUnavailable: 0`).
* The dedicated Postgres is a single StatefulSet; **back it up before an
  upgrade** (the qecentral DB is the client's proof-of-behavior system of
  record). Use the client's PITR tooling, or `verdict_pg_backup.sh` adapted to
  the in-cluster PG. For a regulated SoR, an external managed Postgres
  (`postgres.enabled=false`) with the client's HA + PITR is preferred.
* Secret rotation: rotate in the client's KMS/Vault; ESO re-syncs on
  `externalSecrets.refreshInterval`. The chart-managed PG's role passwords are
  set on first init only — rotate those via `ALTER ROLE` on the running DB.

---

## 9. Compliance & residency summary (for the security review)

* **Data residency:** every table, every evidence artifact, every credential
  ciphertext, and the KEK all stay inside the client's cluster/KMS. The lone
  egress is the audited, host-allowlisted crawl hop to the one onboarded app.
* **Fail-closed:** the service will not boot on dev secrets or the dev KEK in a
  deployed env; the egress allowlist is deny-all until explicitly set; the
  onboarding guard refuses a crawl of any non-attested / production target.
* **Least privilege:** the substrate writer role has INSERT/SELECT on exactly
  seven tables (+ a scoped UPDATE + tenant bootstrap) and nothing else; the app
  role is NOSUPERUSER/NOBYPASSRLS so `FORCE RLS` tenant isolation holds.
* **Evidence integrity:** verdicts are hash-chained and dossiers/waivers are
  append-only (`platform/api/app/services/test_factory/verdict_events.py`), read
  through the Verdict plane — no evidence leaves the tenant.
* **Auditability:** `/metrics` exposes low-cardinality `qec_*` control-plane
  signals (no tenant/app id, no PII); `/health` reports the KEK posture without
  leaking key material.
* **Gated on the client / an external auditor:** a *running* 20-client fleet and
  a *signed* SOC 2 report are operational milestones outside this package — this
  chart builds everything up to that line (installable, isolated, certifiable).
