# VKPower Verdict — Threat Model (Phase 8)

**Scope:** the multi-client (20+ tenant) autonomous Centralized-QE product,
including the three surfaces that carry the most risk:

1. **Multi-client isolation** — many tenants' evidence + credentials in shared
   Postgres, one QE-Central control plane.
2. **The explorer holds client credentials** — to crawl a client app, the
   explorer decrypts and uses that client's real login credentials.
3. **On-prem / regulated install** — the product runs inside the buyer's
   perimeter (a "verdict box") with no outbound dependency on our infrastructure.

**Method:** STRIDE (Spoofing, Tampering, Repudiation, Information disclosure,
Denial of service, Elevation of privilege), each threat → the **mitigation
(mostly already built)** or a **named gap**. Honest: a mitigation marked
_operational_ depends on the deployment, not on code.

**Trust boundaries (data-flow):**

```
[tenant admin JWT] ──► QE-Central API ──► qec DB (role qec, RLS)
                          │  └─ service JWT ──► VKPower factory (UNCHANGED)
                          ├─► substrate writes ──► nexus DB (role qec_substrate, RLS)
                          └─► dispatch (HMAC) ──► Explorer ──► [CLIENT APP]
                                                     │ decrypts client creds (KMS)
                                                     └─ egress sandbox (allow-list)
```

The two crown-jewel boundaries are **(a)** the tenant→DB boundary (RLS) and
**(b)** the explorer→client-app boundary (credentials + egress).

---

## S — Spoofing (identity)

| Threat | Mitigation / gap | Enforcement |
|--------|------------------|-------------|
| Forged/replayed principal JWT to act as another tenant | HS256 signature verified against the shared secret; **audience gate** rejects a foreign `aud`; a token with a missing/empty `tenant_id` is **refused** (no default-tenant fallback). Fail-closed middleware rejects every non-public `/api/*` request without a valid token. | **CODE** — `app/auth.py`, `tests/unit/test_boot_and_aud.py` |
| VKPower human/service token replayed at the Verdict API (or vice-versa) | Distinct `aud` per service; a foreign audience is always rejected; the service token is minted least-privilege (`role=manager`, `sub=svc-qe-central`) and stays audience-compatible with the unchanged VKPower factory. | **CODE** — `app/service_token.py` |
| Explorer completion callback spoofed by an attacker | The callback lives outside `/api/*` and is authenticated by an **HMAC token** (`QEC_EXPLORER_TOKEN`), verified in `internal.py`; a deployed process refuses to boot with an empty/default explorer token. | **CODE** — `app/routers/internal.py`, `app/security/boot_validator.py` |
| Token-in-URL leak replayed to mutate data | The `?token=` query fallback is accepted on **GET only**; state-changing methods require the Bearer header. | **CODE** — `app/auth.py` (`_token_from_request`) |

## T — Tampering (integrity)

| Threat | Mitigation / gap | Enforcement |
|--------|------------------|-------------|
| Someone rewrites a past verdict to hide a failure ("green-wash after the fact") | Verdicts are **hash-chained** per `(tenant, artifact, test)`; the chain is independently re-derivable (`verify_verdict_chains`), so any rewrite is detected and surfaced. The compliance report marks a broken chain `partial`, never `satisfied`. | **CODE** — `verdict_events`, `app/compliance/adapter.py` |
| A dishonest crawl (fabricated actions/outcomes) is persisted | The evidence-bundle schema **refuses** non-monotonic indices, unknown verbs/kinds, or an action with no observed after-outcome (enumerated `RefusalError`). | **CODE** — `app/substrate/schema.py` |
| The verdict is inflated by the LLM | The LLM **never scores**; the decision is deterministic + min-gated over evidence-linked dimensions; unproven steps are declared UNPROVEN. | **CODE** — `app/harness/rules.py` |
| Compliance report itself altered before an auditor reads it | The report carries a `report_digest` (sha256 over its canonical body) a verifier recomputes; the underlying chains are re-derivable. | **CODE** — `app/compliance/adapter.py` (`report_digest`) |
| Substrate row smuggled into another tenant | The RLS `WITH CHECK` clause rejects any INSERT tagged with a foreign `tenant_id`; proven behaviourally through the least-privilege role. | **CODE** — `tests/contract/test_rls_isolation.py` |

## R — Repudiation (accountability)

| Threat | Mitigation / gap | Enforcement |
|--------|------------------|-------------|
| A tenant denies an action / an operator denies a change | Every mutation writes an append-only `audit_log` entry (engine, action, entity, actor); service mutations are attributable to `svc-qe-central`, separable from human admins. | **CODE** — `audit_log`, `app/service_token.py` |
| An exception to a finding is applied silently | Exceptions are **governed waivers** — named owner, reason, expiry; annotate the verdict as WAIVED, never delete the finding, expire automatically. | **CODE** — `verdict_events.create_waiver` |
| Long-term evidence retention for a regulator | Point-in-time recovery of the evidence stores. | **OPS** — Postgres PITR + retention policy |

## I — Information disclosure (confidentiality)

| Threat | Mitigation / gap | Enforcement |
|--------|------------------|-------------|
| Tenant A reads tenant B's evidence/credentials | Postgres **RLS** (FORCE + policy) on every tenant table; the GUC is set per transaction; the compliance loader/bundle additionally filter `WHERE tenant_id` **and** defensively drop foreign-tenant rows (defence in depth). | **CODE** — `app/db/__init__.py`, `app/compliance/adapter.py` (`EvidenceBundle.build`) |
| Client login credentials exposed at rest | Envelope-encrypted (KMS KEK, AAD = `app_id`); **refuse-plaintext** (503) when the envelope is unavailable; credentials are never echoed back (responses expose `has_credentials` only); a soft-delete zeroes the ciphertext. | **CODE** — `app/routers/apps.py` |
| Raw PII persisted from a crawl | PII redacted **at source** before persistence; detector failure is fail-open but surfaced + counted, never silent. | **CODE** — `app/substrate/redact.py` |
| Client credentials exfiltrated by a compromised/hostile page during a crawl | The explorer runs behind an **egress sandbox** (destination allow-list) so a crawled page cannot pivot the browser to an attacker endpoint; crawl is gated to attested **non-prod/disposable** targets only. | **CODE** — egress sandbox + `app/security/prod_guard.py`. _Gap: the sandbox allow-list policy must be reviewed per client (operational)._ |
| Secret leaked in a boot/violation log | The boot validator names the **offending setting**, never the parsed secret/password value. | **CODE** — `app/security/boot_validator.py` |
| Cross-tenant leak through the KEK | Per-tenant KMS KEK resolver + AAD binding; a blob for tenant A cannot be decrypted in tenant B's context. | **HYBRID** — code + KMS provisioning |

## D — Denial of service / availability

| Threat | Mitigation / gap | Enforcement |
|--------|------------------|-------------|
| One tenant's cycles starve the fleet / hammer a client's app | Global + per-tenant **admission caps** with a token bucket; the distributed limiter is **politeness-first fail-closed** (deny/wait, never fail-open and burst a customer app). Leader election ensures exactly one replica scans the fleet. | **CODE** — `app/controlplane/scheduling/admission.py`, `distributed.py`, `leader.py` |
| Runaway spend | Append-only cost meter (can only under-count) + budget gate. | **CODE** — `app/controlplane/cost/meter.py` |
| API abuse from an authenticated principal | Optional per-principal rate limiter (opt-in) + a global exception handler returning a clean 500 with a correlation-id (no stack leak). | **CODE** — `app/api_protect.py` |
| Compliance report exhausts memory on a huge tenant | The loader caps rows per source; the report is a bounded projection, not a bulk export. | **CODE** — `app/compliance/loader.py` (`_ROW_CAP`) |
| Redis/limiter store unreachable | Fail-closed with a bounded backoff — a cycle is deferred, never fired unthrottled. | **CODE** — `distributed.py` |

## E — Elevation of privilege

| Threat | Mitigation / gap | Enforcement |
|--------|------------------|-------------|
| A viewer performs an admin/mutating action | `require_role` RBAC gate at every mutation (admin\|manager); the compliance report is **admin-only**. | **CODE** — `app/auth.py`, `app/routers/compliance.py` |
| The substrate writer role reads/writes beyond its lane | `qec_substrate` holds only INSERT/SELECT on the substrate tables + SELECT on evidence/audit; **zero** grants for `qec` on the nexus DB; explicit negative fences on the crown-jewel tables. | **HYBRID** — `scripts/qec_db_bootstrap.sql` |
| A deployed process runs with development secrets/KEK | The **fail-closed boot gate** refuses to start a staging/production process wearing a dev KEK, empty/default JWT/HMAC secrets, or a default DB password. | **CODE** — `app/security/boot_validator.py` |
| Prod app crawled/submitted-to via a data-only flag | The onboarding bypass flag is honoured **only** in development/test; a real app in staging/production is always fail-closed regardless of any flag in its row. | **CODE** — `app/security/prod_guard.py` |

---

## Named residual gaps (honest)

These are **not** closed by code and are tracked as operational work:

- **Egress allow-list review per client** — the sandbox is built; the per-client
  destination policy is an operational review.
- **KMS provisioning + rotation cadence** — code refuses plaintext; the key
  lifecycle is operational.
- **Backup schedule + restore-drill execution** — code cannot prove a drill ran.
- **SIEM integration + security incident-response runbook** (CC7.3 / CC7.4).
- **Third-party/vendor risk inventory + penetration test schedule** (CC9.x).
- **On-prem key custody** — in a verdict-box install the buyer may hold the KEK;
  key-custody responsibility must be contractually assigned in the DPA.

## Security-review checklist (per release / per new client)

- [ ] All new `/api/*` routes are behind `require_auth`/`require_role` (fail-closed
      middleware is a backstop, not the primary gate).
- [ ] No new tenant table ships without `FORCE ROW LEVEL SECURITY` + a
      `tenant_isolation` policy; RLS isolation test covers a representative row.
- [ ] No new persisted field bypasses PII redaction at source.
- [ ] No credential/secret is logged, echoed in a response, or embedded in a URL.
- [ ] New DB access uses the least-privilege role and the tenant-scoped session
      (GUC set) — never a superuser/BYPASSRLS connection in production.
- [ ] New evidence writes are hash-chained or covered by the audit_log.
- [ ] The boot validator covers any new security-critical setting's dev default.
- [ ] New long-running/dispatching work respects the admission caps + budgets.
- [ ] `docker-compose.qec.yml` / Helm values override every dev default; boot gate
      is armed (`NEXUS_ENV=production`).

## Data-residency guarantee statement

VKPower Verdict is designed to run **fully within the buyer's trust boundary**.
In an on-prem / verdict-box install:

- All tenant evidence (crawl substrate, verdicts, dossiers, waivers, audit_log)
  and all client credentials are stored in the buyer-controlled Postgres inside
  the buyer's perimeter. The product has **no outbound dependency** on VKPower
  infrastructure to operate.
- Envelope-encryption keys (KEK) can be provisioned in the buyer's own KMS; key
  custody can rest entirely with the buyer (contractually assigned in the DPA).
- The only egress is the explorer's crawl to the **buyer-nominated, attested,
  non-prod/disposable** client-app targets, constrained by the egress sandbox
  allow-list. No evidence or credential leaves the perimeter as a side effect of
  normal operation.
- Cross-tenant isolation is enforced by Postgres RLS at the database layer, below
  the application, so a tenant's data never transits another tenant's context.

_This statement describes the code-enforced design posture; the physical
residency of a specific deployment is fixed operationally by where the buyer runs
the verdict box and provisions its KMS, and is documented in the DPA._
