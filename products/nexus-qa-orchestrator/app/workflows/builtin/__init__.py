"""
Built-in chain definitions.

Each module exports a builder function that returns a ChainDefinition.
The load_all_builtin_chains() function collects them all for
registration at startup.
"""

from .canonical_processing import build_canonical_processing_chain
from .qa_testing import build_qa_testing_chain
from .compliance_audit import build_compliance_audit_chain
from .knowledge_capture import build_knowledge_capture_chain
from .regression_suite import build_regression_suite_chain
from .rule_extraction import build_rule_extraction_chain
from .test_generation import build_test_generation_chain
from .knowledge_graph import build_knowledge_graph_chain
from .contradiction_detection import build_contradiction_detection_chain
from .report_generation import build_report_generation_chain

from ..schema import ChainDefinition


def load_all_builtin_chains() -> list[ChainDefinition]:
    """Build and return all built-in chain definitions."""
    return [
        build_canonical_processing_chain(),
        build_qa_testing_chain(),
        build_compliance_audit_chain(),
        build_knowledge_capture_chain(),
        build_regression_suite_chain(),
        # Phase 2 consumer chains (auto-triggered after canonical processing)
        build_rule_extraction_chain(),
        build_test_generation_chain(),
        build_knowledge_graph_chain(),
        build_contradiction_detection_chain(),
        build_report_generation_chain(),
    ]


__all__ = [
    "build_canonical_processing_chain",
    "build_qa_testing_chain",
    "build_compliance_audit_chain",
    "build_knowledge_capture_chain",
    "build_regression_suite_chain",
    "build_rule_extraction_chain",
    "build_test_generation_chain",
    "build_knowledge_graph_chain",
    "build_contradiction_detection_chain",
    "build_report_generation_chain",
    "load_all_builtin_chains",
]
