# VKPower Verdict — Helm chart

Packages the **Verdict plane** (autonomous Centralized QE, proof-of-behavior)
for install in a **regulated client's own Kubernetes cluster** — on-prem or
air-gapped, all data + evidence staying inside the tenant. The VKPower factory
(`platform-api`) is a **bounded, external dependency** referenced by URL; this
chart never re-deploys or modifies it (VKPower stays untouchable).

Full runbook: `QECentral/docs/ONPREM_INSTALL.md` (at the repo root, beside
`VERDICT_DEPLOY_RUNBOOK.md`).

## What it deploys

| Template | Resource(s) | Notes |
|---|---|---|
| `qe-central.yaml` | Deployment + Service (+ optional HPA/PVCs) | control plane + substrate writer; `/health`, `/metrics`, `/api/v1/qec/*` on 8093 |
| `qe-explorer.yaml` | Deployment + Service | the **quarantined** browser; internal-only; `/dev/shm` for Chromium |
| `egress-proxy.yaml` | Deployment + Service + 2 ConfigMaps | squid — single NAT hop with a **fail-closed host allowlist** |
| `repo-intel.yaml` | Deployment + Service (+ optional PVC) | OFF the critical path; disabled by default |
| `verdict-portal.yaml` | Deployment + Service + nginx ConfigMap | Command Center SPA; proxies the API to the in-cluster qe-central |
| `postgres.yaml` | StatefulSet + Service + init ConfigMap | **dedicated qecentral DB**; RLS-safe `qec` role (NOSUPERUSER/NOBYPASSRLS); skip with `postgres.enabled=false` for an external DB |
| `networkpolicy.yaml` | NetworkPolicies | enforces the §1.1 isolation invariants (explorer reaches only the proxy + its callback, **never the DB**) |
| `servicemonitor.yaml` | ServiceMonitor(s) | Prometheus Operator scrape of the `qec_*` metrics |
| `secret.yaml` / `external-secret.yaml` | Secret **or** ExternalSecret | never plaintext; ESO syncs from the client's KMS/Vault |
| `migrations-job.yaml` | Job (Helm hook) | `alembic upgrade head` post-install/upgrade |
| `ingress.yaml` | Ingress | optional external exposure of the portal |
| `serviceaccount.yaml`, `NOTES.txt` | — | — |

## Values switches

| Switch | Default | Effect |
|---|---|---|
| `env.nexusEnv` | `production` | arms the fail-closed boot gate + JWT audience enforcement |
| `kek.provider` | `local` | **must** be `gcp_kms`/`aws_kms` in a deployed env (both the boot gate and the SDK refuse `local`) |
| `postgres.enabled` | `true` | chart-managed qecentral DB vs. an external managed Postgres (`postgres.external.*`) |
| `qeExplorer.enabled` / `egressProxy.enabled` | `true` | the crawl path (with its egress sandbox) |
| `repoIntel.enabled` | `false` | opt-in repo intelligence |
| `portal.enabled` | `true` | the Command Center SPA |
| `networkPolicies.enabled` | `true` | the isolation invariants (keep ON for regulated installs) |
| `externalSecrets.enabled` | `false` | source every credential from KMS/Vault (recommended) |
| `monitoring.serviceMonitor.enabled` | `false` | Prometheus Operator scrape |
| `migrations.enabled` | `true` | run the schema migration as a hook |
| `egressProxy.allowedDomains` | `[]` | **empty ⇒ fail-closed** (denies all real egress) |

Overlays: `values-onprem.yaml` (regulated cluster, KMS/Vault, isolation ON) and
`values-airgapped.yaml` (private registry, no internet).

Every new capability is **opt-in with defaults that preserve today's behavior**;
the two DSNs are composed in the pods with the DB password injected from the
Secret via `$(VAR)` expansion, so no credential is ever written to a ConfigMap.

## Linting (CI)

`helm` is not required locally. A static linter mirrors the nexus-qa chart and
catches typo'd `.Values` paths, undefined helper includes, and unknown
`secretKeyRef` keys:

```bash
python infrastructure/helm/verdict/scripts/chart_lint.py                       # base
python infrastructure/helm/verdict/scripts/chart_lint.py --values values-onprem.yaml
python infrastructure/helm/verdict/scripts/chart_lint.py --values values-airgapped.yaml
```

**Add these jobs to `.github/workflows/deploy-validation.yml`** (same shape as the
nexus-qa jobs already there — this chart follows that chart's conventions):

```yaml
  verdict-helm-static-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: python -m pip install --upgrade pip pyyaml
      - run: python infrastructure/helm/verdict/scripts/chart_lint.py
      - run: python infrastructure/helm/verdict/scripts/chart_lint.py --values values-onprem.yaml
      - run: python infrastructure/helm/verdict/scripts/chart_lint.py --values values-airgapped.yaml

  verdict-helm-template-render:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: azure/setup-helm@v4
        with: { version: v3.14.4 }
      - run: helm lint infrastructure/helm/verdict
      - run: |
          helm template verdict infrastructure/helm/verdict \
            -f infrastructure/helm/verdict/values-onprem.yaml \
            --set secrets.jwtSecret=ci-placeholder \
            --set secrets.explorerToken=ci-placeholder \
            --set secrets.qecDbPassword=ci-placeholder \
            --set secrets.substrateDbPassword=ci-placeholder \
            --set secrets.postgresPassword=ci-placeholder \
            --set kek.provider=gcp_kms --set kek.gcpKey=ci/placeholder \
            > /tmp/verdict-rendered.yaml
          echo "Rendered $(wc -l < /tmp/verdict-rendered.yaml) lines"
```
