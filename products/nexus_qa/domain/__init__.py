"""
Nexus QA Domain — Insurance & QA domain extensions.

This package contains all insurance-specific and QA-specific knowledge
extracted from the generic engines, organized by extension type.
"""

from .vocabulary import build_vocabulary_extension
from .pii_patterns import build_pii_extension
from .graph_schema import build_graph_schema_extension
from .reasoning import build_reasoning_extension
from .document_types import build_document_type_extension
from .data_generators import build_data_generator_extension
from .reports import build_report_extension
from .execution import build_execution_extension

__all__ = [
    "build_vocabulary_extension",
    "build_pii_extension",
    "build_graph_schema_extension",
    "build_reasoning_extension",
    "build_document_type_extension",
    "build_data_generator_extension",
    "build_report_extension",
    "build_execution_extension",
]
