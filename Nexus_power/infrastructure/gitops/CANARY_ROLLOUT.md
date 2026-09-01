# Canary Rollout Runbook

This runbook describes the canary delivery lifecycle for every
opted-in service (see `rollout.components` in the env's `platform.yaml`
overlay). Both engine and platform services are eligible; the canary
mechanism is identical.

The mechanism is Argo Rollouts in `workloadRef` mode — the existing
Deployment is the single source of truth for pod template; Argo
Rollouts manages the Deployment's replica counts during canary
progression. No pod spec is duplicated between Deployment and Rollout.

---

## What progressive delivery looks like

For each rollout, traffic flows like this:

```
git push → Argo CD syncs Deployment
            ↓ (new pod template detected)
         Argo Rollouts emits a Canary ReplicaSet
            ↓
   ┌────────────────────────────────────────┐
   │ step 0: setWeight 10 (10% of traffic)  │
   │ step 1: pause 5m                       │
   │ step 2: analysis (4 templates)         │
   │ step 3: setWeight 25                   │
   │ step 4: pause 10m                      │
   │ step 5: analysis (2 templates)         │
   │ step 6: setWeight 50                   │
   │ step 7: pause 15m                      │
   │ step 8: analysis (2 templates)         │
   │ step 9: setWeight 100  → stable RS     │
   └────────────────────────────────────────┘
```

Any analysis run that returns failure aborts the rollout immediately:
the Rollout flips to `Degraded`, the canary ReplicaSet scales down,
and the stable RS continues serving 100% of traffic.

---

## SLO gates (analysis templates)

All four templates live in
[`infrastructure/helm/nexus-qa/templates/analysis-templates.yaml`](../helm/nexus-qa/templates/analysis-templates.yaml).
They are parameterised — the Rollout supplies the `service` arg, the
rest come from env values.

| Template          | What it checks                                  | Default threshold              |
|-------------------|-------------------------------------------------|--------------------------------|
| `prometheus-up`   | The metrics provider is reachable at all        | `vector(1) == 1` for 3 samples |
| `error-rate`      | 5xx ratio over a rolling 2-minute window        | ≤ 1% (≤ 0.5% in production)    |
| `latency-p95`     | p95 request duration                            | ≤ 1.5s (≤ 1.0s in production)  |
| `slo-burn-rate`   | SRE-book fast-burn: (1 - SR_1h) / (1 - SLO)     | < 14.4 (2% budget burn in 1h)  |

The `slo-burn-rate` template is the critical one for monthly error
budgets. The other three are early-warning signals.

---

## Operator commands

The Argo Rollouts CLI is the workflow's primary surface. Install:

```
brew install argoproj/tap/kubectl-argo-rollouts   # or download from GitHub
```

### Watch a rollout

```
kubectl argo rollouts get rollout nexus-qa-eyes-engine -n nexus-qa --watch
```

You see the current step, weights, healthy/desired replica counts per
RS, and any in-flight AnalysisRun. The dashboard view fits in one
terminal.

### Promote past a paused step

Pause steps are explicit operator approval gates — analysis runs are
automatic. To advance past a paused step:

```
kubectl argo rollouts promote nexus-qa-eyes-engine -n nexus-qa
```

To skip remaining steps and immediately promote to 100%:

```
kubectl argo rollouts promote nexus-qa-eyes-engine -n nexus-qa --full
```

`--full` is a destructive override; it bypasses every remaining
analysis gate. Use only when the canary signal has been confirmed
manually (operator-driven smoke test) AND the analysis run is failing
on a known false positive (metrics gap, prometheus restart).

### Abort a failing canary

```
kubectl argo rollouts abort nexus-qa-eyes-engine -n nexus-qa
```

Abort scales the canary RS down immediately. The stable RS continues
serving 100% of traffic. The rollout enters `Aborted` state; a new
sync from Argo CD will retry from step 0 (after the operator commits
a fix).

### Retry after fix

After the underlying issue is addressed (commit pushed, Deployment
updated), the rollout auto-restarts. To manually kick off a retry:

```
kubectl argo rollouts retry rollout nexus-qa-eyes-engine -n nexus-qa
```

### Restart pods on demand (no canary)

For configuration-only changes that don't change the pod template
(e.g. ConfigMap update), the Rollout doesn't fire. Use:

```
kubectl rollout restart deploy/nexus-qa-eyes-engine -n nexus-qa
```

This is a standard Deployment restart — the Rollout sees the new pod
template hash and runs a canary on the restart.

---

## Notifications

The argo-rollouts controller posts to Slack and PagerDuty via the
notification ConfigMap at
[`infrastructure/gitops/resources/argo-rollouts/00-notification-configuration.yaml`](./resources/argo-rollouts/00-notification-configuration.yaml).

Webhook tokens are pulled from the cluster's KMS by the ESO
ExternalSecret at
[`resources/argo-rollouts/10-notification-secret.yaml`](./resources/argo-rollouts/10-notification-secret.yaml).
The remote keys are `nexus-qa/argo-rollouts-slack-token` and
`nexus-qa/argo-rollouts-pagerduty-token`; rotate via the standard
flow in [`SECRETS_ROTATION.md`](./SECRETS_ROTATION.md).

Triggered events:
| Event                  | Routes to                            |
|------------------------|--------------------------------------|
| Rollout paused         | `#release` Slack channel             |
| Rollout aborted        | `#release` Slack + PagerDuty platform|
| Analysis run failed    | `#release` Slack + PagerDuty platform|

---

## When the canary aborts

1. Slack notification fires on `on-analysis-run-failed`.
2. Find the AnalysisRun:
   ```
   kubectl argo rollouts get rollout <name> -n <ns>
   # ↑ shows the AnalysisRun name attached to the current step.
   kubectl get analysisrun <run> -n <ns> -o yaml
   ```
3. Inspect the metric result. The condition that fired is shown under
   `status.metricResults`.
4. Compare against Grafana — if the metric agrees, the canary is
   genuinely worse than stable: revert the commit.
5. If the metric disagrees, you have a measurement gap — fix the
   metric (the gate, not the canary).
6. After fix, push a new commit. Argo CD syncs, the Rollout retries.

The 10–15 minute window between analysis windows gives operators time
to look without forcing a blind abort.

---

## Per-env timing

| Env        | Total walk time | Pauses (10%/25%/50%) | Analysis thresholds       |
|------------|-----------------|----------------------|---------------------------|
| dev        | n/a             | rollout disabled     | n/a                       |
| staging    | ~10 min         | 30s / 1m / 2m        | 99% SLO, 2% err, 2s p95   |
| production | ~30 min         | 5m / 10m / 15m       | 99.5% SLO, 0.5% err, 1s p95 |

The staging settings come from `rollout.shortPauses: true` in
[`envs/staging/values/platform.yaml`](./envs/staging/values/platform.yaml).
Production keeps the default long pauses defined in the chart.

---

## What this runbook does NOT cover

- Database schema migrations — those are handled by a pre-deploy Job,
  not by a Rollout. See `migrations` runbook (separate).
- Stateful service rollouts (Postgres, Neo4j, Redis) — these are not
  in `rollout.components`; they restart in-place during Helm upgrade.
- Argo Rollouts dashboard installation — disabled by default; enable
  via `controller.dashboard.enabled` in env values if needed.
