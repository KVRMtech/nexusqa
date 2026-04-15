"""
Nexus Mouth Engine — Reporting, Traceability & Compliance Output.

The mouth that speaks results to humans. In regulated industries,
testing isn't done when tests pass — it's done when you can PROVE
tests were run correctly, traceably, and completely.

The Mouth Engine generates:
1. Traceability Matrices — Rule → Test Case → Test Result → Evidence
2. Compliance Reports — State filing readiness, regulatory gap analysis
3. Executive Summaries — High-level dashboards for C-suite
4. Audit-Ready PDFs — Immutable evidence packages with digital signatures
5. Test Coverage Reports — Rule coverage %, confidence scoring
6. Defect Summary Reports — Failed tests with root cause analysis
7. Regulatory Gap Analysis — Which rules lack test coverage by state

Key Design Decisions:
- Template-based rendering (Jinja2) lets business users customize reports
- HTML first, then PDF generation (dual-format output)
- Every report carries a unique report ID + generation timestamp
- All source data is embedded for reproducibility (no external links that break)
- Reports are immutable artifacts — once generated, they never change
- Audit trail: who requested, when, with what parameters

Insurance-Specific Reports:
- State Filing Compliance Matrix (50 states × products × rule coverage)
- Rate Table Validation Report (expected vs actual premium comparisons)
- Underwriting Rules Verification (decision tree coverage analysis)
- Claims Processing Audit Trail (end-to-end claim lifecycle)
- Producer Licensing Compliance (NPN + state appointment validation)

v0.2.0 — Modular refactor.  Report generators live in ``app.generators``,
HTML renderer in ``app.renderers``.  This file retains configuration,
request/response models, the engine class, and the entry-point.
"""

from __future__ import annotations

import os
import uuid
import json
import hashlib
from datetime import datetime, timezone
from typing import Optional
from enum import Enum
from collections import defaultdict

from fastapi import Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.models import (
    NexusRequest, NexusResponse, JobResponse, JobStatus, Confidence,
)
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.events import NexusEvent

# ── Modular sub-packages ───────────────────────────────────────
from app.generators import (
    TraceabilityMatrixGenerator,
    ComplianceReportGenerator,
    ExecutiveSummaryGenerator,
    TestCoverageReportGenerator,
    DefectSummaryGenerator,
    CoverageLevel,
)
from app.renderers import HTMLReportRenderer


# ─── Configuration ─────────────────────────────────────────────

class MouthConfig(EngineConfig):
    engine_name: str = "mouth"
    engine_port: int = 8010

    # Report storage
    report_storage_path: str = "/app/data/reports"

    # PDF generation
    pdf_engine: str = "weasyprint"  # weasyprint | reportlab
    max_report_pages: int = 500
    include_evidence_screenshots: bool = True

    # Templates
    template_dir: str = "/app/templates"

    # Branding
    company_name: str = "Nexus QA Platform"
    company_logo_path: Optional[str] = None

    # Report retention (days)
    report_retention_days: int = 365


# ─── Enums ─────────────────────────────────────────────────────

class ReportType(str, Enum):
    TRACEABILITY_MATRIX = "traceability_matrix"
    COMPLIANCE_REPORT = "compliance_report"
    EXECUTIVE_SUMMARY = "executive_summary"
    TEST_COVERAGE = "test_coverage"
    DEFECT_SUMMARY = "defect_summary"
    AUDIT_PACKAGE = "audit_package"
    REGULATORY_GAP = "regulatory_gap"
    RATE_VALIDATION = "rate_validation"
    FULL_SESSION_REPORT = "full_session_report"


class ReportFormat(str, Enum):
    HTML = "html"
    PDF = "pdf"
    JSON = "json"
    CSV = "csv"


class ReportStatus(str, Enum):
    QUEUED = "queued"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


# ─── Request / Response Models ────────────────────────────────

class GenerateReportRequest(BaseModel):
    """Request to generate a report."""
    tenant_id: str
    session_id: str
    report_type: ReportType
    format: ReportFormat = ReportFormat.HTML
    title: Optional[str] = None
    description: Optional[str] = None
    # Input data references
    rules: list[dict] = Field(default_factory=list)
    test_cases: list[dict] = Field(default_factory=list)
    test_results: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    # Filters
    product_filter: Optional[str] = None
    state_filter: Optional[str] = None
    date_range_start: Optional[str] = None
    date_range_end: Optional[str] = None
    # Options
    include_evidence: bool = True
    include_recommendations: bool = True
    include_sign_off_section: bool = True


class ReportMetadata(BaseModel):
    """Metadata for a generated report."""
    report_id: str
    report_type: ReportType
    format: ReportFormat
    title: str
    session_id: str
    tenant_id: str
    generated_at: str
    generated_by: str
    page_count: int = 0
    file_size_bytes: int = 0
    checksum_sha256: str = ""
    status: ReportStatus = ReportStatus.QUEUED


class ReportContent(BaseModel):
    """Full report response."""
    metadata: ReportMetadata
    content: str = ""  # HTML/JSON content (for non-PDF formats)
    download_url: Optional[str] = None  # For PDF artifacts
    summary: dict = Field(default_factory=dict)


# ─── The Mouth Engine ─────────────────────────────────────────

class MouthEngine(NexusEngine):

    def __init__(self):
        super().__init__(
            name="mouth",
            version="0.2.0",
            config=MouthConfig(engine_name="mouth", engine_port=8010),
            description="Reporting, Traceability & Compliance Output Engine",
        )
        # Report generators (from app.generators)
        self.traceability_gen = TraceabilityMatrixGenerator()
        self.compliance_gen = ComplianceReportGenerator()
        self.executive_gen = ExecutiveSummaryGenerator()
        self.coverage_gen = TestCoverageReportGenerator()
        self.defect_gen = DefectSummaryGenerator()
        # Renderer (from app.renderers)
        self.html_renderer = HTMLReportRenderer()

    async def on_startup(self):
        """Initialize report storage and load report extensions from plugins."""
        os.makedirs(self.config.report_storage_path, exist_ok=True)
        self.health.set_mode("report_storage", "filesystem")
        count = await self.job_store.count()
        self.health.set_mode("reports_stored", str(count))

        # Load report extensions from domain plugins
        try:
            report_ext = self.plugin_registry.get_merged_reports()
            if report_ext and report_ext.report_types:
                self._plugin_report_types = {
                    rt.type_id: rt for rt in report_ext.report_types
                }
        except Exception:
            self._plugin_report_types = {}

    def register_routes(self, app):

        engine = self  # Closure reference

        # ── Generate Report ────────────────────────────────────

        @app.post("/api/v1/mouth/generate", response_model=dict)
        async def generate_report(
            req: GenerateReportRequest,
            background_tasks: BackgroundTasks,
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Generate a report.

            Supports:
            - traceability_matrix: Rule → Test → Result → Evidence mapping
            - compliance_report: Regulatory compliance assessment
            - executive_summary: C-suite one-pager
            - test_coverage: Detailed coverage analysis with confidence
            - defect_summary: Failed test analysis with root causes
            - full_session_report: All of the above combined
            """
            report_id = f"RPT-{uuid.uuid4().hex[:12].upper()}"

            metadata = ReportMetadata(
                report_id=report_id,
                report_type=req.report_type,
                format=req.format,
                title=req.title or f"{req.report_type.value.replace('_', ' ').title()} Report",
                session_id=req.session_id,
                tenant_id=req.tenant_id,
                generated_at=datetime.now(timezone.utc).isoformat(),
                generated_by=user.user_id,
                status=ReportStatus.QUEUED,
            )

            await engine.job_store.set_job(report_id, {
                "metadata": metadata.model_dump(),
                "content": "",
                "data": {},
            })

            background_tasks.add_task(
                engine._generate_report_async,
                report_id=report_id,
                req=req,
                metadata=metadata,
            )

            return {
                "report_id": report_id,
                "status": "generating",
                "report_type": req.report_type.value,
                "format": req.format.value,
            }

        # ── Get Report Metadata ────────────────────────────────

        @app.get("/api/v1/mouth/reports/{report_id}")
        async def get_report(
            report_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Get report status and metadata."""
            stored = await engine.job_store.get_job(report_id)
            if not stored:
                raise HTTPException(
                    status_code=404, detail=f"Report {report_id} not found",
                )

            return {
                "metadata": stored["metadata"],
                "ready": stored["metadata"]["status"] == ReportStatus.COMPLETED.value,
            }

        # ── Get Report Content ─────────────────────────────────

        @app.get("/api/v1/mouth/reports/{report_id}/content")
        async def get_report_content(
            report_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Download the generated report content.

            Returns HTML, JSON, or CSV based on the report format.
            """
            stored = await engine.job_store.get_job(report_id)
            if not stored:
                raise HTTPException(
                    status_code=404, detail=f"Report {report_id} not found",
                )

            if stored["metadata"]["status"] != ReportStatus.COMPLETED.value:
                raise HTTPException(
                    status_code=202,
                    detail=(
                        f"Report is still {stored['metadata']['status']}. "
                        f"Try again shortly."
                    ),
                )

            fmt = stored["metadata"].get("format", "json")

            if fmt == ReportFormat.HTML.value:
                from fastapi.responses import HTMLResponse
                return HTMLResponse(content=stored["content"])
            elif fmt == ReportFormat.CSV.value:
                from fastapi.responses import PlainTextResponse
                return PlainTextResponse(
                    content=stored["content"],
                    media_type="text/csv",
                    headers={
                        "Content-Disposition": f"attachment; filename={report_id}.csv",
                    },
                )
            else:
                return {"report_id": report_id, "data": stored.get("data", {})}

        # ── Get Report Raw Data ────────────────────────────────

        @app.get("/api/v1/mouth/reports/{report_id}/data")
        async def get_report_data(
            report_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Get raw report data (JSON) regardless of format."""
            stored = await engine.job_store.get_job(report_id)
            if not stored:
                raise HTTPException(
                    status_code=404, detail=f"Report {report_id} not found",
                )

            return {
                "report_id": report_id,
                "data": stored.get("data", {}),
                "metadata": stored["metadata"],
            }

        # ── List Reports ───────────────────────────────────────

        @app.get("/api/v1/mouth/reports")
        async def list_reports(
            tenant_id: Optional[str] = None,
            session_id: Optional[str] = None,
            report_type: Optional[str] = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """List all reports, optionally filtered."""
            results = []
            all_reports = await engine.job_store.list_jobs(limit=500)
            for stored in all_reports:
                rid = stored.get(
                    "job_id",
                    stored.get("metadata", {}).get("report_id", ""),
                )
                meta = stored.get("metadata", {})
                if tenant_id and meta.get("tenant_id") != tenant_id:
                    continue
                if session_id and meta.get("session_id") != session_id:
                    continue
                if report_type and meta.get("report_type") != report_type:
                    continue
                results.append({
                    "report_id": rid,
                    "report_type": meta.get("report_type"),
                    "title": meta.get("title"),
                    "status": meta.get("status"),
                    "format": meta.get("format"),
                    "generated_at": meta.get("generated_at"),
                    "session_id": meta.get("session_id"),
                })

            return {"reports": results, "total": len(results)}

        # ── Verify Integrity ───────────────────────────────────

        @app.post("/api/v1/mouth/verify")
        async def verify_report_integrity(
            report_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Verify report integrity by recomputing checksum.

            Ensures the report hasn't been tampered with since generation.
            Critical for audit compliance.
            """
            stored = await engine.job_store.get_job(report_id)
            if not stored:
                raise HTTPException(
                    status_code=404, detail=f"Report {report_id} not found",
                )

            content = stored.get("content", "")
            original_checksum = stored["metadata"].get("checksum_sha256", "")
            current_checksum = hashlib.sha256(content.encode()).hexdigest()

            is_valid = original_checksum == current_checksum

            return {
                "report_id": report_id,
                "integrity_valid": is_valid,
                "original_checksum": original_checksum,
                "current_checksum": current_checksum,
                "status": (
                    "VERIFIED" if is_valid
                    else "TAMPERED — INTEGRITY COMPROMISED"
                ),
            }

        # ── Stats ──────────────────────────────────────────────

        @app.get("/api/v1/mouth/stats")
        async def get_stats(user: NexusUser = Depends(get_current_user)):
            """Engine statistics."""
            type_counts: dict[str, int] = defaultdict(int)
            status_counts: dict[str, int] = defaultdict(int)
            all_reports = await engine.job_store.list_jobs(limit=500)
            for stored in all_reports:
                meta = stored.get("metadata", {})
                type_counts[meta.get("report_type", "unknown")] += 1
                status_counts[meta.get("status", "unknown")] += 1

            total = await engine.job_store.count()

            return {
                "engine": "mouth",
                "version": "0.2.0",
                "total_reports": total,
                "by_type": dict(type_counts),
                "by_status": dict(status_counts),
                "capabilities": [
                    "traceability_matrix",
                    "compliance_report",
                    "executive_summary",
                    "test_coverage",
                    "defect_summary",
                    "full_session_report",
                    "integrity_verification",
                ],
                "supported_formats": [f.value for f in ReportFormat],
            }

    # ── Background Report Generation ───────────────────────────

    async def _generate_report_async(
        self,
        report_id: str,
        req: GenerateReportRequest,
        metadata: ReportMetadata,
    ):
        """Background report generation."""
        try:
            stored = await self.job_store.get_job(report_id)
            if not stored:
                return
            stored["metadata"]["status"] = ReportStatus.GENERATING.value
            await self.job_store.set_job(report_id, stored)

            meta_dict = stored["metadata"]
            report_data: dict = {}
            html_content = ""

            if req.report_type == ReportType.TRACEABILITY_MATRIX:
                report_data = self.traceability_gen.generate(
                    req.rules, req.test_cases, req.test_results, req.evidence,
                )
                if req.format == ReportFormat.HTML:
                    html_content = self.html_renderer.render_traceability_matrix(
                        report_data, meta_dict,
                    )

            elif req.report_type == ReportType.COMPLIANCE_REPORT:
                report_data = self.compliance_gen.generate(
                    req.rules, req.test_cases, req.test_results,
                    req.state_filter,
                )
                if req.format == ReportFormat.HTML:
                    html_content = self.html_renderer.render_compliance_report(
                        report_data, meta_dict,
                    )

            elif req.report_type == ReportType.EXECUTIVE_SUMMARY:
                trace_data = self.traceability_gen.generate(
                    req.rules, req.test_cases, req.test_results, req.evidence,
                )
                comp_data = self.compliance_gen.generate(
                    req.rules, req.test_cases, req.test_results,
                    req.state_filter,
                )
                report_data = self.executive_gen.generate(
                    session_name=req.title or req.session_id,
                    rules=req.rules,
                    test_cases=req.test_cases,
                    test_results=req.test_results,
                    traceability_summary=trace_data["summary"],
                    compliance_summary=comp_data["summary"],
                )
                if req.format == ReportFormat.HTML:
                    html_content = self.html_renderer.render_executive_summary(
                        report_data, meta_dict,
                    )

            elif req.report_type == ReportType.TEST_COVERAGE:
                report_data = self.coverage_gen.generate(
                    req.rules, req.test_cases, req.test_results,
                )
                if req.format == ReportFormat.HTML:
                    html_content = self.html_renderer.render_traceability_matrix(
                        {
                            "matrix": report_data["coverage_details"],
                            "summary": report_data["summary"],
                        },
                        meta_dict,
                    )

            elif req.report_type == ReportType.DEFECT_SUMMARY:
                report_data = self.defect_gen.generate(
                    req.test_cases, req.test_results,
                )
                if req.format == ReportFormat.HTML:
                    html_content = self.html_renderer.render_defect_summary(
                        report_data, meta_dict,
                    )

            elif req.report_type == ReportType.FULL_SESSION_REPORT:
                trace_data = self.traceability_gen.generate(
                    req.rules, req.test_cases, req.test_results, req.evidence,
                )
                comp_data = self.compliance_gen.generate(
                    req.rules, req.test_cases, req.test_results,
                    req.state_filter,
                )
                exec_data = self.executive_gen.generate(
                    session_name=req.title or req.session_id,
                    rules=req.rules,
                    test_cases=req.test_cases,
                    test_results=req.test_results,
                    traceability_summary=trace_data["summary"],
                    compliance_summary=comp_data["summary"],
                )
                cov_data = self.coverage_gen.generate(
                    req.rules, req.test_cases, req.test_results,
                )
                defect_data = self.defect_gen.generate(
                    req.test_cases, req.test_results,
                )

                report_data = {
                    "executive_summary": exec_data,
                    "traceability": trace_data,
                    "compliance": comp_data,
                    "coverage": cov_data,
                    "defects": defect_data,
                }

                if req.format == ReportFormat.HTML:
                    sections = [
                        self.html_renderer.render_executive_summary(
                            exec_data, meta_dict,
                        ),
                        self.html_renderer.render_traceability_matrix(
                            trace_data, meta_dict,
                        ),
                        self.html_renderer.render_compliance_report(
                            comp_data, meta_dict,
                        ),
                        self.html_renderer.render_defect_summary(
                            defect_data, meta_dict,
                        ),
                    ]
                    html_content = sections[0]
                    report_data["section_count"] = len(sections)

            # Handle format output
            if req.format == ReportFormat.HTML:
                content = html_content
            elif req.format == ReportFormat.JSON:
                content = json.dumps(report_data, indent=2, default=str)
            elif req.format == ReportFormat.CSV:
                content = _data_to_csv(report_data)
            else:
                content = json.dumps(report_data, indent=2, default=str)

            # Compute checksum for immutability verification
            checksum = hashlib.sha256(content.encode()).hexdigest()

            stored["content"] = content
            stored["data"] = report_data
            stored["metadata"]["status"] = ReportStatus.COMPLETED.value
            stored["metadata"]["checksum_sha256"] = checksum
            stored["metadata"]["file_size_bytes"] = len(content.encode())
            await self.job_store.set_job(report_id, stored)

        except Exception as e:
            err_stored = await self.job_store.get_job(report_id)
            if err_stored:
                err_stored["metadata"]["status"] = ReportStatus.FAILED.value
                err_stored["metadata"]["error"] = str(e)
                await self.job_store.set_job(report_id, err_stored)


# ─── Helper Functions ──────────────────────────────────────────

def _data_to_csv(data: dict) -> str:
    """Convert report data to CSV format."""
    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output)

    # Try to find a list of dicts in the data
    for key in ["matrix", "coverage_details", "defects", "compliance_items"]:
        if key in data and isinstance(data[key], list) and data[key]:
            headers = list(data[key][0].keys())
            writer.writerow(headers)
            for item in data[key]:
                row = []
                for h in headers:
                    val = item.get(h, "")
                    if isinstance(val, (list, dict)):
                        val = json.dumps(val)
                    row.append(val)
                writer.writerow(row)
            break
    else:
        # Fallback: dump summary
        if "summary" in data:
            for k, v in data["summary"].items():
                writer.writerow([k, v])

    return output.getvalue()


# ─── Entry Point ──────────────────────────────────────────────

def main():
    engine = MouthEngine()
    engine.run()


if __name__ == "__main__":
    main()

