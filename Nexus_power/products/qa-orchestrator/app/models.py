"""
QA Orchestrator — Domain Models.

Pipeline stage enum, session model, request/response models.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PipelineStage(str, Enum):
    """Stages of the QA pipeline."""
    UPLOADED = "uploaded"
    INGESTING_DOCUMENTS = "ingesting_documents"
    SHIELDING = "shielding"
    TRANSCRIBING = "transcribing"
    VISUAL_ANALYZING = "visual_analyzing"
    EXTRACTING_RULES = "extracting_rules"
    GENERATING_TESTS = "generating_tests"
    GENERATING_TEST_DATA = "generating_test_data"
    STORING_KNOWLEDGE = "storing_knowledge"
    EXECUTING_TESTS = "executing_tests"
    GENERATING_REPORTS = "generating_reports"
    NOTIFYING = "notifying"
    COMPLETED = "completed"
    FAILED = "failed"


class KTSession(BaseModel):
    """Knowledge Transfer session with full pipeline tracking."""
    session_id: str
    tenant_id: str
    name: str = ""
    description: str = ""
    created_by: str = ""
    created_at: str = ""
    pipeline_stage: PipelineStage = PipelineStage.UPLOADED
    stages_completed: list[str] = Field(default_factory=list)

    # Job IDs for async engine calls
    audio_job_id: Optional[str] = None
    video_job_id: Optional[str] = None

    # Counters
    rules_extracted: int = 0
    tests_generated: int = 0
    tests_executed: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    documents_ingested: int = 0
    test_data_records: int = 0
    reports_generated: int = 0

    error: Optional[str] = None


class CreateSessionRequest(BaseModel):
    tenant_id: str
    name: str
    description: str = ""


class RunPipelineRequest(BaseModel):
    tenant_id: str
    sut_url: Optional[str] = None
    sut_credentials: Optional[dict] = None
    skip_test_execution: bool = False
    notify_on_complete: bool = True


class SessionSummary(BaseModel):
    session: KTSession
    rules: list = Field(default_factory=list)
    test_cases: list = Field(default_factory=list)
    test_results: list = Field(default_factory=list)
    timeline: list = Field(default_factory=list)
