# Persona × Environment Matrix — Engineering Implementation Plan

**Status:** FOUNDER-REVIEW v1.0 (2026-07-26)
**Companion to:** `PERSONA_ENVIRONMENT_MATRIX_PLAN.md` (the design; client
answers + behavior-class amendment locked in commits `6ec5d3c`, `6f7c527`).
**Rule of the build:** every phase ships production-complete — deployed,
live-verified with a falsifiable exit proof, committed — before the next
begins. No stubs, no examples, generic only. All changes ADDITIVE; the frozen
VKPower factory is untouched.

---

## 0. Engineering standards (apply to every phase)

Reused patterns, all proven in this codebase:

| Concern | Pattern to reuse | Existing exemplar |
|---|---|---|
| Secrets at rest | EnvelopeService, AAD-bound blobs | `services/test_factory/auth_profiles.py` |
| Migrations | idempotent SQL + RLS + explicit grants | `scripts/apply_heal_events.sql`, `apply_run_reports.sql` |
| Audit | Part-11 hash chain events | `heal_evidence.record_heal_event` |
| Concurrency | bounded locks, TTL, finally-release | `_RECOVERY_RUNNER_LOCK`, cert debounce |
| Run env injection | secrets ride env vars, never bundles | form-login `NEXUS_LOGIN_*` |
| Honest degradation | fail-closed to UNVERIFIED/BLOCKED, never green | evidence_report state machine |
| Deploy | LF-normalize → scp → docker cp → restart → boot-scan | every deploy this month |
| Portal build | PowerShell `tsc && vite build`, POSIX tar path, backup dist | Evidence tab deploys |
| Tests | behavior tests + source-pins; assert config LINES not substrings; measure pages not bytes | `tests/test_evidence_report_*.py` |

Two ID gotchas that already bit us — carry them forward: a run has TWO ids
(ingest-minted `run_id` vs runner-job `ci_run_id`) — every new join accepts
both; ingest mints its own run ids — correlate by artifact where needed.

---

## Phase P0 — Schema, registries, APIs (the once-only foundation)

### DB (one migration: `platform/api/scripts/apply_persona_matrix.sql`)
Seven tables, all `tenant_id`-scoped, RLS policies, app-role grants
(SELECT/INSERT/UPDATE; DELETE only where lifecycle demands — reservations):

1. `login_recipes` — pk `recipe_id`; `app_id`, `version` INT, `steps` JSONB,
   `slots` JSONB, `source` VARCHAR, `verified_at`, `verified_env`,
   `created_at`. Unique `(tenant_id, app_id, version)`.
2. `personas` — pk `persona_id`; `app_id`, `name`, `description`,
   `traits` JSONB, `behavior_class` VARCHAR NULL, `status`,
   `is_recording_baseline` BOOL.
3. `persona_credentials` — pk `(persona_id, environment_id)`;
   `slot_values` BYTEA (EnvelopeBlob, AAD = `persona_id|environment_id`),
   `label` (non-secret), `last_verified_at`, `verify_status`.
4. `persona_expected_values` — pk `(persona_id, environment_id, value_key)`;
   `expected_value` TEXT, `source` VARCHAR.
5. `environments` — EXTEND the existing qe-central `app_environments`
   (ALTER ADD COLUMN, additive): `posture` VARCHAR DEFAULT 'full',
   `data_epoch` DATE NULL, `verified_at`, `recipe_id` NULL,
   `is_production_like` BOOL DEFAULT false.
6. `value_classifications` — pk `(artifact_id, value_key)`; `class` VARCHAR,
   `evidence` VARCHAR, `observed_examples` JSONB (capped), `created_at`.
7. `persona_reservations` — pk `(persona_id, environment_id)`; `run_id`,
   `expires_at` (TTL; acquire = upsert-if-expired, release at ingest).

### Backend — new modules (`platform/api/app/services/test_factory/`)
* `persona_registry.py` — CRUD for personas/credentials/expected-values.
  Credential writes envelope-encrypt; reads decrypt ONLY into run dispatch
  (never serialized to any response). Every write + every dispatch-read emits
  a Part-11 audit event (`persona_credential_written` / `_used`).
* `login_recipes.py` — recipe model, versioning (new capture ⇒ version+1,
  never overwrite), slot validation (`secret|totp_seed|fixed_code|plain`).
* `persona_reservations.py` — acquire/release/expire; mirrors the recovery
  lock discipline (bounded wait, finally-release, TTL sweep on acquire).

### Backend — router additions (`app/routers/test_factory.py`)
| Endpoint | Method | Role |
|---|---|---|
| `…/personas` | GET/POST | list/create (manager+) |
| `…/personas/{id}` | GET/PATCH | detail incl. per-env verify status |
| `…/personas/{id}/credentials/{env}` | PUT | write card (WRITE-ONLY; admin/manager) |
| `…/personas/{id}/expected-values/{env}` | GET/PUT | answer sheet |
| `…/login-recipes` | GET | versions + verify state |
| `…/login-recipes/{id}/verify` | POST | replay probe (P1 activates) |

qe-central: extend the environments router with `posture`, `data_epoch`;
factory_proxy `_ALLOWED_ROOTS` already covers `test-factory` (no change);
add the persona endpoints to `_PRIVILEGED_READ_RX`? — No: credential GET never
exists; card PUT is already write-gated. Expected-values GET is non-secret.

### Back-compat shim (the zero-action guarantee)
`persona_registry.ensure_baseline(app_id)` — if an artifact has a legacy
`formlogin::` profile and no personas: materialize `persona-0`
(`is_recording_baseline=true`) + a v1 single-page recipe from
`_AUTH_CONFIG_JSON` shape + a card for the artifact's bound env. Idempotent;
called lazily on first registry read.

### Portal (`verdict-portal/src/studio/`)
`PersonaRegistryPanel.tsx` (list, traits, per-env verify chips, card editor
that is write-only — shows "saved, never displayed again"), wired as a
sub-tab under the existing Studio; `factoryApi.ts` methods.

### Tests — `tests/test_persona_matrix_p0.py`
RLS two-tenant isolation; card round-trip decrypts only via dispatch path;
card never appears in ANY GET (walk the router responses); baseline shim
idempotency; reservation TTL acquire/expire semantics; migration grants pin
(no DELETE on credentials/expected-values).

**Exit proof (live):** venkata app untouched — run still green via the shim
persona-0; write a card via API then attempt to read it back through every
persona GET ⇒ absent everywhere; two-tenant RLS probe blocked; audit chain
shows `persona_credential_written` with chain_ok=true.

---

## Phase P1 — Login recipes: capture at crawl, replay at run

### Crawl side (nexus-qe-explorer — additive)
The crawler already performs the login from the seed manifest. Add a
**login-demonstration recorder**: during the auth phase, record the ordered
(page URL-shape, control label/role, action, WHICH seed field supplied the
value) tuples; on crawl completion POST them to platform-api
`…/login-recipes/capture` which stores steps with values replaced by named
slots (slot name = seed-manifest field name; value → type inference:
password-kind ⇒ `secret`, else `plain`; fixed-code field flagged by the
operator later). File location TBD at build start (explorer's auth module);
the CONTRACT is fixed here: the capture endpoint + payload schema.

### Replay engine (compiler)
* `compiler.py`: replace the single-page `_AUTH_SETUP_TS` body with a
  RECIPE-DRIVEN globalSetup: reads `vkpower.auth.recipe.json` (emitted into
  the bundle by `_configured_files` when a recipe is bound — non-secret:
  steps + slot NAMES only); fills slots from env vars
  `NEXUS_SLOT_<NAME>`; multi-page (goto/fill/click/waitFor between steps);
  `totp_seed` slots computed at f