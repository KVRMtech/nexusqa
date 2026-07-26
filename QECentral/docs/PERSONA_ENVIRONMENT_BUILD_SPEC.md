# Persona × Environment Matrix — Engineering Build Spec

**Companion to** `PERSONA_ENVIRONMENT_MATRIX_PLAN.md` (the strategy).
**This document** is the implementation contract: exact schema, services,
endpoints, crawl/generator/compiler touchpoints, portal surfaces, tests, and
deploy steps — phase by phase. Production-ready, generic, additive.

**Status:** BUILD-READY DRAFT v1.0 (2026-07-26). Not started; awaiting "proceed".

---

## 0. Ground rules (apply to every phase)

* **Additive only.** Every migration is `CREATE TABLE IF NOT EXISTS` + RLS +
  grant, following `scripts/apply_auth_profiles.sql` / `apply_heal_events.sql`.
  No existing table altered destructively. Every code path degrades safe when a
  new table is absent (pre-migration) — mirrors `auth_profiles` helpers.
* **Secrets** use the existing `EnvelopeService`
  (`request.app.state.envelope_service`): `encrypt(tenant_id, bytes, aad=key)`
  → `EnvelopeBlob.to_bytes()`; never plaintext, never returned by an API. Every
  credential write/read appends a `heal_evidence.record_heal_event` audit row.
* **Tenant isolation** via `tenant_scoped_session(tenant_id)` (RLS `set_config`)
  on every query — no exceptions.
* **Generic:** no client vocabulary in code. Traits, behavior classes, slot
  names are all client-supplied DATA.
* **Deploy** per the standing runbook: LF-normalize → `gcloud scp` → `docker cp`
  into `nexus-platform-api` (or `nexus-qe-central`) → restart → verify clean
  boot → migration via `docker cp … nexus-postgres` + `psql -f`. Portal: build
  in PowerShell, back up `/usr/share/nginx/html`, swap `index.html`+`assets/`,
  preserve `trace/`,`lifeco/`,`testapp/`.
* **Test discipline:** each phase ships structural + behavioral tests AND a
  live exit-proof script run against artifact `574ce778`. Never-green-wash: an
  exit proof that only dry-runs is not a proof.

---

## 1. Schema — the whole model lands in P0

One migration file `scripts/apply_persona_env.sql`, idempotent. SDK models in a
new `app/services/test_factory/persona_store.py` (binds `nexus_sdk.db.Base`,
table created out-of-band — the `auth_profiles` pattern). Column list is the
contract; types are Postgres.

### 1.1 `login_recipes`
```
recipe_id       VARCHAR(64) PK
tenant_id       VARCHAR(64) NOT NULL
app_id          VARCHAR(64) NOT NULL
version         INTEGER     NOT NULL DEFAULT 1     -- monotonic per (tenant,app)
steps           JSONB       NOT NULL               -- ordered action list (§4.1)
slots           JSONB       NOT NULL DEFAULT '[]'  -- [{name,type,step_index,label}]
source          VARCHAR(24) NOT NULL DEFAULT 'crawl_demonstration'
status          VARCHAR(16) NOT NULL DEFAULT 'active'  -- active|superseded
verified_at     TIMESTAMPTZ
verified_env    VARCHAR(64) DEFAULT ''
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
UNIQUE (tenant_id, app_id, version)
```
`slots[].type ∈ {secret, totp_seed, fixed_code, plain}`. New recipe version on
re-capture; old runs keep pinning the version they used.

### 1.2 `personas`
```
persona_id            VARCHAR(64) PK
tenant_id             VARCHAR(64) NOT NULL
app_id                VARCHAR(64) NOT NULL
name                  VARCHAR(120) NOT NULL
description           TEXT DEFAULT ''
traits                JSONB NOT NULL DEFAULT '[]'   -- client tags
behavior_class        VARCHAR(64) NOT NULL DEFAULT ''  -- §5 of plan
status                VARCHAR(16) NOT NULL DEFAULT 'active'  -- active|retired
is_recording_baseline BOOLEAN NOT NULL DEFAULT FALSE
created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
UNIQUE (tenant_id, app_id, name)
```

### 1.3 `persona_credentials`  (secrets — envelope-encrypted)
```
persona_id      VARCHAR(64) NOT NULL
environment_id  VARCHAR(64) NOT NULL
tenant_id       VARCHAR(64) NOT NULL
blob            BYTEA       NOT NULL          -- EnvelopeBlob of {slot: value}
slot_names      JSONB       NOT NULL DEFAULT '[]'  -- NON-secret: which slots present
last_verified_at TIMESTAMPTZ
verify_status   VARCHAR(16) NOT NULL DEFAULT 'unverified'  -- live|failed|stale|unverified
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
PRIMARY KEY (persona_id, environment_id, tenant_id)
```
AAD = `f"personacred::{persona_id}::{environment_id}"`. `slot_names` lets the UI
show completeness without decrypting.

### 1.4 `persona_expected_values`  (answer sheets)
```
persona_id      VARCHAR(64) NOT NULL
environment_id  VARCHAR(64) NOT NULL
tenant_id       VARCHAR(64) NOT NULL
value_key       VARCHAR(200) NOT NULL   -- FK-ish to value_classifications.value_key
expected_value  TEXT NOT NULL
source          VARCHAR(24) NOT NULL DEFAULT 'crawl_observed'  -- crawl_observed|client_supplied|diff_proven
updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
PRIMARY KEY (persona_id, environment_id, tenant_id, value_key)
```
Expected values are NOT secret (they are what the app displays) but ARE
PII-adjacent — export redaction (existing `report_export.redact_report`) applies.

### 1.5 `value_classifications`
```
artifact_id     VARCHAR(64) NOT NULL
tenant_id       VARCHAR(64) NOT NULL
value_key       VARCHAR(200) NOT NULL
class           VARCHAR(20) NOT NULL   -- member_derived|app_constant|volatile|structural|unknown
evidence        VARCHAR(24) NOT NULL   -- identity_echo|diff_proven|stable_across_personas|unclassified
scenario_id     VARCHAR(64) DEFAULT ''
step_number     INTEGER DEFAULT 0
detail          JSONB DEFAULT '{}'
updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
PRIMARY KEY (artifact_id, tenant_id, value_key)
```

### 1.6 `persona_reservations`
```
reservation_id  VARCHAR(64) PK
persona_id      VARCHAR(64) NOT NULL
environment_id  VARCHAR(64) NOT NULL
tenant_id       VARCHAR(64) NOT NULL
run_id          VARCHAR(64) NOT NULL DEFAULT ''
acquired_at     TIMESTAMPTZ NOT NULL DEFAULT now()
expires_at      TIMESTAMPTZ NOT NULL
released_at     TIMESTAMPTZ
UNIQUE (persona_id, environment_id, tenant_id) WHERE released_at IS NULL  -- partial: one live hold
```

### 1.7 `environments` — EXTEND existing Environment Profiles (qe-central owns them)
Additive columns on the existing profile store (qe-central migration):
`posture VARCHAR(16) DEFAULT 'full'`, `data_epoch DATE`,
`verified_at TIMESTAMPTZ`, `recipe_id VARCHAR(64) DEFAULT ''`,
`recipe_version INTEGER DEFAULT 0`. `posture ∈ {full,no_submit,read_only}`;
default-deny: any profile whose name/kind matches production ⇒ posture forced
to `read_only` unless an explicit signed override row exists.

Grants: all persona tables `SELECT,INSERT,UPDATE` for role `nexus`
(reservations also need `UPDATE` for release); NO `DELETE` (retire via status).
RLS policy `tenant_id = current_setting('nexus.current_tenant_id', true)`.

---

## 2. Phase P0 — Foundation (schema + registries + back-compat)

**Deliverables**
1. `scripts/apply_persona_env.sql` — §1.1–1.6 tables, RLS, grants, indexes
   (`ix_personas_tenant_app`, `ix_persona_cred_env`, `ix_reservations_live`).
2. `app/services/test_factory/persona_store.py`:
   - ORM rows for all six tables (bind SDK `Base`).
   - `save_recipe / get_recipe / list_recipes / supersede_recipe`
   - `save_persona / get_persona / list_personas / retire_persona`
   - `save_persona_credential(envelope, …, slot_values: dict)` /
     `get_persona_credential(envelope, …) -> dict|None` (mirrors
     `auth_profiles.save_form_login/get_form_login` exactly).
   - `set_expected_value / get_expected_values`
   - `save_classification / get_classifications`
   - `acquire_reservation(ttl) -> id|None` (atomic `INSERT … ON CONFLICT DO
     NOTHING` against the partial unique index) / `release_reservation`
     / `expire_stale_reservations`.
   - `build_persona_bundle(recipe, credential) -> (auth_config, login_env)` —
     the generalization of `auth_profiles.build_form_login_bundle`: emits a
     `strategy:"recipe"` config (§4.2) + `NEXUS_LOGIN_<SLOT>` env per slot.
3. Router `app/routers/persona_env.py` (mounted like `test_factory`), CRUD:
   - `POST/GET/DELETE …/apps/{app_id}/recipes`
   - `POST/GET …/apps/{app_id}/personas` (+ `/retire`)
   - `PUT …/personas/{persona_id}/credentials/{environment_id}` (write-only;
     returns slot_names, never values)
   - `PUT/GET …/personas/{persona_id}/expected-values/{environment_id}`
   - `GET …/apps/{app_id}/classifications`
   - All admin|manager for writes; audit event per credential write/read.
4. **Back-compat shim** in `persona_store`: if an app has an
   `e2e_auth_profiles` form-login row but no persona, expose a synthetic
   `persona-0` (name "default", `is_recording_baseline=true`) whose credential
   reads through `auth_profiles.get_form_login`. Zero migration for existing
   clients; they gain a persona named "default" that already works.
5. qe-central: extend Environment Profile schema (§1.7) + CRUD; portal
   Environments screen gains posture + data_epoch fields; new Personas &
   Recipes registry screens (read-only lists in P0, editing in P2).
6. Portal bridge: add `personas`, `recipes` roots to `factory_proxy.py`
   `_ALLOWED_ROOTS`; the credential PUT paths join `_PRIVILEGED_READ_RX`-style
   egress gating (writes already gated by method).

**Tests:** `test_persona_store.py` (round-trip encrypt/decrypt via AAD;
reservation atomicity under concurrent acquire; back-compat persona-0
synthesis; RLS cross-tenant deny). **Exit proof (live):** existing app runs
byte-identical; a written credential card decrypts only through the API;
two tenants cannot read each other's personas.

**Effort:** ~1 build cycle. Highest-risk = the six-table migration; mitigated
by the proven idempotent pattern.

---

## 3. Phase P1 — Login Recipe: capture at crawl, replay at run

**Capture (crawl side, qe-central/explorer — additive):**
* The crawl already performs the login to pass the auth wall. Wrap that
  sequence in a recorder that emits `steps` + `slots`:
  - each `fill` whose value came from the seeded login secret → a `slot`
    (name inferred from the field's label/name/autocomplete: `member_number`,
    `password`, `pin`, `otp`; unknown → `slotN`, renamable in the UI);
  - `goto`, `click`, `waitForLoadState`, keypad `click` sequences preserved in
    order; multi-page transitions captured natively (it is just more steps).
* On `complete_crawl`, persist via `persona_store.save_recipe(source=
  'crawl_demonstration')` and create/attach `persona-0` with
  `is_recording_baseline=true`; store the seeded values as persona-0's
  credential card for the crawl environment.

**Replay (compiler + platform-api):**
* Compiler: generalize `_AUTH_SETUP_TS` (globalSetup) from single-page form to
  a **recipe interpreter** — reads `vkpower.auth.config.json` with
  `strategy:"recipe"`, iterates `steps`, resolving each slot from
  `process.env["NEXUS_LOGIN_" + SLOT.toUpperCase()]`. `totp_seed` slots compute
  the current code (RFC 6238, ~30 lines, no dependency); `fixed_code` slots
  read the literal env value (this is the client's chosen MFA rung). Writes
  `vkpower.auth.json` storageState on success — downstream unchanged.
* `persona_store.build_persona_bundle` feeds `_configured_files(auth_config=…)`
  and the run env — the exact seam `auth_config`/`login_env` already occupy, so
  every run path (client/cert/live/heal) inherits it via the existing
  `_run_form_login`-shaped resolver (renamed `_run_persona_auth`, superset).
* **Recipe verification probe** endpoint `POST …/recipes/{id}/verify` runs the
  recipe headless with a designated card; on failure classifies WHICH step drifted
  (selector not found) and returns a recipe-drift error — NOT a product failure.
  Stamps `verified_at`.

**Tests:** compiler emits a valid multi-step globalSetup from a 3-step recipe;
TOTP computation matches a known vector; a broken step selector yields a named
drift error. **Exit proof (live):** a multi-screen login app → one crawl →
recipe → probe green; break one selector → named recipe-drift, not app-blame.

**Effort:** ~1.5 cycles. Risk = the globalSetup interpreter; contained because
it replaces one already-isolated file and is inert unless strategy=recipe.

---

## 4. Data contracts (frozen in P0, consumed later)

### 4.1 Recipe `steps` schema
```
[{ "action": "goto",  "url_slot": "base" },
 { "action": "fill",  "slot": "member_number", "selector_hints": {...} },
 { "action": "click", "role": "button", "name": "Next" },
 { "action": "fill",  "slot": "password", ... },
 { "action": "fill",  "slot": "pin", ... },
 { "action": "click", "role": "button", "name": "Log On" },
 { "action": "wait",  "state": "networkidle" }]
```
`selector_hints` reuse the compiler's existing ladder (`getByLabel`/role/…), so
recipe replay inherits the same self-healing locator strategy as steps.

### 4.2 `vkpower.auth.config.json` strategy=recipe
```
{ "strategy": "recipe",
  "loginPath": "/",
  "steps": [ ...as above... ],
  "slots": [ {"name":"member_number","type":"secret","env":"NEXUS_LOGIN_MEMBER_NUMBER"},
             {"name":"pin","type":"fixed_code","env":"NEXUS_LOGIN_PIN"} ] }
```
Back-compat: `strategy:"form"` (today) and `strategy:"none"` remain valid — the
interpreter dispatches on strategy, so existing bundles are untouched.

---

## 5. Phase P2 — Personas at run time

**Run request** (`RunConfigRequest`, additive fields):
`persona_id: str = ""`, `environment_id: str = ""` (env already flows via
`env_context`; this names the registry row for credential + posture lookup).

**Dispatch flow** (in `playwright_run` and the cert/live paths):
1. Resolve environment row (posture, recipe binding, env_assertion).
2. `acquire_reservation(persona_id, env, run_id, ttl=run_timeout)` → 409 with
   an honest "persona busy" body if held (mirrors runner-lock 409s).
3. `build_persona_bundle(recipe, credential)` → `auth_config` + `login_env`.
4. **Persona preflight:** a 1-scenario headless probe that logs in via the
   recipe and reads the landing page. Fail → run is BLOCKED, `test_data`
   attribution (existing `CATEGORY_DATA`), zero suite steps executed, honest
   report — the app is never blamed for a dead member.
5. Run proceeds; ingest releases the reservation.

**Report:** Trust Block (`evidence_report.build_trust_block`) gains
`identity: {persona, behavior_class, login: "fresh-recipe", preflight: ok}` and
`environment: {name, posture, assertion_proved: bool}`. The run record stores
`persona_id`/`environment_id` so a snapshot is reproducible.

**Portal:** "Run as" selector beside the environment selector in the Playwright
panel; personas listed with traits + behavior_class; a card-incomplete persona
is shown disabled with "credentials missing for this environment".

**Tests:** same suite green as persona A and B; wrong-password persona →
BLOCKED + test_data + 0 steps; concurrent dispatch for one persona serializes
(second gets 409). **Exit proof (live):** two personas on venkata, both green,
distinct sessions proven in the reporter log (`[nexus-auth] … OK`).

**Effort:** ~1.5 cycles.

---

## 6. Phase P3 — Honest per-persona oracles (the differentiator)

**Crawl tagging (additive):** at capture, any observed value that equals a
seeded persona-0 identity field (name, member no, email…) is written to
`value_classifications` as `member_derived / identity_echo`, keyed by a stable
`value_key` (scenario_id + step + field). Everything else defaults `unknown`.

**Generator/compiler consumption:**
* Generator stamps each expected-value assertion with its `value_key`.
* Compiler resolves a `member_derived` expectation at compile time from the
  `(persona, env)` answer sheet: present → assert the persona's value; absent →
  emit a STRUCTURAL check (present/non-empty/format) tagged UNVERIFIED. Never
  assert persona-0's value against another persona (the green-wash we forbid).
* `app_constant` expectations assert normally for everyone.

**Two-persona diff crawl (opt-in, once per app):** new orchestrator
`persona_diff.py`:
1. Crawl as persona-0 and persona-1 (the client's second member), same env.
2. **Value diff:** per `value_key`, differ ⇒ `member_derived/diff_proven` and
   write each persona's value to its answer sheet; identical ⇒
   `app_constant/stable_across_personas`; changed-on-recrawl of the SAME
   persona ⇒ `volatile` (a third control crawl of persona-0 isolates time-drift).
3. **Structure diff:** page-set and per-page repeated-block counts compared →
   `structural/diff_proven` records (pages/sections present for one persona and
   not the other; repeat cardinality where a block repeats). Feeds §7.

**Report:** per-persona PROVEN/INFERRED/UNVERIFIED split in the oracle
scorecard, so "how much of the suite is proven for persona B" is answerable.

**Tests:** run as B asserts B's sheet values, UNVERIFIED where unset, never A's
values; a synthetic diff flips a known `unknown` to `diff_proven`. **Exit
proof (live):** diff-crawl venkata with two members; verify a value-diff set
and a structure-diff set are produced with evidence.

**Effort:** ~2 cycles (the classifier is the intellectual core).

---

## 7. Phase P4 — Environment governance & structural behavior

**Posture enforcement:** compiler reads env `posture`; `read_only`/`no_submit`
compile submit/mutation steps into an honest refusal ("fenced by environment
posture: read_only") — extends the existing submit-gate rather than a new gate.
Production posture default-deny (§1.7).

**Cardinality-driven repetition:** where the structure diff (§6) found a block
differing only by COUNT, the generator marks that block repeatable and drives
the repeat count from persona data — one suite serves any family size.

**Behavior-class scoping:** where the structure genuinely forks, affected cases
bind to the `behavior_class` that demonstrated them. Dispatch as an out-of-class
persona is REFUSED at dispatch with guidance ("demonstrated for class X;
persona Y is class Z — diff-crawl class Z to unlock") — never run-and-fail,
never silent skip.

**Environment health probe** (seconds, NOT a re-crawl):
`POST …/environments/{id}/verify` — reachability + recipe-shape + designated
card login → `verified_at`. Re-crawl per env is explicitly forbidden
(artifact-duplication failure mode).

**Certification scoping:** certification runs, quarantine, and the exploratory
gate key on `(artifact, environment, behavior_class)`. Trust Block shows the
matrix of which (env × class) cells a suite has actually proven.

**Tests:** read_only env executes 0 mutating steps and says why; cert on env A
does not grant trust on env B. **Exit proof (live):** point venkata at a
read_only profile → mutations refused; a class-scoped case refuses an
out-of-class persona with the guidance string.

**Effort:** ~2 cycles.

---

## 8. Phase P5 — 100-client scale & operations

* **Trait selection:** `persona_id: "match:trait1,trait2"` resolves via
  `list_personas(traits=…)` + `acquire_reservation` to a concrete free persona;
  the report records which was chosen.
* **Staleness scheduler:** on an env `data_epoch` change, mark that env's
  credential cards `stale`; a background probe re-validates or flags. Reuses the
  reservation/probe machinery; a stale card is a preflight BLOCK, never a
  mid-run surprise.
* **Bulk card import/export:** write-only encrypted import
  `POST …/personas/import` (N cards → N runnable personas, all secrets
  unreadable via every read API); export is redacted + audit-logged.
* **Impersonation adapter:** a recipe `action:"impersonate"` step type that hits
  a client's "switch user" endpoint instead of driving the login UI — used only
  where a client confirms one exists (assumed absent by default).
* **Ops:** credential-access audit review surface (reads the Part-11 chain);
  secret rotation procedure (re-encrypt cards under a new envelope key);
  per-tenant concurrent-reservation cap.

**Exit proof (live):** a trait query resolves→reserves→runs unnamed; an env
refresh flips cards to stale then a probe restores; a bulk import of N cards
yields N runnable personas with secrets unreadable.

**Effort:** ~2 cycles.

---

## 9. Cross-cutting: what already exists vs what is new

| Capability | Exists today | This build |
|---|---|---|
| Envelope secret store + AAD | `auth_profiles`, `EnvelopeService` | reuse verbatim |
| Form-login → run-owned login | `build_form_login_bundle`, `_run_form_login` | generalize to recipes |
| Per-env routing (url/cookies/headers) | Environment Profiles (LIVE) | extend with posture/epoch |
| Attribution `test_data` category | `attribution_engine` | reuse for dead-persona BLOCK |
| Certification / quarantine / exploratory gate | `test_runs.py` | key on (env, class) |
| Trust Block / evidence report | `evidence_report` | add identity + env + matrix |
| Submit-gate / fences | live | extend for posture |
| Runner reservation pattern | `_RECOVERY_RUNNER_LOCK` | model for persona reservation |
| Portal bridge egress gating | `factory_proxy` | add persona/recipe roots |
| Recipe capture | — | NEW (crawl-side) |
| Recipe replay interpreter | single-page `_AUTH_SETUP_TS` | generalize |
| Value classification + diff crawl | — | NEW (the differentiator) |
| Behavior-class structural scoping | — | NEW |

Roughly **55–60% is reuse/extension**; the genuinely new engineering is recipe
capture+replay, the value/structure classifier, and behavior-class scoping.

---

## 10. Sequence & dependency

P0 (schema) → P1 (recipes) → P2 (personas) → P3 (oracles) → P4 (governance +
structure) → P5 (scale). P1 depends only on P0; P3 depends on P1+P2; P4 depends
on P3's structure diff. Each phase deploys and proves before the next starts.
Total ≈ 10–11 build cycles. Nothing in a later phase forces a change to an
earlier phase's schema or API — that is the design's whole point.

## 11. Client-answer bindings (2026-07-26)
* MFA fixed → `fixed_code` slot only in P1; TOTP path built but untested until a
  client needs it.
* Second member confirmed → P3 diff crawl is in scope, not deferred.
* Impersonation assumed absent → P5 adapter optional; per-env member numbers →
  `persona_credentials` keying (already core).
