"""Test Factory — additive subsystem that turns demonstrated Pages & Forms
evidence into production test cases.

Additive by construction: it READS the frozen Pages & Forms data
(``page_visits`` / ``page_actions``) and EMITS the existing canonical
``nexus_sdk.models.ProductionTestCase`` type.  It does not modify any frozen
storyboard / extractor code.

Phase 1 (this module): demonstrated-only functional E2E generation —
every emitted step traces to an action the user actually performed and a
value they actually entered.  No assumptions, no inferred option values.
"""

from .generator import (
    DemonstratedGenerationResult,
    PageActionInput,
    PageVisitInput,
    generate_demonstrated_test_cases,
)

__all__ = [
    "DemonstratedGenerationResult",
    "PageActionInput",
    "PageVisitInput",
    "generate_demonstrated_test_cases",
]
