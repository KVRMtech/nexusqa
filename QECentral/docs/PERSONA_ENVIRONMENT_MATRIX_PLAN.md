# Persona × Environment Matrix — Canonical Design & Phased Build Plan

**Status:** FOUNDER-REVIEW DRAFT v1.0 (2026-07-26)
**Scale target:** 100+ client tenants. Built ONCE, generic by construction —
no client-specific code anywhere; every client difference is DATA (recorded
recipes, registry rows, credential cards), never a code branch.
**Doctrine:** never-green-wash applies to identity and environment exactly as
it applies to steps: a check that cannot be honestly verified for THIS member
in THIS environment is reported as unverified — never faked green, never
falsely red, and a run never inherits ambient identity.

---

## 0. The model (one sentence)

> **RUN = Suite × Environment × Persona.**
> The suite records the JOURNEY once. Environment and persona are resolved at
> dispatch from governed registries — never baked into a test.

Trust is scoped to the tuple: certified means *certified where and as whom*.

---

## 1. Entities (the schema that must be right on day one)

All tables: tenant-scoped with RLS, additive migrations, idempotent SQL
(`scripts/apply_*.sql` pattern). All secrets envelope-encrypted with AAD
binding — never plaintext, never returned by any API, access recorded on the
Part-11 audit chain.

### 1.1 `login_recipes` — the choreography, per application
- `recipe_id`, `tenant_id`, `app_id`, `version` (recipes are VERSIONED — login
  pages change; old runs must still know which recipe they used)
- `steps` (JSON): ordered actions — goto / fill(slot) / click / wait —
  supporting MULTI-PAGE flows (USAA-style: member number → next screen →
  password + PIN), keypad clicks, and submit detection
- `slots` (JSON): named blanks discovered at capture — e.g.
  `member_number`, `password`, `pin`, `otp` — each typed:
  `secret` | `totp_seed` | `fixed_code` | `plain`
- `source`: `crawl_demonstration` | `recorded` (provenance — recipes are
  captured from a real login, never hand-authored)
- `verified_at`, `verified_env`: the last successful replay probe

### 1.2 `personas` — logical identities, per application
- `persona_id`, `tenant_id`, `app_id`, `name`, `description`
- `traits` (JSON tags): "active-term-life", "TX", "two-beneficiaries" —
  free-form, client-defined vocabulary (generic: we never ship a domain list)
- `status`: active | retired; `is_recording_baseline` (the member the crawl
  used — persona-0, whose observed values ARE the recorded baseline)

### 1.3 `persona_credentials` — the ingredient cards, per (persona, environment)
- key: (`persona_id`, `environment_id`)
- `slot_values` (envelope-encrypted JSON): slot name → secret value.
  Slots mirror the recipe's slots — the schema imposes NO fixed field set,
  which is what makes one design serve email+password AND
  member+password+PIN+OTP without modification
- `last_verified_at`, `verify_status` (live | failed | stale) — personas rot
  when environments refresh; staleness is tracked, not discovered mid-run

### 1.4 `persona_expected_values` — the answer sheets, per (persona, environment)
- key: (`persona_id`, `environment_id`, `value_key`)
- `value_key`: a stable identifier for a member-derived expectation
  (bound to the classification in 1.6), `expected_value`, `source`
  (`crawl_observed` | `client_supplied` | `diff_proven`)

### 1.5 `environments` — extend the EXISTING Environment Profiles (live today)
Add: `posture` (`full` | `no_submit` | `read_only`) with **default-deny for
anything named/flagged production**; `env_assertion` (exists);
`data_epoch` (last refresh date); `verified_at` (health-probe result);
`recipe_id` override (an env whose login differs binds its own recipe version).

### 1.6 `value_classifications` — what kind of value is each expectation?
- key: (`artifact_id`, `value_key`)
- `class`: `member_derived` | `app_constant` | `volatile` | `unknown`
- `evidence`: how we know — `identity_echo` (matched the logged-in member's
  own seed data at capture), `diff_proven` (differed across a two-persona
  crawl), `stable_across_personas` (identical across the diff crawl),
  `unclassified`
- Fail-closed consumption rule: `unknown` + non-baseline persona ⇒ the check
  runs as STRUCTURAL (present/format) and reports UNVERIFIED. Honest, never
  fabricated.

### 1.7 `persona_reservations` — no two runs mutate the same member
- (`persona_id`, `environment_id`, `run_id`, `expires_at`) — acquired at
  dispatch, released at ingest, TTL-expired so a crashed run never wedges a
  persona.

---

## 2. Phases — each production-complete, none requiring rework of a prior one

Ordering rationale: schema first (§1 lands whole in P0 even though later
phases fill it — so no migration churn); recipes before personas (personas
are useless without replayable login); classification before persona-oracles
(honesty depends on it); governance and scale last (they harden, not reshape).

### Phase P0 — Schema + registries (the once-only foundation)
* All seven tables above, migrations, RLS, envelope encryption, audit events
  for every credential write/read.
* CRUD APIs + registry surfaces in the portal (environments extend the
  existing screen; personas and recipes get theirs).
* Backward compatibility: today's single form-login profile auto-represents as
  persona-0 with a one-step recipe — every existing client keeps working with
  zero action.
**Exit proof:** an existing app runs exactly as before with the new schema
live; a persona card written then read back decrypts only via the API path;
RLS blocks cross-tenant reads in a two-tenant test.

### Phase P1 — Login Recipe: capture at crawl, replay at run
* Crawl-side (additive): the login the crawl ALREADY performs is saved as a
  parameterized recipe — steps + named slots — instead of being discarded.
* Replay engine: multi-step recipe replay replaces the single-page login,
  driven entirely by recipe data; TOTP computation and fixed-code slots
  supported; SMS/e-mail OTP explicitly deferred (adapter slot reserved).
* Recipe verification probe: replays the recipe with a designated card,
  stamps `verified_at`. Wrong page shape ⇒ honest recipe-drift error naming
  the failed step — never a misleading test failure.
**Exit proof:** on an app with a multi-screen login, one crawl produces a
recipe; the probe replays it green; deliberately breaking one selector
produces a named recipe-drift failure, not a product-blame failure.

### Phase P2 — Personas at run time
* "Run as" selector beside the existing environment selector (portal + API);
  the run's identity is a DECLARED input on the run record and in the report.
* Run-owned login: dispatch resolves (persona, env) → card + recipe; the run
  authenticates itself; ambient sessions are never reused.
* Persona preflight: recipe login + landing-page read before the suite; on
  failure the run is BLOCKED with `test_data` attribution — the application
  is never blamed for a dead test member.
* Reservations enforced at dispatch; Evidence Report Trust Block gains
  "Environment: X (proved) · Identity: Maria (fresh login)".
**Exit proof:** the same suite runs green as persona A and persona B on the
same environment; a persona with a wrong password yields BLOCKED +
test-data attribution and zero executed steps; two concurrent dispatches for
one persona serialize.

### Phase P3 — Honest oracles per persona (the differentiator)
* Crawl tagging (additive): values echoing the logged-in member's seed data
  are tagged `member_derived/identity_echo` at capture.
* Generator/compiler consume classifications: member-derived expectations
  resolve from the (persona, env) answer sheet; missing answer ⇒ structural
  check + UNVERIFIED, surfaced in the report's per-persona PROVEN split.
* Two-persona diff crawl (opt-in, once per app): crawl as persona-0 and one
  more member; differing values ⇒ `member_derived/diff_proven`; identical ⇒
  `app_constant`; changed-on-recrawl ⇒ `volatile`. Upgrades `unknown` to
  proven classes with evidence, per act-then-diff doctrine.
**Exit proof:** run as persona B shows persona-B's values asserted where
sheets exist, UNVERIFIED (not green, not red) where they don't; the diff
crawl demonstrably flips a set of unknowns to proven classes; zero
member-derived value from persona-0 is ever asserted against persona B.

### Phase P4 — Environment governance
* Posture enforcement: `read_only`/`no_submit` postures compile the
  constraint into the run (submit/mutation steps refuse with an honest
  "fenced by environment posture" — extends the existing submit-gate);
  production posture is default-deny.
* Environment health probe (seconds, not a crawl): reachability + recipe
  shape + designated card login ⇒ `verified_at`. NO re-crawl per environment
  — ever (artifact duplication is the known failure mode).
* Certification scoped per (suite, environment); Trust Block and run-gates
  read the (suite, env) pair; cross-env parity view keyed accordingly.
**Exit proof:** the same suite pointed at a `read_only` env executes zero
mutating steps and says why; certification on env A demonstrably does not
grant trust on env B until env B certifies.

### Phase P5 — 100-client scale & operations
* Trait-based selection ("run as: any persona matching [traits]") resolving
  via reservation to a concrete free persona.
* Persona re-validation scheduler keyed to `data_epoch` (env refresh ⇒ cards
  marked stale ⇒ probe re-validates or flags).
* Bulk import/export of persona cards (encrypted in transit, write-only API);
  impersonation-endpoint adapter (where a client's test env offers
  "switch user", a recipe step type uses it instead of the login UI).
* Credential-access audit review surface; secrets rotation procedure;
  per-tenant capacity limits on concurrent reservations.
**Exit proof:** a trait query resolves, reserves and runs without naming a
persona; an env refresh event flips its personas to stale and a probe
restores them; a bulk import of N cards yields N runnable personas with all
secrets unreadable via every read API.

---

## 3. Explicitly deferred (named so absence reads as decision, not omission)
* SMS/e-mail OTP inbox adapters (design slot reserved in recipe slots).
* Automatic persona MINING from client data pools (P5 selects among managed
  personas; it does not fabricate them).
* Cross-client persona federation — never: personas are tenant-private.

## 4. Client-input questions (answers shape config, never code)
1. MFA posture per test environment: fixed code, TOTP, or real SMS?
2. Can a second test member be provisioned per app (enables the diff crawl)?
3. Does any test env expose an impersonation/"switch user" endpoint?
