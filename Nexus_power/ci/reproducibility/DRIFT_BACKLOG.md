# Unreachable / latent-code backlog — VKPower Verdict services

> **Generated** by `ci/reproducibility/reachability_gate.py`. A *snapshot*, not a
> maintained allow-list — regenerate anytime. Silencing an entry here changes nothing;
> the gate reads the live tree.

**Total: 60 modules across 4 service(s) are reachable from no running entrypoint; 2 service(s) are clean.**

> **Measured 2026-07-21:** these are **not** git-vs-deployed drift. A file-hash diff of
> git `platform/api` against the running VM was 162/164 identical — these modules are
> **latent/dead in *both* git and the running system** (present, some tested, wired
> into nothing). This is cleanup debt, not a "what runs differs from git" problem.

Resolve each by exactly one of:

1. **Wire it** into the service at its real integration point, if it is a real feature
   meant to be active (this *turns a feature on* — a behavior change that needs the
   module owner and a test run, not a blind edit), or
2. **Remove it** from git if it is superseded / never activated.

Do not add an ignore/baseline entry to make the gate pass — that hides the debt.

## `platform/api` — 50 orphan(s)  ·  109/159 reachable from `main`

- [ ] `app.routers.tenants`
- [ ] `app.services.agentic`
- [ ] `app.services.agentic.agentic_prefs`
- [ ] `app.services.agentic.auto_diagnosis`
- [ ] `app.services.agentic.governor`
- [ ] `app.services.agentic.live_options`
- [ ] `app.services.agentic.requirement_oracle`
- [ ] `app.services.agentic.semantic_diagnosis`
- [ ] `app.services.agentic.triage`
- [ ] `app.services.diff_and_heal.action_resolver`
- [ ] `app.services.diff_and_heal.control_ledger`
- [ ] `app.services.diff_and_heal.false_heal_benchmark`
- [ ] `app.services.diff_and_heal.heal_calibration`
- [ ] `app.services.diff_and_heal.heal_capture_store`
- [ ] `app.services.diff_and_heal.heal_learning`
- [ ] `app.services.diff_and_heal.heal_policy`
- [ ] `app.services.diff_and_heal.heal_slo`
- [ ] `app.services.diff_and_heal.journey_graph`
- [ ] `app.services.diff_and_heal.self_heal`
- [ ] `app.services.diff_and_heal.visual_locate`
- [ ] `app.services.env_parity`
- [ ] `app.services.flywheel.aggregator`
- [ ] `app.services.flywheel.featurize`
- [ ] `app.services.oracle_scorecard`
- [ ] `app.services.script_factory.triage`
- [ ] `app.services.script_factory.versions`
- [ ] `app.services.script_factory.wait_scope_resolver`
- [ ] `app.services.test_factory.after_extractor`
- [ ] `app.services.test_factory.agentic_heal`
- [ ] `app.services.test_factory.anchor_extractor`
- [ ] `app.services.test_factory.assistant`
- [ ] `app.services.test_factory.defect_report`
- [ ] `app.services.test_factory.delivery`
- [ ] `app.services.test_factory.delivery.connectors`
- [ ] `app.services.test_factory.delivery.exporters`
- [ ] `app.services.test_factory.enrich_extractor`
- [ ] `app.services.test_factory.fidelity`
- [ ] `app.services.test_factory.heal_scheduler`
- [ ] `app.services.test_factory.induced_drift_benchmark`
- [ ] `app.services.test_factory.network_oracle`
- [ ] `app.services.test_factory.options_extractor`
- [ ] `app.services.test_factory.perceptual_diff`
- [ ] `app.services.test_factory.playwright_auditor`
- [ ] `app.services.test_factory.proposer`
- [ ] `app.services.test_factory.provenance`
- [ ] `app.services.test_factory.recording_quality`
- [ ] `app.services.test_factory.redaction`
- [ ] `app.services.test_factory.run_screenshots`
- [ ] `app.services.test_factory.runner_jobs`
- [ ] `app.services.test_factory.semantic_oracle`

## `platform/qe-central` — 6 orphan(s)  ·  94/100 reachable from `app.main`

- [ ] `app.controlplane.scheduling.crawl_queue`
- [ ] `app.services.certificate`
- [ ] `app.services.data_library`
- [ ] `app.services.funnel_classifier`
- [ ] `app.services.reuse_coverage`
- [ ] `app.services.signing`

## `engines/repo-intel` — 3 orphan(s)  ·  28/31 reachable from `main`

- [ ] `app.db`
- [ ] `app.lens`
- [ ] `app.lens.llm_lens`

## `platform/auth-service` — 1 orphan(s)  ·  7/8 reachable from `main`

- [ ] `app.brute_force`

## `engines/qe-explorer` — 0 orphan(s)  ·  15/15 reachable from `app.main`

Clean — every shipped module is wired into the running app.

## `platform/gateway` — 0 orphan(s)  ·  7/7 reachable from `main`

Clean — every shipped module is wired into the running app.
