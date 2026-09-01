"""
HTML Report Renderer.

Renders report data structures into professional, self-contained HTML
documents with inline CSS.  Reports are portable — they look correct
when opened as standalone files with no web server.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class HTMLReportRenderer:
    """
    Renders report data into professional HTML.

    Uses inline CSS for portability — reports must look good
    when opened as standalone files without a web server.
    """

    # Professional color scheme
    COLORS: dict[str, str] = {
        "primary": "#1a365d",
        "secondary": "#2d3748",
        "success": "#38a169",
        "warning": "#d69e2e",
        "danger": "#e53e3e",
        "info": "#3182ce",
        "light": "#f7fafc",
        "border": "#e2e8f0",
    }

    # ── Public Renderers ───────────────────────────────────────

    def render_traceability_matrix(
        self, data: dict, metadata: dict,
    ) -> str:
        """Render traceability matrix as HTML."""
        summary = data["summary"]
        matrix = data["matrix"]

        rows_html = ""
        for entry in matrix:
            status_color = {
                "full": self.COLORS["success"],
                "high": "#48bb78",
                "moderate": self.COLORS["warning"],
                "low": "#ed8936",
                "critical": self.COLORS["danger"],
            }.get(entry["coverage_status"], self.COLORS["danger"])

            rows_html += f"""
            <tr>
                <td style="font-weight:600">{entry['rule_id']}</td>
                <td>{entry['rule_description'][:100]}{'...' if len(entry['rule_description']) > 100 else ''}</td>
                <td style="text-align:center">{entry['rule_priority'].upper()}</td>
                <td style="text-align:center">{entry['test_case_count']}</td>
                <td style="text-align:center;color:{self.COLORS['success']}">{entry['tests_passed']}</td>
                <td style="text-align:center;color:{self.COLORS['danger']}">{entry['tests_failed']}</td>
                <td style="text-align:center">{entry['tests_not_run']}</td>
                <td style="text-align:center">
                    <span style="background:{status_color};color:white;padding:2px 8px;border-radius:4px;font-size:0.8rem">
                        {entry['coverage_status'].upper()}
                    </span>
                </td>
            </tr>"""

        # Always include "Traceability Matrix" as the report-type title.
        # The generic metadata title is used as a subtitle/descriptor.
        meta_title = metadata.get("title", "")
        display_title = "Traceability Matrix"
        subtitle_parts = []
        if meta_title and meta_title != display_title:
            subtitle_parts.append(meta_title)
        subtitle_parts.append(f"Session: {metadata.get('session_id', 'N/A')}")

        return self._wrap_html(
            title=display_title,
            subtitle=" — ".join(subtitle_parts),
            body=f"""
            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px">
                {self._metric_card("Total Rules", summary['total_rules'], self.COLORS['primary'])}
                {self._metric_card("Rules Covered", summary['rules_with_tests'], self.COLORS['success'])}
                {self._metric_card("Coverage %", f"{summary['overall_coverage_pct']}%", self.COLORS['info'])}
                {self._metric_card("Pass Rate", f"{summary['overall_pass_pct']}%",
                    self.COLORS['success'] if summary['overall_pass_pct'] >= 80 else self.COLORS['danger'])}
            </div>

            <table style="width:100%;border-collapse:collapse;font-size:0.9rem">
                <thead>
                    <tr style="background:{self.COLORS['primary']};color:white">
                        <th style="padding:10px;text-align:left">Rule ID</th>
                        <th style="padding:10px;text-align:left">Description</th>
                        <th style="padding:10px;text-align:center">Priority</th>
                        <th style="padding:10px;text-align:center">Tests</th>
                        <th style="padding:10px;text-align:center">Passed</th>
                        <th style="padding:10px;text-align:center">Failed</th>
                        <th style="padding:10px;text-align:center">Not Run</th>
                        <th style="padding:10px;text-align:center">Coverage</th>
                    </tr>
                </thead>
                <tbody>{rows_html}</tbody>
            </table>
            """,
            metadata=metadata,
        )

    def render_executive_summary(
        self, data: dict, metadata: dict,
    ) -> str:
        """Render executive summary as HTML."""
        metrics = data["key_metrics"]
        health = data["overall_health"]
        health_color = {
            "HEALTHY": self.COLORS["success"],
            "NEEDS ATTENTION": self.COLORS["warning"],
            "AT RISK": "#ed8936",
            "CRITICAL": self.COLORS["danger"],
        }.get(health, self.COLORS["danger"])

        risks_html = ""
        for risk in data.get("top_risks", []):
            sev_color = {
                "high": self.COLORS["danger"],
                "medium": self.COLORS["warning"],
                "low": self.COLORS["info"],
            }.get(risk["severity"], self.COLORS["info"])
            risks_html += f"""
            <tr>
                <td>{risk['area']}</td>
                <td style="text-align:center">{risk['failed_tests']}</td>
                <td style="text-align:center">
                    <span style="background:{sev_color};color:white;padding:2px 8px;border-radius:4px">
                        {risk['severity'].upper()}
                    </span>
                </td>
            </tr>"""

        return self._wrap_html(
            title=f"Executive Summary — {data['session_name']}",
            subtitle=f"Generated: {data['generated_at'][:10]}",
            body=f"""
            <div style="text-align:center;margin:24px 0;padding:24px;background:{health_color};color:white;border-radius:12px">
                <div style="font-size:2rem;font-weight:700">{health}</div>
                <div style="font-size:1rem;margin-top:8px;opacity:0.9">{data['recommendation']}</div>
            </div>

            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px">
                {self._metric_card("Rules Found", metrics['business_rules_extracted'], self.COLORS['primary'])}
                {self._metric_card("Tests Gen.", metrics['test_cases_generated'], self.COLORS['info'])}
                {self._metric_card("Pass Rate", f"{metrics['pass_rate_pct']}%",
                    self.COLORS['success'] if metrics['pass_rate_pct'] >= 80 else self.COLORS['danger'])}
                {self._metric_card("Compliance", f"{metrics['compliance_rate_pct']}%",
                    self.COLORS['success'] if metrics['compliance_rate_pct'] >= 80 else self.COLORS['danger'])}
            </div>

            <h3 style="color:{self.COLORS['primary']}">Top Risks</h3>
            <table style="width:100%;border-collapse:collapse">
                <thead>
                    <tr style="background:{self.COLORS['primary']};color:white">
                        <th style="padding:8px;text-align:left">Area</th>
                        <th style="padding:8px;text-align:center">Failed Tests</th>
                        <th style="padding:8px;text-align:center">Severity</th>
                    </tr>
                </thead>
                <tbody>{risks_html if risks_html else '<tr><td colspan="3" style="text-align:center;padding:12px">No risks identified</td></tr>'}</tbody>
            </table>

            <div style="margin-top:32px;padding:16px;background:{self.COLORS['light']};border-radius:8px">
                <h3 style="color:{self.COLORS['primary']};margin-top:0">Sign-Off</h3>
                <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px">
                    <div style="border:1px dashed {self.COLORS['border']};padding:16px;border-radius:8px">
                        <div style="font-weight:600">QA Lead</div>
                        <div style="margin-top:24px;border-top:1px solid {self.COLORS['border']};padding-top:4px;font-size:0.8rem">
                            Signature / Date
                        </div>
                    </div>
                    <div style="border:1px dashed {self.COLORS['border']};padding:16px;border-radius:8px">
                        <div style="font-weight:600">Product Owner</div>
                        <div style="margin-top:24px;border-top:1px solid {self.COLORS['border']};padding-top:4px;font-size:0.8rem">
                            Signature / Date
                        </div>
                    </div>
                    <div style="border:1px dashed {self.COLORS['border']};padding:16px;border-radius:8px">
                        <div style="font-weight:600">Compliance Officer</div>
                        <div style="margin-top:24px;border-top:1px solid {self.COLORS['border']};padding-top:4px;font-size:0.8rem">
                            Signature / Date
                        </div>
                    </div>
                </div>
            </div>
            """,
            metadata=metadata,
        )

    def render_compliance_report(
        self, data: dict, metadata: dict,
    ) -> str:
        """Render compliance report as HTML."""
        summary = data["summary"]
        items = data["compliance_items"]

        rows_html = ""
        for item in items:
            status_style = {
                "compliant": f"background:{self.COLORS['success']};color:white",
                "partial": f"background:{self.COLORS['warning']};color:white",
                "non_compliant": f"background:{self.COLORS['danger']};color:white",
                "not_assessed": (
                    f"background:{self.COLORS['border']};"
                    f"color:{self.COLORS['secondary']}"
                ),
            }.get(item["status"], "")

            gaps_html = (
                "<br>".join(f"• {g}" for g in item.get("gaps", [])) or "—"
            )

            rows_html += f"""
            <tr>
                <td style="font-weight:600">{item['requirement_id']}</td>
                <td>{item['requirement_description']}</td>
                <td style="text-align:center">{item['jurisdiction']}</td>
                <td style="text-align:center">
                    <span style="{status_style};padding:2px 8px;border-radius:4px;font-size:0.8rem">
                        {item['status'].upper().replace('_', ' ')}
                    </span>
                </td>
                <td style="font-size:0.8rem">{gaps_html}</td>
                <td style="font-size:0.8rem">{item.get('remediation', '')}</td>
            </tr>"""

        return self._wrap_html(
            title=metadata.get("title", "Compliance Report"),
            subtitle=(
                f"Jurisdiction: {summary.get('jurisdiction', 'ALL')} | "
                f"Assessment Date: {summary.get('assessment_date', 'N/A')[:10]}"
            ),
            body=f"""
            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px">
                {self._metric_card("Total Req.", summary['total_requirements'], self.COLORS['primary'])}
                {self._metric_card("Compliant", summary['compliant'], self.COLORS['success'])}
                {self._metric_card("Non-Compliant", summary['non_compliant'], self.COLORS['danger'])}
                {self._metric_card("Rate", f"{summary['compliance_rate_pct']}%",
                    self.COLORS['success'] if summary['compliance_rate_pct'] >= 80 else self.COLORS['danger'])}
            </div>

            <table style="width:100%;border-collapse:collapse;font-size:0.85rem">
                <thead>
                    <tr style="background:{self.COLORS['primary']};color:white">
                        <th style="padding:8px;text-align:left">Requirement</th>
                        <th style="padding:8px;text-align:left">Description</th>
                        <th style="padding:8px;text-align:center">Jurisdiction</th>
                        <th style="padding:8px;text-align:center">Status</th>
                        <th style="padding:8px;text-align:left">Gaps</th>
                        <th style="padding:8px;text-align:left">Remediation</th>
                    </tr>
                </thead>
                <tbody>{rows_html}</tbody>
            </table>
            """,
            metadata=metadata,
        )

    def render_defect_summary(
        self, data: dict, metadata: dict,
    ) -> str:
        """Render defect summary as HTML."""
        summary = data["summary"]
        defects = data["defects"]

        rows_html = ""
        for defect in defects:
            sev_color = {
                "critical": self.COLORS["danger"],
                "high": "#ed8936",
                "medium": self.COLORS["warning"],
                "low": self.COLORS["info"],
            }.get(defect["severity"], self.COLORS["info"])

            rows_html += f"""
            <tr>
                <td style="font-size:0.8rem">{defect['test_case_id'][:12]}</td>
                <td>{defect['test_name'][:80]}</td>
                <td style="text-align:center">
                    <span style="background:{sev_color};color:white;padding:2px 6px;border-radius:4px;font-size:0.75rem">
                        {defect['severity'].upper()}
                    </span>
                </td>
                <td>{defect['error_message'][:100]}</td>
                <td style="font-size:0.8rem;color:{self.COLORS['secondary']}">{defect['suggested_root_cause'][:80]}</td>
            </tr>"""

        return self._wrap_html(
            title=metadata.get("title", "Defect Summary Report"),
            subtitle=f"Total Failures: {summary['total_failures']}",
            body=f"""
            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px">
                {self._metric_card("Critical", summary.get('critical', 0), self.COLORS['danger'])}
                {self._metric_card("High", summary.get('high', 0), '#ed8936')}
                {self._metric_card("Medium", summary.get('medium', 0), self.COLORS['warning'])}
                {self._metric_card("Low", summary.get('low', 0), self.COLORS['info'])}
            </div>

            <table style="width:100%;border-collapse:collapse;font-size:0.85rem">
                <thead>
                    <tr style="background:{self.COLORS['primary']};color:white">
                        <th style="padding:8px;text-align:left">Test ID</th>
                        <th style="padding:8px;text-align:left">Test Name</th>
                        <th style="padding:8px;text-align:center">Severity</th>
                        <th style="padding:8px;text-align:left">Error</th>
                        <th style="padding:8px;text-align:left">Root Cause</th>
                    </tr>
                </thead>
                <tbody>{rows_html if rows_html else '<tr><td colspan="5" style="text-align:center;padding:20px">No defects found — all tests passed!</td></tr>'}</tbody>
            </table>
            """,
            metadata=metadata,
        )

    # ── Private Helpers ────────────────────────────────────────

    def _metric_card(self, label: str, value: Any, color: str) -> str:
        """Render a single KPI metric card."""
        return f"""
        <div style="background:white;border:1px solid {self.COLORS['border']};border-top:4px solid {color};
                    padding:16px;border-radius:8px;text-align:center">
            <div style="font-size:0.8rem;color:{self.COLORS['secondary']};text-transform:uppercase">{label}</div>
            <div style="font-size:1.8rem;font-weight:700;color:{color};margin-top:4px">{value}</div>
        </div>"""

    def _wrap_html(
        self,
        title: str,
        subtitle: str,
        body: str,
        metadata: dict | None = None,
    ) -> str:
        """Wrap content in a full HTML document."""
        meta = metadata or {}
        report_id = meta.get("report_id", "N/A")
        generated_at = meta.get(
            "generated_at", datetime.now(timezone.utc).isoformat(),
        )[:19]
        generated_by = meta.get("generated_by", "Nexus QA Platform")

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — Nexus QA</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: {self.COLORS['light']};
            color: {self.COLORS['secondary']};
            line-height: 1.6;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
        table td, table th {{ padding: 8px 10px; border-bottom: 1px solid {self.COLORS['border']}; }}
        tr:hover {{ background: #edf2f7; }}
        @media print {{
            body {{ background: white; }}
            .no-print {{ display: none; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div style="background:{self.COLORS['primary']};color:white;padding:24px;border-radius:12px;margin-bottom:24px">
            <div style="display:flex;justify-content:space-between;align-items:center">
                <div>
                    <h1 style="font-size:1.5rem;margin-bottom:4px">{title}</h1>
                    <div style="opacity:0.8;font-size:0.9rem">{subtitle}</div>
                </div>
                <div style="text-align:right;font-size:0.8rem;opacity:0.8">
                    <div>Report ID: {report_id}</div>
                    <div>Generated: {generated_at}</div>
                    <div>By: {generated_by}</div>
                </div>
            </div>
        </div>

        <!-- Body -->
        {body}

        <!-- Footer -->
        <div style="margin-top:32px;padding-top:16px;border-top:2px solid {self.COLORS['border']};
                    font-size:0.75rem;color:#a0aec0;text-align:center">
            <p>Generated by Nexus QA Platform | Report ID: {report_id}</p>
            <p>This report is an immutable artifact. Any modifications invalidate the checksum.</p>
            <p style="margin-top:4px">Checksum will be computed on final output.</p>
        </div>
    </div>
</body>
</html>"""
