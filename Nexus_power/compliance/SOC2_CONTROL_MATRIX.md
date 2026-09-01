# SOC2 Type 1 / Type 2 Control Matrix

**Status:** Internal evidence-gathering scaffold. This document tracks which Trust Services Criteria controls are implemented vs missing, and points to the *evidence* an auditor needs to attest each one.

**Audit type:** Type 1 (point-in-time) first; Type 2 (continuous-monitoring period) after.
**Auditor:** TBD — engagement starts week 1 of Phase 15.
**Internal owner:** Platform team lead.

This is **NOT** a SOC2 audit report. It's the working document the team maintains BETWEEN audits to keep evidence current.

---

## Trust Services Criteria coverage

Legend: ✅ implemented + evidence on file · ⚠️ implemented, evidence gap · ❌ not implemented

### Security (CC)

| TSC | Control | Status | Evidence location |
|---|---|---|---|
| CC1.1 | Code of conduct + governance | ❌ | Need HR sign-off; not engineering-owned |
| CC1.2 | Board oversight of security | ❌ | Need exec attestation |
| CC2.1 | Internal communication of policies | ⚠️ | [docs/](Nexus_power/docs/) covers ops + DR; need formal information security policy |
| CC3.1 | Risk identification + assessment | ⚠️ | [OPERATOR_RUNBOOK §6](Nexus_power/docs/OPERATOR_RUNBOOK.md#6-what-we-know-is-rough-be-honest-with-the-client) documents known gaps; formal risk register needed |
| CC4.1 | Continuous monitoring / SIEM | ⚠️ | Prometheus + alerts ([prometheusrule.yaml](Nexus_power/infrastructure/helm/nexus-qa/templates/prometheusrule.yaml)) — needs SIEM integration |
| CC5.1 | Logical access controls | ✅ | [auth/__init__.py](Nexus_power/sdk/nexus-sdk/nexus_sdk/auth/__init__.py) JWT validation; ESO for secret rotation |
| CC5.2 | Role-based authorization | ✅ | `role` claim on JWT; checked at every router |
| CC5.3 | User access reviews (quarterly) | ❌ | Process not defined — needs HR/IT |
| CC6.1 | Logical + physical access restrictions | ✅ | Cloud-native: K8s RBAC + cloud IAM. Document each |
| CC6.2 | New users provisioned | ⚠️ | Engineering: SCIM via [024_org_awareness.py](Nexus_power/alembic/versions/024_org_awareness.py); needs IT runbook |
| CC6.3 | User access removed on termination | ❌ | Needs IT offboarding playbook |
| CC6.4 | Restricted access to environments | ✅ | Separate namespaces per env; ClusterSecretStore per env |
| CC6.5 | Multi-factor auth | ⚠️ | JWT path supports it; SSO integration pending |
| CC6.6 | Encryption in transit | ✅ | TLS everywhere via [ingress.yaml](Nexus_power/infrastructure/helm/nexus-qa/templates/ingress.yaml); cert-manager |
| CC6.7 | Encryption at rest | ✅ | Cloud-provider default (EBS / Persistent Disk / managed disks). KMS-backed |
| CC6.8 | Vulnerability management | ⚠️ | Dependabot enabled; container scanning needed in CI |
| CC7.1 | System operations + monitoring | ✅ | [prometheusrule.yaml](Nexus_power/infrastructure/helm/nexus-qa/templates/prometheusrule.yaml) + [DR_RUNBOOK.md](Nexus_power/docs/DR_RUNBOOK.md) |
| CC7.2 | Anomaly detection | ⚠️ | Per-tenant rate limit + alert on 429 spike. Need anomaly-detection ML pass |
| CC7.3 | Detection of security incidents | ⚠️ | Auth failure alerts present; need formal SIEM hook |
| CC7.4 | Incident response | ⚠️ | [OPERATOR_RUNBOOK §3](Nexus_power/docs/OPERATOR_RUNBOOK.md#3-the-five-things-youll-be-paged-for) covers ops incidents. Security incidents need separate runbook |
| CC7.5 | Recovery from security events | ❌ | Needs incident-response playbook with forensics steps |
| CC8.1 | Change management process | ⚠️ | Branch protection + CI in [integration.yml](Nexus_power/.github/workflows/integration.yml); need formal CAB for production changes |
| CC9.1 | Vendor management | ❌ | Need to inventory + risk-rate every third-party (Ollama, sentence-transformers, etc.) |
| CC9.2 | Vendor risk monitoring | ❌ | Annual review process not defined |

### Availability (A1)

| TSC | Control | Status | Evidence location |
|---|---|---|---|
| A1.1 | Backup + recovery procedures | ✅ | [DR_RUNBOOK.md](Nexus_power/docs/DR_RUNBOOK.md) + CNPG Barman config in [postgres-cnpg.yaml](Nexus_power/infrastructure/helm/nexus-qa/templates/postgres-cnpg.yaml) |
| A1.2 | Capacity monitoring | ⚠️ | [Grafana dashboards](Nexus_power/infrastructure/helm/nexus-qa/templates/grafana-canonical-dashboards.yaml) + KEDA. Phase 9 load test pending for measured numbers |
| A1.3 | Environmental + redundancy | ✅ | Multi-AZ via TopologySpread; CNPG sync replicas; Redis Sentinel |

### Confidentiality (C1)

| TSC | Control | Status | Evidence location |
|---|---|---|---|
| C1.1 | Confidential data classified | ⚠️ | Tenant data classified via RLS; need formal data classification policy |
| C1.2 | Encryption of confidential data | ✅ | At-rest (EBS/PD KMS) + in-transit (TLS) |

### Processing Integrity (PI1)

| TSC | Control | Status | Evidence location |
|---|---|---|---|
| PI1.1 | Data inputs validated | ✅ | Pydantic validation at every API + worker boundary |
| PI1.2 | Data processed completely + accurately | ✅ | Workflow plane provides at-least-once with idempotency keys + checkpoints |
| PI1.3 | Outputs reviewed for accuracy | ⚠️ | Manual QA process; needs SME approval workflow doc |

### Privacy (P)

| TSC | Control | Status | Evidence location |
|---|---|---|---|
| P1.1 | Notice to data subjects | ❌ | Need privacy policy + DPA. See [DPA_TEMPLATE.md](compliance/DPA_TEMPLATE.md) |
| P2.1 | Consent obtained | ❌ | Customer-facing process |
| P3.1 | Personal data collected only when authorized | ⚠️ | Shield engine redacts PII pre-Backbone; per-tenant retention via [030_tenant_lifecycle.py] (P16 deliverable) |
| P4.1 | Personal data used for stated purpose | ⚠️ | Per-LOB PIA needed (Phase 15 deliverable) |
| P4.2 | Personal data retention | ✅ | RLS + per-tenant retention; GDPR offboarding flow (Phase 16) |
| P5.1 | Personal data access on request | ✅ | Tenant-scoped queries via RLS; export endpoint (Phase 16) |
| P6.1 | Personal data disclosed only with consent | ✅ | Tenant isolation enforced via Postgres RLS |
| P7.1 | Quality of personal data | ⚠️ | Drift detector ([data_quality/drift.py]) checks schema drift; PII accuracy is customer responsibility |
| P8.1 | Personal data monitored | ⚠️ | Audit log of every tenant-data access; needs PII-specific audit query |

---

## Evidence collection

Run [scripts/collect_soc2_evidence.sh](Nexus_power/scripts/collect_soc2_evidence.sh) on the 1st of every month. Outputs:

- `compliance/evidence/YYYY-MM/access_logs/` — JWT issuance + revocation history (last 30 days)
- `compliance/evidence/YYYY-MM/rbac_snapshot.yaml` — current K8s RBAC + IAM bindings
- `compliance/evidence/YYYY-MM/dependency_versions.txt` — all pinned versions + CVE scan output
- `compliance/evidence/YYYY-MM/dr_drill_summary.md` — last quarter's drill reports
- `compliance/evidence/YYYY-MM/change_log.md` — list of every merged PR with reviewer + CI green status

The auditor pulls from this directory during the engagement. **Do not skip a month** — Type 2 audit needs continuous evidence.

---

## Gaps before audit-ready

These need to be closed BEFORE engaging an auditor. Each one's owner is named.

| Gap | Owner | Estimated effort |
|-----|-------|------------------|
| Formal information security policy | CISO/Legal | 2 weeks |
| User access review process (quarterly cadence) | IT | 1 week setup + ongoing |
| Vendor risk inventory + annual review | Legal + Engineering | 2 weeks |
| SIEM integration + alert routing | Platform | 1 week |
| Security incident response playbook | Platform + Security | 2 weeks |
| Container vulnerability scanning in CI | Platform | 3 days |
| Annual penetration test schedule | Security | calendar — external |
| Customer-facing privacy policy | Legal + Marketing | 1 week |
| Per-LOB Privacy Impact Assessment | Legal + Product | 2 weeks per LOB |

**Aggregate calendar elapsed before SOC2 Type 1 audit can start:** ~6-8 weeks of internal prep + 4-6 weeks of auditor engagement = 10-14 weeks. Type 2 needs a 3-12 month observation period AFTER Type 1.

---

## What this matrix is NOT

- It's not a SOC2 audit. Auditors produce the audit report; we produce the evidence.
- It's not a substitute for legal review. The customer-facing artifacts ([DPA_TEMPLATE.md](compliance/DPA_TEMPLATE.md), privacy policy) need lawyer sign-off before sending to a customer.
- It's not a guarantee of audit pass. Even with every ✅, the auditor can find issues with documentation, evidence retention, or process maturity.

Use this matrix to **drive evidence-gathering work** and **identify gaps before the auditor finds them**.
