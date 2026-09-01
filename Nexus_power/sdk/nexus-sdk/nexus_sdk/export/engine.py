"""
Nexus Export Engine — multi-format test case exporter.

Supported formats:
  - EXCEL  (.xlsx) — styled multi-sheet workbook via openpyxl
  - CSV    (.csv)  — standard RFC 4180 CSV
  - JSON   (.json) — full nested structure
  - HTML   (.html) — styled report

Usage:
    engine = ExportEngine()
    result = await engine.export_test_cases(
        test_cases=cases,
        fmt=ExportFormat.EXCEL,
        output_dir=Path("/tmp/exports"),
    )
"""

from __future__ import annotations

import csv
import io
import json
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence

import structlog

from nexus_sdk.models import ProductionTestCase

logger = structlog.get_logger()


# ─── Export Format Enum ───────────────────────────────────────────

class ExportFormat(str, Enum):
    """Supported export output formats."""
    EXCEL = "excel"
    CSV = "csv"
    JSON = "json"
    HTML = "html"
    PLAYWRIGHT_TS = "playwright_ts"


# ─── Export Result ────────────────────────────────────────────────

class ExportResult:
    """Result of an export operation."""

    __slots__ = (
        "job_id",
        "format",
        "file_path",
        "file_size_bytes",
        "record_count",
        "step_count",
        "duration_ms",
        "success",
        "error",
        "created_at",
    )

    def __init__(
        self,
        *,
        job_id: str,
        fmt: ExportFormat,
        file_path: Path,
        file_size_bytes: int = 0,
        record_count: int = 0,
        step_count: int = 0,
        duration_ms: float = 0.0,
        success: bool = True,
        error: Optional[str] = None,
    ):
        self.job_id = job_id
        self.format = fmt
        self.file_path = file_path
        self.file_size_bytes = file_size_bytes
        self.record_count = record_count
        self.step_count = step_count
        self.duration_ms = duration_ms
        self.success = success
        self.error = error
        self.created_at = datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "format": self.format.value,
            "file_path": str(self.file_path),
            "file_size_bytes": self.file_size_bytes,
            "record_count": self.record_count,
            "step_count": self.step_count,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
        }


# ─── Export Engine ────────────────────────────────────────────────

class ExportEngine:
    """
    Production-grade test case exporter.

    Thread-safe, stateless — create one instance per application.
    """

    def __init__(self, *, default_output_dir: Optional[Path] = None):
        self._default_dir = default_output_dir or Path(os.getenv(
            "NEXUS_EXPORT_DIR", "/tmp/nexus-exports",
        ))

    async def export_test_cases(
        self,
        test_cases: Sequence[ProductionTestCase],
        fmt: ExportFormat = ExportFormat.EXCEL,
        output_dir: Optional[Path] = None,
        *,
        filename_prefix: str = "nexus-testcases",
        title: str = "Nexus QA — Test Cases",
        include_summary: bool = True,
    ) -> ExportResult:
        """
        Export test cases to the specified format.

        Args:
            test_cases: Non-empty list of ProductionTestCase objects.
            fmt: Output format.
            output_dir: Override default output directory.
            filename_prefix: Prefix for the generated filename.
            title: Title for Excel summary / HTML header.
            include_summary: Include summary sheet (Excel) or section (HTML).

        Returns:
            ExportResult with file_path, size, and timing data.
        """
        job_id = str(uuid.uuid4())
        start = datetime.now(timezone.utc)
        out_dir = output_dir or self._default_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        timestamp = start.strftime("%Y%m%d_%H%M%S")
        step_count = sum(len(tc.steps) for tc in test_cases)

        try:
            if fmt == ExportFormat.EXCEL:
                file_path = out_dir / f"{filename_prefix}_{timestamp}.xlsx"
                file_path = self._export_excel(
                    test_cases, file_path, title=title, include_summary=include_summary,
                )
            elif fmt == ExportFormat.CSV:
                file_path = out_dir / f"{filename_prefix}_{timestamp}.csv"
                file_path = self._export_csv(test_cases, file_path)
            elif fmt == ExportFormat.JSON:
                file_path = out_dir / f"{filename_prefix}_{timestamp}.json"
                file_path = self._export_json(test_cases, file_path)
            elif fmt == ExportFormat.HTML:
                file_path = out_dir / f"{filename_prefix}_{timestamp}.html"
                file_path = self._export_html(
                    test_cases, file_path, title=title, include_summary=include_summary,
                )
            elif fmt == ExportFormat.PLAYWRIGHT_TS:
                file_path = out_dir / f"{filename_prefix}_{timestamp}.zip"
                file_path = self._export_playwright_ts(test_cases, file_path)
            else:
                raise ValueError(f"Unsupported export format: {fmt}")

            file_size = file_path.stat().st_size
            elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000

            result = ExportResult(
                job_id=job_id,
                fmt=fmt,
                file_path=file_path,
                file_size_bytes=file_size,
                record_count=len(test_cases),
                step_count=step_count,
                duration_ms=elapsed,
                success=True,
            )

            logger.info(
                "export.completed",
                job_id=job_id,
                format=fmt.value,
                records=len(test_cases),
                steps=step_count,
                size_bytes=file_size,
                duration_ms=round(elapsed, 1),
                path=str(file_path),
            )
            return result

        except Exception as exc:
            elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000
            logger.error(
                "export.failed",
                job_id=job_id,
                format=fmt.value,
                error=str(exc),
            )
            return ExportResult(
                job_id=job_id,
                fmt=fmt,
                file_path=out_dir / f"{filename_prefix}_FAILED",
                file_size_bytes=0,
                record_count=len(test_cases),
                step_count=step_count,
                duration_ms=elapsed,
                success=False,
                error=str(exc),
            )

    # ─── Format-specific renderers ────────────────────────────

    @staticmethod
    def _export_excel(
        test_cases: Sequence[ProductionTestCase],
        output_path: Path,
        *,
        title: str,
        include_summary: bool,
    ) -> Path:
        """Delegate to Excel renderer (openpyxl)."""
        from nexus_sdk.export.excel import render_excel
        return render_excel(
            test_cases, output_path, title=title, include_summary=include_summary,
        )

    @staticmethod
    def _export_csv(
        test_cases: Sequence[ProductionTestCase],
        output_path: Path,
    ) -> Path:
        """
        Export test cases as a flat CSV — one row per step.

        Columns: Test Case ID, Title, Test Type, Priority, Status,
                 Step Number, Action, Expected Result, Data Refs
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)

            # Header
            writer.writerow([
                "Test Case ID",
                "Title",
                "Test Type",
                "Priority",
                "Status",
                "Step Number",
                "Action",
                "Expected Result",
                "Data References",
                "Preconditions",
                "Data Workbook Fields",
            ])

            for tc in test_cases:
                preconditions_text = " | ".join(
                    p.description for p in tc.preconditions
                )
                data_fields_text = " | ".join(
                    f"{e.field_name}={e.field_value}" for e in tc.data_workbook
                )

                if not tc.steps:
                    writer.writerow([
                        tc.test_case_id,
                        tc.title,
                        tc.test_type,
                        tc.priority,
                        tc.status,
                        "",
                        "(No steps defined)",
                        "",
                        "",
                        preconditions_text,
                        data_fields_text,
                    ])
                    continue

                for step in tc.steps:
                    data_refs = ", ".join(step.input_data_refs)
                    writer.writerow([
                        tc.test_case_id,
                        tc.title,
                        tc.test_type,
                        tc.priority,
                        tc.status,
                        step.step_number,
                        step.action,
                        step.expected_result,
                        data_refs,
                        preconditions_text,
                        data_fields_text,
                    ])

        return output_path

    @staticmethod
    def _export_json(
        test_cases: Sequence[ProductionTestCase],
        output_path: Path,
    ) -> Path:
        """Export test cases as JSON with full nested structure."""
        output_path.parent.mkdir(parents=True, exist_ok=True)

        export_data = {
            "export_metadata": {
                "format": "nexus-testcase-v1",
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "total_test_cases": len(test_cases),
                "total_steps": sum(len(tc.steps) for tc in test_cases),
            },
            "test_cases": [
                _tc_to_json_dict(tc) for tc in test_cases
            ],
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)

        return output_path

    @staticmethod
    def _export_html(
        test_cases: Sequence[ProductionTestCase],
        output_path: Path,
        *,
        title: str,
        include_summary: bool,
    ) -> Path:
        """Export test cases as a styled HTML report."""
        output_path.parent.mkdir(parents=True, exist_ok=True)

        html = _render_html(test_cases, title=title, include_summary=include_summary)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        return output_path

    @staticmethod
    def _export_playwright_ts(
        test_cases: Sequence[ProductionTestCase],
        output_path: Path,
    ) -> Path:
        """Export test cases as a ZIP archive of Playwright .spec.ts files.

        One ``.spec.ts`` file is generated per test case.  Each step becomes a
        ``test.step()`` block.  Steps that carry a ``target_element`` (Playwright
        selector) are emitted as ``await page.locator(...).click()`` / fill calls.

        Steps with evidence anchors (evidence_scene_id, evidence_control_id) receive
        an evidence comment on every line.  Steps failing the automation gate are
        emitted as ``test.skip('UNPROVEN: {reason}')`` — they are never silently
        dropped so the coverage is always transparent.

        The coverage header in each file reports:
            // X of Y steps automation-ready. Z unproven steps.
        """
        import zipfile
        import re

        _PROOF_THRESHOLD = 0.85
        _ACTIONABLE_KWDS = {"click", "fill", "type", "enter", "input", "select", "navigate", "submit"}

        output_path.parent.mkdir(parents=True, exist_ok=True)

        def _safe_name(text: str) -> str:
            return re.sub(r"[^a-zA-Z0-9_\-]", "_", text)[:60]

        def _step_to_ts(step, indent: str = "    ") -> str:
            selector = step.target_element or ""
            action_lower = (step.action or "").lower()
            expected = step.expected_result or ""

            # Evidence metadata — carried in step.metadata_json by the persister
            meta: dict = getattr(step, "metadata_json", {}) or {}
            evidence_scene_id = meta.get("evidence_scene_id") or ""
            evidence_control_id = meta.get("evidence_control_id") or ""
            evidence_edge_id = meta.get("evidence_edge_id") or ""
            proof_confidence = float(meta.get("proof_confidence") or 0.0)
            selector_source = meta.get("selector_source") or ("ocr" if selector else "none")

            # Evidence comment (attached to every step that has anchors)
            evidence_comment = (
                f"{indent}// Evidence: scene_id={evidence_scene_id[:8] or 'none'}, "
                f"confidence={proof_confidence:.2f}, "
                f"selector_source={selector_source}\n"
                if evidence_scene_id or evidence_control_id
                else ""
            )

            # Gate check for actionable steps
            is_actionable = any(kw in action_lower for kw in _ACTIONABLE_KWDS)
            if is_actionable:
                if not evidence_scene_id:
                    reason = "no evidence_scene_id"
                elif not evidence_control_id:
                    reason = "no evidence_control_id"
                elif proof_confidence < _PROOF_THRESHOLD:
                    reason = f"proof_confidence={proof_confidence:.2f} below threshold"
                elif not selector:
                    reason = "selector not OCR-backed"
                elif not expected:
                    reason = "no expected_output"
                else:
                    reason = ""
                if reason:
                    return (
                        f"{evidence_comment}"
                        f"{indent}test.skip(true, {json.dumps('UNPROVEN: ' + reason)});\n"
                        f"{indent}// Skipped step: {action_lower[:80]}\n"
                    )

            # Emit automation call
            if selector:
                if any(kw in action_lower for kw in ("fill", "type", "enter", "input")):
                    data = step.input_data_refs[0] if step.input_data_refs else "<value>"
                    pw_call = f'await page.locator({json.dumps(selector)}).fill({json.dumps(data)});'
                elif any(kw in action_lower for kw in ("select", "choose")):
                    pw_call = f'await page.locator({json.dumps(selector)}).selectOption("");'
                elif any(kw in action_lower for kw in ("verify", "assert", "check", "expect", "should")):
                    pw_call = f'await expect(page.locator({json.dumps(selector)})).toBeVisible();'
                else:
                    pw_call = f'await page.locator({json.dumps(selector)}).click();'
            else:
                pw_call = f'// TODO: {json.dumps(step.action)}'

            expect_line = (
                f"{indent}  await expect(page.getByText({json.dumps(expected[:120])})).toBeVisible();\n"
                if expected and is_actionable
                else ""
            )

            return (
                f"{evidence_comment}"
                f"{indent}await test.step({json.dumps(step.action)}, async () => {{\n"
                f"{indent}  {pw_call}\n"
                f"{expect_line}"
                f"{indent}}});\n"
            )

        def _tc_to_spec(tc: ProductionTestCase) -> str:
            total_steps = len(tc.steps)
            # Count automation-ready: have a selector and expected result
            meta_list = [getattr(s, "metadata_json", {}) or {} for s in tc.steps]
            proven = sum(
                1 for s, m in zip(tc.steps, meta_list)
                if s.target_element
                and m.get("evidence_scene_id")
                and float(m.get("proof_confidence") or 0) >= _PROOF_THRESHOLD
                and s.expected_result
            )
            unproven = total_steps - proven

            lines = [
                "import { test, expect } from '@playwright/test';",
                "",
                f"// Test Case: {tc.test_case_id}",
                f"// Type: {tc.test_type}  Priority: {tc.priority}  Status: {tc.status}",
                f"// {proven} of {total_steps} steps automation-ready. {unproven} unproven steps.",
                "",
                f"test.describe({json.dumps(tc.title)}, () => {{",
            ]
            if tc.preconditions:
                lines.append("  test.beforeEach(async ({ page }) => {")
                for pre in tc.preconditions:
                    lines.append(f"    // Precondition: {pre.description}")
                lines.append("  });")
                lines.append("")

            lines.append(f"  test({json.dumps(tc.description or tc.title)}, async ({{ page }}) => {{")
            for step in tc.steps:
                lines.append(_step_to_ts(step, indent="    "))
            lines.append("  });")
            lines.append("}});")
            lines.append("")
            return "\n".join(lines)

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for tc in test_cases:
                spec_content = _tc_to_spec(tc)
                filename = f"{_safe_name(tc.title or tc.test_case_id)}.spec.ts"
                zf.writestr(filename, spec_content)

        return output_path

    async def export_to_bytes(
        self,
        test_cases: Sequence[ProductionTestCase],
        fmt: ExportFormat = ExportFormat.EXCEL,
        *,
        title: str = "Nexus QA — Test Cases",
        include_summary: bool = True,
    ) -> tuple[bytes, str]:
        """
        Export to in-memory bytes (useful for streaming HTTP responses).

        Returns:
            Tuple of (file_bytes, content_type).
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await self.export_test_cases(
                test_cases,
                fmt=fmt,
                output_dir=Path(tmpdir),
                title=title,
                include_summary=include_summary,
            )
            if not result.success:
                raise RuntimeError(f"Export failed: {result.error}")

            content_type_map = {
                ExportFormat.EXCEL: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ExportFormat.CSV: "text/csv; charset=utf-8",
                ExportFormat.JSON: "application/json; charset=utf-8",
                ExportFormat.HTML: "text/html; charset=utf-8",
                ExportFormat.PLAYWRIGHT_TS: "application/zip",
            }

            with open(result.file_path, "rb") as f:
                data = f.read()

            return data, content_type_map[fmt]


# ─── JSON Serialization Helper ────────────────────────────────────

def _tc_to_json_dict(tc: ProductionTestCase) -> dict[str, Any]:
    """Convert a ProductionTestCase to a JSON-serializable dict."""
    return {
        "test_case_id": tc.test_case_id,
        "title": tc.title,
        "description": tc.description,
        "test_type": tc.test_type,
        "priority": tc.priority,
        "status": tc.status,
        "version": tc.version,
        "preconditions": [
            {"description": p.description, "is_verified": p.is_verified}
            for p in tc.preconditions
        ],
        "steps": [
            {
                "step_number": s.step_number,
                "action": s.action,
                "expected_result": s.expected_result,
                "target_system": s.target_system,
                "target_element": s.target_element,
                "input_data_refs": s.input_data_refs,
                "verification": s.verification,
                "screenshot_required": s.screenshot_required,
            }
            for s in tc.steps
        ],
        "data_workbook": [
            {
                "field_name": e.field_name,
                "field_value": e.field_value,
                "field_type": e.field_type,
                "is_sensitive": e.is_sensitive,
                "generator_hint": e.generator_hint,
            }
            for e in tc.data_workbook
        ],
        "target_systems": tc.target_systems,
        "validates_rules": tc.validates_rules,
        "tags": tc.tags,
        "source_session_id": tc.source_session_id,
        "suite_id": tc.suite_id,
        "generated_by": tc.generated_by,
        "approved_by": tc.approved_by,
        "approved_at": tc.approved_at.isoformat() if tc.approved_at else None,
        "created_at": tc.created_at.isoformat(),
        "updated_at": tc.updated_at.isoformat() if tc.updated_at else None,
        "metadata": tc.metadata,
    }


# ─── HTML Renderer ────────────────────────────────────────────────

def _render_html(
    test_cases: Sequence[ProductionTestCase],
    *,
    title: str,
    include_summary: bool,
) -> str:
    """Render test cases as a styled HTML document."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total_steps = sum(len(tc.steps) for tc in test_cases)

    parts: list[str] = []
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Calibri, Arial, sans-serif; background: #f5f7fa; color: #212121; padding: 24px; }}
  h1 {{ color: #1F4E79; margin-bottom: 8px; }}
  .meta {{ color: #607D8B; font-size: 0.9rem; margin-bottom: 24px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 32px; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.12); }}
  th {{ background: #1F4E79; color: #fff; padding: 10px 14px; text-align: left; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.5px; }}
  td {{ padding: 8px 14px; border-bottom: 1px solid #E0E0E0; font-size: 0.9rem; vertical-align: top; }}
  tr:nth-child(even) td {{ background: #E8F0FE; }}
  .data-ref {{ font-weight: bold; color: #0D47A1; }}
  .section-title {{ color: #1F4E79; font-size: 1.2rem; margin: 24px 0 12px; border-bottom: 2px solid #1F4E79; padding-bottom: 4px; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px; margin-bottom: 32px; }}
  .summary-card {{ background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.12); }}
  .summary-card .label {{ font-size: 0.8rem; color: #607D8B; text-transform: uppercase; }}
  .summary-card .value {{ font-size: 1.5rem; font-weight: bold; color: #1F4E79; }}
  .sensitive {{ color: #E65100; font-style: italic; background: #FFF3E0; padding: 2px 6px; border-radius: 3px; }}
</style>
</head>
<body>
<h1>{_esc(title)}</h1>
<p class="meta">Exported {now} &bull; {len(test_cases)} test cases &bull; {total_steps} steps</p>
""")

    if include_summary:
        parts.append("""<div class="summary-grid">""")
        parts.append(f"""<div class="summary-card"><div class="label">Test Cases</div><div class="value">{len(test_cases)}</div></div>""")
        parts.append(f"""<div class="summary-card"><div class="label">Total Steps</div><div class="value">{total_steps}</div></div>""")
        parts.append(f"""<div class="summary-card"><div class="label">Data Fields</div><div class="value">{sum(len(tc.data_workbook) for tc in test_cases)}</div></div>""")
        parts.append(f"""<div class="summary-card"><div class="label">Preconditions</div><div class="value">{sum(len(tc.preconditions) for tc in test_cases)}</div></div>""")
        parts.append("</div>")

    # Test Cases table
    parts.append('<h2 class="section-title">Test Cases</h2>')
    parts.append("<table><thead><tr><th>Test Case ID</th><th>Title</th><th>Step #</th><th>Action</th><th>Expected Result</th></tr></thead><tbody>")

    import re
    data_ref_re = re.compile(r"(\(Data\.\w+\))")

    for tc in test_cases:
        if not tc.steps:
            parts.append(f"<tr><td>{_esc(tc.test_case_id)}</td><td>{_esc(tc.title)}</td><td></td><td><em>No steps defined</em></td><td></td></tr>")
            continue

        for i, step in enumerate(tc.steps):
            tc_id_cell = f"<td rowspan=\"{len(tc.steps)}\">{_esc(tc.test_case_id)}</td>" if i == 0 else ""
            title_cell = f"<td rowspan=\"{len(tc.steps)}\">{_esc(tc.title)}</td>" if i == 0 else ""

            action_html = data_ref_re.sub(
                r'<span class="data-ref">\1</span>', _esc(step.action),
            )
            parts.append(
                f"<tr>{tc_id_cell}{title_cell}"
                f"<td style=\"text-align:center\">{step.step_number}</td>"
                f"<td>{action_html}</td>"
                f"<td>{_esc(step.expected_result)}</td></tr>"
            )

    parts.append("</tbody></table>")

    # Preconditions
    has_preconditions = any(tc.preconditions for tc in test_cases)
    if has_preconditions:
        parts.append('<h2 class="section-title">Preconditions</h2>')
        parts.append("<table><thead><tr><th>Test Case ID</th><th>#</th><th>Precondition</th></tr></thead><tbody>")
        for tc in test_cases:
            for i, pre in enumerate(tc.preconditions, 1):
                parts.append(f"<tr><td>{_esc(tc.test_case_id)}</td><td style=\"text-align:center\">{i}</td><td>{_esc(pre.description)}</td></tr>")
        parts.append("</tbody></table>")

    # Data Workbook
    has_data = any(tc.data_workbook for tc in test_cases)
    if has_data:
        parts.append('<h2 class="section-title">Data Workbook</h2>')
        parts.append("<table><thead><tr><th>Test Case ID</th><th>FieldName</th><th>FieldValue</th><th>Type</th><th>Sensitive</th></tr></thead><tbody>")
        for tc in test_cases:
            for entry in tc.data_workbook:
                val_html = (
                    f'<span class="sensitive">{_esc(entry.field_value[:2])}****</span>'
                    if entry.is_sensitive
                    else _esc(entry.field_value)
                )
                parts.append(
                    f"<tr><td>{_esc(tc.test_case_id)}</td>"
                    f"<td>{_esc(entry.field_name)}</td>"
                    f"<td>{val_html}</td>"
                    f"<td>{_esc(entry.field_type)}</td>"
                    f"<td>{'Yes' if entry.is_sensitive else 'No'}</td></tr>"
                )
        parts.append("</tbody></table>")

    parts.append("</body></html>")
    return "\n".join(parts)


def _esc(text: str) -> str:
    """HTML-escape a string."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
