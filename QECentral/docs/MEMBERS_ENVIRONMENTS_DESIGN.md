# Members × Environments — findings and design

**Date:** 2026-08-01 · **Status:** F1 and F2 built + deployed; F3–F7 outstanding
(see *Build status* at the foot)
**Method:** 51 scenarios enumerated across 4 lenses (slot/card binding, member lifecycle,
environments, operator surface), each judged against real code. Adversarial verification was
**partial** — 25 of 56 agents hit session limits, including the synthesis step — so the findings
below are the ones I re-verified BY HAND against the source. Classifications from unverified
agents are treated as leads, not conclusions.

Ranking rule: **`silently_wrong` outranks everything.** A loud failure costs an afternoon; a silent
one costs trust in the product. This system's whole promise is never to report green on something
it did not test.

---

## F1 — Selecting an environment does not route the run  ⛔ CRITICAL

**Verified by hand** in `platform/api/app/routers/test_factory.py::playwright_run`:

```python
base_url = (body.base_url or "").strip()                       # from the REQUEST
if body.env_context and body.env_context.get("base_url"):
    base_url = str(body.env_context["base_url"]).strip()

governance_env = await persona_store.get_environment(          # the registered env IS loaded
    session, tenant_id=..., artifact_id=..., environment_id=environment_id) or {}
gate = persona_governance.gate_dispatch(governance_env, ...)   # ...and used for POSTURE ONLY
```

`TpEnvironmentRow` **has** a `base_url` column (`persona_store.py:153`), the Studio form collects
it, and `get_environment` returns it — and nothing at dispatch ever applies it. `environment_id`
selects the credential card, the posture, the reservation, the cardinality plan and the member-data
answers. **Where the traffic actually goes comes from the request body**, which the portal defaults
to the recorded origin.

**Consequences, in order of severity:**

1. **Production can be hit while the UI says uat.** Register `prod` correctly
   (`is_production=true, write_authorized=false`), then run with the env box on `uat` while
   `base_url` still holds the crawled production origin: `gate_dispatch` evaluates **uat's** posture,
   passes, and a mutating suite executes against production. **Production default-deny does not fire,
   because it is evaluated against a LABEL and not against the DESTINATION.**
2. **Every environment-scoped claim downstream is unsound.** The report, the parity trend and the
   certification ledger all record `environment_id`, so they attest to an environment the run may
   never have touched.
3. **Routing cookies/headers are silently dropped** unless the caller happens to pass `env_context`,
   so a cookie-selected lane (same host, `x-env=dev`) lands on the host default — production.

**Design.** The environment must be the single source of truth for the destination:

- At dispatch, when `environment_id` resolves to a registered environment, its `base_url`,
  `cookies`, `headers` and `http_credentials` **must** produce the run's `env_context`. Caller-
  supplied `base_url` is then an override that must **match** the registered origin or be refused.
- `gate_dispatch` must receive the **resolved destination origin**, not just the row. Posture is a
  property of where the traffic lands.
- If `environment_id` is set and the environment is unregistered → **refuse** (`422`), naming the id.
  Today it silently degrades to "default read_write, non-production", which is the most permissive
  possible reading of an unknown target.
- A run whose destination origin differs from every registered environment must be labelled
  `environment: unknown` in the report rather than inheriting the selected id.

---

## F2 — The credential card is typed by hand, not derived from the recipe

**Verified.** `verdict-portal/src/studio/PersonaMatrixPanel.tsx:135` defaults the slot box to the
literal string `'member_number, password'`, and line 344 asks the operator to retype slot names.
Zero references to the active recipe's `slots`.

A mismatch (typo, case, or simply the wrong vocabulary for this app) saves cleanly and displays as a
normal card. At run time the compiled login finds the slot missing, **skips the entire login**, and
the suite runs unauthenticated — so the failures are attributed to the application. This is
precisely the founder-reported symptom of runs sitting on the login page.

It is also the one place the product's own vocabulary leaks: the default names a field the
customer's app may not have.

**Design.**

- The card editor **renders one input per slot of the artifact's ACTIVE recipe**, labelled with the
  recipe's own `label`, and masked per slot `type`. There is no free-text slot box.
- **No recipe yet** → the card form is not shown at all; the panel states the prerequisite and links
  to Record. (Provisioning a card before a recipe is guaranteed-wrong work.)
- **Several recipes** → the operator picks the login type first; the card binds to that
  `login_type_key`.
- **Server-side refusal** (a UI fix alone is not a fix): `PUT .../credentials/{env}` must reject a
  card whose slot names do not cover the active recipe's required slots, naming the missing and the
  unexpected ones. The API is the contract; the UI is a convenience.
- Cards store the `login_type_key` they were provisioned against.

---

## F3 — "verified" can be true of a card that cannot log in

A mistyped password is stored (write-only, never returned), the row shows **verified**, and nobody
can inspect it. Verification must mean something falsifiable.

**Design.** `verify_status` is set **only** by a real login preflight that reached the recipe's Home
oracle, and is **reset to unverified** whenever (a) the card is rewritten, (b) the recipe version
changes, or (c) the environment's `data_epoch` rolls. A card that has never been proven displays as
*unproven*, never as blank-but-fine. Add a per-card **Verify** action so the operator can prove a
card without dispatching a suite.

---

## F4 — A recipe re-recorded after cards exist orphans every card

Slot names come from the app's form; a re-record can change them (notably when a slot name was
derived from a framework-generated id). `save_recipe` mints a new active version and supersedes the
old one, while existing cards still carry the previous slot names — so the whole fleet silently
stops authenticating, with only a console warning.

**Design.** On save of a new recipe version, **diff the slot sets against existing cards** and
surface the breakage as a first-class state: cards affected are marked `stale_slots` and any run
using them is **BLOCKED**, naming the added/removed slots. Never let a slot change degrade into a
skipped login.

---

## F5 — Member identity is ambiguous, and retirement does not stop a run

Two members may share a display name with nothing to tell them apart in the picker; `retire` marks a
row but dispatch does not consult it, so CI pinned to a retired `persona_id` keeps running as a
decommissioned account; and a `persona_id` from a different artifact in the same tenant is accepted.

**Design.** Unique display name per artifact (or always render a stable short id beside the name);
dispatch **refuses** a retired or foreign-artifact persona with a named reason; a persona retired
mid-run releases its reservation and the in-flight run is marked BLOCKED rather than completing under
a decommissioned identity.

---

## F6 — Two disjoint environment models

Onboarding collects rich **Environment Profiles** (cookies, headers, basic-auth, env-pin, fences)
while Studio's Members & Environments keeps a separate registry (posture, production flag,
`base_url`, epoch). Nothing links them, and an operator reasonably believes they are one list.

**Design.** One registry. The Studio panel edits the same rows onboarding creates, keyed by
`environment_id`; the profile's routing fields become the run's `env_context` (per F1). Until they
are unified, the panel must state plainly which list it is showing.

---

## F7 — The matrix is invisible

The operator cannot see at a glance which member × environment combinations are actually runnable.
Everything is discovered by dispatching and failing.

**Design.** Render the **matrix**: members down, environments across, each cell one of
`ready` / `no card` / `unproven` / `stale slots` / `blocked (posture)`. The cell is the affordance —
click an empty one to provision that card. The screen should make the next correct action obvious
without the operator holding the model in their head.

---

## Order of operations the surface should enforce

```
1. Record the login            -> yields the recipe + its slots      (already built)
2. Register environments       -> base_url + routing + posture       (must actually route: F1)
3. Add members                 -> identities only, no secrets
4. Provision cards             -> fields DERIVED from the recipe     (F2)
5. Verify a card               -> a real login, reaching Home        (F3)
6. Run as member x environment -> only from a `ready` cell           (F7)
```

Steps 4–6 are refused server-side when their prerequisite is missing, each with a named reason.

---

## What this design does NOT solve

- **Perishable sessions.** A captured session remains a convenience for the first crawl; the recipe
  plus a card is the durable path. No attempt is made to keep sessions alive.
- **Out-of-band second factors.** SMS/email OTP, CAPTCHA and passkeys stay out of scope; they need a
  login hook or an imported session, as today.
- **Automatic classification of member-derived values.** That is the member-data track
  (`MEMBER_DATA_RESOLVER_PLAN.md`); this document is only about getting the right member into the
  right environment.
- **Per-member branching of a single recipe.** One artifact, one active login type per member
  population. A genuinely different login form is a different recipe.

---

## Implementation order (by risk retired per unit of work)

1. **F1** — environment must route the run, and posture must be judged on the destination. Nothing
   else matters while a run can hit production while reporting uat.
2. **F2** — derive card fields from the recipe, and refuse a non-covering card server-side.
3. **F3 + F4** — make `verified` mean something, and make a slot change BLOCK instead of skip.
4. **F5** — retired/foreign persona refusal.
5. **F7** — the matrix view.
6. **F6** — unify the environment registries (largest, least urgent).

---

## Build status

| | state | where |
|---|---|---|
| **F1** environment routes the run | **built · deployed · flag `NEXUS_ENV_ROUTING` OFF** | `environment_routing.py`, dispatch in `test_factory.py` |
| **F2** card derived from the recipe + server-side refusal | **built · deployed · live-proven** | `card_contract.py`, `GET …/login-contract`, `PUT …/credentials/{env}`, `PersonaMatrixPanel.tsx` |
| F3 `verified` must mean a real login | not built (card rewrite already resets `verify_status`) | |
| F4 a slot change must BLOCK, not skip | not built (cards now record `login_type_key` + `recipe_version`, which is the hook) | |
| F5 retired / foreign-artifact persona refused at dispatch | not built | |
| F7 the matrix view | not built | |
| F6 one environment registry | not built | |

### F2, proven live (2026-08-01, `8831d6a4…`, real recorded login)

```
login-contract      -> email "Email address", password "Password"  (v1, lt_a99e085a…)
member_number/password -> 422  missing [email]     unexpected [member_number]
e-mail/password        -> 422  missing [email]     unexpected [e-mail]
email/"   "            -> 422  missing [password]  unexpected []
email/password         -> 200  slot_names [email, password] · recipe_version 1 · unverified
stored row             -> names only, 354 bytes ciphertext, no plaintext
```

**Known gap surfaced by the proof, not yet addressed:** `login_domain` resolved to
`sslip.io` for `136-85-106-73.sslip.io`. The registrable-domain rule collapses every
app on a public suffix-adjacent host into one domain, which weakens fleet reuse keying
(`login_type_key` still includes the login path and the field signature, so it is not a
correctness hole today). Worth a PSL check before reuse is offered across tenants.
