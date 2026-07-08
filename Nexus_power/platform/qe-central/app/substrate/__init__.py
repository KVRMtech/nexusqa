"""QE-Central substrate package — the §2 substrate-contract implementation.

Turns an :class:`ExplorationBundle` (from the explorer, or a deterministic
fixture in Phase-0) into the VKPower substrate rows the UNCHANGED factory
chain reads (``page_visits`` / ``page_actions`` / ``ground_truth_events`` /
``visual_frames``), plus the screenshot assets in the eyes ArtifactStore
namespace.

Modules:
  * ``schema``  — pydantic models + evidence-rule validators (refusals are
                  first-class: :class:`RefusalError` with an enumerated reason).
  * ``redact``  — PII redaction at source (``[REDACTED:class]`` placeholders,
                  counts returned, fail-open on detector errors).
  * ``writer``  — THE atomic substrate writer (one transaction, one
                  ``extractor_version`` string, UNIQUE-constraint-safe).
  * ``assets``  — screenshot PNG upload via the eyes ArtifactStore namespace,
                  emitting ``visual_frames``-compatible asset paths.
"""
from .schema import (
    ActionRecord,
    AfterBundle,
    AnchorBundle,
    ExplorationBundle,
    PageState,
    RefusalError,
    ScreenshotRef,
    validate_bundle,
)
from .writer import WriteStats, write_exploration

__all__ = [
    "ActionRecord",
    "AfterBundle",
    "AnchorBundle",
    "ExplorationBundle",
    "PageState",
    "RefusalError",
    "ScreenshotRef",
    "WriteStats",
    "validate_bundle",
    "write_exploration",
]
