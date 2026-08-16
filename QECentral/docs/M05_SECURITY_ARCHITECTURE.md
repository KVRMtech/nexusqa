# M0.5 — Security Architecture (ship-stopper closure)

Status: **implemented, tested, not yet deployed.**
Branch: `feat/qec-dynamic-catalog-p0-p6`.
Suites: `platform/qe-central/tests/security`, `engines/qe-explorer/tests/security`.
Gate: `.github/workflows/security-m05.yml`.

This document is the architecture record for the twelve M0.5 controls. It says
what the boundary is, where it is enforced, and what a bypass would have to
defeat. It is written for the next person to change this code.

---

## 0. The shape of the system

```
operator / portal ──JWT──▶ qe-central  /api/v1/qec/*        (tenant-scoped, RLS)
                                │
                                │ 1. reserve worker   ┐
                                │ 2. write egress fence├─ ORDER IS THE CONTROL
                                │ 3. dispatch         ┘
                                ▼
                      qe-explorer  /api/v1/explore         (fleet token, per-tenant slot)
                                │
                                │ browser ──▶ squid ──▶ the client's app
                                │            (host allowlist)
                                │
                                └─signed callback──▶ qe-central /internal/*
                                   (fleet token + v2 HMAC envelope + crawl binding)
```

Three trust boundaries, three different credentials:

| boundary | credential | what it proves |
| --- | --- | --- |
| operator → qe-central | HS256 JWT (`NEXUS_JWT_SECRET`) | which tenant, which role |
| qe-central ↔ qe-explorer | fleet token (`QEC_EXPLORER_TOKEN`) | membership of this fleet |
| explorer → `/internal/*` | v2 HMAC envelope + crawl binding | this body, now, once, for this crawl |

The recurring M0.5 defect was a boundary whose credential proved LESS than the
code assumed: a fleet token was read as if it named a tenant, a body signature
was read as if it named a crawl, a request body was read as if it named an
owner.

---

## 1. T-SEC-01 — Authentication defaults

**Was:** `docker-compose.qec.yml` shipped
`NEXUS_JWT_SECRET: ${NEXUS_JWT_SECRET:-test-secret-do-not-use-in-production}`
and `app/config.py` defaulted the field to `dev-jwt-secret-change-me`. A fresh
deployment therefore accepted any token signed with a value printed in this
repository — including `{"role": "admin", "tenant_id": "<any victim>"}`.

**Now:**
- the compose file uses the `:?` required-variable form for both secrets, so an
  unset value **stops the deploy** instead of substituting one;
- `Settings.nexus_jwt_secret` defaults to `""` — unset means *authentication is
  impossible*, not *use the one in the source tree*;
- `config.jwt_secret_usable(secret, env)` is the pure rule, enforced at
  **request time** by `auth.assert_signing_key_usable()` (before any signature
  is evaluated) and at **mint time** by `service_token.mint_service_jwt` and
  `fleet.rbac`.

The rule:

| secret | environment | verdict |
| --- | --- | --- |
| empty | any | refuse |
| known dev default | `development`/`test`/`dev`/`local` | allow (logged) |
| known dev default | anything else, incl. unrecognised | refuse |
| < 32 chars | `staging`/`production` | refuse |
| otherwise | any | allow |

`DEPLOYED_ENVS` and `LOCAL_ENVS` are deliberately **not** complements: a typo'd
`NEXUS_ENV` is neither, so it fails closed.

Why request-time and not only the boot gate: the boot gate does not run for an
in-process app, does not re-run when configuration changes, and says nothing
about `development` — which is what a fresh `docker compose up` actually is.

---

## 2. T-SEC-02 — The `/internal/*` boundary

**Was:** `jwt_auth_middleware` gates `/api/*` only. The internal router is
mounted outside that prefix on purpose (the explorer holds no JWT), so the
prefix had **no boundary authentication at all** — it relied on each handler
remembering its own HMAC check, and the container published 8093 on `0.0.0.0`.

**Now:** `auth.internal_auth_middleware` authenticates the **prefix** with the
per-fleet token (constant-time, rotation-aware). A request without it never
reaches a handler, so a route that forgets its own check — or a route added
later — is still covered.

The published port is bound to `127.0.0.1` by default
(`QEC_BIND_ADDRESS`). That is **defence in depth, not the control**: the seam is
refused from localhost and from inside the docker network too.

Two factors, deliberately different:

- the **token** proves the caller is in the fleet;
- the **signature** proves the body is intact, fresh, un-replayed, and about
  this crawl.

---

## 3. T-SEC-03 — Worker reservation before the egress fence

**Was:** the dispatch loop wrote a worker's squid allowlist and *then* posted
`/explore` and discovered the worker was busy. While worker W crawled for tenant
A, tenant B's dispatch reached the file write and **overwrote W's fence**. B's
own dispatch then 409'd — but A's live browser was now fenced by B's allowlist.
The attacker never needed their own crawl to start.

**Now** the sequence is:

1. authenticate the caller (`require_role`)
2. resolve the tenant (JWT claim)
3. resolve the crawl (the pending `qe_explorations` row)
4. **reserve the worker atomically** (`POST /api/v1/reserve`)
5. ownership established (the worker records the tenant)
6. write **that worker's own** allowlist
7. dispatch

A busy worker refuses at step 4 and its allowlist file is never opened. A
dispatch that aborts releases the slot (`POST /api/v1/reserve/{id}/release`,
owner-only, refused once running).

`JobManager.reserve` is the single atomic claim — FastAPI handlers run
single-threaded on the loop, so there is no await between check and mark.

A worker image without `/api/v1/reserve` answers 404 and the client **fails
closed** rather than dispatching anyway: a mixed-version pool must not silently
restore the race.

---

## 4. T-SEC-04 — `allowed_hosts` at the write boundary

**Was:** `fences.allowed_hosts` is tenant-controlled and becomes the squid
allowlist verbatim. Squid reads `.com` as *this domain and every subdomain*, so
`[".com"]` turned the fenced browser into an open SSRF proxy. Validation
happened (partially) at crawl time, so the dangerous value was already
persisted.

**Now:** `app/security/host_policy.py` is the single normalise-then-validate
gate, called from **every** path that persists `fences`
(`routers/apps._validated_fences`: app create, app update, env-profile create,
env-profile update) and again at dispatch for rows written before it existed.

Doctrine:

- **normalise first, validate second** — percent-decoding (repeated), zero-width
  and NUL stripping, IDNA folding, case folding, trailing dots, brackets, zone
  ids all resolved *before* any check;
- **no IP literals at all**, in any encoding — one rule covers
  `169.254.169.254`, `::1`, `::ffff:169.254.169.254`, `2130706433`, `0x7f000001`,
  `0177.0.0.1`, `127.1`;
- **a non-alphabetic final label is an address, not a domain** — the structural
  catch-all, because enumerating numeric encodings is a losing game;
- **public suffixes are never a fence** — `com`, `co.uk`, `herokuapp.com`;
- **a URL or a path is refused**, because `acme.example/admin` allowlists all of
  `acme.example` and silently discards the part the operator cared about;
- **one bad entry refuses the whole list** — a partially-accepted allowlist is
  not a fence.

A single-label exact entry (`acme-life`) stays legal: it authorises exactly one
hostname on an internal network. A single-label **wildcard** does not.

---

## 5. T-SEC-05 — Non-disposable environments are observation-only

**Was:** `prod_guard.resolve_effective_fences` forces observe-only for a
production environment — but only on the multi-env `env_resolver` path, which a
single-env crawl never travels. The dispatch read
`fences.get("observe_only")` and nothing else, so an app whose fences simply
lacked the key was dispatched free to fill, click and advance.

**Now** the decision is made twice, independently:

- `routers/explorations.resolve_crawl_observe_only` — in the crawl path, before
  anything is dispatched, recorded on the row as evidence;
- `engines/qe-explorer/app/main.resolve_observe_only` — inside crawl execution,
  from the attestation the process was actually handed.

The rule, fail-closed: mutation is permitted **only** when the attested
`env_kind` is `disposable`. Absent, blank or unrecognised is treated as
production. An explicit `observe_only` is a floor that is never lowered, and a
dispatch that disagrees with the signed attestation loses to the attestation.

> **Product impact, stated plainly.** This is stricter than the behaviour before
> M0.5. `staging`, `uat` and `production_test` crawls are now catalogue-only:
> they capture pages, fields, locators and navigation, but do not fill forms or
> walk funnels. Only `disposable` mutates. That is what SI-05 and T-SEC-05
> specify (`env_kind != disposable ⇒ observe_only = true`), and it aligns the
> FILL gate with the SUBMIT gate, which was already disposable-only. Teams that
> relied on staging crawls walking funnels must re-attest those environments as
> `disposable`.

---

## 6. T-SEC-06 / T-SEC-11 — Callback authenticity, freshness and rotation

**Was:** `HMAC-SHA256(secret, raw_body)`. It proved the sender held the fleet
secret and nothing else. A captured `/complete` body + signature replayed
forever — re-running the substrate write, the auto-generate and the autowalk
dispatch cascade each time. The same signature authenticated a call about a
*different* crawl. And with one global secret and no key id, rotation was a flag
day that rejected every in-flight callback.

**Now** — `app/security/hmac_auth.py`, duplicated **byte-identically** into the
explorer (the two services do not share a package; CI asserts the hashes match):

```
X-QEC-Signature: v2;kid=<16 hex>;ts=<epoch ms>;nonce=<32 hex>;sig=<64 hex>

sig = HMAC-SHA256(key, "v2\n{kid}\n{ts}\n{nonce}\n{scope}\n{sha256(body)}")
```

Verification, fail-closed, in this order:

1. the envelope parses and names a **known key id**;
2. `ts` is within the skew window — past **and** future;
3. the **nonce** has not been consumed;
4. the signature matches over the body hash **and the scope**;
5. only then is the nonce consumed — so a forged call cannot burn a legitimate
   nonce.

`scope` is `{operation}:{crawl_id}`, so an envelope for one crawl cannot
authenticate a call about another. The nonce store is process-wide and bounded:
one store for every internal endpoint, so a signature captured at
`/pick-advance` cannot be replayed at `/complete`.

**Rotation** (`KeyRing`): key ids are derived as `sha256(secret)[:16]` — explicit
in the envelope, deterministic, and revealing nothing. Signing always uses the
current key; verification also accepts the previous key until
`QEC_EXPLORER_TOKEN_PREVIOUS_EXPIRES_AT` (epoch seconds) passes.

```
K1 active  →  K2 introduced (K1 → _PREVIOUS, deadline set)  →  overlap  →  K1 retired
```

A half-configured rotation (previous key set, deadline absent or unparseable)
retires the old key **immediately** — the dangerous failure is an operator who
forgets the deadline and leaves a superseded key valid forever.

The legacy v1 bare-hex signature no longer verifies. Accepting it "for
compatibility" would leave the replay hole open behind a flag.

---

## 7. T-SEC-07 — Tenant and crawl binding

**Was:** every mid-crawl endpoint read `tenant_id` out of the request **body**
and handed it to a service that queried, spent LLM budget against, and returned
data for it. `/complete` located the exploration by
`(exploration_id, body.tenant_id)`.

**Now:** the **crawl id** is the identity. `internal._bind_crawl` resolves it
server-side *under the claimed tenant's RLS scope*: a row that comes back is
proof the claim is true, and a false claim finds nothing. Everything downstream
uses values off the **row**.

`_authenticate_internal` is the one function every mid-crawl endpoint calls, so a
new endpoint cannot be added with a weaker check by accident. `/complete`
additionally asserts the callback's `exploration_id` belongs to the bound crawl.

The refusal is a plain **404**, identical for "owned by someone else" and "does
not exist" — a 403/404 distinction would let an attacker enumerate other
tenants' crawl ids one guess at a time.

On the worker, the same principle: the fleet token proves *qe-central*, never
*which tenant it is acting for*, so `GET /api/v1/explore/{id}` and
`POST .../cancel` are owner-scoped by the tenant that reserved the crawl.

---

## 8. T-SEC-08 — One clock domain

**Was:** `Attestation.is_submit_capable(now_ms)` compared `now_ms` against
`expires_at_ms`. `expires_at_ms` is epoch millis (~1.7e12); the only caller
passed `crawler.now_ms()`, which is `MonotonicClock` — **milliseconds since the
crawl started**, a number in the thousands.

`5_000 < 1_760_000_000_000` is true, and stays true for about fifty thousand
years. The freshness gate could not expire anything: an attestation that lapsed
months ago still authorised an irreversible submit.

**Now:**

- **one canonical representation** for persisted/protocol timestamps: **epoch
  milliseconds, UTC**. qe-central converts the stored ISO `expires_at` at
  dispatch; the guard compares against a wall-clock reading it takes itself;
- the parameter is `now_epoch_ms`, not `now_ms` — the old name was ambiguous
  enough to cause this;
- a value below `_MIN_PLAUSIBLE_EPOCH_MS` (2001-09-09) is **refused, not
  compared**, and logged as a clock-domain error. Silently treating a monotonic
  reading as "very early, therefore fresh" is the bug itself;
- monotonic clocks remain correct for what they measure — the auth window and
  the submit window are durations and still use `now_ms`.

---

## 9. T-SEC-09 — Terminal jobs are evicted

**Was:** `JobManager.finish` cleared the active slot but left the job in
`_by_id` forever. `_by_id` grew by one entry per crawl for the process's
lifetime, and a completed crawl stayed indistinguishable from a live one in
every lookup that authorises an operation.

**Now** the lifecycle is explicit — `created → running → finished` — and
`finish()` copies the job's final progress into a **bounded** terminal ring (32
entries) and then drops it from every active map: `_by_id`, `_pending`,
`_owner`, `_state`.

`active_count` is the leak canary; a 500-crawl regression test asserts it stays
at zero. `GET /api/v1/explore/{id}` still answers for a just-finished crawl,
from the terminal ring, marked `lifecycle: finished`.

---

## 10. T-SEC-12 — PII egress has one chokepoint

**Was:** the guard existed and worked, and exactly one caller invoked it. Ten
`complete_llm` sites and both screenshot endpoints reached the model with no
scan. A guard each caller must remember to call is a convention, and ten
unguarded sites are what a convention looks like after a year.

**Now** the check is at the **wire**: `platform_api.complete_llm` and
`platform_api.complete_vision` are the only two functions in this service that
talk to a model, and both call `_assert_egress_clean` immediately before the
request is built. A new caller inherits the guard by construction.

```
application data ──▶ PII egress guard ──▶ block ──▶ deterministic floor
                                       └─ clean ──▶ external provider
```

A block returns `LLMResult(ok=False, …)` — the same shape every caller already
handles for "the model was unavailable" — so a refusal degrades to the
deterministic floor instead of raising into a crawl.

The suite asserts there is no second route: no module outside the guarded client
names `/api/v1/llm/`, and no provider SDK is imported anywhere in the service.

`QEC_PII_EGRESS_GUARD=0` disables it for false-positive diagnosis, and logs
loudly every time — a deployment running unguarded is visible in its own logs.

---

## 11. Observability of refusals

Every security rejection emits a structured line carrying event type, endpoint,
method, correlation id, tenant/crawl where known, and a stable reason
**category**:

```
qe_central.security.internal_api_unauthenticated endpoint=… reason=missing_fleet_token
qec.security.internal_refused endpoint=pick-advance reason=crawl_not_owned_by_claimed_tenant
qec.hmac.callback_rejected reason=nonce_replayed scope=complete:…
qec.egress.pii_detected site=llm:brief_compile patterns=[ssn]
qe_central.auth.refused_unsafe_signing_key reason=jwt_secret_is_development_default
qec.guard.attestation_clock_domain_error now_ms=5000
```

Never logged: JWT secrets, HMAC keys, nonces, raw credentials, matched PII
values, or complete authentication headers. The PII guard logs pattern **names**
only — refusing to send an identifier and then copying it into our own logs is
not a refusal.

---

## 12. Running the gate

```bash
# the whole M0.5 red team
pytest platform/qe-central/tests/security -q
pytest engines/qe-explorer/tests/security -q      # from engines/qe-explorer

# the application suites (unchanged expectations)
pytest platform/qe-central/tests -q
pytest tests --ignore=tests/browser -q            # from engines/qe-explorer
```

CI: `.github/workflows/security-m05.yml` — three lanes (qe-central red team,
explorer red team, shipped-configuration assertions) summarised by a single
required `M0.5 security gate` check.

---

## 13. Deployment notes

**Breaking configuration changes.** `docker compose -f docker-compose.qec.yml up`
now **refuses to start** unless both are set:

```bash
export NEXUS_JWT_SECRET=$(openssl rand -hex 32)   # must match platform-api's
export QEC_EXPLORER_TOKEN=$(openssl rand -hex 32) # per-fleet
```

**Both services must be deployed together.** The v2 signature scheme is not
wire-compatible with v1 — a new qe-central rejects an old explorer's callbacks
and vice versa. The explorer image must also carry `/api/v1/reserve`, or
qe-central fails closed rather than fencing a worker it does not hold.

**Rotating the fleet token** without downtime:

```bash
export QEC_EXPLORER_TOKEN_PREVIOUS=$OLD
export QEC_EXPLORER_TOKEN_PREVIOUS_EXPIRES_AT=$(( $(date +%s) + 3600 ))
export QEC_EXPLORER_TOKEN=$(openssl rand -hex 32)
# redeploy both services; after the deadline, clear the two _PREVIOUS vars.
```

**Port binding.** 8093 is now loopback-only. If a reverse proxy terminates on a
different host, set `QEC_BIND_ADDRESS=0.0.0.0` — and note that the `/internal`
seam is still refused without the fleet token.
