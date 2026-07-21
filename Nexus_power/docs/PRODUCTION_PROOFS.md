# VKPower Verdict — production proofs, phase by phase

The distance to a 9.5/10 production bar is not missing engineering — most of it is
built. It is **operational proof**: showing each phase's exit criterion holds, on
evidence. This maps every phase to the exact proof, and states honestly whether it is
**already proven**, **machinery-ready** (one command, once infra is up), or **needs
infra / an external party**.

Status legend: ✅ proven · 🔧 machinery ready (run it) · ⏳ needs infra/customer/auditor

| Phase | Exit criterion | The proof (run this) | Status |
|---|---|---|---|
| **0** Reproducible source | git == what runs | `ci/reproducibility/verify_deployment.py` (deploy-verify gate); measured git↔VM diff = 162/164 identical | ✅ source proven; 🔧 gate wired |
| **0** No orphaned code | shipped code is wired | `ci/reproducibility/reachability_gate.py` (CI-ratcheted on clean services) | ✅ measured; 🔧 backlog tracked |
| **1** Hardened deploy | one env built from CI images | `scripts/prove_install.sh` — kind + build-from-git + `helm install verdict` (on-prem) + migrations + deploy-verify + smoke | 🔧 machinery ready |
| **1** Chart integrity | chart renders for all profiles | `infrastructure/helm/verdict/scripts/chart_lint.py` (base + on-prem + air-gapped, strict) — in `deploy-validation.yml` | ✅ proven (0 errors) |
| **2** Backups + DR | restore rehearsed, evidence intact | `scripts/dr_drill.sh` — backup → restore → row-count witness on the evidence tables + RTO | 🔧 machinery ready |
| **3** Tenant isolation | cross-tenant access denied | `tests/contract/test_rls_isolation.py` — behavioural RLS proof across **all 21 tables** (`WITH CHECK` smuggle-rejection), run against **real Postgres** in `qec-ci.yml` | ✅ **proven in CI** |
| **3** Secret / network posture | secrets external, network isolated | chart `external-secret.yaml`, `networkpolicy.yaml`, `secret.yaml`, KEK; `--strict` chart-lint proves every `secretKeyRef` is emitted | ✅ present + lint-proven |
| **3** External assurance | 3rd-party pen test | — | ⏳ needs auditor |
| **4** Scale + isolation under load | stays up + fair at target load | `cd.yml` → `load-test-gate` (smoke + 100-user) on the staging deploy | ⏳ needs the pipeline to run |
| **5** Observability | metrics + alerts | chart `servicemonitor.yaml` (scrape) + `prometheusrule.yaml` (SLO rules + alerts over real `qec_*` metrics) | ✅ present + metric-verified; ⏳ alerts firing needs a live Prometheus |
| **6** Honest regression, per-flow | catch a real regression, name the flow | verified live: healthy=green → break one route → 1 GENUINE_REGRESSION + 8 PASS (`fix(cycle) 61580c2`) | ✅ proven live |
| **6** Value-drift auto-catch | catch a wrong number automatically | needs a value-reaching (form→result) generated flow — scoped in `docs/HARDENING_SUMMITLIFE_HANDOVER_2026-07-21.md` | ⏳ not yet |
| **7** Pilot → GA | one reference install on a client's cluster | on-prem runbook + `prove_install.sh` as the dress rehearsal | ⏳ needs a design partner |

## The single highest-leverage run
`scripts/prove_install.sh` on any machine with Docker + kind + helm. It exercises
Phase 0, 1, 5 wiring, and the migrations path in one shot, and ends green only if every
pod is provably the git checkout. Follow it with `scripts/dr_drill.sh` (Phase 2). Those
two commands convert three ⏳/🔧 rows to ✅ without any cloud or customer.

## What genuinely needs others
- The `cd.yml` **load-test gate** passing on a real staging cluster (Phase 4).
- A **third-party security audit** (Phase 3 external assurance).
- **One design-partner install** (Phase 7).

Everything else is either proven or one command away.
