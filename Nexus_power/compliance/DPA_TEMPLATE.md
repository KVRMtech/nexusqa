# Data Processing Agreement — Template

**Status:** Engineering draft. **Must be reviewed by legal counsel before sending to a customer.** This template captures the technical commitments the platform can credibly back; the legal/commercial terms are out of scope.

Last reviewed by legal: TBD.

---

## Parties

- **Controller** — the customer (the data subject's organization).
- **Processor** — Nexus QA platform.
- **Subprocessors** — listed in Annex B.

## Definitions

- **Personal Data:** as defined in GDPR Art. 4(1) / CCPA §1798.140.
- **Process:** as defined in GDPR Art. 4(2).
- **Data Subject:** the natural person whose Personal Data is processed.

## 1. Scope of Processing

The Processor processes the following categories of Personal Data on behalf of the Controller:

- Voice recordings (audio uploaded by the Controller)
- Screen recordings (video uploaded by the Controller)
- Transcripts derived from the above
- Identifiers used by the Controller to organize submissions (session id, tenant user id)

Personal Data is **never** sold, rented, or shared with any third party except the Subprocessors listed in Annex B.

## 2. Duration

For the term of the Master Service Agreement plus the retention period in §6 below.

## 3. Subprocessors

The Processor uses the Subprocessors in Annex B. Before adding a new Subprocessor, the Processor will give the Controller **30 days' notice** and the right to terminate without penalty if the Controller objects.

## 4. Technical and Organizational Measures

The Processor implements measures appropriate to the risk:

- **Encryption in transit.** TLS 1.2+ for all customer endpoints.
- **Encryption at rest.** Cloud-provider KMS (AWS KMS / GCP CMEK / Azure Key Vault) for all persistent storage.
- **Access control.** Role-based access via JWT + tenant-scoped Postgres RLS. Operator access logged + reviewed quarterly (CC5.3 control).
- **Tenant isolation.** Hard isolation at the storage layer (RLS), the queue layer (per-tenant rate limit), and the search layer (Milvus per-tenant filter or partition).
- **PII redaction.** The Shield engine redacts identifiable personal data (names, email, phone, SSN, etc.) before content reaches the Backbone (vector + knowledge graph). The unredacted media is retained only in the source-of-truth artifact store; access to it is logged.
- **Audit logging.** Every read/write to tenant-scoped data emits an audit log entry. Logs retained per §6.
- **Availability.** Postgres CNPG synchronous replication + Redis Sentinel + multi-AZ topology. Measured RTOs in [DR_RUNBOOK.md](Nexus_power/docs/DR_RUNBOOK.md).
- **Vulnerability management.** Dependabot for dependencies + container image scanning in CI. Critical CVEs patched within 7 days.
- **Penetration testing.** Annual third-party test; remediation closure tracked in `compliance/pen_test_reports/`.

## 5. Data Subject Rights

The Processor will, on request from the Controller:

- **Access.** Export all Personal Data for the named Data Subject within **30 days**. Format: machine-readable (JSON + media files).
- **Rectification.** Update the Personal Data within **30 days**.
- **Erasure (right to be forgotten).** Hard delete the Personal Data + all derived artifacts (transcripts, embeddings, knowledge graph nodes) within **30 days** of request. The Processor will confirm completion in writing.
- **Restriction.** Suspend processing within **5 business days** of request.
- **Portability.** Same as Access; format machine-readable.
- **Objection.** Same as Restriction.

Self-service for Access + Erasure is provided via the operator-facing admin UI (see [P16 — tenant lifecycle](Nexus_power/sdk/nexus-sdk/nexus_sdk/tenant_lifecycle.py)). Bulk requests via the Customer Success contact.

## 6. Retention + Deletion

| Data class | Default retention | Customer-configurable? |
|-----|-----|-----|
| Source audio/video uploads | 90 days | Yes (per-tenant config, 1-365 days) |
| Canonical transcripts | 1 year | Yes (per-tenant config, 30-1825 days) |
| Vector embeddings | 1 year (same as canonical) | Linked to canonical retention |
| Knowledge graph nodes | 1 year | Linked to canonical retention |
| Audit logs | 7 years | No (regulatory requirement) |
| Backups | 30 days | Yes (per-tenant config, up to 1 year) |

On contract termination: complete deletion within **60 days** unless legal hold applies. Confirmation provided to the Controller.

## 7. Breach Notification

The Processor will notify the Controller without undue delay, and **within 72 hours** of becoming aware of a Personal Data breach. Notification includes:

- Nature of the breach + categories + approximate number of Data Subjects affected
- Likely consequences
- Measures taken or proposed to address the breach
- Contact point for further information

## 8. Audits

The Processor will:

- Provide an annual **SOC2 Type 2 report** (after the first 12 months of operation).
- Permit Controller audits at the Processor's premises with reasonable notice + scope agreement.
- Respond to standard third-party audit questionnaires (SIG, CSA CAIQ) within 30 days.

## 9. International Transfers

For Controllers in the EEA / UK:

- **Standard Contractual Clauses (SCCs)** apply where required.
- The Processor's deployment region is **customer-selectable**: us-east-1, us-west-2, eu-central-1, ap-southeast-2 currently supported.
- Personal Data does NOT leave the selected region except where strictly required by the Customer's chosen integrations.

---

## Annex A — Controller-specific configuration

To be filled in per customer:

- **Customer name:**
- **Deployment region:**
- **Custom retention overrides:**
- **Per-LOB Privacy Impact Assessment reference:**
- **Designated breach-notification contact:**

## Annex B — Subprocessor list

| Subprocessor | Function | Personal Data Categories | Jurisdiction |
|---|---|---|---|
| AWS / GCP / Azure (Customer's choice) | IaaS (compute, storage, KMS) | All | Customer-selected region |
| OpenAI / Anthropic (only if customer opts into managed LLM) | Inference | Transcripts (redacted) | US / EU per provider |
| Sentry / Datadog (only if customer opts in to managed observability) | Operational telemetry | None (no Personal Data forwarded) | US |

**No PII reaches a Subprocessor that isn't listed here.** If the Customer's chosen integrations would imply additional Subprocessors (e.g. an integration to a CRM), the Customer is the Controller of that downstream flow.

---

## Sign-off

```
Controller:           __________________________
Authorized signer:    __________________________
Date:                 __________________________

Processor:            Nexus QA
Authorized signer:    __________________________
Date:                 __________________________
```

---

**Reminder for engineering:** Every commitment in this document corresponds to a control in [SOC2_CONTROL_MATRIX.md](compliance/SOC2_CONTROL_MATRIX.md). When you change a default (e.g. retention period) you MUST update both files.
