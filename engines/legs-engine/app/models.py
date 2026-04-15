"""
Legs Engine — Shared execution models.

These models are imported by both main.py and the app/ sub-modules.
Having them in a shared location breaks the circular import chain:
  main.py ← app.executors → main.py (BAD)
  main.py ← app.models ← app.executors (GOOD)
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ExecutionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class StepExecutionDetail(BaseModel):
    """Detailed result of executing one test step."""
    step_number: int
    action: str
    expected: str
    actual: str = ""
    status: ExecutionStatus
    duration_ms: float = 0.0
    screenshot_path: Optional[str] = None
    error_message: Optional[str] = None
    element_found: bool = True
    self_healed: bool = False
    heal_details: Optional[str] = None


class TestExecutionResult(BaseModel):
    """Complete execution result for one test case."""
    test_id: str
    test_name: str
    status: ExecutionStatus
    total_steps: int
    steps_passed: int
    steps_failed: int
    duration_ms: float
    steps: list[StepExecutionDetail]
    evidence_path: Optional[str] = None
    explored_paths: list[dict] = Field(default_factory=list)


class ExplorationResult(BaseModel):
    """Result of autonomous exploration."""
    pages_discovered: list[dict]
    forms_found: list[dict]
    links_followed: list[dict]
    errors_found: list[dict]
    total_pages: int
    total_interactions: int
    exploration_tree: dict = Field(default_factory=dict)
