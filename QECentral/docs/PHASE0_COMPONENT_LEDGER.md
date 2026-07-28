# Phase 0 — Baseline Component Ledger

**Purpose:** inventory what exists so every later phase is *extend*, not *rebuild*, and so nothing production is broken by a rename/refactor. Built from a parallel read-only audit of the repo (2026-07-27). Verdicts: **[READY]** production-ready · **[PARTIAL]** exists, needs hardening/activation · **[NEW]** to build · **[FROZEN]** do not touch.

Legend for the action column: **KEEP** as-is · **EXTEND** additively · **RENAME** · **ACTIVATE** (exists, wire it on) · **BUILD** (new).

---

## ⚠️ Critical scoping finding — there are TWO "persona" concepts

`"persona"` = **2016 occurrences across 109 files**, but they are two unrelated concepts:

| Concept | Where | Action |
|---|---|---|
| **QEC/Verdict member-persona** (`tp_persona_*`, `PersonaMatrixPanel.tsx`, `persona_store.py`, qec persona API) | `platform/api`, `verdict-portal`, `qe-central` | **RENAME → Member** (the founder's ask) |
| **Canonical/Video "Process-Oracle persona"** (`PersonaGalleryPage`, `personaStore.ts`, `PersonaWorkspacePage`) | `Nexus_power/client` (the FROZEN video product) | **[FROZEN] DO NOT TOUCH** — different concept |

A blind global rename would break the frozen video product. The rename is scoped to the QEC concept ONLY. (Exact file list pending the persona-surface audit.)

---

## Track A & D substrate — audited (Agent 4)

### Run orchestration — **[READY]** with two narrow gaps
- **KEEP** `qe-central/app/controlplane/scheduling/admission.py` — real `AdmissionController`: global cap + per-tenant cap + per-`canonical_host` token bucket + per-host mutex; structural fairness (`max_per_tenant < max_global`); fail-closed (`host_unconfigured`/`rate_unconfigured`); buckets start empty (no restart burst). The P2 "admission-is-a-mutex" is literally implemented.
- **KEEP** `.../scheduling/distributed.py` — `RedisAdmissionBackend`, one atomic Lua `ADMIT_LUA` on Redis server clock; leases self-heal via TTL; store outage → deny/wait, never fail-open. `QEC_ADMISSION_BACKEND=memory|redis`.
- **KEEP** `.../controlplane/leader.py` — Postgres `pg_try_advisory_lock` leader election; wired in `main.py` (`run_as_leader(cycle_driver_daemon…)` + a second reaper loop). ⚠️ **FIX** the latent un-awaited `execution_options` bug at `leader.py` L142-150 (also noted in memory).
- **KEEP** driver admission usage (`controlplane/cycle/driver.py`: `_acquire_admission`, bounded wait `QEC_ADMISSION_MAX_WAIT_SECONDS`, release in finally, fail-closed → FAILED).
- **GAP → BUILD (narrow Phase 7):** the **runner tier** is the least-scaled — `infrastructure/runner/server.js` is a **single container, one-run-at-a-time process `busy` mutex, no runner pool, no cross-runner queue, and no orphan-child reaper beyond the per-run `HARD_TIMEOUT`**. This is the exact cause of the orphaned-chromium "busy" lock we hit live. Real work = a runner pool/queue + orphan-child reaping.
- **GAP → ACTIVATE:** the stale-exploration reaper (`controlplane/reaper.py::reap_stale_explorations`, RLS-safe, race-guarded) ships **default-OFF** (`QEC_REAPER_TICK_SECONDS` unset even in the prod bootstrap). Turn it on + verify.

### Multi-tenancy + per-tenant crypto — **[READY]**
- **KEEP** RLS: `alembic/versions/010_row_level_security.py` (nexus) + `qec_001_initial.py` (every ~21 qecentral tables) — `ENABLE`+`FORCE ROW LEVEL SECURITY` + `tenant_isolation USING (tenant_id = current_setting('nexus.current_tenant_id', true))`, `FORCE RLS` across 48 files. GUC set in `qe-central/app/db/__init__.py` session helpers; tested (`test_rls_isolation.py`, `test_rls_coverage_complete.py`). App-level explicit `tenant_id ==` filters are primary, RLS is the net.
- **KEEP** envelope: `sdk/nexus-sdk/nexus_sdk/security/envelope.py` — **KEK is per-tenant** (`wrap(tenant_id, dek)`; Local derives a deterministic per-tenant key from the master key), **DEK random per ciphertext**, `tenant_id` bound as AAD (KMS) — the correct envelope pattern → per-tenant crypto isolation ALREADY EXISTS. `LocalKekProvider` refuses outside dev/test; boot validator refuses `local` in staging/prod.
- **NOTE (deployment):** the live box runs `NEXUS_ENV=development` + local KEK (dev posture). Production/regulated tenants → `gcp_kms`/`aws_kms` (providers already exist: `GcpKmsProvider`, `AwsKmsProvider`).
- **Phase 1 impact:** "confirm per-tenant key + clean tenant boundary" is largely DONE — Phase 1 is mostly the Member rename + Verify-documents stage, not tenancy plumbing.

### Backup / DR — **[PARTIAL→SUBSTANTIAL]** (plan assumption REFUTED)
The plan called this "New — biggest gap." **That is wrong.** Honest evidence of an existing, tested backup+restore substrate:
- **KEEP** `scripts/verdict_pg_backup.sh` — `pg_dump -Fc` of BOTH `nexus`+`qecentral`; local + optional GCS offsite (`GCS_BACKUP_BUCKET`); retention 14; refuses too-small dumps; **`--restore-drill`** restores into a throwaway DB and PROVES recovery (alembic head matches AND no source-non-empty table is empty after restore); node_exporter staleness metrics.
- **KEEP** `scripts/dr_drill.sh` — K8s DR rehearsal with RTO measurement; `qec_ci_dr_seed.sh` seeds probe rows so the drill proves DATA recovery.
- **KEEP** cron: `scripts/verdict_box_bootstrap.sh` installs the backup script + crontab `17 3 * * *`; `.env.production` refuses to bootstrap without `GCS_BACKUP_BUCKET`.
- **KEEP** `infrastructure/helm/.../postgres-cnpg.yaml` — CloudNativePG 3-instance sync cluster + WAL archiving to S3 (30d) + `ScheduledBackup` (needs the CNPG operator, not installed by the chart).
- **GAP → ACTIVATE/VERIFY (revised Phase 2):** (a) confirm the cron is actually live on the CURRENT box — this session's backups were manual, so the automated path may not be wired on the running VM; (b) `docs/DR_RUNBOOK.md` has placeholder RTO/RPO values — fill measured targets; (c) CNPG PITR needs the operator. So Phase 2 shrinks from "build backup" to "activate + verify + document backup."

### Governance / evidence — **[READY]**
- **KEEP** posture: `persona_governance.py::gate_dispatch` ENFORCES refusal (prod default-deny for mutating runs, `no_submit` floor on prod, 0 scripts on refusal); `qe-central/app/security/prod_guard.py::assert_crawlable` fail-closed onboarding (prod never an attested crawl target; dev bypass never honored in staging/prod).
- **KEEP** certification ledger (`tp_certification_ledger`, RLS'd) + `persona_store.py::record_certification`.
- **KEEP** Certificate-of-Execution: `qe-central/app/services/certificate.py` — ed25519-signed, offline-verifiable, tamper-detecting (`verify_certificate` re-derives digest + re-derives `certified` + verifies signature). Hash-chained evidence: `verdict_events.py::VerdictEventRow.chain_hash = sha256(prev + payload)`, immutable; `DecisionDossierRow`, governed `WaiverRow`.
- **KEEP** RBAC/audit: immutable `AuditLogRow`; role-scoped service JWTs; `qec_approval_events`; `QEC_AUTH_PROVIDER=jwt|oidc|saml` fail-closed; JWT audience isolation `QEC_REQUIRE_AUD`.
- **Phase 8 impact:** most of Phase 8 is DONE. Remaining is enterprise polish (per-tenant cost attribution, filling audit coverage gaps), not core build.

---

## Pending audits (other three agents in flight)
- **Persona rename surface** — exact file/symbol/table/column list scoped to the QEC concept; where the login identifier is modeled (generic vs hardcoded `member_number`).
- **Crawl pipeline + login recipe** — the Onboard→Access→Explore→Generate flow; `tp_login_recipes` format; the home-reached oracle; existing optional/branch handling.
- **Environment model + credential store** — `tp_environments`/`app_environments`, env_resolver, cookie/header injection; `tp_persona_credentials` schema; reservation/rotation/staleness.

---

## Plan impact (honest, so far)
The heavy **scale infrastructure already exists and is tested** — admission/fairness, RLS, per-tenant envelope crypto, backup+restore-drills, posture governance, signed tamper-evident certificates. This **materially de-risks and shrinks** Tracks A and D:
- **Phase 2 (backup):** re-scoped from "build" → **activate + verify + document**.
- **Phase 7 (orchestration):** mostly ready; real work narrows to the **runner-pool/queue + orphan reaping** and **turning the reaper on**.
- **Phase 8 (governance/evidence):** mostly ready; remaining is polish.
- **Phase 1 (tenancy/crypto):** already ready; Phase 1 becomes the **Member rename + Verify-documents stage**.

⇒ The genuine build effort concentrates where the plan said the leverage is: the **record-once capture UX** (Phase 3/4), the **recipe library + reuse** (Phase 5, genuinely new), and the **Member rename + Verify-documents** model (Phase 1) — plus the narrow orchestration/backup gaps above. This is a smaller, sharper surface than the raw plan implied — good news, honestly arrived at.

---

## Phase 0 completion status + execution feasibility (autonomous session 2026-07-27)

**DONE + VERIFIED this session** (branch `feat/record-once-run-anywhere`, commits `8625e98`, `8e50922`):
- Phase 0 audit + this ledger (read-only) — complete.
- **Phase 1/3 CORE — verify-documents + Home-reached oracle, both sides, additive & backward-compatible:**
  - *Interpreter (replay):* `compiler.py::_AUTH_SETUP_TS` now supports **optional steps** (a verify-documents interstitial some members hit / others skip — skipped when absent, short timeout, not a drift-abort) + an **`assert_home` oracle** (success = logged-in state reached, not step count) + **surface-don't-fabricate** (an unrecorded interstitial that blocks Home fails honestly, no session). Test `tests/test_verify_documents_oracle.py`.
  - *Builder (produce):* `persona_store.build_login_recipe(cfg, verify_documents=, home=)` + `_assert_home_step` (refuses a signal-less oracle) + `_verify_document_steps` (marks interstitial steps optional). Test `tests/test_verify_documents_recipe.py`.
  - *End-to-end data path CONFIRMED:* `save_recipe` stores `steps` JSONB verbatim; `build_persona_bundle` (persona_store.py:1017) passes `recipe.steps` verbatim into `auth_config` → the new `assert_home`/`optional` steps flow untouched from builder → store → run → interpreter. Both ends unit-tested, middle traced.
  - **35 local tests green** (new + existing compiler/recipe/auth, no regression).
  - *Remaining for this capability (NOT locally verifiable — needs crawler/CI/VM):* the crawl RECORDER that calls `build_login_recipe` with the login/interstitial/landing it observes during a crawl, and the API/onboarding wiring. That is Phase 3 proper.
- **Phase 5 CORE — login-type fingerprint (the reuse-matching brain), verified (commit `6c4f086`):** `login_fingerprint.py` — `login_type_key` / `login_type_descriptor` / `login_form_signature`, pure/deterministic. Key = `domain + login-page path + login-FORM fingerprint`; distinguishes dotcom vs portal on ONE host (base URL alone never causes false reuse), robust to cosmetics (case/order/whitespace/dynamic-ids). Test `tests/test_login_fingerprint.py` incl. the USAA dotcom-vs-portal case. **39 local record-once tests green total.** *Remaining (NOT locally verifiable):* the DB-backed recipe-library keyed by this fingerprint + the onboard reuse-proposal that consume it (Phase 5 wiring).

**Verification reality (this GATES the rest — no green-washing):**
- ✅ **Locally verifiable, no DB/browser:** the compiler + recipe-interpreter + pure-logic layers (`persona_diff`/`persona_scale`/`persona_governance`). Real, test-verified code is possible here now.
- ⛔ **NOT locally verifiable** (needs Postgres / CI / the live VM / an npm frontend build): member DB CRUD, the run + crawl pipeline, RLS, and ALL frontend. Per the no-stubs rule, code in these layers is NOT claimed "done" from an unattended local session — it must pass on CI or the VM first.

**The rename (Persona→Member) — do NOT blind-execute unattended:**
- Surface = 2016 refs across DB + API + frontend + 11 tests, colliding with TWO frozen concepts. Safe strategy: rename the **entity at the API / domain / UI layer only and KEEP physical table names** (`tp_personas`…) to avoid a live-DB data migration; cut the API routes + frontend over together (frontend build on the VM/PowerShell, never git-bash — known API-base mangling bug); run the DB + frontend suites on CI/VM. Needs the founder in the loop + the full test loop.

**Honest sequencing recommendation (resume here):**
1. Verified interpreter + builder + fingerprint slices — DONE.
2. **Phase 2 (backup) — DONE + VERIFIED on the live box (2026-07-28):** activated the existing `verdict_pg_backup.sh`; **restore-drill PASSED** (recovery proven — qecentral 4 rows/24 tables + nexus 1147 rows/116 tables restore to matching alembic heads, 0 empty); daily **cron installed** (root, `17 3 * * *`); **offsite GCS working** to `gs://verdict-backups-8d85a07a` (asia-southeast1, 30-day lifecycle, VM SA granted objectAdmin) — PROVEN: `nexus_*.dump` + `qecentral_*.dump` land in the bucket; local retention 14. GOTCHAS discovered: (a) the VM's OAuth scope was `devstorage.read_only` → widened to `read_write` (needs a VM stop/start); (b) after the scope change **root's gsutil cached the stale read-only token** → `rm -rf /root/.gsutil` to force a fresh metadata fetch; (c) **⚠️ this zone (asia-southeast1-a) is capacity-fragile — e2/n2/n1-standard-4 were ALL exhausted; the box only restarted on `n2d-standard-4` (AMD pool). Do NOT stop this VM unless necessary — a stop can strand it.** The box is now on **n2d-standard-4**, same static IP.
3. Phases 1 (rename, keep-tables), 3 (crawl→`save_recipe` wiring), 4 (env recorder), 5 (recipe library/fingerprint) → execute against the DB/CI/VM/frontend loops with review, phase-gated.

## Autonomous session 2 (2026-07-28) — a / b / c progress

- **(c) Member rename — frontend DONE + LIVE + VERIFIED.** Verdict-portal UI labels Persona→Member (tab "Members & Environments", panel title, "No members yet", "Member saved", "Default identity", …); internal keys / `persona_id` vars / `/personas` API calls UNCHANGED (nothing breaks); frozen video client untouched. `npm run build` green (1868 modules); **deployed to the live portal** (correct bundle `index--eD8cjvU.js` served, HAS_MEMBERS, portal 200). *(Deploy gotcha: git-bash `tar -czf C:/…` treats `C:` as a remote host → partial archive; use `gcloud scp --recurse dist/*` instead. Backup at `/usr/share/nginx/html.bak-*`.)*
- **(b) reuse brain — DONE + tested (pure).** `login_fingerprint.py`: `login_type_key` (domain+login-page+form fingerprint; dotcom≠portal on one host), `propose_reuse` (observed form → reuse/record; bare known domain → reuse-one/disambiguate). Tests `test_login_fingerprint.py`, `test_login_reuse.py`.
- **(b) recipe library DB — DONE + scratch-DB VERIFIED.** `tp_login_recipes.login_type_key` (migration `apply_login_type_key.sql`, additive+idempotent), `save_recipe(login_type_key=…)`, `find_recipes_by_login_type` (per-tenant, active-only). Scratch-DB proof: lookup returns same-tenant+key+active only.
- **NOT done (need backend deploy + live-crawl + review; left on the branch):** (b) API reuse-proposal endpoint + frontend reuse-prompt UI; (a) crawl recorder (pure extraction + `complete_crawl`→`save_recipe` wiring + qe-explorer emitting the observed login/interstitial/landing); (c) `/members` API aliases; the coordinated **backend deploy** (platform-api restart — deliberately deferred: fragile zone + one outage already this session) + the **E2E crawl of VKPower Life**. Branch `feat/record-once-run-anywhere`.
