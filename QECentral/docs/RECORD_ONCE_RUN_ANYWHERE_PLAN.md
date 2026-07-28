# Record Once, Reuse Fleet-Wide, Run Anywhere — Production Implementation Plan

**Status:** Finalized & founder-aligned 2026-07-27. Decisions locked. Awaiting go-ahead to start Phase 0.
**Scale target:** 100+ clients (tenants) · 1000+ apps.
**Posture:** EXTEND & HARDEN the existing P0–R6 engine — not a rebuild. Only Phase 2 (backup) and Phase 5 (recipe library/reuse) are genuinely new.
**Supersedes the "Persona" framing** in `PERSONA_ENVIRONMENT_*` docs — a Persona is reframed as a **Member**.

Companion readable version: `VKPower_Build_Plan.html` (repo root + founder Desktop).

---

## 0. The one invariant

> **A RUN = Tenant · App · Environment · Member · Scenario**, executed in one fixed, recorded order:
>
> **Environment → Member → Verify-documents → Home → Scenario**

- **Order is fixed; presence of each stage is NOT.** Each stage is `required` or `optional`, and optional stages are skipped when they don't apply.
  - **Environment** — always present (can be as small as "go to URL").
  - **Member (login)** — required only if the app needs login (public flows skip it).
  - **Verify-documents** — **optional**. Absent at *app level* (app has no doc step) → not in the recipe at all; or conditional at *member level* (app sometimes shows it) → in the recipe as `if present → do, else skip`.
  - **Home** — the success checkpoint: login succeeds when the logged-in **state** is reached (state oracle), NOT when a fixed step count completes. This is why a member who signs a document and a member who skips it both count as logged-in.
  - **Scenario** — always (the test itself).
- **Three swappable dials, each recorded once in the crawl:** ① Environment ② Login ③ Scenario. The Login dial expands into the mini-sequence Member → Verify-documents → Home.

## 1. Six non-negotiables at this scale

1. **Tenant isolation is absolute** — every row tenant-scoped, FORCE RLS backstop, explicit tenant filters, per-tenant envelope key. One client's secrets never share a key with another's.
2. **Record once → reuse fleet-wide** — recipes are shared assets keyed by login-type/domain; a handful serve hundreds of apps.
3. **Everything is a parameterized recipe** — login and environment are both `steps + blanks`; members, boxes, runways are just values.
4. **Home is the truth** — login success = reaching the logged-in state, so a verify-documents branch never breaks replay.
5. **Fair, self-healing runs** — per-tenant fairness + admission mutex + orphan cleanup; no client starves another, no stuck "busy" locks.
6. **Backup & audit are foundations, not afterthoughts** — automated per-tenant backup/restore + tamper-evident evidence from day one.

## 2. Locked decisions (2026-07-27)

- **A1 — "Members", not "Personas".** Identifier is **generic**: the login blank adapts to the app's field (`{member_number}` | `{email}` | `{username}` | `{policy_no}`). Display label re-skinnable per client (Members / Customers / Users); default **Members**.
- **A2 — CIT boxes = both.** The box/runway is a `{parameter}`: save named presets for fixed boxes (786, 787…), or pass the value at launch for spun-up-per-test. One recipe, filled from a preset or at run time.
- **A3 — Verify-documents = record the steps.** Recorded in the crawl like login; replayed, skipped when the member has none. **Safety net:** a document the recording doesn't cover is **surfaced**, not auto-clicked.
- **A4 — Pooled cloud now, siloable per client.** One multi-tenant cloud + per-tenant keys by default; Phase 1 keeps tenant boundaries clean so a regulated client can be carved into a dedicated / on-prem / per-region instance **without rework**.

## 3. Reuse matching — the brain of Phase 5

**Login reuse key = a "login-type" =** `domain` + `login-page path` + `login-form fingerprint` (the fields + steps, e.g. "member# + password + Continue + PIN + Verify").
- The form fingerprint is what distinguishes **dotcom** login from **portal** login on the **same domain** — base URL alone is necessary but NOT sufficient.
- On the 2nd user/app: crawler reaches a login page → fingerprints it → checks the tenant recipe library.
  - **Match** → propose reuse: *"Portal login for usaa.com already recorded — just enter your member number."* No re-record.
  - **Ambiguous** (domain has both dotcom + portal) → ask *"dotcom or portal?"* (usually auto-detected from the page landed on).
  - **No match** → record once; it joins the library for the next user.
- **Scope:** tenant-level by default (isolation). Cross-tenant anonymized "login-type templates" are a possible future, NOT the default.

**Environment reuse = the app's saved list.** First user records RWA/RWB/RWC/CIT-template/Prod once → saved as the app's environments. Every user after **picks** from a run-time dropdown (dynamic CIT box → pick template + type the box number). No re-recording; "+ Record new environment" only for a genuinely new one.

---

## 4. The plan — Phase 0 → 8

Legend: **[Built]** exists today · **[Partial]** pieces exist, make first-class / scale-harden · **[New]** to build.

### Phase 0 — Baseline audit & lock  **[Built — audit only]**
- **Objective:** Inventory what exists so every later phase is "extend," not "rebuild."
- **Deliverables:** component ledger (keep / rename / extend / new) across data model, crawl pipeline, envelope crypto, posture, evidence; a map of Onboard→Access→Explore→Generate→Run; confirmation the existing suite runs green.
- **Where:** whole codebase (read-only audit).
- **Done when:** the ledger is written and reviewed; the current test suite passes; no un-backed-up data path remains.
- **Depends on:** — .

### Phase 1 — Multi-tenant spine + canonical run-model  **[Built → extend]**
- **Objective:** Lock the shared run-model + plain naming on clean tenant isolation.
- **Deliverables:** run-model = Tenant·App·Environment·Member·Scenario with each stage flagged required/optional; **Verify-documents as a first-class optional stage**; rename **Persona → Member** (model + API + tab) with the generic identifier field; per-tenant envelope key confirmed; tenant boundary clean enough to silo (A4).
- **Where:** data model, platform-api, qe-central, portal tab.
- **Done when:** a run record carries the 5 stages; a member with no document skips verify-docs and still reaches Home; a cross-tenant read returns 0 rows (RLS proven); the tab reads "Members".
- **Depends on:** Phase 0.

### Phase 2 — Backup, DR & tenant lifecycle  **[New — biggest gap]**
- **Objective:** Automated, tested per-tenant backup/restore + provisioning.
- **Deliverables:** scheduled encrypted per-tenant dumps (nexus + qecentral) + KEK backup, offsite; one-command restore runbook; tenant provision / de-provision / export; backup-failure alerting.
- **Where:** platform ops / a backup service + cron.
- **Done when:** a restore drill reproduces a tenant's data (cases/members, cards **decrypt**) into a scratch instance; a simulated backup failure alerts.
- **Depends on:** Phase 1 (tenant boundary). *(Moved to the front after the 2026-07 near data-loss.)*

### Phase 3 — Login recorder in the crawl (incl. Verify-documents)  **[Partial → firm up]**
- **Objective:** Record login once at Access as a parameterized recipe; record verify-documents steps; success = Home.
- **Deliverables:** "Record Login" at Access → recipe with the login field auto-blanked (detect member#/email/username); **Verify-documents recorded as its own optional step-set** (replayed or skipped); **Home-reached state oracle**; unrecorded-document safety net (surface, don't auto-click).
- **Where:** Access UI, crawler, recipe store (`tp_login_recipes`), oracle.
- **Done when:** crawl VKPower Life → recipe with `{member_number}`+`{pin}` blanks (+ verify-docs step if present); a member with a pending doc replays the doc step, a member without skips it, **both reach Home**.
- **Depends on:** Phase 1.

### Phase 4 — Environment recorder in the crawl  **[Partial → firm up]**
- **Objective:** Record environment selection as a parameterized recipe, recorded first.
- **Deliverables:** "Record Environment" at Access → URL box `{box}`, cookie/runway `{runway}`, or prod+guardrails; named presets per app + a run-time picker (with a fill field for dynamic boxes); prod carries posture guardrails.
- **Where:** Access UI, environment store (`tp_environments`), run launcher.
- **Done when:** USAA → CIT box template `{box}` + RWA/RWB cookie recipes saved; the launcher shows the picker; picking RWA sets the cookie then proceeds; `{box}=999` passed at launch works.
- **Depends on:** Phase 1.

### Phase 5 — Recipe Library + smart reuse  **[New — highest leverage]**
- **Objective:** Record once, reuse fleet-wide; propose the matching recipe instead of re-recording.
- **Deliverables:** login-type key (domain + login-page + form-fingerprint) + a per-tenant recipe library; reuse proposal at onboard/access ("already recorded — enter your member number"); dotcom-vs-portal disambiguation + auto-detect from the landed page; environment reuse via the app's saved list.
- **Where:** recipe library, onboarding/access, fingerprinting service.
- **Done when:** onboarding a 2nd app on the usaa.com **portal** proposes the existing portal recipe (no re-record); a **dotcom** app on usaa.com is NOT wrongly matched to portal; a 2nd tester on the same app just picks env + enters their identifier.
- **Depends on:** Phase 3, 4.

### Phase 6 — Members & credentials at scale  **[Partial → scale-harden]**
- **Objective:** Tenant-level members, per-tenant encryption, reservation/rotation/staleness.
- **Deliverables:** members as tenant-level entities (reusable across a client's apps) with the generic identifier; encrypted cards per tenant; **reservation** (one live session per member) + reservation cap; secret **rotation**; **staleness sweep**; bulk member import (CSV).
- **Where:** member store (rename `tp_persona_credentials` → members), reservation service, envelope.
- **Done when:** a member is reused across 2 apps; two concurrent runs cannot grab the same member session; a stale card is flagged; rotating a secret makes the old blob unusable.
- **Depends on:** Phase 1.

### Phase 7 — Run orchestration at scale  **[Partial → scale-harden]**
- **Objective:** Fair, isolated, self-healing concurrent runs.
- **Deliverables:** queue/scheduler with **per-tenant fairness** + concurrency caps; **admission mutex** (one-run-per-key); autoscaling runner pool; run lifecycle — heartbeats, timeouts, **orphan cleanup** (no stuck busy locks), retry/resume.
- **Where:** orchestrator, runner pool, admission.
- **Done when:** 50 concurrent runs across 5 tenants respect per-tenant caps + fairness; a killed run releases its lock and cleans its browser processes; no orphaned chromium after a timeout.
- **Depends on:** Phase 1, 6.

### Phase 8 — Governance, evidence & operate  **[Partial → complete]**
- **Objective:** Enterprise-grade audit / evidence / observability for regulated clients.
- **Deliverables:** per-tenant RBAC, audit trail, approval workflows, PII-egress controls; posture governance (prod default-deny / no-submit) per environment; tamper-evident **Certificate-of-Execution** per run; observability — health, SLOs, per-tenant cost attribution, surface toggles.
- **Where:** all services + a governance/observability layer.
- **Done when:** a prod run is fenced (no submit); every run yields a verifiable certificate; a tampered evidence bit fails verification; an audit query shows who-ran-what per tenant.
- **Depends on:** all prior.

---

## 5. Where each phase lands in the crawl

```
ONBOARD ─▶ ACCESS ★ ─▶ EXPLORE ─▶ GENERATE ─▶ RUN
             │            │
   P3 Record Login (blanks + verify-docs)   P3/P6 branch discovery
   P4 Record Environment (box/runway)       (unchanged downstream)
   P5 Reuse proposal ("already recorded")
```
Almost all new capture slots into the **Access** step. Explore discovers branches. Generate → Run are untouched — current functionality is not disturbed.

## 6. Sequencing note

Phases 0–2 (foundation + backup) first; 3–4 (recorders) next; 5 (reuse library) once recorders exist; 6–8 (scale/ops/governance) harden for the fleet. Phases can overlap where dependencies allow (e.g., 3 and 4 in parallel; 6 alongside 5).
