# Summit Life eval — deployment hardening & handover notes (2026-07-21)

Live fixes applied during the Summit Life proof-of-behavior demonstration, and exactly
how to fold each into the official build so **nothing stays hand-applied**.

Status legend: **IN SOURCE** = already committed, deployed image just stale ·
**DEPLOY-CONFIG** = needs a standing config entry · **TEST-HARNESS** = eval-only infra ·
**CODE FOLLOW-UP** = a real code change still to be made.

---

## 1. SDK `PageVisitRow.displayed_values` column — **IN SOURCE**

**Symptom fixed:** the value-oracle grounded a premium (`$84.32`) but it never reached
`page_visits` — every displayed value was silently dropped on insert.

**Root cause:** repo↔VM divergence. The deployed `nexus_sdk` image predated the
`displayed_values` / `network_calls` columns on the `PageVisitRow` ORM model, so the
bulk `insert(PageVisitRow, ...)` dropped those keys (the DB column and the source model
both already have them).

**Source of truth already correct:**
`sdk/nexus-sdk/nexus_sdk/db/models.py:2486` — `displayed_values: Mapped[list] = mapped_column(JSON, ...)`.

**Hand-applied (temporary):** `patch_sdk.py` inserted the two columns into the running
`nexus-qe-central` container's site-packages model.

**Fold-in (permanent):** rebuild the qe-central image from current source so the running
model matches. On the VM:
```bash
cd /home/srika/nexus-src/Nexus_power
docker compose build qe-central        # picks up the current SDK
docker compose up -d --force-recreate qe-central
# verify:
docker exec nexus-qe-central python3 -c \
  "from nexus_sdk.db.models import PageVisitRow; \
   print('displayed_values' in [c.name for c in PageVisitRow.__table__.columns])"   # -> True
```
After this, the in-container hotfix is redundant and can be dropped.

---

## 2. Runner accepts owned self-signed TLS — **DEPLOY-CONFIG**

**Symptom fixed:** the factory bakes `https://` into compiled tests; the owned eval app
served plain http → `ERR_CONNECTION_REFUSED`. Serving https (self-signed) then tripped
the runner's cert check (`ERR_CERT_AUTHORITY_INVALID`).

**Fix (no frozen-factory change):** the factory's generated `playwright.config` already
reads Chromium launch args from `NEXUS_LAUNCH_ARGS`
(`platform/api .../script_factory/compiler.py:1523`). Adding `--ignore-certificate-errors`
there makes the runner's Chromium accept the cert. Applied via a compose override that
recreated `nexus-runner`:
```yaml
# docker-compose.runner override
services:
  nexus-runner:
    environment:
      NEXUS_LAUNCH_ARGS: "--ignore-certificate-errors --no-sandbox --disable-dev-shm-usage"
```

**Fold-in (permanent):** add that `environment` entry to
`docker-compose.runner.yml` directly (currently only sets `RUNNER_TOKEN`).

> **Production-correct refinement (do NOT ship the global flag to customers):**
> disabling cert verification globally weakens MITM protection for *real* customer apps
> (which have valid certs and never need it). The right design is **per-app opt-in** — a
> `fences.allow_insecure_tls` flag consulted by the run-env builder, so only flagged
> disposable eval targets skip verification. That builder lives in the frozen factory, so
> it's a sign-off-gated follow-up. The global flag is acceptable **for this eval VM only**.

---

## 3. TLS terminator for the eval app — **TEST-HARNESS**

`summitlife-app` is an nginx TLS terminator (self-signed CN=summitlife-app, listens 80+443)
in front of the Next.js backend `summitlife-next`. Eval-only; not part of the product.
Recreate with `scratchpad/tls_terminator.sh`. Both containers run on
`nexus_power_qec-egress` (+ `nexus_power_nexus`); the host must be in the squid allowlist
(qe-central repopulates it per-crawl from `fences.allowed_hosts`).

---

## Open code follow-ups

- **per-flow failure pinpointing — FIXED IN SOURCE + VALIDATED LIVE (2026-07-21).**
  Root cause was NOT missing per-scenario data — the frozen factory's
  `GET /api/v1/test-factory/{artifact_id}/runs/{run_id}` already returns a full
  per-scenario timeline. The bug: `ingest_run` mints a fresh PK `run_id` and stores the
  runner's `NEXUS_RUN_ID` under `ci_run_id`, but `_correlate_run` fetched the timeline by
  the runner id (keyed on PK) → always missed → empty `scenarios` → `_failed_scenarios`
  fell back to flagging *every* selected flow.
  **Fix (qe-central only, no frozen edit):** added `CycleClient.list_runs`
  (`GET …/runs?limit=N`) and made `_correlate_run` resolve `ci_run_id → PK` before
  fetching the timeline (`driver.py`). Also fixes run metering (was under-counting as
  `unmetered_run` for the same reason).
  **Validated:** with one route broken, a cycle now reports `failed=1/9` →
  **GENUINE_REGRESSION ×1, PASS_UNCHANGED ×8** (was ×9/×0).
  **Status:** in the repo source; applied to the running `nexus-qe-central` as a hotfix
  (`scratchpad/patch_driver.py`, backup `.bak`) for validation. **Fold-in:** commit the
  source change and rebuild the qe-central image (same rebuild as §1) so the hotfix is
  redundant.
- **value-oracle auto-catch of a price drift — SCOPED (machinery exists; not frozen-blocked).**
  The whole chain is already wired: `script_factory/compiler.py:1229-1232` compiles a case's
  `value_assertions` via `test_factory/value_oracle.py:value_assertion_lines` into a HARD
  `await __nxNum(page.locator('<source_hint>'), <expected>, <tol>)`; `test_factory/service.py:328`
  attaches the confirmed `answer_key.outcomes` to the case; the factory `generate` endpoint
  accepts `answer_key`. **The real gap is structural, not code-frozen:** outcomes attach to
  **case 0 — "the deepest happy path that reaches the OUTPUT page"** (`service.py:326-330`).
  Summit Life's auto-generated tests are simple *navigation* journeys that LEAVE the page
  where `$84.32` renders, so there is no flow that lands-and-stays on the value's page for the
  assertion to run against. The value-oracle model targets a **form→result** flow (fill a
  quote → submit → a results page shows the premium), which our nav-journey crawl didn't
  produce (Phase-B submit didn't yield a coherent value-reaching journey).
  **To demonstrate/ship:** drive a coherent quote-submit flow so a generated case ENDS on the
  results page, ground the premium **on that dynamic results page** (`export const dynamic`
  already set on `/` ; `/quote?submitted=1` is dynamic), confirm that outcome, regenerate —
  then a `BREAK_PREMIUM` rate change is auto-caught as a hard `__nxNum` failure. Mostly
  non-frozen (crawl coverage + confirm), no frozen compiler edit required. Verify qe-central
  passes the app `answer_key` on the cycle `generate` call.
- **~~submit-path value capture~~ — RESOLVED (verified 2026-07-21).** Both the source and
  the *deployed* `emit.PageStateRecord` already carry `displayed_values`/`network_calls`
  (instantiation with the kwarg succeeds in-container). The earlier `TypeError` log line
  was stale/unrelated; not an active bug.

---

## Verified demonstration (for the record)

- Healthy app → cycle **GREEN** (9/9 pass, `PASS_UNCHANGED`).
- Broke a route → cycle **RED**, flagged **GENUINE_REGRESSION · needs_review** (no
  green-wash, no false heal). Break is surgical at the step level; reporting is batch-level.
- Restored → **GREEN** again.
- Value oracle: `$84.32` premium grounded → persisted → confirmed as a proven oracle
  (numeric, ±0.01).
