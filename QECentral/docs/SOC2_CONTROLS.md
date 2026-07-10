# VKPower Verdict — SOC 2 Controls Mapping (Phase 8)

**Status:** Internal control-mapping matrix for the VKPower Verdict autonomous
Centralized-QE product. It maps each relevant SOC 2 Trust Services Criterion to
**how Verdict implements it** and **the evidence artifact an auditor inspects**.

**This is NOT a SOC 2 audit report.** Auditors produce the report; we produce the
evidence and the mapping. Every row is marked honestly as **code-enforced** (a
structural code invariant proven by the unit/contract suite), **operational**
(depends on the running deployment or an external auditor), or **hybrid** (a code
invariant with an operational dependency).

**Machine-readable projection.** The same mapping is produced live, per tenant,
from the actual hash-chained evidence by the read-only compliance adapter:

```
GET /api/v1/qec/compliance/soc2/report        (admin, tenant-scoped)
GET /api/v1/qec/compliance/frameworks
```

The adapter (`platform/qe-central/app/compliance/adapter.py`, `SOC2Adapter`) is a
**read-only projection** over `verdict_events`, `decision_dossiers`,
`verification_waivers`, and `audit_log`. It captures nothing new and mutates
nothing; a regulator can recompute a report's `report_digest` from its body and
re-derive the verdict hash chains to prove the evidence was never rewritten.

Legend: **CODE** = code-enforced · **OPS** = operational · **HYBRID** = both ·
_Status column_ reflects the design guarantee, not a point-in-time audit result.

---

## Security (Common Criteria)

| TSC | Control | Enforcement | How Verdict implements it | Evidence artifact an auditor inspects |
|-----|---------|-------------|---------------------------|----------------------------------------|
| CC6.1 | Logical access — tenant isolation | **CODE** | Every DB session sets `nexus.current_tenant_id` inside its transaction; all tenant tables carry `FORCE ROW LEVEL SECURITY` + a `tenant_isolation` policy, enforced through least-privilege roles (a superuser would bypass RLS, so the test refuses to run through one). | `app/db/__init__.py` (`tenant_scoped_*`), `alembic_qec` `qec_001` (FORCE RLS), `tests/contract/test_rls_isolation.py` (behavioural 4-part proof) |
| CC6.1b | Service-token audience isolation | **CODE** | Inbound JWTs are verified against `QEC_JWT_AUDIENCE`; a token with a foreign `aud` is always rejected (401), an untenanted token is refused — no VKPower↔Verdict token replay. | `app/auth.py` (`_decode_token`), `app/service_token.py`, `tests/unit/test_boot_and_aud.py` |
| CC6.3 | Least-privilege DB roles (segregation of duties) | **HYBRID** | The owning role `qec` has **zero** grants on the nexus DB; the writer role `qec_substrate` holds only `INSERT/SELECT` on the substrate tables (+ `SELECT` on the evidence/audit tables). No role can escalate across tenants. | `scripts/qec_db_bootstrap.sql` (grants + explicit negative fences). _Ops: role creation + password rotation on the deployment._ |
| CC6.7 | Encryption of credentials at rest | **HYBRID** | Client credentials are envelope-encrypted (KMS KEK, AAD = `app_id`); when the envelope service is unavailable the write **refuses (503)** rather than store plaintext. A deployed process **refuses to boot** on a development KEK. | `app/routers/apps.py` (`_encrypt_credentials`), `app/security/boot_validator.py`, `app/main.py` (`_kek_provider`). _Ops: KMS key provisioning + rotation + IAM binding._ |
| CC6.8 | Prevention of unauthorized activity | **CODE** | A crawl/cycle against a real client app **physically refuses** until the app is onboarding-`live`: signed rules-of-engagement + a non-prod/disposable env attestation (unexpired) + a passed safety preflight. Fail-closed by default; the dev bypass is never honoured in staging/production. | `app/security/prod_guard.py`, `tests/unit/test_prod_guard.py` |
| CC7.1 | System operations monitoring | **HYBRID** | Prometheus `/metrics` exposition; a correlation-id on every request/response and log line; `/health` runs a live RLS-GUC round-trip + a KEK canary and reports honest `degraded` state. | `app/observability/`, `app/main.py` `/health`. _Ops: Prometheus scrape, dashboards, alert routing._ |
| CC7.2 | Anomaly / tamper detection | **CODE** | Every verdict is hash-chained per `(tenant, artifact, test)`; the chain is independently re-derivable, so any rewrite of a past verdict is detectable. Detected breaks are surfaced in the report, never hidden. | `verdict_events` (chain), `app/compliance/adapter.py` (`verify_verdict_chains`) |
| CC7.4 | Security incident response | **OPS** | Detection/triage/forensics runbooks operate on the `audit_log` + hash chains as the evidentiary record. | `QECentral/docs/THREAT_MODEL.md` (security-review checklist). _Ops: incident-response runbook + on-call process._ |
| CC8.1 | Change management | **CODE** | Every ship/verdict writes an immutable, reproducible decision dossier (input hashes, rules applied, risk model, template-rendered rationale) chained to its verdict — replaying the inputs reproduces the decision byte-for-byte. | `verdict_events.build_dossier` → `decision_dossiers` |

## Availability

| TSC | Control | Enforcement | How Verdict implements it | Evidence artifact |
|-----|---------|-------------|---------------------------|-------------------|
| A1.1 | Capacity management | **CODE** | Global + per-tenant admission caps with a token bucket (in-memory or a fail-closed distributed Redis limiter — politeness-first: it denies/waits, never fail-opens and bursts a customer's app) bound customer-facing load; the append-only cost meter + budget gate bound spend. | `app/controlplane/scheduling/admission.py` (+ `distributed.py`), `app/controlplane/cost/meter.py` |
| A1.2 | Backup & recovery | **OPS** | Postgres point-in-time recovery covers both logical DBs from one backup setup; recovery is proven by a periodic restore drill. | `infrastructure/` (Postgres PITR) + restore-drill runbook. _Ops: backup + restore-drill execution must be evidenced from the running deployment._ |

## Confidentiality

| TSC | Control | Enforcement | How Verdict implements it | Evidence artifact |
|-----|---------|-------------|---------------------------|-------------------|
| C1.1 | PII redaction at source | **CODE** | Every value persisted to the substrate (`page_actions.value`, form-snapshot values, ground-truth values) is PII-redacted **before** persistence (ssn/card/password/email/phone/dob → `[REDACTED:class]`); no raw PII at rest. Detector failure is fail-open **but surfaced + counted**, never silent. | `app/substrate/redact.py` |
| C1.2 | Confidential data isolation + encryption | **HYBRID** | Confidential tenant data is RLS-isolated and, for credentials, envelope-encrypted at rest. | `app/db/__init__.py` (RLS) + `app/routers/apps.py` (envelope). _Ops: TLS termination + at-rest disk encryption._ |

## Processing Integrity

| TSC | Control | Enforcement | How Verdict implements it | Evidence artifact |
|-----|---------|-------------|---------------------------|-------------------|
| PI1.1 | Input validation | **CODE** | The evidence-bundle schema makes a dishonest crawl **impossible to write**: monotonic indices, pinned vocabularies, an after-outcome on every action — a broken rule raises an enumerated `RefusalError` (422 at the boundary, never a silent drop). | `app/substrate/schema.py` |
| PI1.2 | **Refuse-not-green-wash** processing integrity (the crown jewel) | **CODE** | The system **refuses rather than fabricate** a green result: the verdict is deterministic + min-gated (the LLM never scores), unproven steps are declared honestly UNPROVEN, and each verdict is hash-chained so a green-wash cannot be inserted after the fact. | `app/harness/rules.py`, `verdict_events` (hash chain) |
| PI1.3 | Reproducible decision records | **CODE** | Each decision writes a byte-reproducible dossier (input hashes, rules applied, risk model, rationale) chained to its verdict. | `verdict_events.build_dossier` → `decision_dossiers` |
| PI1.4 | Governed exceptions (never silent) | **CODE** | An exception to a finding is a **waiver** with a named owner, a reason, and an expiry; it annotates the verdict as WAIVED, never deletes the finding, and expires automatically. Zero waivers is a valid, satisfied state (no exceptions taken). | `verdict_events.create_waiver` → `verification_waivers` |
| PI1.5 | Immutable audit trail | **CODE** | Every tenant-scoped mutation writes an append-only `audit_log` entry (engine, action, entity, actor). | `audit_log` (`nexus_sdk.db.models.AuditLogRow`) |

---

## Code-enforced vs operational — the honest split

The compliance adapter refuses to green-wash an operational control from evidence
it cannot see. As of this mapping:

- **Code-enforced (proven by the unit/contract suite, always-on):** CC6.1,
  CC6.1b, CC6.8, CC7.2, CC8.1, A1.1, C1.1, PI1.1, PI1.2, PI1.3, PI1.4, PI1.5.
- **Hybrid (code invariant + an operational dependency):** CC6.3, CC6.7, CC7.1,
  C1.2.
- **Operational (needs the running deployment / an external auditor):** CC7.4,
  A1.2 — and the SOC 2 report itself (an auditor's opinion is out of scope for
  any codebase to produce).

The live report surfaces this split in its `attestations` block and marks each
operational control as `operational` status (never `satisfied`).

## What is still operational / out of scope for the codebase

These are **truly operational** and cannot be closed by code — they gate on the
running deployment or a third party, exactly as the product directive requires:

| Item | Owner | Nature |
|------|-------|--------|
| SOC 2 Type 1 / Type 2 auditor opinion | External auditor | The report itself |
| Postgres backup schedule + restore-drill execution | Platform / SRE | A1.2 evidence |
| KMS key provisioning, rotation, IAM binding | Platform / Security | CC6.7 operational half |
| Prometheus scrape + alert routing + SIEM hook | Platform | CC7.1 / CC7.3 |
| Security incident-response runbook + on-call | Security | CC7.4 |
| DB role creation + password rotation | Platform | CC6.3 operational half |
| User provisioning / access reviews / offboarding | IT / HR | CC6.2 / CC6.3 (org process) |
| Vendor risk inventory, penetration test schedule | Security / Legal | CC9.x |

## What this matrix is NOT

- Not a SOC 2 audit. Even with every control code-enforced, an auditor can find
  gaps in documentation, evidence retention, or process maturity.
- Not a substitute for legal review of customer-facing artifacts.
- Not a guarantee of audit pass — it drives evidence-gathering and surfaces gaps
  before the auditor finds them.
