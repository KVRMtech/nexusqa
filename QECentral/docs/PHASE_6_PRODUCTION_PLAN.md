# QE-Central — Phase 6 Production Plan

**Product:** VKPower Centralized QE (working brand: **VKPower Attest**)
**Branch:** `feat/pipeline-trust-track-ab` · **Monorepo:** `c:/Users/srika/nexusqa/Nexus_power`
**Status entering Phase 6:** Phases 0–5.5 built, 760 unit/contract tests green, VM-proven on hand-written substrate. The live half has never run.

---

## 1. THE GOAL (restated) + the framing that survives due diligence

**Goal:** turn the proven-but-never-live QE-Central engine into a production-deployable product we hand to *different real clients* for real **non-prod** regression tests, under its **own separate branded UI**, proving next-level Centralized Regression QE across web UIs of any framework.

**The two headline claims, reframed to what is provably true (this is the *stronger* pitch, not a hedge):**

- ❌ "Works on ANY UI." → ✅ **"Works on any WEB UI of any framework, measured PER STACK with named gaps."**
  The discovery engine (`engines/qe-explorer/app/inventory_js.py`) is a DOM/ARIA walker that recurses *open* shadow roots + *same-origin* iframes. It goes blind on canvas/WebGL/Flutter/Unity (no a11y nodes), closed-shadow subtrees, cross-origin iframes, and MFA/SSO/CAPTCHA login walls. The failure mode today is **silent low recall that reads as coverage** — the exact green-wash we forbid. Fix: every blind surface emits an explicit *"cannot see this surface — named gap"* line, never a low number. Desktop/mobile/mainframe are roadmap via the capture-adapter seam and are labeled OUT-OF-SCOPE, never "all."

- ❌ "Real production test." → ✅ **"Regression testing against an ATTESTED, disposable, production-EQUIVALENT (non-prod) environment, behind a fail-closed egress guard."**
  Enforced structurally, not by policy (see 6.4): a prod-guard at registration + an EXPLORE-phase attestation gate so even the read-crawl refuses a non-attested or prod host.

**Honesty contract carried into pixels and CI:** measured-per-stack recall with named misses; `REFUSED_CORRECTLY` styled as a PASS; `blocked_on_p0_gaps` hard-blocks and names blockers; `GREEN_WASH_DETECTED` count must be 0; every claim regenerable by `make qec-benchmark` and re-verifiable by the client via an offline dossier verifier.

---

## 2. WHAT'S DONE vs THE GAP

**DONE (built + mostly proven at unit/DB/VM level):**
- `platform/qe-central` FastAPI service, own `qecentral` DB, 21 tables all ENABLE+FORCE RLS on `nexus.current_tenant_id`; behavioral cross-tenant proof (`tests/contract/test_rls_isolation.py`) through non-superuser roles.
- `engines/qe-explorer`: the full `PlaywrightBrowserPort` (`app/main.py:433–625`), crawler state machine, a11y `INVENTORY_JS`, two-layer egress fence (squid dstdomain + `context.route` guard), fail-closed 3-phase mutation guard + disposable-env `Attestation`, HMAC callback.
- `engines/repo-intel` App Model; the full REUSED VKPower factory chain (generate→compile→certify→run→heal→verify→triage→report) via `controlplane/cycle/driver.py`.
- Phase 5.5 hardening exists but **INERT**: redis admission (`scheduling/distributed.py`), `pg_advisory_lock` leader election (`leader.py`), `/metrics` (`observability/`), HTTP retries + rate limit (`clients/resilience.py`, `api_protect.py`), KMS envelope (`nexus_sdk/security/envelope.py`), CI workflow (`.github/workflows/qec-ci.yml`).

**THE GAP (Phase 6):**
1. **Explorer has NEVER driven a real browser.** Every test used `FakeBrowserPort`. `QEC_EXPLORER_DISPATCH_ENABLED` defaults false. No live crawl → no real KPI number exists yet.
2. **No branded UI.** Only a static prototype Artifact. The 6-bucket onboarding, fleet, 1% approval queue, honest reports have no frontend.
3. **No persistent/monitored/backed-up deploy.** VM verify used a throwaway container. `qec-ci.yml` has never run (branch unpushed; origin `KVRMtech/nexusqa` exists). `/metrics` never scraped. **Postgres has NO backup/PITR** and `qecentral` shares the VKPower `postgres:16` instance (shared-fate). KEK is dev-local on an unbacked volume.
4. **Nothing fails closed at boot on an unsafe prod config** — default JWT secret, `NEXUS_ENV=development` + `local` KEK, default explorer HMAC token all sail through. `/health` never reports envelope state.
5. **No onboarding flow, no prod-guard, no discovery-recall measurement, no client-verifiable dossier, no kill-switch.**

---

## 3. THE PHASE 6 PROGRAM

Five sub-phases. **Recommended sequence and gating below.** The spine principle: *safety-boot-gate + durability come first (they are the difference between safe and unsafe on real client data); the first real crawl de-risks everything else; the portal and proof corpus consume what the crawl produces.*

```
6.3a (release spine + PITR + real KMS + boot gate)  ──┐  release-blockers, FIRST
6.4a (prod-guard + EXPLORE attestation gate + onboarding + kill-switch)
        │  both gate ▼
6.1 (first live crawl, fences proven on real browser, full loop green)
        │  gates ▼
6.5 (any-UI proof: cross-stack corpus + per-stack recall scorecard + dossier)
6.2 (branded portal — can build ~80% in parallel from 6.1; wired live after)
        │  all gate ▼
FIRST REAL (non-prod) CLIENT PILOT
        │  fast-follows before 2nd concurrent / regulated buyer ▼
6.3b (multi-replica activation + smoke)  6.4b (dedicated PITR DB cutover, SSO/OIDC, on-prem Helm/air-gap bundle)
```

### 6.1 — Live-crawl productionization *(the core Phase-6 gap)*
- **Goal:** `PlaywrightBrowserPort` drives real chromium; the full URL→crawl→substrate→certify→run→report loop runs once end-to-end on a real crawl; per-stack discovery-recall is a published number.
- **Build (≈10–15% new, additive):** the **discovery-recall harness** (grep finds zero recall code today) — a committed ground-truth inventory per corpus app + a comparator over `/work/{crawl_id}/manifest.jsonl` emitting recall per app/stack/control-kind with every MISS named; a **disposable-app reachability harness** (real DNS hostnames e.g. `aegis.qec.test` on `qec-egress-public`, because squid `dstdomain` + `guard.registrable_domain` don't match localhost/IP); a **storageState-IMPORT** auth path (today `auth.py` only *exports* a session — needed to start EXPLORE past MFA/SSO); a **per-crawl coverage-and-gaps report** naming cross-origin-iframe/closed-shadow/canvas/WebGL/auth-wall blind spots; a **one-command live-loop runner**.
- **Reuse (≈85–90%):** the entire coded loop — `PlaywrightBrowserPort`, crawler state machine, `INVENTORY_JS`, `state_fingerprint`, the two-layer egress fence, `auth.py` verified-login, `forms.py` two-phase submit, HMAC callback → `write_exploration`, `driver.py`.
- **Exit (MEASURED):** ≥1 owned app (Aegis or parabank) goes URL→certified→green report on a *real* crawl; the fail-closed fence is demonstrated on the **live** browser (an off-allowlist host is aborted by squid **and** `context.route`; `guard_blocks > 0`); recall harness emits a defensible per-stack number with named misses.
- **Top risk:** `PlaywrightBrowserPort`'s locator ladder / value read-back / networkidle settle have never touched real DOM — first live crawls will surface adapter bugs `FakeBrowserPort` could never catch. Reachability (localhost/IP won't match the allowlist) is a hard prerequisite, not a nicety.

### 6.2 — Branded separate portal *(see §4 for full spec)*
- **Goal:** a DIFFERENT client logs into a standalone **VKPower Attest** SPA (own origin, shared JWT), onboards via the 6-bucket wizard, watches the fleet, works the 1% queue, reads honest reports — wired to real `/api/v1/qec/*`.
- **Build:** new app `platform/qec-portal/` (repo-root `/client` NEVER touched); typed `qec.ts` API client over the entire surface; 6-bucket onboarding wizard with disposable-attestation gate; 1% approval queue + e-sign→approve→materialize; coverage scorecard (named gaps, provenance, universe e-sign); autonomy-per-band charts (null-band honesty); cycle detail; R1–R8 REFUSE-proof panel with green-wash alarm; renders-vs-behaves tier badge; the honesty-UX component kit; the full brand.
- **Reuse:** the entire `/client` toolchain + ~20 UI primitives **vendor-copied** (not cross-imported, so `/client` stays byte-for-byte unchanged); the auth pattern (axios Bearer interceptor + zustand persist + `?token=` evidence fallback). Backend: pure consumer, nothing rebuilt.
- **Exit (MEASURED):** a different client, in a browser, completes onboard→crawl→synthesize→e-sign→materialize→cycle→honest-report end-to-end; `/client` diff is zero bytes; role gating verified; every honesty invariant visible.
- **Top risk (ship-blocking sub-gate):** **CORS.** qe-central sets `Content-Security-Policy: default-src 'none'` and ships NO CORS middleware. Mitigation baked in: serve the SPA **same-origin** behind an nginx reverse proxy (`/api/v1/qec/*`→`qe-central:8093`, `/api/v1/auth/*`→gateway). Secondary: four read seams are partial until backend adds them (fleet summary, change_events/fingerprints drift, dossier, defect escalations) — flagged as explicit asks, composed client-side where possible, never faked.

### 6.3 — Production infrastructure
- **Goal:** persistent, backed-up, monitored, tenant-provisioned deployment fit to hand a first client; retire Claude-as-the-pipeline.
- **6.3a (release-blockers, FIRST):**
  - **RELEASE SPINE:** push the branch; get the first-ever `qec-ci.yml` green; add tag-triggered `.github/workflows/qec-cd.yml` on `vqec-*.*.*` tags (namespaced so it never collides with VKPower's `v*.*.*` cd.yml) → buildx+push the 3 images to GHCR with pinned `nexus-base` digest + SBOM; `scripts/qec_deploy.sh` doing `docker compose -f docker-compose.qec.yml up -d --pull always` + alembic upgrade + `/health` verify + tag rollback on degraded probe.
  - **POSTGRES PITR/BACKUP (highest deferred risk):** a `qec-pg-backup` sidecar (pgBackRest/WAL-G) on the shared `postgres:16` — continuous WAL + nightly base → KMS-encrypted GCS bucket (7-day PITR/30-day base), covering nexus+qecentral in one setup, **plus an automated monthly restore-drill** (a backup never restored is not a backup — the drill IS the gate).
  - **REAL KMS, fail-closed at BOOT:** provision GCP KMS keyring+CryptoKey (envelope `gcp_kms` path is complete — provisioning-only); boot assertion in `main._init_envelope_service`: if `NEXUS_ENV ∉ {development,test}` and provider==`local` (or envelope is None), **REFUSE TO START** (today it logs-and-continues); add an encrypt→decrypt canary to `/health`; drop the `qec-kek` local volume in prod.
  - **OBSERVABILITY SCRAPED:** `docker-compose.qec.observability.yml` overlay (Prometheus scraping `qe-central:8093/metrics` + Grafana) over the already-emitted families (`qec_cycles_*`, `qec_admission_*`, `qec_factory_*`, `qec_substrate_rows_written`, `qec_harness_outcomes`, `qec_cost_units`); alert rules (health degraded, db disconnected, sustained `limiter_unavailable`, factory 5xx, cycle-failure spike, **backup age > 24h**, cost breach).
- **6.3b (fast-follow, before 2nd concurrent client):** flip `QEC_ADMISSION_BACKEND=redis` + `QEC_DAEMON_LEADER_ELECTION=advisory_lock`, run ≥2 replicas, add an integration smoke proving (a) aggregate admits never exceed one host `max_rps`, (b) exactly one leader + failover on kill — requires Redis HA (sentinel) first, since a Redis outage fail-closes the whole fleet.
- **Reuse:** `docker-compose.qec.yml` already encodes the full topology + every inert env knob; the metrics/redis/leader/resilience modules are built — zero new control-plane code; VKPower `cd.yml` gives the GHCR/buildx pattern; `infrastructure/helm/nexus-qa` + terraform are the on-prem template.
- **Exit (MEASURED):** all images built by qec-ci→qec-cd, pulled by tag from GHCR (Claude no longer hand-deploys); `/health=healthy` with Prometheus actively scraping and Grafana showing live cycle/admission/factory/cost panels; a nightly backup lands in the encrypted bucket **and a restore-drill has PROVEN recovery to a timestamp**; creds sealed under real KMS.
- **Top risk:** shared-fate DB (a VKPower incident hits QE-Central until the 6.4b cutover); backup honesty (unrestored backup ≠ backup); don't over-invest in EKS/ArgoCD before a pilot needs it (live = compose-on-VM).

### 6.4 — Multi-client onboarding + security
- **Goal:** safely hand the system to a *different* real client; nothing unsafe boots green.
- **6.4a (FATAL-gating, FIRST — thin orchestration over proven primitives):**
  - **Onboarding state machine** (`qec_002_onboarding.py`: `client_onboardings` draft→attested→preflight_passed→live + hash-chained `rules_of_engagement`); no cycle schedulable until `onboarding=='live'`.
  - **PROD-GUARD** in `apps.py::create_app`: refuse registration unless `env_attestation` present and `env_kind ∈ {staging,disposable}` (never prod) — wires the already-defined-but-dead `_ENV_KINDS`/`_SUBMIT_ENV_KIND`. **This is the single most important onboarding safety fix** (today it accepts any http(s) URL).
  - **EXPLORE-phase attestation gate:** even the READ crawl refuses a non-attested/prod host (closes the gap the SUBMIT-only gate leaves wide open).
  - **Boot-time refusal of known-default secrets:** refuse to start when `NEXUS_JWT_SECRET`/`QEC_EXPLORER_TOKEN` equals any shipped default, or when `NEXUS_ENV=production` and either is unset. One validator; turns a silent catastrophe into a loud startup failure.
  - **aud/product claim** in `auth.py._decode_token` (one line) so a VKPower token can't be replayed on QE-Central (closes live cross-product privilege bleed under the shared JWT secret).
  - **Preflight** (EXPLORE-only crawl proving reachability + verified login + parseable oracle before go-live).
  - **Kill-switch** enforced at the 3 existing choke points (`admission.py`, cycle daemon, explorer `explore_cancel`), full stop < 5 min.
  - **Per-tenant KEK resolver** + `tenant_keks` table (replaces the single-ARN closure); **immutable `qec_admin_events`** hash-chained log for every privileged mutation.
- **6.4b (fast-follow, before 2nd concurrent / regulated buyer):** per-crawl squid-allowlist isolation + post-crawl workspace/memory shred (ships *with* multi-replica); **qecentral cutover to its own PITR-capable instance** (kills shared-fate blast radius); client-facing **Compliance Dossier** + `dossier_roots` + offline verifier (NAIC 7-yr); TOTP MFA now + SAML/OIDC connector seam per deal; disable/white-glove self-signup; on-prem Helm/air-gap bundle + `docs/QEC_SECURITY_REVIEW_AND_PENTEST_PLAN.md` (STRIDE over 8 trust boundaries).
- **Reuse (~80%):** RLS everywhere, the KMS envelope + rotate_kek, the quarantined explorer + squid + HMAC callback, the fail-closed guard + Attestation, the append-only hash-chained approval/baseline logs + `verify_chain`, PII redaction-at-source, the admission gate as the kill-switch choke point, `platform/auth-service` mechanics.
- **Exit (MEASURED):** no app schedulable until `onboarding=='live'` (signed RoE + non-prod attestation + passed preflight); qe-central **refuses to boot** in non-dev with `local` KEK; the end-to-end isolation conformance matrix is green through roles AND the live API (tenant-A JWT → 404 on tenant-B); kill-switch drill halts a running crawl < 5 min.
- **Top risk:** the prod-guard ultimately relies on the client truthfully attesting `env_kind=disposable`; mitigated (not eliminated) by independent reachability heuristics + white-glove human sign-off for SUBMIT tier + honest RoE language.

### 6.5 — Any-UI proof + GTM
- **Goal:** prove framework-agnostic with *regenerable numbers*, not claims; ship a client-verifiable dossier.
- **Build (~20% new):** a **cross-stack disposable corpus** (`benchmarks/qec/corpus.yaml` — Aegis/Skyward/demo-server + one life-insurance-shaped app + 2–3 React/Angular/Vue/SSR apps) with **human-verified answer keys** (`key_status=verified` gate); `run_scorecard.py` (per-stack KPI aggregator); `run_falseheal.py` (source-attributed replay of `?break=` regressions); `dossier.py` + `GET /apps/{id}/dossier` (root-hashed bundle); `verify_dossier.py` (stdlib-only, client-runnable offline verifier); `.github/workflows/qec-benchmark.yml` (`--gate` fails on any GREEN_WASH, per-source false-heal >1%, or KPI regression). 4 GTM docs (KPI definitions, POC playbook, design-partner path, competitive wedge).
- **Reuse (~80%):** every KPI maps to an existing signal (`coverage.upsert_atoms`/`diff_universe`, `harness/runner.py`, `tier_label`, `touch_meter.autonomy_trend`, `cost.meter.aggregate_cost`); the dossier reuses `approval.canonical_json/compute_chain_hash/verify_chain`; safety fences reuse `guard.classify_request` + `Attestation.is_submit_capable` + `refuse_pack.yaml`.
- **Exit (MEASURED):** ≥5 disposable apps across ≥4 UI stacks + 1 life-insurance app, each verified-key; `make qec-benchmark` runs the full loop on a **real** crawl and publishes a per-stack scorecard for **7 KPIs** with named gaps (no blended number); the gate fails on green-wash / false-heal >1% / KPI regression; a client runs `verify_dossier.py` standalone → exit 0.
- **Top risk:** circular answer keys (a key seeded from the pipeline's own extraction inflates recall — enforce human-verified gate); false-heal source taxonomy must be read from the live healer's `fix_kinds`, not invented.

**The 7 published KPIs** (each: formula · source `file:function` · falsification test · target): (1) discovery-recall-per-stack; (2) false-heal-per-source (target <1%); (3) correct-refuse-rate + GREEN_WASH count (must=0); (4) behavioral-coverage-tier (renders vs behaves); (5) autonomy-per-band (never blended); (6) time-to-first-certified-suite; (7) cost-per-certified-suite (USD only when priced, unmetered surfaced).

---

## 4. THE BRANDED PORTAL SPEC

**Separate-app decision:** NEW app at `platform/qec-portal/` (sibling to `platform/qe-central`). Repo-root `/client` (VKPower operator portal) is **never edited**. Same stack as `/client` for maximal pattern reuse (Vite 5 + React 18 + TS-strict + Tailwind 3 `darkMode:'class'` + react-router-dom 6 + zustand persist + axios + recharts + lucide-react + sonner), but a **distinct brand and its own deploy**. Reuse by **vendor-copy** (~20 primitives into `src/components/_vendor/`), not cross-import, so `/client` stays literally untouched; later extract a `@vkpower/ui` workspace package. **Serve same-origin behind nginx** (portal container reverse-proxies to qe-central:8093 + gateway auth) — sidesteps the CORS blocker.

**Auth:** portal calls platform-api `POST /api/v1/auth/login` → shared HS256 JWT (`NEXUS_JWT_SECRET` already shared); zustand persist key `vkqec-auth`; axios interceptor attaches Bearer to every `/api/v1/qec/*`; 401→`/login`; GET-only `?token=` for evidence images. Tenant from JWT `tenant_id`; `role` drives gating (viewer read-only / admin|manager mutate / admin delete). SSO/OIDC is a later drop-in behind the same JWT.

**Screens (all wired to real endpoints):**
- **Login / shell** — LoginPage, ProtectedRoute, AppLayout (tenant badge, role badge, ENV badge from `env_attestation`).
- **Fleet dashboard `/`** — `GET /apps` + per-app rollup (last cycle, coverage verdict, open approvals, open P0 gaps, autonomy mini); stat tiles (total apps / blocked_on_p0_gaps / approval queue / fleet autonomy). *(flags N+1 → wants `GET /fleet/summary`)*.
- **Onboarding wizard `/apps/new`** — the 6 buckets mapped exactly to `POST /apps`: ACCESS (name/base_url/canonical_host/credentials, envelope-encrypted, never echoed), CODE (gitlab repo_binding + webhook URL), DATA (folded into `answer_key.data_seeds` until backend adds a field), ANSWERS (answer_key), SAFETY (`env_attestation` + `fences` with a HARD disposable-attestation gate), OPS (schedule + budgets). On submit → offer "Run first crawl" (honest 503 when dispatch off).
- **App detail `/apps/:id`** tabs — Overview (pause/resume/run-cycle/run-crawl + verdict banner); **Approval Queue "the 1%"** (scenarios grouped by band/diff, drawer with journey+evidence+chain_hash, approve requires typed e-signature = 422 parity, then materialize = 409 unless approved); **Coverage scorecard** (atoms by source/provenance, verdict `ok|blocked_on_p0_gaps`, named gaps, waive/adjudicate, universe e-sign, shrinkage guard); **Autonomy-per-band** (recharts, null band ≠ 100%, never averaged); **Cycle history** (state machine, selected-vs-carried with verdict ages, honest gaps, cost); **REFUSE-proof + certified invariants** (R1–R8 verdicts, `REFUSED_CORRECTLY` as pass, `GREEN_WASH_DETECTED` red alarm); **Tier badge** (renders vs behaves); **Cost** (units-first); **Dossier/Reports** (composed printable). *(flags: dossier, defect-escalation, drift reads need backend seams.)*
- **Honesty-UX kit:** VerdictBanner, RefusedCorrectlyBadge, MeasuredCoverageLabel (never "all"), ProvenanceChip (`G_DETERMINISTIC/G_LIVE_CONFIRMED/G_INFERRED`), BandChip (P0–P3), TierBadge, ESignatureModal (422 parity), HashMono (JetBrains Mono for chain_hash/fingerprints).

**BRAND direction (render-precise):**
- **Product name:** **VKPower Attest** (category descriptor "Centralized Regression QE"; lockup: VKPower house + Attest wordmark). Shortlist if rejected: Verdict, Sentinel, Proofline, Assure. Literal fallback: "VKPower Centralized QE."
- **Icon — a MEASUREMENT-SEAL** (deliberately sibling-but-distinct from VKPower's navy squircle + white V/K + gold bolt): same 52-radius navy squircle so it reads as family, but the symbol is a circular gauge whose needle IS a checkmark landing in the "proven" arc — measured + verified + trusted. Reference SVG:
  `<svg viewBox='0 0 256 256'><rect x=8 y=8 width=240 height=240 rx=52 fill='#0a2540'/><path d='M64 150 A64 64 0 1 1 192 150' fill='none' stroke='#0FB5A6' stroke-width='14' stroke-linecap='round'/><path d='M96 132 L120 158 L168 96' fill='none' stroke='#10B981' stroke-width='18' stroke-linecap='round' stroke-linejoin='round'/></svg>` · Favicon emoji: 🛡
- **Palette:** house base navy `nexus-900 #0a2540` / `nexus-950 #051524`; primary accent **verify-teal** `#0FB5A6→#0E7C74` (swaps VKPower gold to signal trust/measurement, not energy/factory); verdict-semantic status — PROVEN/BEHAVES `#10B981`, INFERRED/renders `#F59E0B`, BLOCKED/REFUSED/P0 `#E5484D`, **REFUSED_CORRECTLY (honest-refuse, styled as pass) `#5B7FFF`**; P0–P3 severity ramp.
- **Type:** Inter (UI) + JetBrains Mono (hashes/IDs/atom keys/fingerprints — load-bearing for evidence legibility). Both reused from `/client`.
- **Deploy:** multi-stage Dockerfile (node build → nginx:alpine) + SPA-fallback nginx.conf; ADDITIVE `qec-portal` service in `docker-compose.qec.yml` (port 3001, `nexus`+`qec-internal` nets). `docker-compose.yml` and `/client` untouched.

---

## 5. FATAL / SERIOUS RISKS → mitigation baked into a sub-phase

**FATAL — nothing fails closed at boot on an unsafe prod config (grounded in code):**
- **Zero backup/PITR on the shared VKPower `postgres:16`.** The client's hash-chained NAIC-7-yr dossier + their envelope-encrypted live-app creds live in a DB with no recovery path, coupled to a separate production system. One VKPower disk failure / bad restore / erroneous DROP vaporizes the one deliverable they paid for. → **6.3a: PITR + proven restore-drill FIRST.** (blast-radius isolation → 6.4b dedicated-instance cutover.)
- **Client creds under a dev key, silently.** Default bring-up (`NEXUS_ENV=development` + `local` KEK on unbacked `qec-kek` volume) sails past the envelope guard; `main.py` catches `ProviderUnavailable` and boots anyway; `/health` never reports envelope state. → **6.3a: per-tenant real KMS + REFUSE-TO-BOOT assertion + `/health` encrypt→decrypt canary + drop the volume.**
- **Tenant isolation trusts an unvalidated secret.** `NEXUS_JWT_SECRET` defaults to `dev-jwt-secret-change-me`; nothing refuses to boot on a default. A known secret → forge `{tenant_id, role:admin}` → RLS worthless. The secret is shared with VKPower and there's no `aud` claim → any VKPower token is a valid QE-Central token (live cross-product bleed). Explorer callback trusts `body.tenant_id` under one fleet-wide HMAC default. → **6.4a: boot-time refusal of known-default secrets + `aud`/product claim (one line each).**

**SERIOUS — the two headline claims fail a security team as worded:**
- **"ANY UI" is falsifiable in the first mis-targeted demo** (canvas/WebGL/Flutter/closed-shadow/cross-origin-iframe/MFA/CAPTCHA → silent near-zero recall reading as coverage). → **6.5 + 6.1: reframe to "any WEB UI, measured per stack"; blind surfaces emit a named "cannot see this surface" gap, never a low number; ask the client's stack up front.**
- **"Real production test" is either dishonest or existentially dangerous.** The EXPLORE read-crawl has NO attestation gate and `apps.py` has NO prod-guard, so today's default permits pointing a crawl at live prod — ingesting real PII at "confidence 1.0," tripping side-effecting GETs, looking like an attack to the client's WAF. The non-prod guarantee rests on an unverified client checkbox. → **6.4a: prod-guard at registration + EXPLORE-phase attestation gate (structural, not policy) + SOC/WAF egress allow-listing of the single proxy identity + honest RoE.**

**SERIOUS — operational:**
- Multi-replica fail-closed stall (redis outage stalls the whole fleet) → **6.3b: Redis HA before flipping the backend.**
- Global squid allowlist rewritten per crawl (cross-tenant egress race at scale) → **6.4b: per-crawl allowlist isolation ships with multi-replica.**
- Self-signup creates tenant+admin with no verification → **6.4b: disable/white-glove-gate.**

---

## 6. WEEK-1 CONCRETE ACTIONS + FOUNDER ASKS

**Recommended first build (the release spine + boot gate — unblocks everything, mostly config over built primitives):**
1. **Push `feat/pipeline-trust-track-ab`** to `origin` (KVRMtech/nexusqa) → get the first-ever `qec-ci.yml` run green (fix any real-Postgres-only failures).
2. Add the **boot-time safety gate** (one validator): refuse to start on default `NEXUS_JWT_SECRET`/`QEC_EXPLORER_TOKEN` or `local` KEK outside dev/test; add the `aud`/product claim to `auth.py._decode_token`; add the envelope encrypt→decrypt canary to `/health`.
3. Add the **PROD-GUARD** to `apps.py::create_app` (wire the dead `_ENV_KINDS`/`_SUBMIT_ENV_KIND`) + the EXPLORE-phase attestation gate.
4. Stand up **Postgres PITR + a restore-drill** on the shared instance.
5. Bring up **qe-explorer persistently**, give Aegis a real DNS hostname on `qec-egress-public`, flip `QEC_EXPLORER_DISPATCH_ENABLED=true`, and drive the **first real crawl** — proving the fence on the live browser (6.1 kickoff).

**Needed from the founder (decisions that reorder ~half the chain):**
- **git push authorization** — the branch is unpushed; CI/CD can't run and Claude stays the pipeline until it lands. *(sandbox-blocked, not an error.)*
- **First-client shape:** vendor-hosted (we run QE-Central against their attested non-prod) **or** on-prem (they run it)? Decides whether the Helm/air-gap bundle is a v1 gate or a fast-follow.
- **Host for the first pilot:** reuse GCP `nexus-vm` (fastest, matches compose, but shared-fate) or a dedicated VM? Decides backup + cutover ordering.
- **KMS + backup authorization:** provision GCP KMS keyring+CryptoKey + the GCS backup bucket + service account (real client creds can't be accepted until this lands).
- **Named P0 approver** (the SME who e-signs P0 scenarios + universe baselines — the "1%" queue has no owner without this).
- **First design-partner app + its UI stack** (and whether they provide a disposable clone) — decides the corpus's "real" slot and whether the any-UI claim is proven on their framework before the demo.
- **Brand sign-off:** is **"VKPower Attest"** acceptable, or use the literal "VKPower Centralized QE" wordmark? Confirm verify-teal over gold.
- **Life-insurance corpus app:** build a synthetic quote/underwriting/beneficiary flow (fast, ours) or use a partner's masked staging clone (more credible, slower)?
