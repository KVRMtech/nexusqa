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
| **2** Backups + DR | restore rehearsed, evidence intact | `scripts/verdict_pg_backup.sh --restore-drill` (alembic-head match + no-empty-restore over a seeded row); wired as a blocking step in `qec-ci.yml` | 🔧 **passed a local rehearsal** (real Postgres, BACKUP_OK + RESTORE_DRILL_PASS); CI gate wired but qec-ci **not yet green end-to-end** |
| **3** Tenant isolation | cross-tenant access denied | `tests/contract/test_rls_isolation.py` — behavioural RLS proof (`WITH CHECK` smuggle-rejection), run against **real Postgres** in `qec-ci.yml` | ⚠️ **NOT yet proven — currently RED.** First real qec-ci run: `test_page_visits_isolates_tenants` → *permission denied*; `ground_truth_events` has no RLS policy. Real, open finding |
| **3** Secret / network posture | secrets external, network isolated | chart `external-secret.yaml`, `networkpolicy.yaml`, `secret.yaml`, KEK; `--strict` chart-lint proves every `secretKeyRef` is emitted | ✅ present + lint-proven |
| **3** External assurance | 3rd-party pen test | — | ⏳ needs auditor |
| **4** Scale (admission is a mutex) | multi-replica can't double-crawl | Helm `verdict.validateScaling` FAILS a render that scales out without redis admission + advisory-lock leader; real-Lua shared-mutex test wired in `qec-ci.yml` (QEC_TEST_REDIS_URL) | 🔧 **render-guard proven locally** (real `helm template`); the real-Lua CI test is wired but the qec-ci suite is not yet green |
| **4** Scale + isolation under load | stays up + fair at target load | `cd.yml` → `load-test-gate` (smoke + 100-user) on the staging deploy | ⏳ needs the pipeline to run |
| **0** platform/api under test | the 300+ oracle/auditor tests run | `platform-api-tests` job (per-file isolation); found + surfaced 7 real regressions from the efd0269 VM-sync (`docs/FINDINGS_PLATFORM_API_REGRESSIONS_2026-07-21.md`) | ✅ suite green **locally** (per-file); ci.yml job runs once workflows are consolidated to the repo root; ⚠️ **7 frozen-factory regressions await founder sign-off** (boot one now fixed) |
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
