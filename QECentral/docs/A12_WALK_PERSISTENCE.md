# A12 / T-WP-01 — Walk Persistence on an Attested Save-Draft Workflow

Status: **implemented and demonstrated in a real browser. NOT deployed, NOT
live-proven against a customer application.**
Branch: `feat/qec-dynamic-catalog-p0-p6`.
Suite: `engines/qe-explorer/tests/browser/test_a12_walk_persistence_live.py` (7 tests).
Depends on: **A11 certification** — see `A11_INDEPENDENT_CERTIFICATION.md`.
ARB gate satisfied: A11 was independently certified before this work began.

---

## 0. What already existed, and what was actually missing

T-WP-01 was not a greenfield milestone, and the honest accounting matters more
than a large-sounding gap. Two suites already existed:

* **`tests/test_gate1_twp01_execution.py` (20 tests)** — the authorisation
  algebra: four conditions, budget, window, audit chain, origin binding. **Every
  one calls `authorize_mutation()` directly and reads its boolean return.**
* **`tests/test_save_draft_wizard_e2e.py` (M1.3 / T-WP-05+06, 7 tests)** — a
  genuine crawl-level proof: the real `Crawler`, the real `GuardContext`, the
  real refuse pack, and a scripted application that refuses to serve step 2
  until a draft has actually been persisted, so walk depth measures what the
  network policy permitted. This is substantial prior art and A12 does not
  supersede it.

What neither covers is the same thing: **no request ever left a browser.** The
characterization harness wires the fake application's network to the real guard
by calling `guard.decide(...)` itself (`tests/characterization/harness.py`). That
proves the *decision* under a faithful simulation of the network layer — it does
not execute the code that enforces the decision in production.

`app.main._make_route_handler` is the only place a WALK decision is ever applied
to a real request, and before A12 it was referenced by exactly one file in the
repository: `app/main.py` itself. No test had ever executed it.

A12's acceptance criterion is not "the authorizer returns True". It is **"the
save-draft wizard successfully persists WALK state."** Those are different
claims, and only the second one is persistence.

| Component | State before A12 | Action |
| --- | --- | --- |
| `WalkAuthorization` / budget / window / audit chain | complete, 20 tests | **untouched** |
| `GuardContext.decide` → `_charge_walk_mutation` | complete | **untouched** |
| `app.main._make_route_handler` (the ENFORCEMENT seam) | complete, **never exercised by any test** | **exercised** |
| An application that actually persists a draft | did not exist | **built** (in the test module) |
| End-to-end demonstration in Chromium | did not exist | **built** |

The gap was precise: the decision is made in `guard_context`, but it is only
*enforced* inside a Playwright `context.route('**/*')` handler. Nothing had ever
driven a real request through that seam.

---

## 1. Why the application is defined in the test module

Fixture `10-save-draft-wizard` is the obvious target and is the wrong one.

Its `Save Draft` is `<button type="button">` with **no handler and no `<script>`
anywhere in the file**. It cannot persist because it never issues a request. It
is a *capture* regression guard for the constraint block, and its own README
states its targeted defect is "None".

The shared `_harness.FixtureServer` is also not usable as-is: it accepts `POST`
only under the scripted `/__net/` namespace, which returns canned status codes
and stores nothing.

A12 therefore ships its own minimal application with genuine server-side state —
`GET /` renders the stored draft server-side, `POST /draft` keeps it. WALK
persistence is only demonstrable against something that has state to persist.

This also avoided editing `_harness.py`, a file several concurrent sessions were
writing at the time.

---

## 2. The chain under test is production code end to end

```
_attest_kit.Issuer                    real Ed25519 key, real signature
  -> attest.verify_provisioning_proof the red-teamed verifier
  -> WalkAuthorization.from_verdict   production
  -> GuardContext(phase=WALK)         production
  -> app.main._make_route_handler     production — the enforcement seam
  -> Chromium context.route('**/*')   real browser, real fetch
  -> an HTTP server that really stores the draft
```

Nothing is stubbed except the crawl's browser *port* (`None`), which the route
handler never touches.

**Persistence is read back after a reload, from a second context with no guard
on it.** A `200` proves a request was answered; only the reload proves it was
kept, and only the unguarded context proves the answer does not depend on the
crawl's permission to ask.

---

## 3. The seven tests, and why each is not redundant

| Test | What it would catch |
| --- | --- |
| `an_attested_walk_persists_the_saved_draft` | **THE acceptance criterion.** Draft survives a reload. |
| `an_unattested_walk_cannot_persist_anything` | The gate is real: same page, same click, no proof → nothing reaches the app. |
| `the_guard_is_what_blocks_it_and_not_the_application` | **Falsification control** (see §4). |
| `a_proof_for_another_origin_does_not_persist_here` | Separates *"a proof was presented"* from *"a proof for THIS environment was presented"*. |
| `the_mutation_is_audited_before_it_is_released` | Ledger names the `proof_id` it crossed on, records `env_kind=disposable`, and the chain verifies. |
| `the_per_step_budget_stops_the_second_save` | Budget bounds what reaches the application, not just what the authorizer returns. |
| `a_closed_window_refuses_even_under_a_valid_proof` | *"This environment may be mutated"* ≠ *"this click may mutate it."* |

---

## 4. The falsification control, and why the suite would be worthless without it

`"blocked"` is read from a **rejected `fetch` promise**. A `fetch` rejects for
many reasons that have nothing to do with attestation: a CORS refusal, a dead
socket, a 500, a typo in the URL. Any of those would make the unattested test
pass **while the WALK gate was wide open** — a green suite proving nothing, which
is the exact failure mode this repository has been burned by before.

`test_the_guard_is_what_blocks_it_and_not_the_application` runs the same page and
the same click with **no route handler installed at all** and requires the POST
to reach the application. That is what licenses attributing every other
`"blocked"` in the file to the guard rather than to the environment.

The negative results are therefore a controlled experiment: the only variable
between "saved" and "blocked" is the `WalkAuthorization`.

---

## 5. Acceptance

| Criterion | Status |
| --- | --- |
| Save-draft wizard successfully persists WALK state | ✅ draft survives a reload, read from an unguarded context |
| Only verified attested environments permit persistence | ✅ unattested blocked (0 writes); a valid proof for **another** origin also blocked |
| No unauthenticated WALK mutation succeeds | ✅ 0 writes reach the application without a verified proof |
| Begins only after A11 certification | ✅ A11 certified first; ARB gate honoured |

**Not claimed:** this is a local Chromium demonstration against a purpose-built
application. It is **not** a live customer-application proof, and it is not
deployed. Gate 2 is where the journey becomes *actual*.
