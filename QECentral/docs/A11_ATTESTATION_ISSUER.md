# A11 / T-WP-02 — QE-Central Attestation Issuer

Status: **implemented, tested, INDEPENDENTLY CERTIFIED WITH FINDINGS
(2026-08-20); NOT deployed; NOT live-proven.**
Certification: `A11_INDEPENDENT_CERTIFICATION.md` — reproducer
`bash Nexus_power/certification/a11/run_certification.sh`.
Branch: `feat/qec-dynamic-catalog-p0-p6`.
Suites: `platform/qe-central/tests/security/test_a11_*.py` (110 tests),
`platform/qe-central/tests/contract/test_gate1_walk_attestation_contract.py` (33),
`engines/qe-explorer/tests/` verifier suites (132).
Migration: `qec_023_attestation_issuer`.

This document is the architecture record for the trust-issuance half of walk
persistence. It says what the trust chain is, where each link is enforced, what
a bypass would have to defeat, and — explicitly — what this milestone does
**not** guarantee.

---

## 0. What already existed, and what was actually missing

The brief described A11 as building the issuance pipeline from scratch. Roughly
half of it was already in the tree, and building the other half started with
finding that out.

| Component | State before A11 | Action |
| --- | --- | --- |
| Explorer **verifier** (`engines/qe-explorer/app/attest.py`, 585 lines, red-teamed) | complete, wired into `main` → `guard_context.walk_attested` → `walker` | **untouched** |
| Ed25519 primitives (`app/services/signing.py`) | complete | **untouched** |
| Issuer **signing layer** (`app/services/walk_attestation.py`, 391 lines, 33 contract tests) | complete | one additive split (`revocation_claims`) |
| Key **custody** | did not exist | **built** — `app/services/attestation_keys.py` |
| **Authoritative provisioning truth** | did not exist | **built** — `env_provisioning_records` + `attestation_issuer.py` |
| Issuance **API** | did not exist | **built** — `app/routers/attestation.py` |
| **Revocation** storage / cache | did not exist | **built** — `app/services/attestation_revocation.py` |
| **Dispatch** integration | did not exist | **built** — `explorations._walk_attestation_for_dispatch` |

The pure signing layer was never the hard part, and it was already done. The
milestone's real content is the four rows in the middle: *custody*, *a truthful
answer to "is this environment genuinely disposable?"*, *revocation*, and
*the API boundary that decides who may ask*.

### Two contradictions between the brief and the shipped verifier

Both were resolved in favour of the verifier, which is already red-teamed and
already deployed.

1. **The claims field set.** The brief specifies claims of
   `tenant_id, app_id, env_id, env_kind, issued_at, expires_at, nonce`. The
   shipped `ProofClaims` model is `extra="forbid"` and declares
   `v, proof_id, issuer, environment_id, env_kind, tenant_id, crawl_id,
   target_origin, reset_procedure, issued_at_ms, expires_at_ms,
   max_walk_mutations_per_step`. Emitting the brief's field set would make
   **every proof fail** as `malformed_claims`. `proof_id` **is** the nonce — it
   is unique per issuance and is what `ProofReplayGuard` keys on. `app_id` is
   deliberately absent from the claims; it is recorded in the audit log instead,
   because the verifier binds on `environment_id` + `target_origin` + `crawl_id`
   and an unverified field in a signed statement is a liability.
   Test: `test_the_issued_claims_carry_exactly_the_fields_the_verifier_accepts`.

2. **"Single-crawl audience".** There is no `audience` claim. The audience *is*
   `crawl_id`, bound inside the signed claims and re-checked by the verifier
   against the dispatch. Same property, existing field.

---

## 1. The trust chain

```
                    ┌─ PLATFORM ADMIN ─────────────────────────────────┐
                    │  role=admin AND platform_admin claim             │
                    │  (a tenant token structurally cannot carry it)   │
                    └───────────────────┬──────────────────────────────┘
                                        │ POST /platform/attestation/
                                        │      provisioning-records
                                        ▼
                        env_provisioning_records            ← THE ROOT FACT
                        (env_kind, target_origin PIN,       tenant CANNOT write
                         budget, expiry, evidence)
                                        │
   ┌─ TENANT ADMIN ─┐                   │
   │ role=admin     │ POST .../provisioning-proof           issuer key: KMS-sealed
   └────────┬───────┘                   │                   Ed25519 (attestation_
            └───────────────────────────┤                    issuer_keys)
                                        ▼
                    attestation_issuer.issue_for_crawl
                    5 gates, all fail-closed  ──────────────▶ attestation_issuance_log
                                        │                     (audit, same txn)
                                        ▼
                    { proof, revocations }   ← both Ed25519-signed
                                        │
                                        │ attached to the crawl dispatch
                                        ▼
              qe-explorer  app/attest.verify_provisioning_proof
              12 checks, DENY by default, holds PUBLIC keys only
                                        │
                                        ▼
                    walk_attested ← derived from the VERDICT, never a flag
                                        │
                                        ▼
                            Phase.WALK  (bounded server-side mutation)
```

Four boundaries, four different things being proved:

| boundary | credential | what it proves |
| --- | --- | --- |
| operator → certification | JWT + `platform_admin` claim | *the platform* says this env is disposable |
| tenant → issuance | JWT + `role=admin` | this caller may **use** an existing certification |
| qe-central → explorer | Ed25519 over canonical JSON | these claims, from this issuer, unmodified |
| explorer → walk | its own verification verdict | this fleet, now, for this crawl, within this budget |

The recurring failure mode this design is built against is the M0.5 one: **a
boundary whose credential proves less than the code assumes.** Specifically, the
tenant's own `env_attestation.env_kind` proves only that a tenant typed a word.

---

## 2. A11.1 — Key custody

`app/services/attestation_keys.py`, table `attestation_issuer_keys`.

### 2.1 Why the key is envelope-sealed and not KMS-resident

> **CORRECTED 2026-08-20 — the original rationale in this section was factually
> wrong**, and the independent certification caught it (FINDING 1,
> `A11_INDEPENDENT_CERTIFICATION.md`). It claimed Cloud KMS has no Ed25519
> asymmetric key type and that KMS-native signing would require changing the
> verifier. **Both halves were false.** Cloud KMS supports `EC_SIGN_ED25519`
> (EdDSA on Curve25519, pure mode, raw input), and adopting it would **not**
> change the verifier at all: `SIG_ALG` is the wire string `"ed25519"`, and
> `EC_SIGN_ED25519` produces exactly Ed25519 bytes over exactly the input the
> verifier already canonicalises. Only the issuer's sign call changes, plus
> extracting the raw 32 bytes from the DER `SubjectPublicKeyInfo` that
> `GetPublicKey` returns.
>
> The residual risk in §2.2 was presented as *"the price of keeping the audited
> Ed25519 verifier."* **That price is not real.** The real reasons are below.
>
> ⚠️ The same false rationale is still in the module docstring of
> `app/services/attestation_keys.py`. It is **not corrected there yet** because
> that file is pinned by digest in `certification/a11/A11_SNAPSHOT.sha256`, and
> editing it would de-certify A11. It must be fixed in a follow-up change that
> is re-certified. Until then, **this section is the authority, not the
> docstring.**

The strongest custody is a key that never leaves the HSM: KMS holds it and
`asymmetricSign` is called per signature. `EC_SIGN_ED25519` makes that
**available**, and it is compatible with the shipped verifier. It was
nevertheless not adopted, on these grounds:

* **A KMS round-trip per signature couples issuance availability to KMS.** Every
  proof would require a live `asymmetricSign` call rather than a live `decrypt`.
  Both couple to KMS; signing couples *per signature* where the envelope couples
  *per unseal*, and an unseal can be scoped to a request while a signature
  cannot be scoped to less than itself.
* **`ASYMMETRIC_SIGN` is a different key purpose from the existing
  `ENCRYPT_DECRYPT` KEK.** It needs its own key provisioning, its own IAM role
  (`roles/cloudkms.signer`), and its own rotation story — none of which exist in
  this deployment, and per `M0_CLOSURE` the prod deploy is *already* blocked on a
  missing KMS key.
* **The envelope pattern is the established M0.5 house pattern** and is what
  `signing.py` was written for ("the envelope sealing of the private key lives in
  the persistence layer").

**This decision should be re-taken on these true grounds.** The certification's
recommendation stands: the envelope design may well still be right, but it was
justified by a fiction, and the next engineer to read this deserves the real
trade-off. Moving to `EC_SIGN_ED25519` would eliminate the §2.2 residual risk
entirely and is the correct long-term direction if the operational cost is
acceptable.

### 2.2 What the envelope model does and does not give

**Does:**

* the private key is never on disk, in an env var, in config, in the image, in a
  deployment manifest, or in git;
* at rest it is an AES-GCM ciphertext whose DEK is wrapped by Cloud KMS
  (AAD = `__platform__`) — a full DB dump or a stolen backup yields **no**
  signing capability without live `cloudkms.cryptoKeyEncrypterDecrypter` on the
  KEK;
* every unseal is a KMS API call, so every unseal appears in Cloud Audit Logs
  whether or not this service is trusted to report it.

**Does not — stated plainly, because no undocumented assumptions are permitted:**

* the plaintext key exists in the qe-central process heap for the duration of a
  signature. A running-process memory disclosure (heap dump, core file,
  arbitrary-read RCE) inside qe-central discloses it. Python cannot zero a
  `str`; `Signer.close()` drops references and no more. It is hygiene, not
  erasure, and is not claimed as more.

That residual risk is bounded by rotation and detected by KMS audit logs. It is
an **accepted, documented assumption**, not a gap — but see the correction in
§2.1: it is **not** the unavoidable price of keeping the audited verifier, as
this document originally claimed. It is eliminable by moving to Cloud KMS
`EC_SIGN_ED25519`, at a cost in issuance availability and new key provisioning.

### 2.3 The key never crosses a module boundary

Nothing in `attestation_keys` returns a plaintext private key. `active_signer`
yields a `Signer` exposing only `sign_claims`, `kid`, `public_key`, `issuer`.
It uses `__slots__`, so there is no instance `__dict__` to walk for one either.
Callers obtain **signatures**, never key material.
Tests: `test_the_private_key_never_leaves_the_custody_module`,
`test_a_signer_is_unusable_after_its_scope_closes`,
`test_the_stored_private_key_is_ciphertext`.

Defence in depth: on every unseal the derived public key is compared against the
stored `public_key` and `kid`. A row whose columns have drifted apart (a bad
restore, a hand-edited row) refuses to sign rather than producing proofs nobody
can verify — an outage that would otherwise look like a fleet trust-store fault
and send the operator to the wrong service.
Test: `test_an_inconsistent_key_row_refuses_to_sign`.

### 2.4 Rotation

The database permits **at most one `active` key** (partial unique index), so
rotation is a sequence, not a race.

| state | signs? | published to fleet? | meaning |
| --- | --- | --- | --- |
| `active` | yes | yes | the current signing authority (exactly one) |
| `retiring` | no | **yes** | superseded; its in-flight proofs must keep verifying |
| `revoked` | no | no | compromised — every proof it signed is suspect |

**Ed25519 key rotation** (`POST /platform/attestation/keys` with `rotate: true`)
retires the incumbent and mints a successor in one transaction. The retired key
stays *published* so proofs already in flight keep verifying until they expire.
Revoking on rotation would invalidate every in-flight crawl at once, turning
routine hygiene into a fleet-wide outage.
Test: `test_rotation_keeps_the_old_key_verifiable`.

**KEK rotation** (`POST /platform/attestation/keys/rewrap`) is a different and
much cheaper operation: it re-wraps each sealed DEK under the current KMS key
version. The signing key does not change, no proof is invalidated, no public key
moves, no explorer needs reconfiguring.

**Operational order — PUBLISH BEFORE YOU SIGN.** Explorers learn public keys
from configuration. A key that signs before the fleet has been told about it
produces `unknown_key_id` on every dispatch. Both key endpoints return the exact
`QEC_ATTESTATION_PUBLIC_KEYS` / `QEC_ATTESTATION_ISSUER` values to deploy, so an
operator never transcribes key material by hand.

Recommended cadence: Ed25519 issuer key every 90 days; KEK re-wrap whenever the
KMS key version rotates; immediate revocation on any suspicion of compromise.

### 2.5 Public key distribution

`GET /platform/attestation/keys` returns active + retiring public keys and the
ready-to-paste trust-store values. It carries no secret, so a deployment
pipeline may call it directly. The alternative — embedding public keys in
trusted explorer builds — remains available and is strictly stronger against a
compromised qe-central; it costs a rebuild per rotation.

### 2.6 Disaster recovery

| loss | consequence | recovery |
| --- | --- | --- |
| `attestation_issuer_keys` rows | no signing authority | bootstrap a new key; republish; **walk persistence is off in the meantime, and nothing else breaks** |
| KMS KEK destroyed | sealed keys unrecoverable | same as above — the Ed25519 key is not itself precious, only its *continuity* is |
| Key compromised | every proof it signed is suspect | `POST .../keys/{kid}/revoke`, refresh every explorer's trust store, then bootstrap |

The issuer key holds no irreplaceable state. That is deliberate: losing it costs
a re-bootstrap and a config push, not evidence. Evidence integrity is owned by
the hash-chain re-derivation described in `signing.py`.

---

## 3. A11.2 — Issuance, and the fact everything rests on

`app/services/attestation_issuer.py`, `app/routers/attestation.py`.

### 3.1 The attack that made a new table necessary

`app_environments.env_attestation` already holds an `env_kind`, on the row the
endpoint has to load anyway. Reading it would have been one line.

It is also written by `PATCH /apps/{id}/environments/{env}` — **a tenant
endpoint**. A tenant who types `"env_kind": "disposable"` into their own
environment profile would thereby cause the platform to sign a statement that
their environment is safe to mutate, and the explorer — which correctly trusts
signatures over dispatch bodies — would believe it.

**Signing a tenant-supplied fact does not make it true. It makes it a signed
lie**, and one that is *harder* to detect than the unsigned kind because
everything downstream is now cryptographically satisfied.

`env_provisioning_records` is the fix. One writer
(`require_platform_admin`), read by the issuer as the **only** source of
`env_kind`. `env_attestation` keeps its existing job (the human RoE statement
that gates SUBMIT) and loses the one it should never have had.

**The trust boundary is now a row, and that is deliberate.** This does not make
"genuinely disposable" a mathematical fact; it makes it an *attributable
platform decision* with a named principal, a timestamp, and evidence attached —
the strongest honest claim available, and exactly what the signature then binds.

### 3.2 The five gates

All fail-closed, all with stable reason codes, none skipped because an earlier
one was ambiguous.

| # | Gate | Refusal | Attack it stops |
| --- | --- | --- | --- |
| 1 | active, unexpired provisioning record exists | `no_provisioning_record` / `provisioning_expired` | tenant self-attestation; stale certification of a torn-down env |
| 2 | the record says `disposable` | `not_disposable` | certifying prod and hoping nobody re-reads it |
| 3 | pinned origin == live `base_url` == requested target | `origin_moved` / `origin_mismatch` | **certify a throwaway host, then PATCH `base_url` to production** |
| 4 | the environment is not revoked | `environment_revoked` | using a burned environment |
| 5 | revocation state is *readable* | `revocation_unavailable` (503) | signing "nothing is revoked" during a DB outage |

Gate 3 is the one a review is most likely to miss. Without it every other gate
still passes — the record is active, disposable and unexpired — and the crawl is
dispatched at **production** holding a valid mutation proof. The claims are
built from the **pin**, never from the row, so a future refactor that loses gate
3 still cannot sign a moved origin.
Test: `test_a_tenant_cannot_move_a_certified_environment_to_production`.

### 3.3 Endpoint specification

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/qec/apps/{app_id}/environments/{env}/provisioning-proof` | tenant admin | mint a crawl-bound proof |
| GET | `/api/v1/qec/apps/{app_id}/environments/{env}/provisioning-record` | tenant admin | what the platform certified (and why) |
| POST | `/api/v1/qec/attestation/revocations` | tenant admin | revoke a proof or an environment |
| GET | `/api/v1/qec/attestation/revocations` | tenant admin | current revocation state |
| POST | `/api/v1/qec/platform/attestation/provisioning-records` | **platform admin** | certify an environment |
| DELETE | `/api/v1/qec/platform/attestation/provisioning-records/{id}` | **platform admin** | withdraw a certification |
| POST | `/api/v1/qec/platform/attestation/keys` | **platform admin** | bootstrap / rotate the issuer key |
| GET | `/api/v1/qec/platform/attestation/keys` | **platform admin** | publish public keys |
| POST | `/api/v1/qec/platform/attestation/keys/{kid}/revoke` | **platform admin** | compromise response |
| POST | `/api/v1/qec/platform/attestation/keys/rewrap` | **platform admin** | KEK rotation |

**Request** (`ProvisioningProofRequest`, `extra="forbid"`):

```json
{ "crawl_id": "…", "target_url": "…", "max_walk_mutations_per_step": 1 }
```

`crawl_id` is required — there is no "issue me one for later" mode, because a
proof not bound to a crawl is a reusable mutation capability. `target_url` can
only *narrow*, never redirect. `max_walk_mutations_per_step` can only
*de-escalate*; a larger request is floored to the certified value.

**Response** (`Cache-Control: no-store` — it is a capability, not a document):

```json
{
  "attestation": { "proof": {…}, "revocations": {…} },
  "proof_id": "…", "kid": "…", "issuer": "…",
  "environment_id": "…", "target_origin": "https://…",
  "issued_at_ms": 0, "expires_at_ms": 0,
  "proof_expires_at_ms": 0, "revocation_expires_at_ms": 0,
  "max_walk_mutations_per_step": 1, "claims_digest": "…"
}
```

**Status codes.** `403` for a refused *statement* (the request was well-formed
and authenticated; the platform declines to say the thing) with
`{reason, message}`; `422` for a malformed request; `429` rate limited; `503`
for KMS unavailable, no issuer key, or unreadable revocation state — all
fail-closed and all retryable.

### 3.4 Rate limiting

The global `PrincipalRateLimiter` is **default-OFF** (`QEC_API_RATE_LIMIT=0`),
so "there is a limiter somewhere" is not a control this endpoint may rely on.
Issuance performs a Cloud KMS decrypt per call; unbounded, that is a billable
denial-of-service against the platform's own root of trust and a route to KMS
quota exhaustion that would take issuance down for **every** tenant. The router
therefore carries its own limiter, ON by default at 1/s per principal with a
burst of 5 (`QEC_ATTESTATION_ISSUE_RATE` / `_BURST`). A proof is minted once per
crawl dispatch, so this is orders of magnitude above the real workload.

### 3.5 Audit trail

Every issuance writes `attestation_issuance_log` **in the same transaction as
the decision** — if the audit write fails, the issuance fails. It records
`proof_id, tenant, app, environment, crawl_id, kid, claims_digest,
target_origin, issued_at_ms, expires_at_ms, budget, issued_to, request_id,
provisioning_id`. `claims_digest` is computed identically to the verifier's, so
an auditor can join a line in the explorer's log to a row here **without either
side holding the proof**.

An unlogged issuance is an unrevocable one: revocation by `proof_id` needs the
id, and only the log has it.

Log lines: `qec.attest.proof_issued` (WARNING — a crawl just gained mutation
authority), `qec.attest.issue_refused` (WARNING), `qec.attest.revoked` (ERROR),
`qec.attest.issuer_key_generated` / `_rotated` (WARNING),
`qec.attest.issuer_key_REVOKED` (ERROR), `qec.attest.environment_certified`
(WARNING when disposable). None carries key material, a signature, or a request
body.

---

## 4. A11.3 — Revocation

`app/services/attestation_revocation.py`, table `attestation_revocations`.

An expiry is not revocation: a proof that leaks ten minutes after issue stays
valid for the rest of its life. The verifier makes a signed, unexpired
revocation list **mandatory** on every dispatch — no list, a stale list, or a
badly-signed list is a DENY for the whole attestation.

**Two subjects.** `proof` (one issued proof) and `environment` (every proof for
an environment, including ones not yet issued). The environment form is the
blast-radius control: when an environment turns out not to be disposable, you
revoke *it*, rather than enumerating proofs while an issuer is still minting more.

**INSERT-ONLY.** A revocation is never edited or deleted — a revocation an
attacker can delete is not a revocation. There is no un-revoke; re-permitting an
environment means certifying it afresh, producing new ids.

**Idempotent.** In an incident two responders will hit the endpoint at once, and
an error that reads like the revocation failed is actively dangerous.

### 4.1 Fail-closed, precisely

The rule — *if revocation status cannot be determined, treat the proof as
revoked* — lands in two different places, and conflating them is how fail-open
bugs get written:

* **At the verifier** (already shipped): an unusable list is a DENY.
* **At the issuer** (this module): if revocation state cannot be **read**, refuse
  to issue. Never sign "nothing is revoked" because the database was
  unreachable — that is a signed lie the fleet believes for the full life of the
  list. `current_revocations` **raises**; the router returns 503.

Tests: `test_unreadable_revocation_state_refuses_issuance`,
`test_the_cache_is_never_populated_by_a_failed_read`.

### 4.2 The cache invariant

**A cache entry may only ever be populated by a successful read.** A failed read
never writes the cache, never extends an entry's TTL, and never causes an
expired entry to be served. A cache that answered from stale data during an
outage would convert the fail-closed path above into a silent fail-open.

TTL is 30s — deliberately much shorter than the signed list's own 10-minute
lifetime, because the two staleness windows compound. Writes invalidate their
tenant's entry synchronously, so within one process a revocation is effective
immediately. Issuance forces a fresh read (`use_cache=False`): a proof is never
minted against a cached "not revoked".

Lists are **per-tenant**, so one customer's dispatch never discloses the
existence or identifiers of another customer's revoked environments.

### 4.3 Honest limit — when revocation takes effect

The explorer verifies the attestation **once, at dispatch**
(`main._walk_authorization`). Revocation is therefore enforced at **admission**:
a revoked proof cannot start a crawl, and cannot be replayed into another (the
claims bind `crawl_id`; `ProofReplayGuard` binds a `proof_id` to the first crawl
that used it). It does **not** retroactively stop a crawl already running under
a proof admitted before the revocation was recorded.

The exposure window is *"the remainder of an in-flight crawl"*, not *"the
remainder of the proof's lifetime"* — bounded further by the per-step mutation
budget and the crawl's own budgets. **To stop an in-flight crawl, revoke *and*
cancel the crawl.** The `DELETE .../provisioning-records/{id}` and revocation
responses both say so rather than leaving an operator to discover it.

This is a property of the shipped verifier's dispatch-time verification model,
not something A11 introduced. Continuous mid-crawl revocation would require the
explorer to re-verify periodically — a change to `attest.py`'s call site, out of
scope here and recorded in §8.

---

## 5. A11.4 — Dispatch integration

`explorations._walk_attestation_for_dispatch` → `_issue_walk_proof` →
`_merge_walk_attestation`.

**Nothing in the crawl request selects the environment.** The provisioning
record is found by matching the crawl's own `base_url` origin against the origin
a platform admin pinned. There is deliberately no `environment_id` parameter on
the crawl API: a caller-supplied identifier would be one more tenant-controlled
input on the path to mutation authority, and the origin match is both sufficient
and unforgeable — a tenant who changes `base_url` to reach a certified record
thereby moves the crawl to the certified origin, which is exactly what the
record authorises.

Two or more active records at one origin (impossible through the API; a partial
unique index forbids it) causes a **refusal to choose**. Any tie-break rule is a
rule an attacker who can create a row gets to exploit.

**Returning `None` is always safe and is the ordinary answer.** No proof means
the verifier denies with `no_proof` and the crawl catalogues without persisting
— precisely pre-A11 behaviour. Every failure mode is therefore swallowed into
`None`: a crawl must not fail because an *optional* capability could not be
granted. The log distinguishes "nothing was certified" (DEBUG — true of every
production crawl forever) from "something was certified but issuance failed"
(WARNING — an operator expected walk persistence and will not get it).

Issuance runs in **its own transaction**. The audit row records that a proof
*was minted*, which stays true even if the dispatch that requested it
subsequently fails; an audit trail that rolls back with its caller under-reports
exactly the incidents it exists for.

### 5.1 The platform contract

> `walk_attested` is set **only** after successful cryptographic verification.

qe-central may only ever *attach bytes*. It has no way to set `walk_attested`
and must not acquire one. Enforced two ways:

* `test_qe_central_has_no_way_to_set_walk_attested` tokenises **every** Python
  file in qe-central and fails if `walk_attested` appears as a NAME in code
  (comments and docstrings explaining why it must not be set are fine, and
  wanted).
* `test_the_dispatch_request_model_has_no_walk_authority_field` asserts the wire
  contract carries no `walk_attested` / `walk_authorization` /
  `max_walk_mutations_per_step`. The proof travels *inside* `attestation`, where
  it is bytes to be verified rather than a flag to be believed.

`_merge_walk_attestation` copies **only** `proof` and `revocations` into the
legacy statement, so a future issuer returning extra keys cannot inject them
into the RoE object the explorer parses with `extra="forbid"`.

---

## 6. A11.5 — Security validation

`platform/qe-central/tests/security/test_a11_*.py` — **110 tests, all green.**

### 6.1 What makes the suite mean something

It verifies against the **real, shipping verifier**:
`engines/qe-explorer/app/attest.py`, loaded from source by path — not a copy,
not a mock, not a frozen fixture. This is possible because `attest.py` imports
nothing from its own package (stdlib + pydantic only), so it loads under a
distinct module name without dragging in the explorer's `app` package, which
would collide with qe-central's own.

Every `rejected` below is therefore a rejection by the **same bytes that run on
a crawl worker**. If somebody edits `attest.py`, these tests change behaviour
immediately. (The Gate-1 contract test freezes the same seam as *data*; this
suite is the complementary half — same seam, checked live.)

Ed25519 is real. AES-GCM envelope sealing is real (`EnvelopeService` +
`LocalKekProvider`). The issuer gates are the production ones. The **only** fake
is the database session, because these gates are pure decisions over rows; it
dispatches on the real ORM entities, evaluates the real WHERE clauses, and
**raises rather than over-matching** when it meets a clause it cannot evaluate,
so it cannot quietly turn a failing gate into a passing test.

### 6.2 Required scenarios

| # | Scenario | Result | Reason code | Test |
| --- | --- | --- | --- | --- |
| 1 | **Forged signature** — attacker's own key | REJECTED | `unknown_key_id` | `test_forged_signature_is_rejected` |
| 1b | Forged signature under the *genuine* `kid` | REJECTED | `bad_signature` | `test_forged_signature_under_a_known_key_id_is_rejected` |
| 1c | Any signed claim edited (×6 fields) | REJECTED | `bad_signature` | `test_editing_any_signed_claim_breaks_the_proof` |
| 1d | `alg: "none"` downgrade | REJECTED | `unsupported_alg` | `test_an_unsigned_proof_is_rejected` |
| 2 | **Expired proof** | REJECTED | `expired` | `test_expired_proof_is_rejected` |
| 2b | Valid 1 ms before expiry (edge, other side) | ACCEPTED | `ok` | `test_a_proof_is_still_valid_one_millisecond_before_expiry` |
| 2c | Fleet lifetime ceiling beats the issuer | REJECTED | `lifetime_too_long` | `test_the_fleet_refuses_an_over_long_proof_even_if_one_were_minted` |
| 3 | **Revoked proof** | REJECTED | `revoked` | `test_revoked_proof_is_rejected` |
| 3b | Revoked *environment* kills existing + future | REJECTED | `revoked` / `environment_revoked` | `test_revoking_an_environment_kills_every_proof_for_it` |
| 3c | Missing revocation list | REJECTED | `no_revocation_list` | `test_a_missing_revocation_list_denies_the_whole_attestation` |
| 3d | Expired revocation list | REJECTED | `revocation_expired` | `test_an_expired_revocation_list_denies_the_attestation` |
| 3e | Attacker-signed empty list | REJECTED | `revocation_bad_signature` | `test_a_revocation_list_signed_by_an_attacker_is_rejected` |
| 4 | **Replay** into another crawl | REJECTED | `crawl_binding_mismatch` | `test_replaying_a_proof_into_a_different_crawl_is_rejected` |
| 4b | Same `proof_id`, second crawl | REJECTED | guard refuses | `test_replaying_the_same_proof_id_twice_is_rejected` |
| 4c | Cross-tenant replay | REJECTED | `tenant_mismatch` | `test_a_proof_for_another_tenant_is_rejected` |
| 4d | Origin replay (aim at production) | REJECTED | `origin_mismatch` | `test_a_proof_is_bound_to_one_origin` |
| 5 | **Tenant self-attestation** | REJECTED | `no_provisioning_record` | `test_a_tenant_cannot_certify_its_own_environment` |
| 5b | Platform says prod, tenant says disposable | REJECTED | `not_disposable` | `test_the_issuer_never_reads_the_tenant_writable_env_kind` |
| 5c | Certify throwaway, repoint `base_url` at prod | REJECTED | `origin_moved` | `test_a_tenant_cannot_move_a_certified_environment_to_production` |
| 5d | Tenant admin POSTs a certification (real JWT) | REJECTED | HTTP 403 | `test_a_tenant_admin_cannot_certify_their_own_environment` |
| 5e | Budget escalation 3 → 10 | FLOORED to 3 | — | `test_a_caller_cannot_widen_the_certified_mutation_budget` |
| — | **Happy path**, end to end | ACCEPTED | `ok` | `test_a_genuinely_provisioned_disposable_environment_enables_walk` |

Plus custody (private key never leaves the module; stored blob is ciphertext;
inconsistent row refuses; rotation keeps old proofs verifiable; revoked key
unpublished; no key ⇒ fail-closed), API authorization (all 9 routes refuse
anonymous; viewers refused; tenant admins refused on all platform routes with
**real minted JWTs**; `extra="forbid"` proved at the wire against 5 smuggling
attempts), dispatch integration (11 paths to "no proof", all safe), and the
cross-service contract (shared constants, canonical encoding byte-identical,
`key_id` derivation, origin normalisation, exact claim field sets).

### 6.3 Two defects this work found

**A false claim in the certification checklist, made true.** Writing Step 5 of
`A11_CERTIFICATION_CHECKLIST.md` produced the line *"double underscores are not
a legal tenant slug"* — asserted about the `__platform__` KEK tenant id the
issuer key is sealed under. It was **false**: `provision_tenant` takes
`tenant_id` verbatim as its idempotency key, so a platform admin could have
provisioned a customer into the platform's own envelope namespace. Never
directly exploitable — `active_signer` re-derives the public key and refuses a
row whose halves disagree — but an assumption that holds only because a *second*
control happens to catch it is exactly the undocumented dependency this
milestone may not have. Now enforced by `fleet.provisioning.RESERVED_TENANT_IDS`
and pinned by two tests.

**A lifetime the API reported wrongly.** Writing scenario 2 exposed a real defect in the first draft of
`attestation_issuer.py`: proofs were minted with a **1-hour** lifetime alongside
**10-minute** revocation lists. Because the verifier requires *both*, the
attestation's real usable life was 10 minutes while the API reported an
`expires_at_ms` fifty minutes later. Fail-*closed* (so never unsafe), but the
API was telling callers a validity window that was not true.

Fixed by tying `DEFAULT_PROOF_LIFETIME_MS` to `DEFAULT_REVOCATION_LIFETIME_MS` —
the revocation list is the half that must stay short, so the proof moves down to
meet it — and by returning `proof_expires_at_ms`, `revocation_expires_at_ms` and
an effective `expires_at_ms` computed as the minimum, so the number cannot
mislead even if the two are ever re-tuned independently.

---

## 7. Production readiness

| Requirement | State | Evidence |
| --- | --- | --- |
| Zero trust | ✅ | no input from the request influences a trust decision; §3.2 |
| Least privilege | ✅ | `min(request, record, fleet policy)`; platform/tenant split |
| Cryptographic integrity | ✅ | Ed25519 over canonical JSON; tamper tests ×6 fields |
| Fail closed | ✅ | no key / no KMS / no record / unreadable revocation ⇒ refuse |
| Complete auditability | ✅ | issuance log in-transaction; KMS audit logs; stable reason codes |
| Deterministic verification | ✅ | injected clock; canonical encoding; contract suite |
| No implicit trust | ✅ | `extra="forbid"` on all models both sides |
| Replay resistance | ✅ | `proof_id` nonce + crawl binding + `ProofReplayGuard` |
| Key isolation | ⚠️ | KMS-sealed; **plaintext in heap during a sign** — §2.2 |
| Rotation readiness | ✅ | one-active index; retire-then-mint; KEK re-wrap |
| Backward compatibility | ✅ | inert until a key is bootstrapped; full suites green (2421 qe-central + 2006 qe-explorer at time of writing) |
| RLS on new tenant tables | ✅ (code) | qec_023 ENABLE+FORCE+policy ×3; key table declared tenant-free with reason |

**Deployment order.** Deploying A11 changes nothing until an operator acts, by
design — with no issuer key, nothing is issued and every crawl behaves exactly
as before.

1. `alembic upgrade head` (qec_023) — purely additive.
2. Deploy qe-central.
3. `POST /platform/attestation/keys` — bootstrap the issuer key.
4. Push `QEC_ATTESTATION_PUBLIC_KEYS` + `QEC_ATTESTATION_ISSUER` to **every**
   explorer worker, and restart them. **Before step 5.**
5. `POST /platform/attestation/provisioning-records` — certify the first
   disposable environment.
6. Dispatch a crawl at that origin; confirm
   `qec.explorations.walk_proof_attached` and
   `qec.explorer.walk_persistence_granted`.

Rollback: revoke the issuer key, or retire the certification. Either returns the
fleet to catalogue-only within one trust-store refresh.

---

## 8. What this milestone does NOT guarantee

Stated explicitly, because no undocumented trust assumptions are permitted.

1. **Key isolation is envelope-grade, not HSM-grade.** §2.2. Bounded by rotation
   and KMS audit logs; would require abandoning the Ed25519 verifier to improve.
2. **"Genuinely disposable" is a platform-admin decision, not a proof.** §3.1.
   The chain guarantees the decision is attributable, unforgeable by the tenant,
   pinned to an origin, and time-bounded. It cannot guarantee the human was
   right. Automated evidence (a teardown-job handle, an ephemeral-namespace id)
   is recorded in `evidence` but is deliberately *not* an input to a decision.
3. **Revocation is enforced at admission, not continuously.** §4.3.
4. **Not deployed. Not live-proven.** Every result here is from the test suite.
   No crawl has yet enabled `Phase.WALK` against a real application under a real
   KMS-sealed key.
5. **GCP KMS is not exercised.** The custody code paths are proven against
   `LocalKekProvider` with real AES-GCM. `GcpKmsProvider` changes *where the KEK
   lives*, not the envelope format, the AAD binding or the unwrap path — but
   that substitution is unproven here and must be verified on the VM.
   (Per `M0_CLOSURE`, prod deploy is separately blocked on a missing GCP KMS key.)
6. **The RLS coverage gate for qec_023 has not run.** It is `QEC_TEST_QEC_DATABASE_URL`-gated
   and skipped without Postgres; CI runs it. The migration follows the qec_003
   pattern exactly, but "follows the pattern" is not "was verified against a
   database".
7. **All findings from the independent certification are CLOSED.** Both original findings (CERT-FINDING-1, the false KMS rationale; CERT-FINDING-2, IPv6 `normalize_origin`) were fixed at `d0605ba`, and the four raised while closing them at `da5b5d0` and after. Zero open. See `CERT_FINDING_REGISTER.md`; the reproducer now runs **161 checks / 0 failures**, and `run_certification.sh` exits 0 end to end.
   Both were fixed in the pinned files themselves — `attestation_keys.py`,
   `attest.py` and `walk_attestation.py` — which lapsed the certification by
   construction, so the record was re-issued against the new SHA by a non-author
   squad rather than patched quietly. Neither was ever a bypass; both failed
   closed throughout.

---

## 9. Independent certification — COMPLETE (with findings)

The ARB rule: **A12 must not begin until A11 is certified by a different
engineering squad**, and implementation alone is insufficient.

**Certified 2026-08-20 by a non-author squad.** Verdict: **CERTIFIED WITH
FINDINGS**; A12 / T-WP-01 is unblocked. Record:
`A11_INDEPENDENT_CERTIFICATION.md`.

The certification pins **nine files by SHA-256**
(`certification/a11/A11_SNAPSHOT.sha256`) and refuses to run if any drifts. It
certifies specific bytes, not "whatever is in the tree". It has two halves: a
reproduction of the author's suite (necessary, weak alone) and independent
checks derived from the contract and threat model, written **without importing
the author's fixtures** — which is where both findings came from.

Reproduce:

```bash
bash Nexus_power/certification/a11/run_certification.sh
# 161 checks; 0 failures. (131/1 -> 150/3 while CERT-FINDING-1 and -2 were
# open; 151/0 when they were fixed; 152/0 with CERT-FINDING-9's third KMS
# assertion; 161/0 once CERT-FINDING-16 gave gates 7, 9, 11 and 12 any
# coverage at all. Every rise is the harness covering MORE — see
# CERT_FINDING_REGISTER.md, which explains each one.)
```

### The two original findings — both CLOSED, neither ever a bypass

| # | Finding | Severity | Status |
| --- | --- | --- | --- |
| 1 | The KMS rationale was factually false — `EC_SIGN_ED25519` exists and needs no verifier change | Material (rationale) | **CLOSED** at `d0605ba`; docstring corrected, re-certified |
| 2 | `normalize_origin` drops IPv6 brackets and is not idempotent → an IPv6 environment gets a valid proof guaranteed to be refused | Medium (availability) | **CLOSED** at `d0605ba`; both copies fixed together, re-certified |

Four further findings were raised *while closing these two* — three against the
fixes themselves, one against the CI gate — and are also closed. The full set,
with the reasoning, is in `CERT_FINDING_REGISTER.md`. It took three passes to
state the KMS rationale truthfully, and every pass was caught by a non-author.

Neither blocks merge. Both fail closed. **Both need a re-certified follow-up**,
because every file they touch is inside the pinned snapshot — fixing them in
place silently de-certifies A11, which is precisely the trap the snapshot exists
to make visible.

### What the certification does NOT cover

* files outside the nine pinned (the test suites, `fleet/provisioning.py`, these
  docs) — including the `RESERVED_TENANT_IDS` guard added after the snapshot;
* GCP KMS (custody proven against `LocalKekProvider` with real AES-GCM);
* the `qec_023` RLS coverage gate (needs Postgres);
* any live `Phase.WALK` proof against a real application.
