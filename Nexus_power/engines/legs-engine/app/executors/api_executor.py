"""
APIExecutor — HTTP-based API test execution via httpx.

Parses test steps in the format ``METHOD /path {optional JSON body}``
and executes them against the target ``base_url``.
"""

from __future__ import annotations

import json
import logging
import time
from ._step_fields import expected_text

logger = logging.getLogger(__name__)


class APIExecutor:
    """Executes API test cases using httpx."""

    def __init__(self, config):
        """
        Parameters
        ----------
        config : LegsConfig
            Engine configuration — ``api_timeout_seconds`` used here.
        """
        self.config = config

    async def execute_api_test(
        self,
        test_case,
        base_url: str,
        variables: dict,
    ):
        """
        Execute an API test case.

        Each step is parsed as ``METHOD /path [body]`` and fired over HTTP.
        Returns a ``TestExecutionResult``.
        """
        import httpx
        from app.models import (
            StepExecutionDetail,
            TestExecutionResult,
            ExecutionStatus,
        )

        start = time.monotonic()
        step_results = []
        overall_status = ExecutionStatus.PASSED

        async with httpx.AsyncClient(
            timeout=self.config.api_timeout_seconds,
        ) as client:
            for step in test_case.steps:
                step_start = time.monotonic()
                try:
                    result = await self._execute_api_step(
                        client, step, base_url, variables,
                    )
                    step_results.append(
                        StepExecutionDetail(
                            step_number=step.step_number,
                            action=step.action,
                            expected=expected_text(step),
                            actual=json.dumps(result),
                            status=ExecutionStatus.PASSED,
                            duration_ms=(time.monotonic() - step_start) * 1000,
                        )
                    )
                except Exception as exc:
                    overall_status = ExecutionStatus.FAILED
                    step_results.append(
                        StepExecutionDetail(
                            step_number=step.step_number,
                            action=step.action,
                            expected=expected_text(step),
                            actual=str(exc),
                            status=ExecutionStatus.FAILED,
                            error_message=str(exc),
                            duration_ms=(time.monotonic() - step_start) * 1000,
                        )
                    )

        elapsed = (time.monotonic() - start) * 1000

        return TestExecutionResult(
            test_id=test_case.test_id,
            test_name=test_case.title,
            status=overall_status,
            total_steps=len(test_case.steps),
            steps_passed=sum(
                1
                for s in step_results
                if s.status == ExecutionStatus.PASSED
            ),
            steps_failed=sum(
                1
                for s in step_results
                if s.status == ExecutionStatus.FAILED
            ),
            duration_ms=elapsed,
            steps=step_results,
        )

    async def _execute_api_step(
        self, client, step, base_url: str, variables: dict,
    ) -> dict:
        """Parse and execute an API step.

        Step action format: ``METHOD /path {optional-json-body}``
        """
        action = step.action
        for var, val in variables.items():
            action = action.replace(f"${{{var}}}", str(val))

        parts = action.strip().split(maxsplit=2)
        method = parts[0].upper() if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"
        body = json.loads(parts[2]) if len(parts) > 2 else None

        url = f"{base_url.rstrip('/')}{path}"
        response = await client.request(method, url, json=body)

        content_type = response.headers.get("content-type", "")
        return {
            "status_code": response.status_code,
            "body": (
                response.json()
                if content_type.startswith("application/json")
                else response.text[:500]
            ),
        }
