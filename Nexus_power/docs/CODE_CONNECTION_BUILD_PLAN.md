# CODE CONNECTION — Production Build Plan (phase-by-phase, no gaps)

**Scope:** everything required to take the "Code" path (connect a client's source
repository → trigger + ground regression) from its current state to a
production-ready, enterprise-grade, 1000+‑clients/day capability — including the
security hardening, the scale spine, and the category-defining innovation
(grounded diff→flow→proof).

**Status legend:** `[✅ built]` `[⚠️ partial]` `[❌ missing]` `[🔒 security-gate]`

**Grounding — what exists today (verified in code):**
- `engines/repo-intel/app/connectors/git.py` — shallow clone, in-memory token,
  token-scrubbed logs, `credential.helper=` off, size cap, per-tenant workdir. `[✅]`
- `engines/repo-intel/app/security/secret_scrub.py` — deterministic secret/PII
  scrubber run before any quote persists. `[✅]`
- `engines/repo-intel/app/lens/llm_lens.py` — off by default, verbatim-quote-
  grounded, atoms-only (never wholesale source). `[✅]`
- `platform/qe-central/app/routers/webhooks.py` — constant-time HMAC, fail-closed,
  opaque 401. `[✅]`
- `platform/qe-central/app/routers/apps.py` — `repo_binding` JSONB stored **plaintext**;
  onboarding collects `webhook_secret` but **no repo clone credential**. `[⚠️/❌]`
- repo-intel gated behind the `repo-intel` compose profile; off the critical path,
  fail-open; clone egress **not** host-fenced. `[⚠️]`

---

## PHASE 0 — Security foundation (BLOCKER: before any private repo connects)

**Goal:** no client credential is ever stored in plaintext; onboarding can supply a
real, encrypted repo credential; the clone path is auditable.

### 0.1 Encrypt `repo_binding` secrets at rest `[🔒][❌]`
- Move `webhook_secret` and any repo token OUT of the plaintext `repo_binding`
  JSONB into an envelope-encrypted blob (reuse `_encrypt_credentials` / the KMS
  `EnvelopeService`, AAD = `app_id`).
- New column `repo_creds_blob BYTEA NULL` on `client_apps` (idempotent RLS migration).
- `_public_view` NEVER returns the secret; expose only `has_repo_credential`,
  `repo_provider`, `repo_project`.
- **Files:** `apps.py` (`_encrypt_repo_creds`), `db/models.py`, new `scripts/apply_repo_creds.sql`,
  `routers/webhooks.py` (`_webhook_secret` decrypts).
- **Accept:** DB row shows no plaintext secret; webhook still verifies; unit test
  `test_repo_creds_encrypted` asserts ciphertext at rest + round-trip.

### 0.2 Repo-credential intake in onboarding `[❌]`
- Onboarding "Code" tab gains a credential field set per provider:
  GitHub App install / deploy key / scoped PAT (fallback); GitLab project access
  token / deploy token.
- Wizard sends `repo_credential: {kind, token|deploy_key|app_installation_id}`;
  server encrypts into `repo_creds_blob`.
- **Files:** `verdict-portal/src/features/onboarding/index.tsx`, `apps.py` (`AppCreate`/`AppUpdate`),
  `types/qec.ts`.
- **Accept:** create a private-repo app end-to-end; token never echoed; `has_repo_credential=true`.

### 0.3 Audit + revocation `[⚠️]`
- Every clone/ls-remote/token-mint writes an `audit_log` row (actor=service,
  tenant, connection, sha, bytes) — secret-free.
- `DELETE /apps/{id}` and a new `POST /apps/{id}/repo/revoke` zero `repo_creds_blob`
  AND call `GitConnector.remove_workdir`.
- **Accept:** revoke wipes ciphertext + workdir; audit shows the revoke.

**Phase 0 exit gate:** no plaintext repo secret anywhere; private clone works with an
encrypted credential; revoke is complete + audited.

---

## PHASE 1 — Enterprise auth model (GitHub App / GitLab, least-privilege)

**Goal:** replace raw-PAT trust with the integration model enterprises require —
scoped, short-lived, revocable, single-repo.

### 1.1 GitHub App `[❌]`
- Register a GitHub App: **Contents: read-only**, **Metadata: read**, webhook
  subscription (push, pull_request). No write scopes.
- Store `app_id` + private key (KMS-encrypted, platform-level secret, not per-tenant).
- On connect: the client installs the App on their repo; we persist the
  `installation_id` (per app row).
- Mint **short-lived installation tokens** (≤1h) on demand for each clone; never
  persist the token.
- **Files:** new `engines/repo-intel/app/connectors/github_app.py` (JWT→installation-token),
  `apps.py` (installation callback), `webhooks.py` (App webhook signature `X-Hub-Signature-256`).
- **Accept:** clone a private repo using a freshly-minted, auto-expiring token; no
  long-lived secret at rest for GitHub App connections.

### 1.2 Deploy-key fallback + GitLab `[❌]`
- Read-only **deploy key** path (single repo, SSH) for buyers who won't install an App.
- GitLab: project access token (read_repository) / deploy token, encrypted.
- **Accept:** each provider path clones a private repo read-only.

### 1.3 Token lifecycle `[❌]`
- Rotation endpoint; expiry tracking; a connection whose credential is invalid
  surfaces `repo_status=needs_reauth` (never a silent failure).
- **Accept:** expired/invalid credential → honest `needs_reauth`, cycle proceeds
  crawl-only (fail-open), operator notified.

**Phase 1 exit gate:** GitHub App + deploy-key + GitLab all clone read-only with the
least-privilege, short-lived model; PAT is fallback-only.

---

## PHASE 2 — Egress fencing + ephemeral sandbox (safe clone at scale)

**Goal:** a malicious or huge repo cannot touch a neighbor, reach internal
services, or leave source behind.

### 2.1 Host-fenced clone egress `[🔒][❌]`
- Route repo-intel's git egress through an allowlisted proxy (mirror the crawler's
  squid pattern): the ONLY reachable hosts are the client's git host(s).
- SSRF guard on the resolved git host (reuse `_is_safe_public_hook` logic: public
  IPs only, block metadata/internal).
- **Files:** `docker-compose.qec.yml` (repo-intel networks + egress proxy),
  `engines/repo-intel/app/config.py` (proxy), allowlist writer.
- **Accept:** clone reaches only the git host; an attempt to a private IP is refused + logged.

### 2.2 Per-connection micro-sandbox `[❌]`
- Run clone + analysis inside an isolated sandbox (gVisor or Firecracker microVM;
  minimum: a locked-down container with `--network` fenced, read-only rootfs,
  no docker socket, seccomp, CPU/mem/pids caps).
- **Disable git hooks** (`core.hooksPath=/dev/null`), reject symlink-escapes,
  zip-bomb/size caps (extend the existing byte cap with file-count + depth caps).
- **Accept:** a repo with a malicious `post-checkout` hook / 10⁶ files / a 50GB
  blob is safely refused; sandbox destroyed after analysis.

### 2.3 Zero-retention guarantee `[❌]`
- Clone lives ONLY in the ephemeral sandbox; wiped on completion or after a TTL
  (default 10 min); NEVER written to a durable volume or backup.
- Emit only the **scrubbed App Model atoms** (already secret-free) to the DB.
- Publish a written data-handling statement: *source never persisted, wiped in N
  minutes, never backed up, optional on-prem.*
- **Accept:** post-analysis, no source on any disk; only atoms in the DB; a
  compliance test asserts the workdir is gone.

**Phase 2 exit gate:** clone egress is host-allowlisted, runs in a destroyed-after
sandbox, and source is provably never retained.

---

## PHASE 3 — Scale & reliability (1000+ clients/day)

**Goal:** webhook storms, concurrent clones, and rotation all behave under load.

### 3.1 Webhook robustness `[⚠️]`
- Dedup by provider **delivery-id**; idempotency key per (app, delivery).
- Per-app + per-tenant **rate limits**; a push storm coalesces to one queued cycle
  (debounce window), never N cycles.
- Verify `pull_request`/`push` event routing; ignore non-actionable events.
- **Accept:** 500 pushes in 10s → bounded cycles; duplicate deliveries → one effect.

### 3.2 Clone concurrency + cache `[❌]`
- Bounded clone workers per tenant + global backpressure queue.
- **Clone cache keyed by SHA** — an unchanged commit is never re-cloned; ls-remote
  short-circuits when SHA == last analyzed.
- **Accept:** re-push of the same SHA does zero clones; concurrent tenants don't
  starve each other.

### 3.3 Observability + SLOs `[⚠️]`
- Metrics: clone latency, bytes, refusal reasons, token-mint count, webhook
  accept/reject, sandbox lifecycle.
- Per-connection health surface (`repo_status`, last_sha, last_analyzed_at).
- **Accept:** dashboards + alerts for clone-failure spikes, egress refusals, token
  errors.

**Phase 3 exit gate:** load test at 1000+ connections/day with storm + cache +
concurrency proven; SLOs met.

---

## PHASE 4 — The differentiator: grounded diff → flow → proof `[❌]`

**Goal:** turn "we connect to your repo" into "we prove your change is safe, with
evidence." No competitor grounds a code diff to a specific, proven UI behavior.

### 4.1 Code→UI atom map (repo-intel)
- Extend repo-intel to emit, per changed construct, the **routes/pages/controls**
  it governs (atom → `page_key`/`control_fp`), verbatim-grounded.
- **Files:** `engines/repo-intel/app/extract/*`, `manifest/seed.py`.
- **Accept:** for a sample repo, a changed handler maps to the exact UI page(s).

### 4.2 Diff → affected flows resolution (qe-central)
- On push/PR: compute the diff (old_sha→new_sha), resolve changed atoms → changed
  `page_keys`/`control_fps`, feed the EXISTING change-detector/selector so ONLY
  affected flows run; the rest carry forward with age labels.
- Reuse `controlplane/cycle/change_detector.py` + `selector.py` (already built).
- **Accept:** a change to one page re-runs only the flows touching it; carry-
  forward is honest.

### 4.3 Evidence post-back
- Result comment/attachment: *"`PremiumCalculator.java:88` → Term Quote flow →
  VERIFIED green (video + assertion proof); Final Expense unaffected (carried, 2h)."*
- Tie every claim to the signed verdict ledger (tamper-evident).
- **Accept:** a PR receives a grounded, evidence-linked verdict.

**Phase 4 exit gate:** a real diff drives a scoped, grounded, evidence-backed cycle
end-to-end on a proving-ground repo.

---

## PHASE 5 — Pre-merge PR gate (shift-left) `[❌]`

**Goal:** block/annotate the PR before merge with grounded evidence.

- GitHub **Check Run** / GitLab **MR status**: `pending → success/failure` with a
  per-flow breakdown and links to video/assertions.
- Configurable gate policy (block on P0 regression; warn otherwise).
- **Files:** `github_app.py` (Checks API), `webhooks.py` (pull_request handler),
  verdict ledger link.
- **Accept:** a PR with a regression shows a failing Check naming the flow + step +
  screenshot; a clean PR passes.

**Phase 5 exit gate:** end-to-end PR gate demoed on a proving-ground repo with both a
green and a regressed PR.

---

## PHASE 6 — Value-add + compliance packaging `[❌]`

- **Secret-leak surfacing:** the scrubber already detects secrets — surface
  *"hardcoded AWS key at `config/prod.rb:14` (masked)"* as a security report
  (opt-in), never storing the raw secret.
- **SOC2 / data-residency:** document the code-handling controls (encryption,
  ephemeral clone, zero-retention, on-prem option); evidence for the audit.
- **On-prem / air-gap** packaging of repo-intel (already SDK-self-contained).
- **Accept:** a one-page "how we handle your code" doc a security team signs off;
  air-gap install runbook.

---

## Cross-cutting requirements (every phase)
- **Tests:** unit (scrub, token hygiene, webhook, SSRF, selector), contract
  (encrypted-at-rest, RLS isolation), integration (clone→atoms→cycle on a
  proving-ground repo), load (Phase 3).
- **Security review gate** at the end of each phase (`🔒` items are blocking).
- **RLS everywhere:** all new tables tenant-scoped + FORCE RLS.
- **Fail-open on intelligence, fail-closed on security:** repo-intel absence never
  blocks a crawl; a security check failure always refuses.
- **Never green-wash:** an unverifiable code claim is demoted, not surfaced.

## Recommended sequencing (highest ROI first)
1. **Phase 0 + Phase 1** together — makes "Code" actually usable AND enterprise-safe
   (the concrete blocker). 
2. **Phase 2** — the security spine for scale.
3. **Phase 4** — the moat (grounded diff→flow→proof).
4. **Phase 3** — hardening for 1000+/day (parallelizable with 4).
5. **Phase 5 + 6** — shift-left gate + compliance packaging.

## Definition of done (production-ready, no gaps)
- A client connects a **private** repo via a **GitHub App** (least-privilege,
  short-lived), secrets **encrypted at rest**, clone **host-fenced** in an
  **ephemeral sandbox** with **zero source retention**, at **1000+/day** with
  storm/cache/concurrency proven, where a **code diff runs only the affected flows**
  and posts a **grounded, evidence-backed PR verdict** tied to a **signed ledger** —
  with a **compliance doc** a security team signs off.
