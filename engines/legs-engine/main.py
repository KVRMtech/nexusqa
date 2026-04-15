"""
Nexus Legs Engine v0.2.0 — Test Execution & Autonomous Exploration.

The engine that actually DOES things. Takes test cases from Heart
and executes them against real systems:
1. Web UI testing via Playwright
2. API testing via httpx
3. Database validation via SQL  (stub — Phase 4)
4. Mainframe testing via Py3270 (stub — Phase 4)

Key capability: AUTONOMOUS EXPLORATION
Using BFS crawl, Legs can:
- Start at a URL and discover all pages/forms/links
- Auto-detect and fill login forms
- Self-heal when selectors break
- Report detailed execution results with screenshots

On-prem: All execution is local. Test systems accessed via internal network.

v0.2.0 — Modular refactor:
  app.executors → WebExecutor, APIExecutor
  app.explorer  → AutonomousExplorer
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from enum import Enum

from fastapi import Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.models import (
    NexusRequest, NexusResponse, JobResponse, JobStatus,
    TestCase, TestStep, TestResult, StepResult,
)
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.events import NexusEvent, fire_stub_alert

# ── Modular sub-packages ───────────────────────────────────────
from app.executors import WebExecutor, APIExecutor
from app.explorer import AutonomousExplorer

logger = logging.getLogger(__name__)


# ─── Configuration ─────────────────────────────────────────────

class LegsConfig(EngineConfig):
    engine_name: str = "legs"
    engine_port: int = 8007

    # Playwright
    browser_type: str = "chromium"
    headless: bool = True
    viewport_width: int = 1920
    viewport_height: int = 1080
    default_timeout_ms: int = 30000
    screenshot_on_failure: bool = True
    screenshot_on_step: bool = True

    # API testing
    api_timeout_seconds: int = 30

    # Mainframe (TN3270)
    mainframe_host: str = ""
    mainframe_port: int = 23
    mainframe_lu: str = ""

    # Execution
    max_concurrent_tests: int = 5
    evidence_storage_path: str = "./data/evidence"

    # Autonomous exploration
    max_exploration_depth: int = 10
    max_exploration_branches: int = 20


# ─── Execution enums ──────────────────────────────────────────

class TargetType(str, Enum):
    WEB_UI = "web_ui"
    API = "api"
    DATABASE = "database"
    MAINFRAME = "mainframe"


# Import shared models from app.models (breaks circular import chain)
from app.models import ExecutionStatus, StepExecutionDetail, TestExecutionResult, ExplorationResult  # noqa: E402


# ─── Request / Response Models ────────────────────────────────

class ExecuteTestRequest(NexusRequest):
    test_case: TestCase
    target_type: TargetType = TargetType.WEB_UI
    base_url: str = Field(..., description="Base URL of the system under test")
    credentials: Optional[dict] = Field(
        default=None, description="Login credentials for SUT"
    )
    variables: dict = Field(
        default_factory=dict, description="Test data variables"
    )


class ExecuteTestResponse(NexusResponse):
    job_id: str
    status: ExecutionStatus


class ExecuteBatchRequest(NexusRequest):
    test_cases: list[TestCase]
    target_type: TargetType = TargetType.WEB_UI
    base_url: str
    credentials: Optional[dict] = None
    variables: dict = Field(default_factory=dict)
    parallel: bool = False


class ExploreRequest(NexusRequest):
    """Autonomous exploration starting from a URL/screen."""
    start_url: str = Field(..., description="Starting URL or screen")
    target_type: TargetType = TargetType.WEB_UI
    credentials: Optional[dict] = None
    max_depth: int = Field(default=5, ge=1, le=20)
    focus_areas: list[str] = Field(
        default_factory=list,
        description="Areas to focus exploration on",
    )


class ExploreResponse(NexusResponse):
    job_id: str
    status: str


# Note: StepExecutionDetail, TestExecutionResult, ExplorationResult, ExecutionStatus
# are imported from app.models (see top of file) to break circular imports.


# ─── The Legs Engine v0.2.0 ──────────────────────────────────

class LegsEngine(NexusEngine):
    def __init__(self):
        self.cfg = LegsConfig()
        super().__init__(
            name="legs",
            version="0.2.0",
            config=self.cfg,
            description="Test Execution & Autonomous Exploration Engine",
        )
        self.web_executor = WebExecutor(self.cfg)
        self.api_executor = APIExecutor(self.cfg)
        self.explorer: Optional[AutonomousExplorer] = None

    async def on_startup(self):
        os.makedirs(self.cfg.evidence_storage_path, exist_ok=True)

        # Wire event bus and initialise browser
        self.web_executor._event_bus = self.event_bus
        await self.web_executor.initialize()

        # Create explorer sharing the same browser instance
        self.explorer = AutonomousExplorer(
            config=self.cfg,
            browser=self.web_executor.browser,
            event_bus=self.event_bus,
        )

        self.health.set_mode(
            "browser",
            "playwright" if self.web_executor.browser else "stub",
        )

        # Domain-plugin execution targets
        try:
            exec_ext = self.plugin_registry.get_merged_execution()
            if exec_ext and exec_ext.execution_targets:
                self._plugin_targets = {
                    t.target_id: t for t in exec_ext.execution_targets
                }
        except Exception:
            logger.debug(
                "Plugin execution targets not available, using defaults",
                exc_info=True,
            )
            self._plugin_targets = {}

    async def on_shutdown(self):
        await self.web_executor.shutdown()

    def register_routes(self, app):

        # ── Execute Single Test ────────────────────────────────

        @app.post(
            "/api/v1/legs/execute",
            response_model=ExecuteTestResponse,
        )
        async def execute_test(
            req: ExecuteTestRequest,
            background_tasks: BackgroundTasks,
            user: NexusUser = Depends(get_current_user),
        ):
            """Execute a single test case against a target system."""
            job_id = str(uuid.uuid4())

            await self.job_store.set_job(job_id, {
                "job_id": job_id,
                "status": ExecutionStatus.QUEUED.value,
                "tenant_id": req.tenant_id,
                "test_id": req.test_case.test_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "result": None,
                "error": None,
            })

            background_tasks.add_task(
                self._run_test,
                job_id,
                req.test_case,
                req.target_type,
                req.base_url,
                req.credentials,
                req.variables,
                req.tenant_id,
            )

            return ExecuteTestResponse(
                success=True,
                engine="legs",
                engine_version="0.2.0",
                job_id=job_id,
                status=ExecutionStatus.QUEUED,
            )

        # ── Execute Batch ──────────────────────────────────────

        @app.post("/api/v1/legs/execute/batch")
        async def execute_batch(
            req: ExecuteBatchRequest,
            background_tasks: BackgroundTasks,
            user: NexusUser = Depends(get_current_user),
        ):
            """Execute multiple test cases."""
            batch_id = str(uuid.uuid4())
            job_ids = []

            for tc in req.test_cases:
                job_id = str(uuid.uuid4())
                await self.job_store.set_job(job_id, {
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "status": ExecutionStatus.QUEUED.value,
                    "tenant_id": req.tenant_id,
                    "test_id": tc.test_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "result": None,
                })
                job_ids.append(job_id)

                background_tasks.add_task(
                    self._run_test,
                    job_id,
                    tc,
                    req.target_type,
                    req.base_url,
                    req.credentials,
                    req.variables,
                    req.tenant_id,
                )

            return {
                "batch_id": batch_id,
                "job_ids": job_ids,
                "total_tests": len(req.test_cases),
            }

        # ── Autonomous Exploration ─────────────────────────────

        @app.post(
            "/api/v1/legs/explore",
            response_model=ExploreResponse,
        )
        async def explore(
            req: ExploreRequest,
            background_tasks: BackgroundTasks,
            user: NexusUser = Depends(get_current_user),
        ):
            """Autonomously explore a web application."""
            job_id = str(uuid.uuid4())

            await self.job_store.set_job(job_id, {
                "job_id": job_id,
                "status": "exploring",
                "tenant_id": req.tenant_id,
                "start_url": req.start_url,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "result": None,
            })

            background_tasks.add_task(
                self._run_exploration,
                job_id,
                req.start_url,
                req.credentials,
                req.max_depth,
                req.focus_areas,
                req.tenant_id,
            )

            return ExploreResponse(
                success=True,
                engine="legs",
                engine_version="0.2.0",
                job_id=job_id,
                status="exploring",
            )

        # ── Get Job ────────────────────────────────────────────

        @app.get("/api/v1/legs/jobs/{job_id}")
        async def get_job(
            job_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            job = await self.job_store.get_job(job_id)
            if not job:
                raise HTTPException(
                    status_code=404, detail="Job not found"
                )
            return job

        # ── Get Batch Status ───────────────────────────────────

        @app.get("/api/v1/legs/batches/{batch_id}")
        async def get_batch(
            batch_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            all_jobs = await self.job_store.list_jobs(limit=1000)
            jobs = [
                j for j in all_jobs if j.get("batch_id") == batch_id
            ]
            if not jobs:
                raise HTTPException(
                    status_code=404, detail="Batch not found"
                )

            return {
                "batch_id": batch_id,
                "total": len(jobs),
                "passed": sum(
                    1
                    for j in jobs
                    if j.get("status") == ExecutionStatus.PASSED.value
                ),
                "failed": sum(
                    1
                    for j in jobs
                    if j.get("status") == ExecutionStatus.FAILED.value
                ),
                "running": sum(
                    1
                    for j in jobs
                    if j.get("status") == ExecutionStatus.RUNNING.value
                ),
                "queued": sum(
                    1
                    for j in jobs
                    if j.get("status") == ExecutionStatus.QUEUED.value
                ),
                "jobs": jobs,
            }

    # ── Background Workers ─────────────────────────────────────

    async def _run_test(
        self,
        job_id: str,
        test_case: TestCase,
        target_type: TargetType,
        base_url: str,
        credentials: Optional[dict],
        variables: dict,
        tenant_id: str,
    ):
        """Background: execute a single test."""
        await self.job_store.update_job(
            job_id, status=ExecutionStatus.RUNNING.value,
        )

        try:
            evidence_dir = os.path.join(
                self.cfg.evidence_storage_path, tenant_id, job_id,
            )
            os.makedirs(evidence_dir, exist_ok=True)

            if target_type == TargetType.WEB_UI:
                result = await self.web_executor.execute_test(
                    test_case, base_url, credentials, variables, evidence_dir,
                )
            elif target_type == TargetType.API:
                result = await self.api_executor.execute_api_test(
                    test_case, base_url, variables,
                )
            else:
                # Mainframe / DB — stubs for Phase 4
                _nexus_env = os.environ.get("NEXUS_ENV", "development").lower()
                if _nexus_env == "production":
                    logger.error(
                        "legs: Mainframe/DB execution requested in production but "
                        "these are Phase 4 stubs. Failing instead of silently skipping."
                    )
                    result = TestExecutionResult(
                        test_id=test_case.test_id,
                        test_name=test_case.title,
                        status=ExecutionStatus.ERROR,
                        total_steps=len(test_case.steps),
                        steps_passed=0,
                        steps_failed=0,
                        duration_ms=0.0,
                        steps=[],
                    )
                else:
                    result = TestExecutionResult(
                        test_id=test_case.test_id,
                        test_name=test_case.title,
                        status=ExecutionStatus.SKIPPED,
                        total_steps=len(test_case.steps),
                        steps_passed=0,
                        steps_failed=0,
                        duration_ms=0.0,
                        steps=[],
                    )

            await self.job_store.update_job(
                job_id,
                status=result.status.value,
                result=result.model_dump(),
            )

            if self.event_bus:
                await self.event_bus.publish(
                    NexusEvent(
                        event_type="legs.test.completed",
                        tenant_id=tenant_id,
                        trace_id=job_id,
                        engine="legs",
                        data={
                            "job_id": job_id,
                            "test_id": test_case.test_id,
                            "status": result.status.value,
                            "steps_passed": result.steps_passed,
                            "steps_failed": result.steps_failed,
                            "duration_ms": result.duration_ms,
                        },
                    )
                )

        except Exception as exc:
            logger.exception("Test execution failed for job %s", job_id)
            await self.job_store.update_job(
                job_id,
                status=ExecutionStatus.ERROR.value,
                error=str(exc),
            )

    async def _run_exploration(
        self,
        job_id: str,
        start_url: str,
        credentials: Optional[dict],
        max_depth: int,
        focus_areas: list[str],
        tenant_id: str,
    ):
        """Background: autonomous exploration."""
        try:
            evidence_dir = os.path.join(
                self.cfg.evidence_storage_path,
                tenant_id,
                f"explore_{job_id}",
            )
            os.makedirs(evidence_dir, exist_ok=True)

            result = await self.explorer.explore(
                start_url, credentials, max_depth, focus_areas, evidence_dir,
            )

            await self.job_store.update_job(
                job_id,
                status="completed",
                result=result.model_dump(),
            )

            if self.event_bus:
                await self.event_bus.publish(
                    NexusEvent(
                        event_type="legs.exploration.completed",
                        tenant_id=tenant_id,
                        trace_id=job_id,
                        engine="legs",
                        data={
                            "job_id": job_id,
                            "pages_discovered": result.total_pages,
                            "forms_found": len(result.forms_found),
                            "errors_found": len(result.errors_found),
                        },
                    )
                )

        except Exception as exc:
            logger.exception("Exploration failed for job %s", job_id)
            await self.job_store.update_job(
                job_id,
                status="error",
                error=str(exc),
            )


# ─── Entry Point ──────────────────────────────────────────────

def main():
    engine = LegsEngine()
    engine.run()


if __name__ == "__main__":
    main()
