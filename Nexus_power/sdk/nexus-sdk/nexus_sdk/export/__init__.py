"""
Nexus Test Case Export Engine.

Production-grade multi-format export for test cases:
  - Excel (.xlsx) with styled multi-sheet workbook
  - CSV (.csv) with proper escaping
  - JSON (.json) with full nested structure
  - HTML (.html) report format

Usage:
    from nexus_sdk.export import ExportEngine, ExportFormat

    engine = ExportEngine()
    result = await engine.export_test_cases(
        test_cases=cases,
        format=ExportFormat.EXCEL,
        output_dir="/tmp/exports",
    )
"""

from nexus_sdk.export.engine import ExportEngine, ExportFormat, ExportResult

__all__ = ["ExportEngine", "ExportFormat", "ExportResult"]
