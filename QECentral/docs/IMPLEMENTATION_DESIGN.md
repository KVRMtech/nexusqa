# QE-CENTRAL — COMPLETE IMPLEMENTATION DESIGN v1

Status: engineering-ready. Synthesized from six code-verified subsystem designs (2026-07-04).
Every `file:line` citation below was read against the local repo (`c:/Users/srika/nexusqa/Nexus_power`) during design verification unless explicitly marked **UNVERIFIED**.
Prime directive: **NEVER break VKPower** — all VKPower-side change is limited to the three additive, env-gated, fail-open extensions in §4, pinned by golden contract tests written BEFORE the first patch.

---

## 0. RECONCILIATION LEDGER (conflicts between subsystem designs, resolved)

| # | Conflict | Resolution | Why (evidence wins) |
|---|----------|-----------|---------------------|
| R-1 | Substrate write path: direct-DB (S1, S6) vs new VKPower create-artifact endpoint (rejected candidate in S6) | **Direct DB write from qe-central**, mirroring the spine-engine precedent | No HTTP create-artifact endpoint exists anywhere (`CanonicalArtifactRow(` constructed only in spine-engine/main.py:4698 + one test); adding one creates a client-writable trust-substrate path needing its own RBAC story. Frameless artifacts can NEVER be populated via the derivation path (page_visit_extractor.py:355-365 `if not frames: return None`), so the DB is the true seam. |
| R-2 | `page_visits.source` value: `'explorer_ground_truth'` (S1) vs `'ground_truth'` (S2, S6) | **`'ground_truth'`** | Verified hard consequence: generator `_PROVEN_SOURCES = {'ground_truth','url_regex'}` (generator.py:556, 581-585) — any other value compiles navigations UNPROVEN; `_TRUSTED_VISIT_SOURCES` already includes it (storyboard.py:599). Crawl provenance is carried by `canonical_artifacts.source_type` + the extractor_version prefix instead. |
| R-3 | Artifact `source_type`: `'qe_central_exploration'` (S1) vs `'qe_central_crawl'` (S2) vs `'live_crawl'` (S4, S6) | **`'live_crawl'`** | S6's composer guard (extension E2) keys on this exact literal; it is the single discriminator every gated extension and tier wrapper reads. Free String(50) column (models.py:1804), collision-free vs video values. |
| R-4 | `extractor_version` string: `'qe-explorer-v1@{exploration_id}'` (S1) vs `'qec_crawler_v1'` (S2) vs `'qec_live_v1'` (S6) | **`qec_live_v1@{crawl_id}`** | Latest-wins selection is by `created_at`, not lexical (service.py:44-58), so any string works; the pinned prefix `qec_live_v1` identifies the writer generation, the `@{crawl_id}` suffix makes every re-crawl a distinct immutable version. ONE string per atomic write (hard rule, §2.3). |
| R-5 | Screenshot home: object store + optional BYTEA table (S1) vs staged-volume→frame-storage copy (S2) vs eyes ArtifactStore namespace (S6) | **Eyes ArtifactStore namespace** — explorer stages PNGs on `qec-crawl-storage`; qe-central validates and uploads via `build_key(tenant,'eyes',session,'{crawl_id}_frames', 'frame_NNNNN.png')` + writes `visual_frames` rows. S1's `qe_screenshots` BYTEA table is **dropped** | Only this path is end-to-end verified: upload (artifact_store.py:94-113 pattern per eyes-engine/main.py:948-1000), path guard (database.py:147-188), serving (eyes-engine/main.py:1061-1129), baseline flow into semantic oracle (test_factory.py:3662-3695 → semantic_oracle.py:148-167). Untrusted browser container never mounts frame-storage. |
| R-6 | `visual_frames` written conditionally behind `QE_WRITE_VISUAL_FRAMES` (S1, because of the storyboard-regenerate clobber risk) vs always | **Written by default**; extension **E2** (composer live_crawl guard) neutralizes the clobber path at its verified root (composer.py:266-280 version-inequality → re-extract). Flag retained only as emergency off-switch | S6 verified the actual clobber mechanism lives in composer `_derive` (triggered by PUT /surfaces → storyboard.py:373-377), not in the extractor; guarding composer is smaller and fail-open. Belt-1: `surface_prefs` upsert (vision surfaces off) at artifact creation; Belt-2: E2. |
| R-7 | QE-Central table home: nexus DB via out-of-band SQL scripts (S1, S3, S4) vs carved-out `qecentral` logical DB with own alembic chain (S5) | **Carved-out `qecentral` logical database** (same postgres:16 instance, docker-compose.yml:98-99), own alembic chain `qec_001…`, dedicated role `qec` with ZERO grants on the `nexus` DB. ALL QE-Central-owned tables (S1's 3, S4's 9, S5's 5, repo-intel's 5) live there with the same RLS policy shape. Substrate writes into the nexus DB use a second, least-privilege DSN (role `qec_substrate`: INSERT/SELECT on the 7 substrate tables + SELECT on `audit_log`) | Substrate rows must stay in nexus (page_visits FK canonical_artifacts, models.py:2447-2451) so a second instance can't remove the dependency; a separate logical DB makes cross-context FKs impossible (engine-enforced boundary) and one PITR setup covers both. Fresh DB ⇒ clean alembic chain beats out-of-band scripts; the ground_truth_events lesson (table shipped with no migration) becomes structurally impossible. |
| R-8 | App registry duplication: `qe_targets` (S1) vs `client_apps` (S5) | **Merged into ONE `client_apps` table** (S5 shape + S1's `creds_blob` KMS envelope and `answer_key` JSONB columns). `POST /api/v1/qec/apps` subsumes `POST /api/v1/qe/targets` | Same entity, two names. |
| R-9 | API prefix: `/api/v1/qe/` (S1) vs `/api/v1/qe-central/` (S4) vs `/api/v1/qec/` (S5) | **`/api/v1/qec/`** everywhere on the qe-central service | Shortest, unambiguous. |
| R-10 | Compose integration: services in main docker-compose.yml under a `qe-central` profile (S3, S5) vs separate `docker-compose.qec.yml` joining the external network (S2) | **One separate `docker-compose.qec.yml`** holding ALL QE-Central services (qe-central, qe-explorer, qec-egress-proxy, repo-intel) | Main docker-compose.yml gets ZERO edits — the no-break guarantee extends to deploy config. Proven precedent: docker-compose.runner.yml:15-21 joins `nexus_power_nexus` as an external network. |
| R-11 | Auth handoff endpoint name/flag: `/auth/save-state` + `QEC_AUTH_SAVE_STATE_ENABLED` (S2) vs `/auth/import` + `NEXUS_QEC_AUTH_IMPORT_ENABLED` (S6) | **`POST …/playwright/auth/import`, flag `NEXUS_QEC_AUTH_IMPORT_ENABLED`** (S6 wording — it owns the definitive extension list) | Same design; one name. |
| R-12 | qe-central port: 8093 (S1) vs 8140 (S5) | **8093** (single container hosts S1+S4+S5 modules) | One service, one port; confirm free at deploy. |
| R-13 | Service JWT role: `admin` (S1) vs `manager` + distinguishable identity (S4) | **`role='manager'`, `sub='svc-qe-central'`, `email='qe-central@service'`** | Passes the admin\|manager RBAC gate (test_factory.py:114-125) with least privilege; service mutations distinguishable from human admins in audit_log. |
| R-14 | Who guards against extractor clobber: additive guard in page_visit_extractor (S1 open decision) vs composer guard (S6 E2) | **Composer guard (E2)** | See R-6. |

---

## 1. SYSTEM OVERVIEW

### 1.1 Containers, stores, arrows

```
                              docker-compose.qec.yml (NEW — main compose untouched)
 ┌────────────────────────────────────────────────────────────────────────────────────┐
 │                                                                                    │
 │  ┌──────────────┐   seed manifest (GET, fail-open)   ┌───────────────────┐         │
 │  │  repo-intel  │◄────────────────────────────────── │    qe-central     │  :8093  │
 │  │    :8014     │   POST /diff (SHA→atoms, fail-open)│  (S1 writer +     │         │
 │  │ (S3, engine) │ ──────────────────────────────────►│   S4 governance + │         │
 │  └──────┬───────┘                                    │   S5 control plane)│        │
 │         │ git clone (egress: client GitLab only)     └──┬──────┬──────┬──┘         │
 │         │                                               │      │      │            │
 │  [qec-internal net, internal:true]                      │      │      │            │
 │  ┌──────────────┐  POST /internal/crawls/{id}/complete  │      │      │            │
 │  │  qe-explorer │ ─────────────────────────────────────►┘      │      │            │
 │  │    :8210     │  (manifest path + in-memory storageState)    │      │            │
 │  │ (S2, browser)│                                              │      │            │
 │  └──────┬───────┘   shared volume qec-crawl-storage            │      │            │
 │         │ browser traffic                                      │      │            │
 │  [qec-egress net, internal:true]                               │      │            │
 │  ┌──────────────────┐                                          │      │            │
 │  │ qec-egress-proxy │──► internet: ONLY client target host(s)  │      │            │
 │  │  (squid :3128)   │                                          │      │            │
 │  └──────────────────┘                                          │      │            │
 └────────────────────────────────────────────────────────────────┼──────┼────────────┘
        [nexus net (external: nexus_power_nexus)]                 │      │
                                                                  │      │
   ┌──────────────────────────┐  HTTP w/ service JWT (manager)    │      │ direct SQL
   │ platform-api :8091       │◄──────────────────────────────────┘      │ (2 DSNs)
   │ (FROZEN factory +        │  /generate /playwright /auto-heal        │
   │  3 additive extensions   │  /verify /triage /rtm /runs              ▼
   │  E1 E2 E3)               │                          ┌─────────────────────────────┐
   └────────┬─────────────────┘                          │ postgres:16 (nexus-postgres)│
            │                                            │  ├─ nexus DB (VKPower +     │
   ┌────────▼─────────┐   ┌────────────────┐             │  │   substrate tables;      │
   │ nexus-runner     │   │ eyes-engine    │             │  │   role qec_substrate:    │
   │ (headed heals,   │   │ serves frames  │             │  │   least-privilege writes)│
   │  unchanged)      │   │ (unchanged)    │             │  └─ qecentral DB (21 QEC    │
   └──────────────────┘   └───────┬────────┘             │      tables; role qec)      │
                                  │                      └─────────────────────────────┘
                          [frame-storage volume / object store — eyes namespace]
```

Explorer isolation invariants: `qe-explorer` sits ONLY on `qec-internal` + `qec-egress` (both `internal: true`) — it has **no route to the internet except through squid** and **no route to any VKPower service or Postgres**. It holds no DB creds and no KMS access; only a per-job HMAC token and in-memory login creds.

### 1.2 Monorepo layout (new folders only)

```
Nexus_power/
├── platform/qe-central/                  # S1 + S4 + S5 — ONE container
│   ├── Dockerfile                        # FROM nexus-base:dev
│   ├── alembic_qec/versions/qec_001_*.py # own chain, qecentral DB (21 tables + RLS)
│   ├── app/
│   │   ├── main.py  config.py  db.py  auth.py  service_token.py
│   │   ├── clients/platform_api.py       # typed httpx client, the R6-pin surface
│   │   ├── routers/{apps,explorations,harness,scenario_gov,cycles,webhooks,cost}.py
│   │   ├── artifacts/creator.py          # tenant bootstrap + SessionRow + CanonicalArtifactRow
│   │   ├── substrate/{schema,redact,writer,assets}.py
│   │   ├── harness/{rules,runner}.py  harness/fixtures/golden_3page_form.json
│   │   ├── services/{criticality,synthesis,coverage,approval,tier_label,touch_meter}.py
│   │   ├── controlplane/scheduling/admission.py
│   │   ├── controlplane/cycle/{driver,change_detector,selector,fingerprints}.py
│   │   ├── controlplane/cost/meter.py
│   │   └── db/models.py
│   └── tests/{contract,harness,unit}/
├── engines/qe-explorer/                  # S2 — browser container
│   ├── Dockerfile                        # FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy
│   └── app/{main,crawler,guard,inventory,fingerprint,auth,forms,emit}.py  app/refuse_pack.yaml
├── engines/repo-intel/                   # S3 — engine container
│   ├── Dockerfile  main.py  requirements.txt
│   ├── app/{connectors/git.py, detect/stack.py, model/store.py, manifest/seed.py,
│   │        drift/report.py, ab/harness.py, lens/llm_lens.py, security/secret_scrub.py}
│   ├── app/extract/{registry,openapi_spec,ts_routes,express_nest,spring}.py
│   └── tests/fixtures/                   # fixture repos + hand-authored answer keys
├── docker-compose.qec.yml                # qe-central, qe-explorer, qec-egress-proxy, repo-intel
├── scripts/qec_budget_gate.py            # CI budget gate
└── platform/api/tests/test_qec_contract_{video_frozen,gt_ingest,screenshots,auth_import}.py
                                          # VKPower-side pin suite (Phase-0, FIRST PR)
```

Ports: qe-central **8093**, qe-explorer **8210**, repo-intel **8014**, squid **3128**. All confirm-free at deploy.

---

## 2. THE SUBSTRATE CONTRACT v1 (the heart)

QE-Central's entire thesis: the VKPower factory reads **tables**, not extractors. Verified end to end: `POST /generate` → `generate_and_store` → `_load_current_pages_and_actions` reads `PageVisitRow`/`PageActionRow` only (service.py:92-178); it never invokes `page_visit_extractor`; missing frames degrade to empty `frame_ref` (service.py:61-89, default at :130). Therefore QE-Central writes rows; VKPower stays byte-identical.

### 2.1 Rows written per crawl (all inside ONE tenant-scoped transaction, nexus DB, role `qec_substrate`)

**Write order:** tenant bootstrap → sessions → canonical_artifacts → surface_prefs → page_visits → page_actions → ground_truth_events (optional journal) → visual_frames. RLS discipline: `SELECT set_config('nexus.current_tenant_id', :tid, true)` inside the transaction before any statement (verbatim mirror of database.py:110-144).

| Table | Fields QE-Central sets (discriminators bold) | Verified contract |
|---|---|---|
| `sessions` | session_id, tenant_id, title=`Live crawl — {host}`, **session_type='live_crawl'** (free string), status='completed' | sessions.py:94-115; session_type unvalidated string |
| `canonical_artifacts` | artifact_id, tenant_id (tenant row MUST exist first — FK; mirror spine `_ensure_tenant_exists`, spine-engine/main.py:4646), session_id (plain String(64), NO FK — models.py:1798), status='completed', **source_type='live_crawl'** (String(50) — models.py:1804), source_filename=target URL, media_fingerprint=sha256(base_url+config+explorer_version) for dedup, scene_count=0, frame_count=N screenshots, quality_gate_* computed honestly from crawl coverage (never forced-pass), full_artifact_json={qec: crawl_id, answer_key_ref, explorer_version} | Field set mirrors spine-engine/main.py:4698-4725; models.py:1776-1845; FORCE-RLS per migration 010 (010_row_level_security.py:32-76); `_require_artifact` checks only existence+tenant (test_factory.py:167-178) so every factory endpoint accepts the row |
| `surface_prefs` | tenant_id, artifact_id, storyboard=**false**, pages_forms=**false**, three_d_journey=**false** (Belt-1 anti-clobber) | SurfacePrefRow surface_prefs.py:44-58; `set_artifact_override` surface_prefs.py:153; gates extractors at composer.py:911/:969/:1015 |
| `page_visits` | sequence_index (monotonic), location/url_host/url_path/url_query/canonical_host, first_seen_ms/last_seen_ms/duration_ms (crawler wall-clock, one monotonic clock), frame_count, **source='ground_truth'**, **extraction_confidence=1.0**, **extractor_version='qec_live_v1@{crawl_id}'** (ONE string for the whole write), form_snapshot={label: redacted_value} INLINE, form_snapshot_signals={label:{type,options,required}}, form_snapshot_extractor_version=same string, primary_scene_id=NULL | DDL+RLS+UNIQUE `uq_page_visits_artifact_sequence_version(artifact_id, sequence_index, extractor_version)` — 034_page_visits.py:44-200 (cols :47-155, unique :156-159, RLS :178-200) + models.py:2418-2503; source free-form String(40), no CHECK |
| `page_actions` | page_visit_id, artifact_id, subaction_index, verb (≤20ch), target_label (≤500), target_kind (≤20), value **PII-REDACTED AT SOURCE**, **confidence=1.0**, **automation_ready=true**, extractor_model='qe-explorer', extractor_version=same string, evidence_signals={**anchor**:{label,kind}, **after**:{outcome,detail,navigated}, url_changed:bool, qec:{role,frame_selector,options,testid,css_hint}} | UNIQUE(page_visit_id, extractor_version, subaction_index) — 035_page_actions.py:37-152 + models.py:2571-2642; `anchor`/`after` unpacked verbatim by the factory (service.py:152-157), navigated derived at service.py:171-175; NO source-based filtering in the load path (service.py:92-138) |
| `ground_truth_events` (optional v1 journal) | uuid5-dedup event_id, kind, url*, target_label, value (redacted), modality='qe_explorer', recorder_version='qec_explorer_v1', signals | models.py:2506-2568 — docstring is source-agnostic and mandates "Form VALUES are PII-redacted AT SOURCE" (:2519-2520). Written direct — NEVER via the HTTP ingest routes (§4 E1). Overlay consumer unreachable for frameless artifacts (page_visit_extractor.py:1965-1976) — journal is audit-only |
| `visual_frames` | frame_id, tenant_id, session_id, job_id='{crawl_id}_frames' (NOT NULL, no default), frame_index, timestamp_seconds **inside owning visit's [first_seen_ms, last_seen_ms] window**, frame_asset_path='{tenant}/{session}/{crawl_id}_frames/frame_NNNNN.png', artifact_id, extracted_text='' (crawler DOM text optional, no OCR), is_keyframe=true | models.py:820-888; path MUST match `safe_frame_asset_path` regex + tenant prefix (database.py:147-188); window-join `_frame_refs` picks LAST frame with first_seen_ms ≤ ts*1000 ≤ last_seen_ms (service.py:61-89); flows into step.screenshot (generator.py:993-996) → eyes serving (eyes-engine/main.py:1061-1129) → semantic-oracle baseline |
| `e2e_auth_profiles` | via extension **E3** only — never direct (encryption stays in one place) | save_profile refuses plaintext, AAD=artifact_id, 2MiB cap (auth_profiles.py:34,55-88); consumed unchanged by run/auto-heal via get_storage_state (test_factory.py:879, :2262) |

### 2.2 Discriminator summary

| Level | Field | Value | Consumer |
|---|---|---|---|
| Artifact | `canonical_artifacts.source_type` | `live_crawl` | E2 composer guard; tier wrapper; UI provenance |
| Session | `sessions.session_type` | `live_crawl` | UI lists |
| Visit | `page_visits.source` | `ground_truth` (existing value — deliberate) | generator PROVEN gate (generator.py:556,581-585); trust badges (storyboard.py:599) |
| Row version | `extractor_version` | `qec_live_v1@{crawl_id}` | latest-wins selection (service.py:44-58); anti-clobber assertions |

### 2.3 The versioning rule (make-or-break)

`_latest_version()` returns the `extractor_version` of the row with **MAX(created_at)** for the artifact; ALL visits and actions are then filtered to `== that version` (service.py:44-58, usage :96-109, :143-151). Therefore, hard rules:

1. **One write = one version string**, all rows inserted in one atomic transaction (a mixed-version or partial write would corrupt selection — fault-injection test R7 proves kill-mid-write leaves zero rows of the new version).
2. **A re-crawl writes a new `qec_live_v1@{new_crawl_id}` version** and instantly becomes current; stale versions remain immutable history.
3. Rows with `source=='missing_page'` are excluded by the loader (service.py:133-137) — QE-Central never writes that value.

### 2.4 The golden substrate contract test (pinned in CI, `platform/qe-central/tests/contract/test_substrate_contract.py`)

Runs against a disposable Postgres at alembic head:
1. Write the golden fixture (`golden_3page_form.json`: 3 visits login→form→confirm, 9 actions with full anchor/after bundles, 1 PII field, 3 screenshots) through `substrate/writer.py`; call `factory_service._load_current_pages_and_actions` in-process against the same DB; assert visits/actions/form_snapshot/anchor/after round-trip **exactly**.
2. Assert constraint names exist: `uq_page_visits_artifact_sequence_version`, `uq_page_actions_visit_version_subaction`; assert `tenant_isolation` policy + `relforcerowsecurity` on page_visits/page_actions/ground_truth_events.
3. Two-version test: write v-old then v-new with staggered created_at → loader returns only v-new (pins service.py:44-58).
4. Navigation-proof test: generated cases from the fixture carry hard `toHaveURL` (source='ground_truth' + confidence 1.0 ≥ 0.9 — pins generator.py:556,581-585).
5. Honesty pins: `GET /playwright` with zero cases → 404 with the honest detail, never a fabricated project (test_factory.py:541-548); viewer-role JWT → 403 on /generate (RBAC, test_factory.py:114-125); `judge_semantic_match` with missing baseline_bytes → uncertain sentinel (semantic_oracle.py:148-167).
6. Frame-path test: every emitted `frame_asset_path` satisfies `safe_frame_asset_path(tenant, path) == path` (database.py:152-158) and `_frame_refs` maps each visit to its screenshot.

Any VKPower refactor that moves these seams fails CI loudly.

---

## 3. THE SIX SUBSYSTEMS

### 3.1 S1 — QE-Central core service (`platform/qe-central/`): substrate writer + Phase-0 REFUSE harness

**Purpose.** Turn an `ExplorationBundle` (from the explorer, or a deterministic fixture in Phase-0) into the §2 substrate so the UNCHANGED factory chain (`/generate → /playwright → /auto-heal/run-config → /verify`) runs on crawler evidence exactly as on video evidence. The Phase-0 harness proves — before any explorer exists — that the whole chain REFUSES honestly when any evidence rule is broken.

**Service shape.** NEW container `qe-central` (FROM nexus-base:dev), FastAPI + SQLAlchemy async + nexus_sdk, NO vision/LLM deps, port 8093, in `docker-compose.qec.yml` on the external `nexus` network. Env: `QEC_DATABASE_URL` (qecentral DB), `NEXUS_DATABASE_URL_SUBSTRATE` (nexus DB, role `qec_substrate`), `NEXUS_JWT_SECRET`, `PLATFORM_API_URL=http://platform-api:8091`, `NEXUS_STORAGE_BACKEND` (MUST match platform-api's so assets are co-readable — docker-compose.yml:522-527), `QE_HARNESS_ENABLED`. Why a container, not a platform-api router: (1) VKPower-untouchable — qe-central crashes/redeploys freely; (2) the seam is the DATABASE (R-1); (3) factory consumed over HTTP with a minted service JWT so every interaction rides the same audited path a human uses.

**Data model** (qecentral DB unless noted; substrate tables per §2):
- `client_apps` (merged registry, R-8): app_id PK, tenant_id (RLS), name, base_url, canonical_host, **creds_blob BYTEA** (KMS envelope, AAD=app_id — mirrors auth_profiles.py:73-75 "never plaintext at rest"), **answer_key JSONB**, env_attestation JSONB, fences JSONB, repo_binding JSONB, schedule JSONB, budgets JSONB, latest_artifact_id, baseline_fingerprint_id, status, timestamps. (Full column detail in §3.5.)
- `qe_explorations`: exploration_id PK, tenant_id, app_id, artifact_id, session_id, status pending|writing|completed|failed|**refused** (first-class — never silently empty), explorer_version, extractor_version (exact string written), stats JSONB {visits,actions,screenshots,redactions}, error, started_at/finished_at.
- `qe_harness_runs`: harness_run_id PK, tenant_id, fixture_name, rule_id R1..R8, expected, observed, verdict REFUSED_CORRECTLY|GREEN_WASH_DETECTED|CHAIN_ERROR|PASS_BASELINE, report JSONB (full HTTP evidence). GREEN_WASH_DETECTED on any rule = deploy-gate failure; honesty results are themselves evidence.

**API surface** (all `/api/v1/qec/…` per R-9):
| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness + DB/GUC self-check + storage backend |
| `POST /api/v1/qec/apps` (subsumes S1's /targets) | register client app; encrypt creds; store answer_key |
| `POST /api/v1/qec/explorations` | THE substrate-write seam. Phase-0: inline `ExplorationBundle` fixture; Phase-1: `{app_id}` dispatches the explorer, which calls back with the same bundle shape. Creates session+artifact, atomically writes substrate, uploads screenshots → `{exploration_id, artifact_id, session_id, extractor_version, stats}` |
| `GET /api/v1/qec/explorations/{id}` | status + stats + honest error/refusal reason |
| `POST /api/v1/qec/harness/run` | run REFUSE matrix rules R1-R8 against fixture variants → `{verdicts[], green_wash_detected}` |
| `GET /api/v1/qec/harness/runs/{id}` | full per-rule request/response evidence |

Consumed factory endpoints (unchanged, service JWT role=manager): `POST /generate` (test_factory.py:181-199), `GET /playwright` (:505-549; honest 404 at :541-548), `POST /auto-heal/run-config` (:2645-2703, RunConfigRequest :750-760) + poll `/playwright/run/{run_id}`, `POST /scripts/{test_id}/verify` (:4545-4614, readiness BLOCKED|DEGRADED|READY). Auditor gate env `NEXUS_AUDITOR_GATE=block` / `NEXUS_AUDITOR_MIN_SCORE=9` already set on platform-api (docker-compose.yml:460-461).

**Key modules:** `main.py` (~150, fail-closed JWT middleware mirroring auth.py:50-61), `config.py` (~70), `db.py` (~90, two engines + verbatim tenant_scoped_session mirror of database.py:110-144), `service_token.py` (~50, mints `{sub:'svc-qe-central', tenant_id, role:'manager'}`), `artifacts/creator.py` (~140, spine :4698 mirror + tenant bootstrap + fingerprint dedup), `substrate/schema.py` (~180, ExplorationBundle pydantic + refusal reasons: monotonic sequence_index, verb/target_kind vocab per 035:61-65, anchor/after presence), `substrate/redact.py` (~90, PII classes → `[REDACTED:class]`, returns redaction counts), `substrate/writer.py` (~280, THE atomic writer per §2), `substrate/assets.py` (~90, per R-5), routers (~320), `harness/rules.py` (~300, R1-R8 as data: fixture-mutation fn + chain step + refusal predicate + green-wash predicate), `harness/runner.py` (~280, fresh artifact per rule, httpx chain drive, verdict classify, non-zero exit on GREEN_WASH), `harness/fixtures/golden_3page_form.json` (~200), `tests/contract/test_substrate_contract.py` (~350, §2.4).

**Verified integration contracts:** all in §2 tables, plus: service JWT decode HS256 with shared `NEXUS_JWT_SECRET`, claims sub/tenant_id/email/role, mutations audited (auth.py:19-63; platform/api/app/config.py:23-24; test_factory.py:108-157; docker-compose.yml:944-945).

**Implementation order:** (1) qec alembic qec_001 + RLS proof-test (insert tenant-A / select tenant-B = 0, WITH CHECK rejects mismatched tenant_id); (2) skeleton + compose + /health; (3) creator.py — artifact visible via platform-api GET (cross-service read under same tenant); (4) schema+redact unit tests; (5) writer.py + §2.4 round-trip test; (6) assets.py + visual_frames; (7) service_token + explorations router → **first cross-product milestone: /generate returns >0 cases from crawler-only evidence**; (8) harness baseline PASS on golden fixture; (9) REFUSE matrix R1-R8 + CI/deploy gate; (10) pinned contract suite in CI; (11) Phase-1 seams (explorer dispatch, creds handoff, synthesis hook).

**The REFUSE matrix (Phase-0 exit criteria):**
| Rule | Mutation | Required honest outcome (pin) |
|---|---|---|
| R1 zero evidence | no page_visits | /generate → 0 cases; /playwright → 404 honest detail (test_factory.py:541-548) |
| R2 ungrounded action | strip anchor bundles | HONEST-10 auditor blocks/downgrades (gate=block, min 9); verdict never PASS |
| R3 dead environment | unreachable base_url on /verify | readiness.status=='BLOCKED' (test_factory.py:4584-4614), never READY |
| R4 missing baseline | no screenshots | semantic oracle returns uncertain sentinel (semantic_oracle.py:148-167), never 'match' |
| R5 cross-tenant | tenant-B JWT on tenant-A artifact | 404 from `_require_artifact` AND raw SQL under B's GUC sees zero rows |
| R6 PII | fixture password field | no raw secret in page_actions.value / form_snapshot / ground_truth_events (pins the storyboard.py:810 leak class) |
| R7 version hijack | concurrent older-version rows; kill mid-write | current selection undisturbed; zero rows of a half-written version |
| R8 auto-heal honesty | step that CANNOT be proven | terminal state has stop_reason, NO clean_run_version — refuse-toward-human, never silent green |

---

### 3.2 S2 — Contained Explorer (`engines/qe-explorer/`): crawler-as-recorder

**Purpose.** Crawl a client's live app from {URL + creds + answer-key} inside a no-egress container as a perfect recorder — it KNOWS every locator/value it touches — emitting confidence-1.0 evidence in EXACTLY the field vocabulary the compiler binds on, while making app mutation physically impossible outside an attested, per-flow-approved submit phase.

**Service shape.** Container `qe-explorer` FROM `mcr.microsoft.com/playwright/python:v1.48.0-jammy` (same-version precedent: infrastructure/docker/Dockerfile.runner:5), FastAPI on :8210, single-flight job lock. Networking per §1.1: `qec-internal` + `qec-egress` (both `internal: true`); browser launched with `--proxy-server=http://qec-egress-proxy:3128`; squid ACL allowlists only the target's registrable domain(s). Shared volume `qec-crawl-storage` (`/work` in explorer, `/qec/crawls` in qe-central). legs-engine untouched (frozen, docker-compose.yml:1184-1269).

**Salvage verdict on `engines/legs-engine/app/explorer/autonomous.py` (read in full, :1-277): SALVAGE NOTHING structural.** No substrate output; locator capture is name/id/type only (:149-157) — not the accessible-name vocabulary the compiler ranks on; `_perform_login` blind-fills and SUBMITS any matching form with zero guard (:186-231, called unconditionally at :86); URL-string visited-set is SPA-blind (:98-101); same-origin check is `href.startswith(start_url)` (:135-137). Reusable ideas only: BFS queue shape.

**Data model.**
- **Crawl manifest** `/work/{crawl_id}/manifest.jsonl` — THE explorer→writer interface, append-only JSONL, `type`-discriminated records: `crawl_meta` {crawl_id, target_url, budgets, guard_version, refuse_pack_version, attestation|null, stop_reason} · `page_state` {state_id, sequence_index, url parts, title, first/last_seen_ms, ax_fingerprint, screenshot path+ts, form_snapshot, form_snapshot_signals, controls:[{role, name, kind, tag, input_type, options, required, disabled, anchor|null, frame_selector, testid, css_hint, value_committed}]} · `action` {state_id, subaction_index, timestamp_ms, verb, target_label, target_kind, value, anchor:{label,kind}, after:{outcome,detail,navigated}, url_changed, to_state, screenshot_after, phase explore|auth|submit} · `guard_event` {kind blocked_method|refused_verb_get|blocked_host|budget_stop|ws_blocked, method, url, rule_id} · `edge`. Field names deliberately 1:1 with service.py:100-176 reads. Passwords: value_committed="" + signals.type="password". Every value passes `nexus_sdk.evidence.pii_detector` before the line is written (models.py:2519-2520 contract).
- **`refuse_pack.yaml`** (versioned data, in repo): irreversible_verbs (delete/purge/pay/transfer/sign/approve/logout/…) each {id, match regex over url-path+query+button-name, applies_to, severity}; mutation_signal_get_rules (GETs that mutate); allow_overrides. Pure data → auditable guard behavior; version stamped into crawl_meta and every guard_event.
- **Written substrate rows:** per §2 (qe-central's writer is a dumb mapper over the manifest).
- **`e2e_auth_profiles`:** storage_state relayed in-memory → extension E3 → save_profile encrypt path.

**API surface.**
| Endpoint | Purpose |
|---|---|
| `POST /api/v1/explore` (auth: X-QEC-Token, RUNNER_TOKEN pattern; 409 if busy) | start crawl: {crawl_id, tenant_id, target_url, credentials|null, answer_key {exact, semantic, regex_rules}, budgets {max_states:200, max_depth:6, max_actions_per_state:30, max_wall_ms, max_requests, rate_per_s:1}, allowed_hosts, phase explore\|submit, submit_approvals, attestation (REQUIRED for submit)} → 202 |
| `GET /api/v1/explore/{crawl_id}` | progress: states/actions/guard_blocks/frontier/elapsed/stop_reason |
| `POST /api/v1/explore/{crawl_id}/cancel` | graceful stop, flush manifest, report partial |
| `POST /internal/crawls/{crawl_id}/complete` (on qe-central, HMAC-signed) | completion callback: manifest path + in-memory storage_state → triggers substrate writer |

**Key modules:** `main.py` (~250, lifecycle + single-flight + HMAC callback), `crawler.py` (~450, AUTH→EXPLORE→[SUBMIT] state machine, priority frontier over state fingerprints, budgets, politeness, resume-from-manifest), `guard.py` (~280 + yaml, fail-closed `context.route('**/*')`: allow GET/HEAD/OPTIONS; abort POST/PUT/PATCH/DELETE unless phase==AUTH (same registrable domain, ≤10 req, ≤30s after login-submit) or phase==SUBMIT (attestation + flow approval + not refuse-pack-matched); mutation-signal-GET aborts; `service_workers='block'`; every block → guard_event; squid enforces HOST under it, guard enforces METHOD — HTTPS is CONNECT-tunneled), `inventory.py` (~200 py + ~180 injected JS: walks DOM incl. open shadow roots + same-origin iframes; accessible name via accname subset label[for] > aria-labelledby > aria-label > wrapping label > placeholder(flagged best_effort); compiler kind vocabulary; options/required/disabled; frame_selector; disambiguation anchor = nearest landmark ancestor computed ONLY on (role,name) duplication, mirroring `_ANCHOR_ROLE`), `fingerprint.py` (~130, sha256 of URL-template + dialog flags + sorted interactive-(role,name,disabled) set — SPA-state-aware, cosmetic-render-invariant), `auth.py` (~220, inventory-matched login, guard AUTH window, login verify = fingerprint changed + no password field + no error live-region, storage_state to memory, re-login on expiry ≤3), `forms.py` (~320, **two-phase**: Phase A fill-anywhere + READ BACK committed values + stop-before-submit recording flow-candidates; Phase B re-drive approved flow, click submit, record grounded after:{outcome:'navigation'}+url_changed), `emit.py` (~160, fsync'd JSONL + PNG store + monotonic clock + source-redaction), Dockerfile/compose/squid.conf (~145 total).

**Verified integration contracts:**
- Compiler binds ONLY by accessible name: `_ladder` getByLabel/getByRole/getByText rungs, kinds text|date|select|link|toggle|button + checkbox/radio via `_refine_kind`; anchors via `_ANCHOR_ROLE`; iframes via observed.frame_selector → frameLocator; field_meta from form_snapshot_signals; **NO testid/css rung exists** → testid/css captured only as `qec.*` diagnostics (compiler.py:297-331, 216-232, 247-280, 174-208, 153-171, 110-119).
- Generator mapping + PROVEN gate + frame_ref window join: per §2 (service.py:100-176, :44-58; generator.py:556, 581-585, :80-115; service.py:61-89).
- Frame serving: eyes GET route + frame-storage mounts (eyes-engine/main.py:1061; docker-compose.yml:788,1369).
- Auth encrypt/inject: auth_profiles.py:34,41-52,55-88,91-116; existing /auth/save pulls ONLY from runner (test_factory.py:3034-3070) — hence E3; run path consumes at :879.
- Compose/network precedent: docker-compose.yml:74-76, :1184-1269; docker-compose.runner.yml:1-21; Dockerfile.runner:1-35.

**Implementation order:** (1) refuse_pack + guard as pure functions + exhaustive unit tests; (2) container + sandbox skeleton — prove from inside: direct internet FAILS, target-via-proxy succeeds, non-allowlisted 403s, nexus services unreachable; (3) inventory vs hand-labeled Aegis :8096 /apply (combobox/slider/accordion/modal/shadow/iframe); (4) fingerprint + frontier on Skyward :8095, manifest golden-stable across two runs; (5) guard wired — tail Aegis access log + squid log: ZERO non-GET/HEAD reached the app; (6) auth + storageState + E3 round-trip (encrypted row present, never plaintext); (7) forms Phase A; (8) **contract test with S1 writer: manifest → rows → /generate → PROVEN navs, fills, frame_refs, auditor green**; (9) forms Phase B submit (refuse without attestation/approval tests); (10) hardening + 45-min soak on both proving grounds.

---

### 3.3 S3 — repo-intelligence engine (`engines/repo-intel/`)

**Purpose.** Turn a client GitLab/git repo into a provenance-tagged App Model (every fact = file:line + verbatim quote), publish an **advisory** crawler seed manifest (directed > blind crawling), produce live-vs-repo drift reports against `page_visits`, and prove value with a directed-vs-blind A/B harness. **Off critical path: every consumer is fail-open** — QE-Central works with zero repo access, just less directed.

**Service shape.** Engine container cloned from legs-engine layout (Dockerfile FROM ${BASE_IMAGE}=nexus-base:dev, COPY app/+main.py, USER nexus — legs-engine/Dockerfile:5-45) WITHOUT Playwright; adds git + py-tree-sitter wheels. `main.py` subclasses NexusEngine, `EngineConfig(engine_name="repo-intel", engine_port=8014)` (8012-8014 verified unused in docker-compose.yml). DB like spine: `nexus_sdk.db.Database(PostgresConfig())` (spine-engine/main.py:1072-1086) — pointed at the **qecentral DB** for its 5 tables (R-7) + a read-only nexus DSN for page_visits. Lives in `docker-compose.qec.yml` (R-10). Heavy/stateful/security-sensitive (client source code) ⇒ never inside platform-api. Structural no-break: writes only its own 5 tables, SELECTs page_visits read-only, serves GETs consumed fail-open.

**Data model (qecentral DB, all RLS):**
- `repo_connections`: connection_id PK, tenant_id, app_id (soft ref), provider gitlab|github|generic_git, base_url, project_path, default_branch, **encrypted_token BYTEA** (EnvelopeBlob.to_bytes(), AAD=connection_id), kek_id, label, status active|revoked|error, last_sync_sha, last_error, timestamps. Clones integration_installations envelope discipline (integrations.py:65-84); refuse-plaintext 503 (auth_profiles.py:71-72 rule); DELETE zeroes ciphertext.
- `app_model_universes`: universe_id PK, tenant_id, connection_id FK CASCADE, deployed_sha, branch, stack_fingerprint JSONB, extractor_versions JSONB, **ceiling_bands JSONB** (published static-rule accuracy ceiling per atom kind — PUBLISHED HONESTY: rules never claim recall above the band; CI grades against the floor), status building|ready|**degraded**|failed|superseded, degraded_extractors, atom_count, timestamps. Unique (connection_id, deployed_sha, md5(extractor_versions)) ⇒ idempotent re-analysis.
- `app_model_atoms`: atom_id PK, tenant_id, universe_id FK CASCADE, kind route|api_endpoint|form|validator_rule|auth_step|feature_flag|data_model|test_intent|nav_edge, value JSONB (kind-normalized), provenance_path/line/sha, **quote TEXT ≤500 (verbatim, secret-scrubbed — the ONLY text the LLM lens may quote)**, extractor, confidence (rule-band, never LLM-scored), source_tier. Indexes (tenant_id,universe_id,kind), (universe_id,kind).
- `crawl_seed_manifests`: manifest_id PK, universe_id FK, manifest JSONB `{version:'seed-v1', ranked_routes:[{path_pattern, criticality_score, criticality_evidence, expected_forms}], auth_recipe:{login_route, field_names, provenance} — NEVER credentials, nav_edges}`; one row per universe (upsert).
- `repo_drift_reports`: report_id PK, universe_id FK, artifact_id (soft ref, validated at query time), summary JSONB, items [{kind route_in_code_unreachable|route_live_not_in_code|form_field_mismatch|validator_untested, code_side, live_side}].

**API surface:** `POST /api/v1/repo-intel/connections` (register, encrypt, `git ls-remote` healthcheck; token never echoed) · `GET /connections` · `DELETE /connections/{id}` (revoke: zero ciphertext + delete workdir) · `POST /connections/{id}/analyze` (async universe build; per-plugin failure ⇒ degraded, never silent partial) · `GET /universes/{id}` (status + ceiling bands + degraded list) · `GET /universes/{id}/atoms?kind=` · `GET /universes/{id}/seed-manifest` (**THE crawler seam; 404 ⇒ crawl blind**) · `POST /universes/{id}/drift` `{artifact_id}` · `GET /universes/{id}/ab-report?directed_artifact_id=&blind_artifact_id=` (directed-vs-blind grading) · `POST /api/v1/repo-intel/{app_id}/diff` `{old_sha,new_sha} → {changed_files, mapped_atoms, stack_supported}` consumed fail-safe-to-full by S5's change detector (contract PROPOSED here — consumer verified in S5 design, endpoint **UNVERIFIED** until built).

**Key modules:** main.py (~250, NexusEngine scaffold + EnvelopeService init cloning knowledge_foundation.py:69-104), connectors/git.py (~200, shallow clone depth=1 --filter=blob:none, token in-memory only + scrubbed from logs, per-tenant workdir cap, sparse-checkout seam), detect/stack.py (~200, manifest-file fingerprints + PLUGIN_CEILINGS), extract/registry.py (~120, plugin Protocol, isolated failures ⇒ degraded), extract/openapi_spec.py (~180), extract/ts_routes.py + express_nest.py (~450, tree-sitter queries: react-router/next/vue/angular routes; express/Nest endpoints; zod/joi/yup/class-validator rules), extract/spring.py (~200, deferred), model/store.py (~250, SDK-Base ORM + GUC helper per integrations.py:99-103), manifest/seed.py (~200, deterministic criticality ranking: auth/payment/txn keywords + validator density + nav fan-in), drift/report.py (~180, pattern normalization :id/{id}/[id]→*), ab/harness.py (~120), lens/llm_lens.py (~150, env-flag OFF; clone of validate_intent_quotes — any non-verbatim quote demotes the whole judgment to 'unverifiable'; NEVER scores, only explains), security/secret_scrub.py (~80), tests (~800).

**Verified integration contracts:** envelope blob format + AAD + refuse-plaintext (envelope.py:84-114; auth_profiles.py:71-76; integrations.py:65-84; knowledge_foundation.py:69-104,249-251) · verbatim-quote demotion (qe_agents.py:249-261, fallback :231-246, evidence :214-228) · RLS template + GUC (apply_ground_truth_events.sql:9-53 shape → now expressed in qec alembic; database.py:110-141; integrations.py:99-103) · engine scaffold/compose/DB (legs-engine/main.py:41-62; legs-engine/Dockerfile:5-45; docker-compose.yml:1184-1194, :1353-1380; spine-engine/main.py:1072-1086; nexus_sdk/auth/__init__.py:247-259) · page_visits read-only seam (models.py:2442-2503) · SDK LLM client (llm/factory.py:34; llm/tiered.py:294,:477) · seed-manifest consumer **UNVERIFIED** (qe-central explorer not yet built; direction pinned by QECentral/docs/QE_CENTRAL_BLUEPRINT.md:46-60, :102-104 + a shared JSON-schema contract test).

**Implementation order:** (1) scaffold + compose (default `docker compose up` does not build it — no-break proof); (2) qec_00x migration + store + RLS isolation test; (3) connections + envelope + git healthcheck (assert ciphertext-at-rest, AAD-swap fails, DELETE zeroes); (4) stack detect + published bands; (5) OpenAPI plugin (malformed spec ⇒ degraded); (6) tree-sitter plugins graded vs hand-authored answer keys — CI fails if recall < published floor or precision < 0.9; planted-secret never surfaces; (7) async /analyze idempotency; (8) seed manifest golden + jsonschema + names-only auth recipe; (9) drift vs synthetic page_visits (exactly 3 expected item kinds); (10) A/B harness synthetic-first, then real seeded-vs-blind crawl pair on Aegis/Skyward; (11) LLM lens adversarial demotion tests; (12) spring.py seam.

---

### 3.4 S4 — Scenario Synthesis + Criticality Registry + Coverage + Approval Gate + Tier Labeler + Human-Touch Meter ("the 1%")

**Purpose.** Turn crawl substrate into a governed regression contract: deterministic journey scenarios; P0..P3 banding via a data-driven criticality registry (fail-up on ambiguity); coverage = enumerable atoms + human-certified invariants with a hash-chained approved-universe baseline and a **shrinkage guard** (a previously-approved atom disappearing raises a P0 possible-deletion gap, never a silent pass); human attention spent ONLY on NEW/CHANGED scenarios (fingerprint diff); every case/suite labeled RENDERS vs BEHAVES so a fill-only suite can never read as behavioral 10/10; every human touch metered into the per-band autonomy KPI. **Zero LLM anywhere** — deterministic + $0, matching the triage/auditor doctrine.

**Service shape.** Routers + services INSIDE `platform/qe-central` (shares Postgres/RLS helper, the substrate writer for materialization, and the service JWT). Files: `routers/scenario_gov.py`, `services/{criticality,synthesis,coverage,approval,tier_label,touch_meter}.py`, tables in qec alembic (R-7). VKPower edits: **ZERO** — read-only substrate/audit_log SELECTs + existing HTTP endpoints.

**Data model (qecentral DB, 9 tables, all RLS):**
- `qec_criticality_registry`: registry_version PK, tenant_id, pack JSONB (signals: {signal_id, band, applies_to url_path|field_label|button_label|action_verb|repo_marker|invariant_link, matchers, rationale}), active, created_by/at. Data-driven clone of `_TRIAGE_RULES` (qe_agents.py:161-170); immutable versions (verdict_events.py:33/50 stamping pattern); seed pack v1 GENERIC (money/auth/PII/destructive/multi-page-submit/invariant-link), domain vocab = optional boost rows.
- `qec_scenarios`: scenario_id PK (uuid5(app_id + journey skeleton) — stable identity), tenant_id, app_id, source_artifact_id, name, journey JSONB (ordered {canonical_host+url_path, verb, normalized target_label, sorted field-label set} — **value-free, locator-free**), criticality_band, criticality_evidence (mirrors qe_agents.py:198-204 shape), registry_version, fingerprint sha256, diff_state new|changed|unchanged|missing, review JSONB (clones case-review block test_factory.py:4206-4234), approved_snapshot JSONB (immutable at approve-time), tier renders|behaves|unlabeled, materialized_artifact_id, status, timestamps. UNCHANGED auto-carries approval (zero touch); only NEW/CHANGED enter the queue; MISSING → shrinkage path.
- `qec_approval_events`: append-only, hash-chained per (tenant, subject_kind, subject_id): chain_hash = sha256(prev + canonical sorted-JSON payload) — byte-for-byte the verdict_events recipe (verdict_events.py:66-113). Approve REQUIRES typed signature (422 otherwise — test_factory.py:4213-4219). carry_forward recorded but NOT a human touch.
- `qec_coverage_atoms`: atom_id (uuid5 of canonical_key), kind route|api_endpoint|form_field|journey_edge|validator, source crawl|repo|answer_key, provenance G_DETERMINISTIC|G_LIVE_CONFIRMED|G_INFERRED, evidence, first/last_seen.
- `qec_certified_invariants`: human-authored P0 statements (e.g. underwriting ceiling), e-sign to certify, requires_disposable_env, linked_scenario_ids — the non-enumerable half; the product EXECUTES and CERTIFIES these, never claims to auto-discover them.
- `qec_universe_baselines`: atoms_hash over sorted canonical_keys, e-signed, hash-chained; the shrinkage guard diffs fresh universes against the LATEST baseline.
- `qec_coverage_gaps`: kind possible_deletion|uncovered_atom|tier_gap|unclassified_fail_up; possible_deletion ALWAYS P0 and blocks the "all green" verdict until adjudicated/waived; waivers ANNOTATE never delete, expire ≤90d (clones WaiverRow semantics verdict_events.py:266-341).
- `qec_case_tiers`: (tenant, artifact, test) PK, tier renders|behaves, evidence (grounded assertions that earned it), computed_at. Lives here because HONEST-10 has no behavioral axis (playwright_auditor.py:37-43 — fixed 5 dimensions) and VKPower stays untouched; suite label = MIN tier.
- `qec_touch_events`: typed touches (scenario_approve, invariant_author, gap_adjudicate, waiver_create, credential_provision, answer_key_edit, heal_approve, case_review, defect_confirm), band, source qec_direct|vkpower_audit, source_ref=audit_log.log_id (dedupe). The autonomy-KPI numerator.

**API surface** (`/api/v1/qec/…`): `POST /apps/{app_id}/universe/compute` (atoms + self-recall vs answer key + baseline diff; shrinkage guard raises P0 gaps) · `POST /apps/{app_id}/universe/approve` (e-sign, chained) · `POST /apps/{app_id}/scenarios/synthesize` (trunk + revisit-branch + per-terminal-form journeys → band → fingerprint → diff → upsert; UNCHANGED carry-forward, zero touch) · `GET /apps/{app_id}/scenarios?state=needs_approval&band=P0` (the approval queue with fingerprint deltas) · `POST /scenarios/{id}/review` (submit|approve|reject|reopen, 422-no-signature) · `POST /apps/{app_id}/invariants` · `POST /scenarios/{id}/materialize` (409 unless approved → substrate-writer produces per-scenario artifact → VKPower POST /generate) · `POST /artifacts/{id}/tier-label` (from GET /rtm: BEHAVES iff ≥1 grounded assertion with oracle_kind ∈ {navigation, outcome-region}; else RENDERS — fail-down) · `GET /apps/{app_id}/coverage` (named gaps, tier distribution, verdict ok|blocked_on_p0_gaps) · `GET /apps/{app_id}/autonomy?cycle_id=` (autonomy % PER BAND, **never averaged**) · `POST /touches` · `GET|PUT /registry/criticality` (PUT = new immutable version).

**Key modules:** criticality.py (~160, clone of qe_agents.py:173-208 loop with DB rules + fail-up-to-P1) · synthesis.py (~320, substrate read + `_split_revisit_branch` idea reimplemented over rows — generator.py:712-729 — + canonical journey JSON + uuid5/sha256 + diff) · coverage.py (~280, universe compute + chain append + shrinkage guard + waiver lifecycle) · approval.py (~220) · tier_label.py (~130) · touch_meter.py (~180, direct writes + audit_log ingest poller deduped on log_id + per-band KPI; optional flywheel mirror via record_label(decision_point='scenario_lifecycle') — ledger.py:110-175, apply_flywheel_labels.sql:21-23) · scenario_gov.py (~300, RBAC clone of test_factory.py:120-124) · models+SQL (~600) · `tests/test_vkpower_contracts.py` (~200 — pins /rtm keys, oracle_kind vocabulary, review vocabulary + 422, audit_log columns, PageVisit/PageAction columns, /generate response keys).

**Verified integration contracts:** marker-table classifier (qe_agents.py:161-208) · /generate incl. reapply_approved-last (test_factory.py:181-199, :110-124; proposer.py:44, 576-616 `_APPROVED_KEY` survive-regenerate) · case-review lifecycle (test_factory.py:4175-4259) · waivers (verdict_events.py:263-341; test_factory.py:4712-4747) · hash chain (verdict_events.py:66-124) · delivery-gate/HONEST-10 attachment points (test_factory.py:566-669; playwright_auditor.py:37-43) · /rtm shape + oracle_kind vocabulary navigation|value-oracle|value-presence|state|outcome-region (test_factory.py:3638-3659; provenance.py:59-68, 102-166) · substrate reads (models.py:2449-2503, 2571-2626) · audit_log ingest (models.py:129-146; test_factory.py:127-157) · RLS/DDL (database.py:110-144; apply_flywheel_labels.sql:1-67) · `materialize_scenario(journey, app_creds_ref) → {artifact_id}` — **UNVERIFIED (to-be-negotiated contract with S1/S2; pattern refs QECentral/docs/STARTING_POINT.md:52, 91-98 + spine-engine/main.py:4698)**.

**Implementation order:** (1) DDL+ORM+RLS test; (2) VKPower contract pin suite; (3) registry + evaluate() graded vs hand-labeled keys (no-match → P1 fail-up, never P2/P3); (4) synthesis + fingerprint goldens (value/locator changes DON'T move the fingerprint; page/field changes DO; identical re-crawl ⇒ 100% unchanged, zero queue entries); (5) universe + baseline + shrinkage (delete one atom → exactly one P0 gap, verdict flips blocked); (6) approval endpoints (+422, snapshot, chain, exactly one touch; carry-forward = zero touches); (7) materialize→generate bridge (stub artifact first, 409-unless-approved); (8) tier labeler truth table (fill-only → RENDERS; grounded navigation → BEHAVES; suite = min; unproven-everything → RENDERS w/ empty evidence); (9) touch meter + audit ingest + per-band KPI (refuses averaged output); (10) coverage scorecard + full fixture E2E measuring the Phase exit numbers.

---

### 3.5 S5 — Control Plane: app registry, fleet scheduling, change-triggered incremental regression, cost metering

**Purpose.** The thinnest orchestration making 1000 clients / 10,000 apps economical and polite: nothing runs without a change event / schedule tick / human trigger; a cheap probe + repo-SHA diff selects only affected cases; every cycle is admission-controlled per tenant, rate-capped per customer host, budget-metered per unit; every skipped case carries a time-bounded, age-labeled verdict — **never a silent green**. Drives the UNCHANGED factory purely over HTTP.

**Service shape.** Modules inside `platform/qe-central` (R-12): 4 routers + 1 lifespan-hosted asyncio cycle-driver daemon (sentinel_daemon pattern: env-gated interval, try/except so a cycle failure never kills the loop, hosted via `asyncio.create_task` in lifespan — qe_agents.py:136-156; platform/api/main.py:141, :223-228; NOT `@app.on_event`, ignored when lifespan= is set). New dep: croniter. Env knobs `QEC_CYCLE_TICK_SECONDS / QEC_MAX_GLOBAL_CYCLES / QEC_MAX_PER_TENANT_CYCLES` (mirrors NEXUS_HEAL_MAX_* at heal_scheduler.py:157-162).

**Data model (qecentral DB per R-7):**
- `client_apps` (merged, R-8): app_id PK · tenant_id · name · base_url · canonical_host (politeness key) · creds_blob BYTEA (envelope, AAD=app_id) · answer_key JSONB · env_attestation JSONB {attested_by, attested_at, env_kind prod|staging|disposable, reset_procedure, expires_at} (**fail-closed**: submit-tier requires disposable AND unexpired) · fences JSONB {allowed_hosts, blocked_path_globs, irreversible_verbs_extra, max_rps, max_crawl_workers, blackout_windows, allow_submit} · repo_binding JSONB · schedule JSONB {manual|interval|cron} · budgets JSONB {max_browser_seconds/llm_tokens/substrate_rows/wallclock per cycle, monthly caps} · latest_artifact_id · baseline_fingerprint_id · status. Verified NOT duplicating AppInstanceRow (models.py:960-999 is per-artifact evidence grouping, not a client registry). Fences are DATA consumed by the explorer job manifest + run dispatcher.
- `app_cycles`: trigger {webhook_repo, schedule, probe_drift, manual, full_floor} · state machine pending→probing→selecting→crawling?→generating→running→healing→verifying→done | failed | **budget_stopped** (terminal HONEST state) | blackout_deferred · selected_scope {mode full|incremental, changed_atoms, selected_test_ids, carried_forward:[{test_id, verdict_run_id, verdict_age_cycles}], selection_reason} · honest_gaps {uncomputable_pages_treated_changed, vanished_pages_possible_deletion} · result. **One ACTIVE cycle per app via partial unique index** WHERE state NOT IN (terminal states) — restart-safe without advisory locks.
- `change_events`: source {repo_sha, probe, manual, schedule_floor} · payload · dedupe_key UNIQUE(app_id, dedupe_key) · processed_cycle_id. Producers write, driver consumes — decouples burstiness; unprocessed events coalesce into one cycle.
- `app_fingerprints`: graph_hash + page_fingerprints {page_key → {structural_hash, control_fps, last_verified_at}}; baseline seeded from `GET /test-factory/{artifact_id}/journey-graph` (test_factory.py:1333-1363; page_key :1354, control_fingerprint :1360). **VERIFIED CAVEAT:** `services/diff_and_heal/journey_graph.py` is ABSENT from the local repo (git ls-files confirms; VM-only per repo↔VM divergence) — sync or vendor before this table can be seeded locally.
- `cost_ledger`: append-only units {browser_seconds, llm_tokens_*, substrate_rows, wallclock_seconds, runner_runs}, source_ref, unit_cost_usd NULLABLE (publish RAW UNITS first — never invent dollars). browser_seconds from E2ETestRunRow.duration_ms (models.py:2005; ingest test_runs_feedback.py:189).
- Token buckets + admission state: **IN-MEMORY ONLY** (deliberate): vendored HealScheduler pattern (global + per-tenant caps + round-robin — heal_scheduler.py:53-149) + per-canonical_host token bucket (rate=fences.max_rps, burst 2×) + per-host mutex. Buckets start EMPTY on restart — restart can never cause a politeness burst.

**API surface:** `POST /api/v1/qec/apps` · `GET/PATCH /apps/{id}` (pause/resume, fences/budgets — audited) · `POST /apps/{id}/attest-env` · `POST /webhooks/gitlab/{app_id}` (X-Gitlab-Token per-app secret → change_events, 202) · `POST /apps/{id}/cycles` `{mode:auto|full|probe_only}` (409 if active) · `GET /apps/{id}/cycles`, `GET /cycles/{id}` (state, selected vs carried-forward WITH verdict ages, honest_gaps, cost snapshot) · `GET /apps/{id}/cost?window=&group_by=` · `POST /cost/entries` (internal, service-JWT: explorer/synthesis self-report) · `GET /scheduler/state` (mirrors HealScheduler.snapshot(), heal_scheduler.py:141-149) · `GET /apps/{id}/fingerprint`.

**Key modules:** controlplane routers (~700) · scheduling/admission.py (~250, vendored — platform.api.app not importable across containers; SDK-lift is a future seam) · cycle/driver.py (~400, tick → due apps → blackout → admission → state machine: probe → detect → select → [crawl changed scope → substrate → /generate] → /playwright/run(test_ids=selected) → poll → /auto-heal/run-config on failures → /verify per changed script → /triage rollup → cost + budget enforcement at every phase boundary; SlaBudget wall-clock guard per cycle, heal_scheduler.py:171-206 pattern) · cycle/change_detector.py (~200, union of repo-diff + probe fingerprint diff, **fail-safe-to-CHANGED**; vanished → possible_deletion gap; uncomputable → CHANGED) · cycle/selector.py (~200, select iff any step page_key ∈ changed OR control_fp ∈ changed OR case is P0 (criticality floor: P0 always runs) OR mode=full; non-selected → carried_forward + verdict_age + max-carry TTL; reuses the affected_scenarios PATTERN — diff.py:441-521 — NOT the /artifacts/diff endpoint which 422s without video scenes, diff_and_heal.py:188-203) · cycle/fingerprints.py (~150) · cost/meter.py (~200, polls GET /runs/{run_id} — never SQL into nexus for runs) · clients/platform_api.py (~200, the single place the endpoint-shape contract test pins) · skeleton+alembic (~700) · scripts/qec_budget_gate.py (~80, CI exit-nonzero on budget drift).

**Verified integration contracts:** `POST …/playwright/run` RunConfigRequest w/ test_ids filter (test_factory.py:2813-2873, filter :2837-2840) · run polling (:2945, job shape :2864-2869) · factory chain (:181, :2645, :4545, :3603, :428; triage qe_agents.py:173) · E2ETestRunRow + ingest (models.py:1977-2022, :2052; test_runs_feedback.py:189-210) · journey-graph (test_factory.py:1333-1363 + the VM-divergence caveat above) · affected_scenarios pattern (diff.py:441-521; guard diff_and_heal.py:165-216) · HealScheduler/SlaBudget (heal_scheduler.py:53-206) · daemon pattern (qe_agents.py:136-156; main.py:141,223-228) · service JWT precedent ("CI generates a long-lived service token" — test_runs_feedback.py:194-195; auth.py:19-63,66-123) · RLS (database.py:111-142) · deploy substrate (docker-compose.yml:98-99, :1185-1190, :427/:943) · repo-intel /diff + explorer probe API — **both UNVERIFIED (sibling contracts proposed here, pinned as JSON-schema fixtures)**.

**Implementation order:** (1) qecentral DB/role bootstrap + skeleton + negative grant test (qec cannot SELECT nexus DB); (2) qec alembic tables + RLS + partial unique index; (3) registry routers + fail-closed attestation; (4) cost meter standalone vs a real historical run_id + budget gate; (5) scheduling primitives + fairness suite; (6) cycle-driver walking skeleton mode=full on a Phase-0 fixture artifact (budget breach ⇒ budget_stopped, never partial done); (7) fingerprint store (**PRECONDITION: journey_graph.py sync/vendor**); (8) change detector (webhook dedupe; stubbed probe payloads; repo-intel fail-safe); (9) selector + carried-forward + PLANTED DELETION fixture; (10) schedule tick + blackouts + coalescing; two-tenant zero-starvation co-gate; (11) exit measurement: incremental ≥10× cheaper than full re-crawl on Aegis/Skyward, published from the ledger; endpoint-shape contract test to CI.

---

### 3.6 S6 — VKPower additive extensions + the no-break guarantee

**Verification outcome.** Of 7 candidate VKPower changes, only **THREE** touch VKPower code (§4). Candidates resolved to zero-modification: (a) create-artifact endpoint → direct-write (R-1); (b) screenshot attach → existing ArtifactStore + visual_frames plumbing (R-5); (c) GT overlay → bypassed, journal-only (overlay unreachable for frameless artifacts, page_visit_extractor.py:1965-1976); (d) no-frames lift → NOT NEEDED (factory reads tables — service.py:92-178; frameless early-return additionally cannot clobber: `visits_written=0, stale_visits_deleted=0` literal at page_visit_extractor.py:1881-1897); (g) auditor behavioral tier → QE-Central wrapper (`qec_case_tiers`), because `score_spec(spec_text, steps, evidence)` is a pure 3-arg function (playwright_auditor.py:139) and /verify already emits CERTIFIED-EVIDENCED when table-loaded actions exist (test_factory.py:4639-4642).

**Modules:** the three patches in §4 (inside platform-api, standard compose build + force-recreate lineage) + qe-central's `substrate/writer.py` and `substrate/assets.py` (§3.1) + the four pin-suite test files (§7). Key additional verified contracts: composer surface gating + freshness clobber path (surface_prefs.py:96-110, :153-157; composer.py:266-280, :770-772, :905-1015; storyboard.py:350-378) · semantic-oracle baseline flow (test_factory.py:3662-3695, :3748; eyes-engine/main.py:1061-1129; run_screenshots.py:44-54, :126-148; semantic_oracle.py:148-167) · GT ingest route pair — redacting sibling vs raw leak (storyboard.py:473-550 vs :722-819, raw at :810) · auto-heal/triage touch neither frames nor extractors (test_factory.py:2645-2703, :879; qe_agents.py:159-208; auth_profiles.py:41-109).

---

## 4. VKPOWER ADDITIVE EXTENSIONS — the definitive list (3 total)

| ID | What | Insertion point | Gate | Fail-open rule | Contract test |
|----|------|-----------------|------|----------------|---------------|
| **E1** | PII + privilege parity fix on GT-events ingest: this route stores `value=e.value` **RAW** and lacks the env/role gates its sibling has | `platform/api/app/routers/storyboard.py:748-819` — hoist `_redact_value` (currently a closure at :508) to module scope; add the sibling's `NEXUS_GROUND_TRUTH_INGEST_ENABLED` 403 gate + admin/manager role gate (pattern :496-499); redact at :810 | env `NEXUS_GROUND_TRUTH_INGEST_ENABLED` (existing flag, default unset ⇒ 403) | `_redact_value` itself fails open: detector unavailable ⇒ source value stands, never raises. Video path byte-identical — recordings never call this route | `test_qec_contract_gt_ingest.py`: T5 flag ON + admin: SSN input stored redacted; T6 flag unset: BOTH ingest routes 403; T7 viewer role: 403 |
| **E2** | Composer live_crawl guard (anti-clobber Belt-2): without it, a user flipping surfaces back ON (PUT /surfaces → `_derive_in_background`, storyboard.py:373-377) makes `_needs_page_visit_extractor` see `qec_live_v1@…` ≠ target version (composer.py:266-280) and the pixel extractor would delete+rewrite qec rows | `platform/api/app/services/storyboard/composer.py` in `_derive`, immediately after surface resolution (:770-772): one SELECT of `CanonicalArtifactRow.source_type` in try/except; if `== 'live_crawl'` force `storyboard_on=False`, `pages_forms_on=False`, log `composer.live_crawl_vision_skipped`. Cheap deterministic steps still run (harmless, keeps panel UI alive) | keyed on `source_type=='live_crawl'` literal only | any exception reading source_type ⇒ flags untouched ⇒ video behavior byte-identical (video artifacts never carry 'live_crawl') | T8 unit: for source_type ∈ (None,'','video','upload') effective flags == resolved surfaces (byte-identical decision path); 'live_crawl' ⇒ both False; raised exception ⇒ untouched. Plus the adversarial end-to-end: live_crawl artifact WITH frames + toggle flipped ON ⇒ qec rows still carry `qec_live_v1@…` after get_storyboard |
| **E3** | Auth import endpoint: crawler-captured storageState → encrypted auth profile (existing `/auth/save` at test_factory.py:3034 pulls ONLY from `runner_client.auth_capture_save`) | `platform/api/app/routers/test_factory.py` adjacent to :3034: `POST /api/v1/test-factory/{artifact_id}/playwright/auth/import` `{storage_state, label?}`; delegates verbatim to `auth_profiles.save_profile` with `request.app.state.envelope_service` (:3044 accessor pattern, :3063-3066 body pattern) | 403 unless env `NEXUS_QEC_AUTH_IMPORT_ENABLED` truthy (mirrors storyboard.py:496-497 gate pattern); 403 unless role admin\|manager; 503 if envelope None (never plaintext, :3045-3050); 422 empty/oversize via save_profile ValueError (:67-70) | flag unset (default) ⇒ 403 ⇒ zero behavior change for every existing deployment | `test_qec_contract_auth_import.py`: T13 flag unset → 403; T14 envelope None → 503 + no row; T15 stub envelope: save → get_storage_state round-trip → auto-heal `_run_storage_state` path picks it up |

**Explicitly rejected/deferred:** `POST /api/v1/artifacts` create endpoint (rejected, R-1) · no-frames extractor lift (not needed) · GT-overlay wiring for crawls (bypassed) · **E4** `evidence_tier` key in /verify (deferred seam: if ever promoted, add key ONLY when `source_type=='live_crawl'` — omit-for-video keeps the response byte-identical; insertion at the verdict dict, test_factory.py:4632) · per-request rps throttling inside compiled Playwright suites (deferred VKPower compiler extension; v1 proxy = workers-cap + one-cycle-per-host).

---

## 5. END-TO-END DATA FLOW — one worked trace

Client submits `https://portal.acmelife.example` + creds + answer-key (+ GitLab repo).

1. **Register.** `POST /api/v1/qec/apps` → `client_apps` row `app_id=a1…`, creds envelope-encrypted (AAD=app_id), answer_key stored, fences `{max_rps:1, allowed_hosts:['acmelife.example']}`.
2. **(Optional) repo intel.** `POST /repo-intel/connections` + `/analyze` → universe `u1` (atoms w/ file:line quotes, ceiling bands) → seed manifest `{ranked_routes:[{path_pattern:'/transfer', criticality_score:0.94, …}], auth_recipe:{login_route:'/login', field_names:['Username','Password']}}`.
3. **Crawl.** qe-central dispatches `POST qe-explorer:8210/api/v1/explore` (crawl_id `c7f2`, seed manifest fetched fail-open — 404 ⇒ blind). Explorer logs in (guard AUTH window: same domain, ≤10 req, ≤30s), explores under the fail-closed guard (zero mutating requests escape; every abort → guard_event), inventories controls by accessible name, fills forms Phase-A from the answer key, reads back committed values, screenshots each state, emits `manifest.jsonl`, calls back `/internal/crawls/c7f2/complete` with in-memory storageState.
4. **Substrate write** (one transaction, GUC set). Artifact `art_c7f2` (`source_type='live_crawl'`), session (`session_type='live_crawl'`), surface_prefs all-vision-off. Example rows:
   - `page_visits`: `{sequence_index:1, location:'https://portal.acmelife.example/transfer', url_host:'portal.acmelife.example', url_path:'/transfer', canonical_host:'acmelife.example', first_seen_ms:41200, last_seen_ms:58900, source:'ground_truth', extraction_confidence:1.0, extractor_version:'qec_live_v1@c7f2', form_snapshot:{'Amount':'250.00','To account':'Savings …1234','SSN':'[REDACTED:ssn]'}, form_snapshot_signals:{'Amount':{'type':'text','required':true}, 'To account':{'type':'select','options':['Checking …9876','Savings …1234']}}}`
   - `page_actions`: `{subaction_index:2, verb:'select', target_label:'To account', target_kind:'select', value:'Savings …1234', confidence:1.0, automation_ready:true, evidence_signals:{'anchor':{'label':'Transfer funds','kind':'region'}, 'after':{'outcome':'value_committed','navigated':false}, 'url_changed':false, 'qec':{'role':'combobox','frame_selector':'','testid':'xfer-to'}}}`
   - `visual_frames`: `{frame_index:4, timestamp_seconds:47.3, frame_asset_path:'t_acme/s_9d2/c7f2_frames/frame_00004.png'}` (PNG uploaded to the eyes ArtifactStore namespace).
   - storageState → `POST …/playwright/auth/import` (E3) → encrypted `e2e_auth_profiles` row.
5. **Synthesize + govern (S4).** Journeys → scenario `sc_transfer` banded **P0** (matched signals: money-path `/transfer` + multi-page submit; registry `crit-v1-…`). NEW ⇒ approval queue; human approves once with typed signature ⇒ chained `qec_approval_events` row + one `scenario_approve` touch. Universe computed; human approves baseline (atoms_hash chained).
6. **Generate.** `POST /test-factory/art_c7f2/generate` (service JWT role=manager) → cases from table evidence; navigation steps **PROVEN** (source='ground_truth', conf 1.0 → hard `toHaveURL`, generator.py:556,581-585).
7. **Compile + certify.** `GET …/playwright` → deterministic zero-LLM compile (getByLabel/getByRole from recorded names; frameLocator from frame_selector; anchors via `_ANCHOR_ROLE`); HONEST-10 delivery gate scores the zip (`vkpower-audit-report.json`); `/verify` → deterministic rubric + readiness probe + `certification_level: CERTIFIED-EVIDENCED` (table-loaded actions, test_factory.py:4639-4642). S4 tier-labeler reads `/rtm` → case has a grounded navigation assertion ⇒ **BEHAVES**.
8. **Run + heal.** Cycle driver (S5) `POST …/auto-heal/run-config {test_ids:[…], base_url, autonomous:true}` → 15-rung healer; storage_state auto-injected from the E3 profile (test_factory.py:879). Unprovable step ⇒ terminal `stop_reason`, no clean_run_version (R8 honesty).
9. **Verify + dossier + triage.** `/verify` per changed script → verdict_events chain + dossier; `/triage` classifies failures product/script/env (qe_agents.py:173).
10. **Report.** Cycle closes `done` with cost rollup (browser_seconds from run duration_ms); coverage scorecard names every uncovered atom; autonomy KPI: P0 band touched once (the approval) — everything else autonomous. Next GitLab push → webhook → change_events → incremental cycle selects only cases whose page_key/control_fp changed; unselected cases carry age-labeled verdicts.

---

## 6. BUILD ORDER (Phase 0-5)

**Phase 0 — Substrate + honesty (THE FIRST PR = pin suite + harness).**
Modules: `platform/api/tests/test_qec_contract_video_frozen.py` (T1-T4 golden hashes, written and green against CURRENT HEAD **before any patch**); qec alembic qec_001; qe-central skeleton (main/config/db/auth/service_token); artifacts/creator; substrate/{schema,redact,writer,assets}; harness/{rules,runner,fixtures}; extensions E1/E2/E3 + their test files; docker-compose.qec.yml (qe-central only).
Exit metrics: pin suite green pre- and post-patch; golden fixture → `/generate` >0 cases with PROVEN navs; REFUSE matrix R1-R8 all `REFUSED_CORRECTLY`, `PASS_BASELINE` on golden; one real video artifact re-processed on the VM with `/generate` + `/playwright` output hashes identical pre/post-branch.

**Phase 1 — Contained explorer.**
Modules: engines/qe-explorer/* (guard→sandbox→inventory→fingerprint/crawler→auth→forms-A→emit), qec networks + squid, explorer dispatch in qe-central.
Exit: crawl of Aegis/Skyward → substrate → factory chain green; **zero mutating requests escaped** (app access log + squid log); inventory fidelity vs hand labels; auditor/HONEST-10 gate green on crawl-only evidence; encrypted auth round-trip.

**Phase 2 — repo-intel + directed crawling.**
Modules: engines/repo-intel/* (connections→stack→plugins→analyze→seed→drift→A/B), explorer seed-manifest consumption (fail-open).
Exit: per-plugin recall ≥ published ceiling-band floor + precision ≥0.9 vs answer keys (CI-graded); directed-vs-blind A/B on proving grounds shows measured coverage delta; planted secrets never surface.

**Phase 3 — Scenario governance ("the 1%" machinery).**
Modules: services/{criticality,synthesis,coverage,approval,tier_label,touch_meter} + scenario_gov router + S4 tables.
Exit: criticality precision vs hand-labeled keys published; identical re-crawl ⇒ zero approval-queue entries; deleted approved atom ⇒ exactly one P0 possible_deletion gap + `blocked_on_p0_gaps`; fill-only suite labeled RENDERS (cannot be gamed by a green 10/10); per-band autonomy KPI emitting.

**Phase 4 — Control plane + incremental regression + cost.**
Modules: controlplane/* + clients/platform_api + webhooks + cost meter + budget gate. PRECONDITION: journey_graph.py VM-sync/vendor.
Exit: incremental cycle cost ≥10× below full re-crawl across N synthetic change events, flat as app count scales; two tenants on a rate-limited target with zero admission-starvation SLA misses; budget breach ⇒ `budget_stopped`, never partial `done`; planted deletion ⇒ P0 gap, cases never silently carried.

**Phase 5 — Fleet autonomy + submit tier.**
Modules: forms Phase-B submit flows (attestation + per-flow approval), certified-invariant execution on disposable envs, autonomy trend reporting, scale hardening (queue seams, sparse-checkout, second-DB cutover trigger).
Exit: 99%/1% measured per criticality band per cycle on ≥1 design-partner app; every human touch typed and counted; submit-tier refusals proven (no attestation ⇒ refuse).

---

## 7. TESTING & CI

1. **Golden-hash video pins (Phase-0, first PR):** T1 compile_project(fixture) byte hash; T2 generate_demonstrated_test_cases on frozen video-shaped fixture (source='url_regex'); T3 score_spec 3-arg signature + frozen key-set/overall ("same suite in → byte-identical project out" is the design promise, test_factory.py:518-521); T4 frameless extractor: `visits_written=0, stale_visits_deleted=0`, pre-seeded rows survive (page_visit_extractor.py:1881-1897).
2. **Substrate contract suite (§2.4)** — qe-central CI vs disposable Postgres at head.
3. **Extension gate-condition tests** (T5-T8, T10-T15 per §4) — every OFF-state asserted byte-identical.
4. **REFUSE harness R1-R8** — CI job AND recurring VM deploy-smoke gate; verdicts persisted to `qe_harness_runs` (honesty is itself auditable evidence); any GREEN_WASH_DETECTED fails build + deploy.
5. **Explorer safety suite:** guard table-tests (verb × method × phase); egress proof (container cannot reach anything but allowlist); mutation-proof via target access log + squid log; accname fixtures; fingerprint invariance goldens.
6. **Answer-key-graded measurement (measure-first doctrine):** repo-intel plugin recall/precision vs hand-authored keys (fail below published floor); criticality precision vs hand labels; directed-vs-blind A/B deltas — all CI-published numbers, never self-reported.
7. **VKPower endpoint-shape contract tests** (S4's `test_vkpower_contracts.py` + S5's typed-client pin): /generate, /test-cases, /playwright(+run/status), /auto-heal/run-config, /verify, /triage, /rtm (+oracle_kind vocabulary), /runs/{id}, /journey-graph, review vocabulary + 422-no-signature, audit_log columns — upstream drift fails in QE-Central's CI, not production.
8. **RLS/tenancy:** insert-A/select-B=0 on every new table; `qec` role negative grants on nexus DB; R5 harness rule run under role `nexus_app` so the check is real, not vacuous.
9. **Honesty/red-team gates per phase:** Phase-0 = R1-R8; Phase-1 = mutation-proof + refuse-without-attestation; Phase-3 = shrinkage + fail-up + RENDERS-cannot-be-gamed; Phase-4 = budget_stopped + verdict-age rendering + cost meter can only under-count (uncorrelated run ⇒ `unmetered_run` gap flag, never invented browser_seconds).
10. **Cross-subsystem JSON-schema fixtures:** ExplorationBundle/manifest schema (S1↔S2), seed-v1 manifest (S2↔S3), repo-diff + probe contracts (S5↔S2/S3) — siblings build to pinned schemas before the counterpart exists.
11. **Operational byte-identical gate before every merge to the deploy branch:** one real video recording re-processed on the VM stack, `/generate` + `/playwright` hashes diffed pre/post.

CI additions: `scripts/qec_budget_gate.py` (exit non-zero on cost-per-cycle over budget), harness runner exit code wired as deploy gate after every compose rebuild.

---

## 8. OPEN DECISIONS FOR THE FOUNDER

1. **Answer-key PII policy:** store client-supplied test values raw behind RLS+KMS with a signed synthetic-data attestation (needed for replay fills) vs redact + keyed vault. Blocks nothing in Phase 0 (fixture data), decide before first real client crawl.
2. **Submit-phase shape:** separate targeted "flow execution" job after scenario approval (recommended) vs inline same-crawl submit — affects S4's approval UX and S2's Phase-B design; decide before Phase-1 step 9.
3. **P0 double sign-off:** scenario-level e-signature only, or additionally per-case review for P0/invariant-linked (double touch cost)? Recommended: scenario-only for P1-P3, both for P0 — design-partner call.
4. **Carry-forward TTL + full-crawl floor period** (weekly recommended): a founder-approved honesty parameter — it bounds the staleness of every "green" shown.
5. **Rate card:** publish raw units only at launch (recommended — no invented dollars) vs configure `unit_cost_usd` day one.
6. **journey_graph.py / heal_calibration.py repo↔VM divergence:** sync the VM commit into the local repo vs vendor a ~50-line builder — hard blocker for Phase-4 step 7; the local repo currently has a router importing a module that does not exist locally.
7. **Webhook ingress on-prem:** proxy `POST /webhooks/gitlab/{app_id}` through platform/gateway (new gateway route — remember the missing `/api/v1/test-runs` route incident) vs customers' GitLab reaching qe-central:8093 directly.
8. **DB role hardening per environment:** dev compose superuser (GUC discipline still enforced) vs `nexus_app`/`qec_substrate` least-privilege in staging/prod — recommended own roles; also defines when the R5 harness rule runs under the restricted role in CI.
9. **KMS on the current GCP VM:** memory says KMS re-provision needed on new GCP; if `NEXUS_KEK_PROVIDER=local`, repo-intel/qe-central must refuse client tokens/creds in any `NEXUS_ENV != development` — confirm env story before Phase-1/2 handle real secrets.
10. **Postgres backup (PITR/pgBackRest) on nexus-postgres:** flagged highest live risk; one setup covers both nexus and qecentral DBs — prerequisite before client #2.
11. **E1 hardening scope:** minimal (redaction only) vs full gate parity on the `:748` route — full parity recommended, but confirm no external caller uses that route with the flag unset (repo grep found no server-side callers; the instrumented recorder is the only known client).
12. **WebSocket policy during explore:** block entirely (may break SPA data loads) vs allow-to-target + log (recommended v1) — revisit after the first real crawl.
13. **cycle_id semantics** (per crawl vs per scheduled regression run) — touch meter treats it as opaque until the control plane fixes it.
14. **MFA/CAPTCHA logins:** v1 fallback = VKPower's interactive noVNC capture (test_factory.py:2996) feeding storageState to the explorer — needs a qe-central UX hook.
15. **Named P0 approval-gate owner** (who blesses payment/underwriting scenarios) — blocks nothing technically, blocks the autonomy KPI's meaning.

---
*End of document.*
