"""
Mouth Engine — Report generators.

Re-exports
~~~~~~~~~~
- :class:`TraceabilityMatrixGenerator` — Rule → Test → Result → Evidence matrix
- :class:`ComplianceReportGenerator` — Regulatory domain compliance assessment
- :class:`ExecutiveSummaryGenerator` — C-suite one-page overview
- :class:`TestCoverageReportGenerator` — Detailed test coverage analysis
- :class:`DefectSummaryGenerator` — Failure analysis and root-cause suggestions

Supporting models re-exported for convenience:
- :class:`TraceabilityEntry`
- :class:`ComplianceItem`
- :class:`CoverageLevel`
"""

from .traceability import TraceabilityMatrixGenerator, TraceabilityEntry, CoverageLevel
from .compliance import ComplianceReportGenerator, ComplianceItem
from .executive import ExecutiveSummaryGenerator
from .coverage import TestCoverageReportGenerator
from .defect import DefectSummaryGenerator

__all__ = [
    "TraceabilityMatrixGenerator",
    "TraceabilityEntry",
    "CoverageLevel",
    "ComplianceReportGenerator",
    "ComplianceItem",
    "ExecutiveSummaryGenerator",
    "TestCoverageReportGenerator",
    "DefectSummaryGenerator",
]
