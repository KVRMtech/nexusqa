"""
Built-in Chain: Compliance Audit.

Product: Nexus Compliance

Takes documents (policy forms, rate filings, endorsements) and
audit-checks them for rule consistency, missing coverage,
and regulatory compliance — then generates an audit report.

DAG:
    document_ingestion ─→ pii_redaction ─→ rule_extraction ─→ knowledge_check ─→ compliance_report
"""

from ..schema import (
    ChainDefinition,
    RetryPolicy,
    StageDefinition,
)


def build_compliance_audit_chain() -> ChainDefinition:
    return ChainDefinition(
        chain_id="nexus.compliance-audit",
        name="Compliance Audit Pipeline",
        description=(
            "Audit documents for regulatory compliance: ingest documents → "
            "redact PII → extract rules → cross-check against knowledge graph → "
            "generate compliance report"
        ),
        version="1.0.0",
        tags=["compliance", "audit", "insurance", "regulatory"],
        stages=[
            # ── Stage 1: Ingest documents ─────────────────────
            StageDefinition(
                stage_id="document_ingestion",
                name="Document Ingestion",
                description="Ingest audit-target documents via Spine engine",
                engine="spine",
                endpoint="/api/v1/spine/ingest",
                request_type="multipart",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                },
                file_mappings={
                    "file": "$temp.item",
                },
                condition="$workflow.input.document_file_ids",
                timeout_seconds=300,
                on_failure="fail",
                for_each="$workflow.input.document_file_ids",
                for_each_item_key="item",
                for_each_concurrency=3,
            ),
            # ── Stage 2: Redact PII from extracted text ──────
            StageDefinition(
                stage_id="pii_redaction",
                name="PII Redaction",
                description="Scan and redact PII from all extracted document text via Shield engine",
                engine="shield",
                endpoint="/api/v1/shield/redact",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "text": "$workflow.input.document_text",
                },
                depends_on=["document_ingestion"],
                timeout_seconds=60,
                on_failure="fail",
            ),
            # ── Stage 3: Extract rules from documents ────────
            StageDefinition(
                stage_id="rule_extraction",
                name="Rule Extraction from Documents",
                description="Extract business rules and regulatory requirements from documents via Heart engine",
                engine="heart",
                endpoint="/api/v1/heart/extract-rules",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "transcript": "$stages.pii_redaction.output.safe_text",
                },
                depends_on=["pii_redaction"],
                timeout_seconds=300,
                retry_policy=RetryPolicy(max_retries=2),
                on_failure="fail",
            ),
            # ── Stage 4: Cross-check against knowledge graph ─
            StageDefinition(
                stage_id="knowledge_check",
                name="Knowledge Graph Cross-Check",
                description="Store and cross-reference extracted rules against existing knowledge via Backbone engine",
                engine="backbone",
                endpoint="/api/v1/backbone/rules",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "rule": "$temp.item",
                },
                depends_on=["rule_extraction"],
                timeout_seconds=30,
                on_failure="continue",
                for_each="$stages.rule_extraction.output.rules",
                for_each_item_key="item",
                for_each_concurrency=5,
            ),
            # ── Stage 5: Generate compliance audit report ────
            StageDefinition(
                stage_id="compliance_report",
                name="Compliance Report Generation",
                description="Generate a compliance audit report with findings via Mouth engine",
                engine="mouth",
                endpoint="/api/v1/mouth/generate",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "report_type": "compliance_report",
                    "format": "html",
                    "title": "$workflow.input.audit_title",
                    "rules": "$stages.rule_extraction.output.rules",
                    "knowledge_check_results": "$stages.knowledge_check.output.items",
                },
                depends_on=["knowledge_check"],
                timeout_seconds=120,
                on_failure="fail",
            ),
        ],
    )
