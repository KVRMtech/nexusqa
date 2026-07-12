# CODE CONNECTION — Production Build Plan (evidence-based, phase-by-phase)

**Scope:** everything required to take the "Code" path (connect a client's source
repository → trigger + ground regression) from its *actual* current state to a
production-ready, enterprise-grade, 1000+‑clients/day capability.

**This is the RE-VERIFIED plan (2026-07-12).** It supersedes two earlier drafts
that were wrong in opposite directions — the first understated what exists, the
second overstated readiness. This version is grounded in a fresh, independent
code + runtime audit and prices the *real* blockers.

**Status legend:** `[✅ verified]` `[⚠️ built-but-not-running / partial]` `[❌ missing]` `[🔒 security-gate]`

---

## GROUND TRUTH — what the audit actually found

The critical realization: **the pieces exist in code, but the feature is
NON-FUNCTIONAL end-to-end in production.** "In a file" ≠ "runs."

| Component | Status | Evidence |
|---|---|---|
| repo-intel engine **deployed/running** | `[❌ runtime]` | box `docker ps -a` → **NO repo-intel container**; gated behind `repo-intel` compose profile |
| repo-intel DB schema | `[✅]` | box: `repo_connections, app_model_universes, app_model_atoms, crawl_seed_manifests` all present |
| Encrypted-token connection store | `[✅ code]` | `engines/repo-intel/app/model/secrets.py::seal_token` (refuses `local` KEK outside dev), `model/store.py::create_connection` (RLS) |
| Analyze pipeline (clone→atoms→seed) | `[✅ code / ❌ never run]` | `engines/repo-intel/app/main.py::_run_analyze` real; but engine not deployed |
| **qe-central → repo-intel client** | `[❌ absent]` | grep for any call to `/connections`,`/analyze`,`/diff` from `platform/qe-central` → **zero hits** |
| **Cycle daemon** (webhook consumer) | `[⚠️ built, DISABLED]` | box logs `qec.cycle_daemon.disabled`; `QEC_DAEMON_LEADER_ELECTION=none` |
| Webhook → `change_events` | `[✅]` | `webhooks.py` records events, idempotent `UNIQUE(app_id,dedupe_key)`; **GitLab-only** (no GitHub) |
| SHA extraction → `repo_shas` | `[❌]` | discovery `SELECT event_id,tenant_id,app_id,source,created_at` OMITS `payload` (`driver.py:1767`); `repo_shas` default `(None,None)` |
| `run_cycle` fetches `repo_diff` | `[❌]` | `run_cycle` passes only `repo_shas=`; `repo_diff` never passed → always `None` |
| repo-intel `/diff` producer | `[❌ honest stub]` | `main.py::diff` returns `stack_supported=false` |
| change_detector **consumption** | `[✅]` | `change_detector.py:184-197` consumes `RepoDiff`, **fails safe to full** |
| `repo_binding`/`webhook_secret` at rest | `[⚠️ PLAINTEXT]` | `apps.py:180 repo_binding=payload.repo_binding` (no encryption); `webhooks.py:75` reads plaintext |
| Git token hygiene / secret scrub / LLM lens | `[✅]` | `git.py`, `security/secret_scrub.py`, `lens/llm_lens.py` — as claimed |
| Clone egress fencing | `[❌]` | repo-intel not on the crawler's squid allowlist path |

**Two hard blockers I under-weighted before:** the engine isn't deployed (C1) and
**nothing in qe-central calls repo-intel** (C5). The daemon that would consume a
webhook is disabled (C6). So a repo webhook today records an event that **nothing
reads**, against an analysis engine that **isn't running**, with **no client** to
bridge them.

**What this changes:** P0 is NOT "encrypt one field + wire onboarding." P0 is
"stand up the runtime": deploy the engine, build the missing client, enable the
daemon, and encrypt the secret. Conversely, the P4 "moat" is *cheaper* than a
greenfield build because the store, analyze pipeline, and consumer already exist —
but it is strictly gated behind P0.

---

## PHASE 0 — Runtime foundation (the REAL blockers) 🔒

**Goal:** make the Code path *physically able to execute* — engine running, secret
safe, services connected, consumer on. Nothing downstream works until this lands.

### 0.1 Deploy repo-intel + provision its KMS envelope `[❌ runtime][🔒]`
- Bring up the `repo-intel` service (compose profile `repo-intel`) with the
  **GCP-KMS envelope** (`NEXUS_KEK_PROVIDER=gcp_kms` + `NEXUS_KEK_GCP_KEY`) — it
  `secrets.py::_kek_provider` **refuses `local` outside development** (fail-closed),
  identical to the fix platform-api needed.
- Give it its DB DSN (tenant-scoped role), the shared JWT secret, and health checks.
- **Files:** `docker-compose.qec.yml` (un-gate/enable repo-intel), `.env.production`,
  bootstrap script.
- **Accept:** `repo-intel` container healthy; `GET /health` 200; a manual
  `POST /connections` seals a token (envelope ready).
- **Effort:** M (deploy + KMS wiring + smoke test). **Dependency:** KMS key + ADC
  (already proven for qe-central/platform-api).

### 0.2 qe-central → repo-intel client + connection lifecycle `[❌ absent]`
- New typed client in `platform/qe-central/app/clients/repo_intel.py` (service JWT,
  mirrors `explorer_client.py`): `create_connection`, `analyze`, `get_diff`,
  `revoke_connection`.
- On app create/update **with a repo credential**: qe-central calls repo-intel
  `POST /connections` (token sealed there), stores the returned `connection_id` on
  `client_apps.repo_binding.connection_id`; the raw token is relayed once, **never
  persisted in qe-central**.
- On app delete/revoke: call repo-intel `DELETE /connections/{id}` (wipes workdir).
- **Files:** `clients/repo_intel.py` (new), `apps.py` (create/update/delete hooks),
  `clients/config.py` (repo-intel URL).
- **Accept:** creating a repo-bound app produces a repo-intel connection with a
  sealed token; deleting the app revokes it. Contract test pins the client shape.
- **Effort:** M–L (new client + lifecycle + tests).

### 0.3 Encrypt the webhook secret / repo credential at rest `[⚠️ PLAINTEXT][🔒]`
- The `webhook_secret` in `client_apps.repo_binding` is **plaintext today**. Two
  acceptable fixes (pick one, documented):
  - (a) envelope-encrypt the `repo_binding` secret sub-fields at rest (reuse the
    KMS envelope), decrypt in `webhooks.py::_webhook_secret`; OR
  - (b) keep only a **hash** of the webhook secret and compare a computed HMAC
    (works because GitHub uses HMAC signatures; for GitLab's plain-token model,
    (a) is required).
- The repo *clone* token never lives in qe-central (it's sealed in repo-intel via
  0.2), so this item is specifically the **webhook secret**.
- **Files:** `apps.py`, `webhooks.py`, migration if a column is added.
- **Accept:** DB shows no plaintext webhook secret; webhook still verifies;
  `test_webhook_secret_encrypted`.
- **Effort:** S–M.

### 0.4 Enable the cycle daemon + leader election `[⚠️ DISABLED]`
- The daemon that turns a `change_events` row into a cycle is **off**
  (`qec.cycle_daemon.disabled`, `LEADER=none`). A webhook is inert without it.
- Enable it; add **leader election** (advisory-lock or the existing
  `controlplane/leader.py` seam) so multiple qe-central instances don't double-fire
  at 1000/day.
- Add an ops guard: `change_events` accumulating unprocessed → alert.
- **Files:** `main.py` (lifespan daemon start), `config.py` (flags),
  `controlplane/leader.py`.
- **Accept:** a recorded `change_events` row fires exactly one cycle;
  two instances → one leader fires; unprocessed backlog alarms.
- **Effort:** M (enable + HA + observability).

**Phase 0 exit gate:** a repo-bound app can be created (sealed token in repo-intel),
a GitLab webhook fires exactly one cycle via the daemon, no plaintext secret at
rest, repo-intel healthy. *(The cycle still runs on the live-fingerprint/full
change signal — code-scoped selection is P4.)*

---

## PHASE 1 — Enterprise auth model (GitHub App / GitLab, least-privilege)

**Goal:** replace raw-PAT trust with the model enterprises require.

### 1.1 GitHub App `[❌]`
- Register a GitHub App: **Contents: read**, **Metadata: read**, webhook (push,
  pull_request). No write scopes. Store `app_id` + private key (KMS, platform-level).
- Client installs the App → persist `installation_id` per app.
- Mint **short-lived installation tokens** (≤1h) per clone; never persist.
- **Files:** `engines/repo-intel/app/connectors/github_app.py` (new, JWT→install-token),
  `apps.py` (install callback), the repo-intel connection accepts an install-id kind.
- **Accept:** clone a private repo with a freshly-minted auto-expiring token.
- **Effort:** L.

### 1.2 GitHub webhook handler `[❌ — GitLab-only today]`
- Add `POST /webhooks/github/{app_id}` verifying `X-Hub-Signature-256` (HMAC-SHA256
  over the raw body) in constant time; route `push`/`pull_request`.
- **Files:** `webhooks.py`.
- **Accept:** a GitHub push is verified + recorded; a forged signature → 401.
- **Effort:** S–M (mirrors the GitLab handler).

### 1.3 Deploy-key + GitLab + token lifecycle `[❌]`
- Read-only deploy key (single repo, SSH); GitLab project access token / deploy
  token. Rotation endpoint; invalid credential → honest `repo_status=needs_reauth`
  (cycle proceeds crawl-only, fail-open).
- **Effort:** M.

**Phase 1 exit gate:** GitHub App + GitHub webhook + deploy-key + GitLab all work
read-only with least-privilege, short-lived tokens; PAT is fallback-only.

---

## PHASE 2 — Egress fencing + ephemeral sandbox (safe clone at scale) 🔒

**Goal:** a malicious/huge repo can't touch a neighbor, reach internal services,
or leave source behind.

### 2.1 Host-fenced clone egress `[❌]`
- Route repo-intel's git egress through an allowlisted proxy (mirror the crawler's
  squid pattern): only the client's git host(s) reachable. SSRF guard on the
  resolved host (reuse `_is_safe_public_hook`: public IPs only, block metadata/internal).
- **Files:** `docker-compose.qec.yml` (repo-intel networks + egress proxy),
  `engines/repo-intel/app/config.py`.
- **Accept:** clone reaches only the git host; a private-IP target is refused + logged.
- **Effort:** M.

### 2.2 Per-connection micro-sandbox `[❌]`
- Run clone + analysis in an isolated sandbox (gVisor/Firecracker; minimum: locked
  container, fenced network, read-only rootfs, no docker socket, seccomp, CPU/mem/
  pids caps). **Disable git hooks** (`core.hooksPath=/dev/null`), reject symlink
  escapes, add file-count + depth caps (extend the existing byte cap).
- **Accept:** malicious hook / 10⁶ files / 50GB blob safely refused; sandbox
  destroyed after analysis.
- **Effort:** L.

### 2.3 Zero-retention guarantee `[❌]`
- Clone lives ONLY in the ephemeral sandbox; wiped on completion / after a TTL
  (default 10 min); never on a durable volume or backup. Emit only the scrubbed
  atoms. Publish a written data-handling statement.
- **Accept:** post-analysis, no source on any disk; only atoms in DB; a compliance
  test asserts the workdir is gone.
- **Effort:** M.

**Phase 2 exit gate:** clone egress host-allowlisted, runs in a destroyed-after
sandbox, source provably never retained.

---

## PHASE 4 — The differentiator: grounded diff → flow → proof (gated on P0)

**Goal:** turn "we connect to your repo" into "we prove your change is safe, with
evidence." Cheaper than greenfield — the atom model, analyze pipeline, and the
change-detector CONSUMPTION + fail-safe are **already built**; wire the producers.

### 4.1 Code→UI atom map (repo-intel) `[❌ — atoms exist, page map does not]`
- Atoms already carry `kind/value/quote/provenance`. ADD: per atom, the
  routes/pages/controls it governs (atom → `page_key`/`control_fp`), verbatim-grounded.
- **Files:** `engines/repo-intel/app/extract/*`, `manifest/seed.py`.
- **Effort:** L (the genuinely novel modeling work).

### 4.2 Real incremental `/diff` producer `[❌ — currently a stub]`
- Implement `POST /{app_id}/diff` for real: SHA→SHA git diff → changed files →
  remap to changed atoms → `page_keys`/`control_fps` (replaces `stack_supported=false`).
  Needs two analyzed universes (before/after) — depends on 4.1.
- **Files:** `engines/repo-intel/app/main.py`, a new `extract/diff_mapper.py`.
- **Effort:** L.

### 4.3 Wire qe-central: SHA + diff into the cycle `[❌]`
- Daemon discovery: **read `change_events.payload`** (old/new sha) — today it omits
  it (`driver.py:1767`). Pass `repo_shas` through `_fire_cycle` → `run_cycle` →
  `execute_cycle`.
- `run_cycle`: **fetch `repo_diff`** from repo-intel (via the 0.2 client) and pass
  it to `execute_cycle` — today `repo_diff` is never passed.
- The EXISTING `change_detector.py` + `selector.py` then scope the run to affected
  flows (already consume it, already fail-safe).
- **Files:** `driver.py` (discovery SELECT, `_fire_cycle`, `run_cycle`).
- **Accept:** a change to one page re-runs only the flows touching it; carry-forward
  is honest; unreachable repo-intel still fails safe to full (already holds).
- **Effort:** M (wiring; the consumer is done).

**Phase 4 exit gate:** a real diff drives a scoped, grounded cycle end-to-end on a
proving-ground repo.

---

## PHASE 3 — Scale & reliability (1000+ clients/day)

### 3.1 Webhook robustness `[⚠️ dedup DONE]`
- ✅ Idempotent dedup already built (`UNIQUE(app_id,dedupe_key)`). ❌ ADD per-app/
  per-tenant **rate limits** + push-storm **debounce** (coalesce to one queued cycle).
- **Effort:** M.

### 3.2 Clone concurrency + cache `[❌]`
- Bounded clone workers per tenant + global backpressure. **Clone cache by SHA** —
  `ls-remote` short-circuits when SHA == last analyzed (skip re-clone).
- **Effort:** M.

### 3.3 Observability + SLOs `[⚠️]`
- Metrics: clone latency/bytes/refusals, token mints, webhook accept/reject,
  sandbox lifecycle, daemon backlog. Per-connection `repo_status`/`last_sha`.
- **Effort:** M.

**Phase 3 exit gate:** load test at 1000+ connections/day (storm + cache +
concurrency) with SLOs met.

---

## PHASE 5 — Pre-merge PR gate (shift-left) `[❌]`
- GitHub Check Run / GitLab MR status: per-flow verdict + links to video/assertions,
  tied to the signed verdict ledger. Configurable gate policy.
- **Files:** `github_app.py` (Checks API), `webhooks.py` (pull_request handler).
- **Effort:** M–L. **Depends on:** P1 (App), P4 (scoped verdict).

## PHASE 6 — Value-add + compliance `[❌]`
- Surface detected secrets (the scrubber already finds them) as an opt-in report
  (masked). SOC2 / data-residency doc. On-prem/air-gap packaging (SDK-self-contained).
- **Effort:** M (mostly packaging + docs).

---

## Cross-cutting (every phase)
- **Tests:** unit (scrub, token hygiene, webhook HMAC, SSRF, selector), contract
  (encrypted-at-rest, RLS isolation, repo-intel client shape), integration
  (connection→analyze→atoms→diff→scoped cycle on a proving-ground repo), load (P3).
- **Security review gate** per phase (`🔒` blocking).
- **RLS everywhere;** new tables FORCE RLS.
- **Fail-open on intelligence, fail-closed on security:** repo-intel absence never
  blocks a crawl (already true via `change_detector` fail-safe + seed-manifest 404);
  a security check failure always refuses.
- **Never green-wash:** an unverifiable code claim is demoted, not surfaced (already
  the doctrine in `llm_lens.py`).

## Dependency chain (cannot be naively parallelized)
```
P0.1 deploy repo-intel + KMS
   └─> P0.2 qe-central↔repo-intel client ──> P0.3 encrypt secret (parallel)
          └─> P0.4 enable daemon + leader-election
                 └─> P1 GitHub App/webhook ──> P4.1 atom→page map
                                                  └─> P4.2 real /diff ──> P4.3 cycle wiring
   └─> P2 egress fence + sandbox (parallel with P1, before real client repos)
P3 scale · P5 PR gate (needs P1+P4) · P6 compliance (needs P2)
```

## Effort reality (corrected)
- **P0 is the big one** — deploy a new service + KMS, build a new inter-service
  client + lifecycle, enable/HA a distributed daemon, encrypt a secret. Multi-week,
  NOT a quick wiring job (the earlier draft's mistake).
- **P4 is cheaper than it looks** — the store, analyze pipeline, consumer, and
  fail-safe exist; 4.3 is wiring. But it is **strictly gated behind P0** and behind
  the genuinely-novel 4.1 (atom→page modeling).
- **P1/P2** are standard-but-real enterprise integration + isolation work.

## Definition of done (production-ready, no gaps)
A client connects a **private** repo via a **GitHub App** (least-privilege,
short-lived), the webhook secret is **encrypted at rest**, the clone is
**host-fenced** in an **ephemeral zero-retention sandbox**, the **deployed**
repo-intel engine analyzes it, the **enabled, leader-elected daemon** turns a push
into exactly one cycle, a **real code diff scopes the run to affected flows** and
posts a **grounded, evidence-backed PR verdict** tied to a **signed ledger** — at
**1000+/day** with storm/cache/concurrency proven — with a **compliance doc** a
security team signs off.
