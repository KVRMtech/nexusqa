# Playwright-tab enhancements — deploy runbook

Implements the ranked critical-enhancement set for Test Studio → Playwright tab.
All changes are **additive**; the frozen canonical pipeline is untouched.

## What shipped (per enhancement)

| # | Enhancement | Backend | Frontend | Migration |
|---|---|---|---|---|
| 1 | Per-step ACTUAL screenshot (baseline-vs-actual) | `run_screenshots.py` (ORM+store/fetch), upload+serve endpoints in `test_runs_feedback.py`, reporter capture in `compiler.py` `_NEXUS_REPORTER_TS`, `auth.py` `?token=` fallback | `api.getRunScreenshotUrl`, `StepTimeline.tsx`, `TriagePanel.tsx` | **YES** `apply_run_screenshots.sql` |
| 2 | Real-regression oracle (`outcome_contradicted`) | `test_runs.py` `outcome_contradicted_from_error` wired into timeline + `triage.py` + `self_heal.py` (refuse-to-heal `REAL_REGRESSION` branch) | `StepTimeline.tsx` REAL_REGRESSION chip | no |
| 3 | Durable run registry + verify-by-run_id | `runner_jobs.py` (ORM+persist/get), `test_runs.py` `find_run_by_ci_run_id`, `_poll_heal` correlates by run_id, register/persist at create+terminal, status-endpoint DB fallback | — | **YES** `apply_runner_jobs.sql` |
| 4 | Human approval gate for auto-healed versions | `versions.py` (`proposed` flag in data_json, `get_active_version`/`active_versions_for_artifact` skip pending, `approve_version`), `_poll_heal` saves proposed, approve endpoint | `api.approveScriptVersion`, `StepTimeline.tsx` Approve button | no (uses data_json) |
| 5 | Numeric/symbol/date value oracle | `compiler.py` `_value_oracle` (text/date/select branches) | — | no |
| 6 | Assertion QUALITY (not count) fidelity | `fidelity.py` strong/weak split | — | no |
| 7 | Generalized anchor scoping | `compiler.py` `_anchor_scope` + `generator.py` threads `anchor_kind` | — | no |
| 8 | Per-run timeline by run_id + runs list | `test_runs.py` `build_run_timeline_by_id`/`recent_runs`, two router endpoints | `api.getRunTimeline`/`api.listRuns` (UI run-picker = follow-on) | no |
| 14 | Per-test timeout in generated config | `compiler.py` `timeout: 60_000` + `expect.timeout` | — | no |
| 15 | Remove dead Sauce button | — | `PlaywrightExecutionPanel.tsx` | no |

Remaining (next batch, see report): #9 full date-picker interaction, #10 cancel/abort,
#11 panel role-gating, #12 trace/video surfacing, #13 reporter run-scoped-token hardening.

## Deploy order (REQUIRES per-action authorization for the prod DB + deploy)

1. **Apply the 2 migrations FIRST** (additive, idempotent, RLS — mirror `apply_script_versions.sql`):
   ```
   psql "$DATABASE_URL" -f scripts/apply_run_screenshots.sql
   psql "$DATABASE_URL" -f scripts/apply_runner_jobs.sql
   ```
   Both degrade safely if NOT yet applied (screenshot upload → 503 → reporter skips
   → `screenshot_url` empty → UI shows "awaiting capture" as today; runner_jobs →
   in-memory path unaffected). So code can deploy before the migration without breaking
   — the features just stay dormant until the tables exist.

2. **Deploy platform-api code** via the established docker-cp lineage (NOT rebuild):
   docker cp the 12 changed `app/**` files into the running `nexus-platform-api`
   (`/app/service/app/...`) + the 2 new modules (`run_screenshots.py`, `runner_jobs.py`),
   then restart the container. Verify with a grep of a deployed marker
   (e.g. `_value_oracle`, `outcome_contradicted_from_error`, `_anchor_scope`).

3. **Rebuild the client** on the VM (`docker compose build client`) or via PowerShell —
   NEVER through git-bash (VITE_API_BASE mangling). Changed: `api.ts`, `StepTimeline.tsx`,
   `TriagePanel.tsx`, `PlaywrightExecutionPanel.tsx`.

4. **Reporter**: the new `nexus-reporter.ts` is emitted by the compiler, so it takes
   effect for any newly generated/downloaded bundle and for server-side runs (compiled
   fresh each run). Existing downloaded bundles keep the old reporter until re-downloaded.

## Behavior notes
- Compiler output changed (#5/#7/#14) → existing saved ScriptVersions will read as
  "drifted" in fidelity until regenerated. Expected (it's the improvement).
- Auto-healed/TrueFix versions are now **proposed** (not active) until approved — a
  deliberate behavior change (the P0 human-gate). Human Save/Restore are unchanged
  (active immediately).
