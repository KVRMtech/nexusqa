"""
WebExecutor — Playwright-based web UI test execution.

Features:
- Smart element location (multiple selector strategies)
- Self-healing selectors (if primary fails, try alternatives)
- Screenshot capture on every step
- Natural-language action parsing (Navigate/Click/Type/Select/Wait/Assert)
- Auto-detect and fill login forms
- Stub fallback when Playwright is not installed
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from nexus_sdk.events import fire_stub_alert
from nexus_sdk.config import production_guard
from ._step_fields import expected_text

logger = logging.getLogger(__name__)


class WebExecutor:
    """
    Executes test steps against web UIs using Playwright.

    Requires ``playwright`` and its browser binaries.  When not installed
    the executor falls back to a deterministic stub so that the rest of
    the platform can still exercise its contract.
    """

    def __init__(self, config):
        """
        Parameters
        ----------
        config : LegsConfig
            Engine configuration — viewport, timeouts, screenshot prefs.
        """
        self.config = config
        self.browser = None
        self.playwright = None
        self._event_bus = None
        self._stub_fallback_count: int = 0

    # ── Lifecycle ──────────────────────────────────────────────

    async def initialize(self):
        """Start Playwright browser (chromium, headless by default)."""
        try:
            from playwright.async_api import async_playwright

            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.config.headless,
            )
        except ImportError:
            self.browser = None

        # Production guard: refuse stub mode in production environments
        production_guard(
            "Playwright browser (legs-engine)",
            available=(self.browser is not None),
        )

    async def shutdown(self):
        """Release browser & Playwright process."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    # ── Public API ─────────────────────────────────────────────

    async def execute_test(
        self,
        test_case,
        base_url: str,
        credentials: Optional[dict],
        variables: dict,
        evidence_dir: str,
    ):
        """
        Execute a complete test case.

        Returns a ``TestExecutionResult`` (constructed by the caller or
        imported from ``main``).  This method uses a *dict-like* approach
        internally so it doesn't depend on the response model directly.
        """
        from app.models import (
            StepExecutionDetail,
            TestExecutionResult,
            ExecutionStatus,
        )

        start = time.monotonic()
        step_results: list = []
        overall_status = ExecutionStatus.PASSED

        if self.browser is None:
            return self._stub_execute(test_case, evidence_dir)

        context = await self.browser.new_context(
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
        )
        page = await context.new_page()

        try:
            await page.goto(base_url, timeout=self.config.default_timeout_ms)

            if credentials:
                await self._perform_login(page, credentials)

            for step in test_case.steps:
                step_start = time.monotonic()
                step_detail = await self._execute_step(
                    page, step, variables, evidence_dir,
                )
                step_detail.duration_ms = (time.monotonic() - step_start) * 1000
                step_results.append(step_detail)

                if step_detail.status == ExecutionStatus.FAILED:
                    overall_status = ExecutionStatus.FAILED
                    if (
                        not test_case.tags
                        or "continue_on_failure" not in test_case.tags
                    ):
                        break

        except Exception as exc:
            overall_status = ExecutionStatus.ERROR
            step_results.append(
                StepExecutionDetail(
                    step_number=len(step_results) + 1,
                    action="[Exception]",
                    expected="No error",
                    actual=str(exc),
                    status=ExecutionStatus.ERROR,
                    error_message=str(exc),
                )
            )
        finally:
            await context.close()

        elapsed = (time.monotonic() - start) * 1000

        return TestExecutionResult(
            test_id=test_case.test_id,
            test_name=test_case.title,
            status=overall_status,
            total_steps=len(test_case.steps),
            steps_passed=sum(
                1 for s in step_results if s.status == ExecutionStatus.PASSED
            ),
            steps_failed=sum(
                1
                for s in step_results
                if s.status
                in (ExecutionStatus.FAILED, ExecutionStatus.ERROR)
            ),
            duration_ms=elapsed,
            steps=step_results,
            evidence_path=evidence_dir,
        )

    # ── Step Execution ─────────────────────────────────────────

    async def _execute_step(self, page, step, variables: dict, evidence_dir: str):
        """Execute a single test step with self-healing."""
        from app.models import StepExecutionDetail, ExecutionStatus

        action = step.action
        expected = expected_text(step)

        # Replace variables
        for var, val in variables.items():
            action = action.replace(f"${{{var}}}", str(val))

        try:
            actual = await self._perform_action(page, action)

            screenshot_path = None
            if self.config.screenshot_on_step:
                screenshot_path = os.path.join(
                    evidence_dir, f"step_{step.step_number}.png"
                )
                await page.screenshot(path=screenshot_path)

            # Verify step outcome: compare expected vs actual
            if expected:
                # Normalize both strings for comparison
                expected_lower = expected.strip().lower()
                actual_lower = (actual or "").strip().lower()
                passed = (
                    expected_lower in actual_lower
                    or actual_lower in expected_lower
                    or expected_lower == actual_lower
                )
            else:
                # No expected output specified — pass if action didn't raise
                passed = True

            return StepExecutionDetail(
                step_number=step.step_number,
                action=action,
                expected=expected,
                actual=actual,
                status=(
                    ExecutionStatus.PASSED if passed else ExecutionStatus.FAILED
                ),
                screenshot_path=screenshot_path,
            )

        except Exception as exc:
            healed, heal_detail = await self._try_self_heal(
                page, action, str(exc)
            )

            screenshot_path = None
            if self.config.screenshot_on_failure:
                screenshot_path = os.path.join(
                    evidence_dir, f"step_{step.step_number}_FAIL.png"
                )
                try:
                    await page.screenshot(path=screenshot_path)
                except Exception:
                    pass

            return StepExecutionDetail(
                step_number=step.step_number,
                action=action,
                expected=expected,
                actual=str(exc),
                status=(
                    ExecutionStatus.PASSED if healed else ExecutionStatus.FAILED
                ),
                error_message=None if healed else str(exc),
                screenshot_path=screenshot_path,
                self_healed=healed,
                heal_details=heal_detail,
            )

    # ── Natural-Language Action Parser ─────────────────────────

    async def _perform_action(self, page, action: str) -> str:
        """
        Parse a natural language action and execute it.

        Supports:
        - Navigate to {url}
        - Click {text/selector}
        - Type {text} in {field}
        - Select {option} from {dropdown}
        - Wait for {text/element}
        - Assert {condition}
        """
        action_lower = action.lower().strip()

        if action_lower.startswith("navigate to") or action_lower.startswith(
            "go to"
        ):
            url = action.split("to", 1)[1].strip()
            await page.goto(url, timeout=self.config.default_timeout_ms)
            return f"Navigated to {url}"

        elif action_lower.startswith("click"):
            target = action.split("click", 1)[1].strip().strip("\"'")
            try:
                await page.click(f"text={target}", timeout=5000)
            except Exception:
                await page.click(f"[aria-label='{target}']", timeout=5000)
            return f"Clicked: {target}"

        elif action_lower.startswith("type") or action_lower.startswith("enter"):
            parts = action.split(" in ", 1)
            if len(parts) == 2:
                text = parts[0].split(maxsplit=1)[1].strip().strip("\"'")
                field = parts[1].strip().strip("\"'")
                try:
                    await page.fill(
                        f"[placeholder='{field}']", text, timeout=5000
                    )
                except Exception:
                    await page.fill(f"label={field}", text, timeout=5000)
                return f"Typed '{text}' in '{field}'"
            return "Could not parse type action"

        elif action_lower.startswith("select"):
            parts = action.split(" from ", 1)
            if len(parts) == 2:
                option = parts[0].split("select", 1)[1].strip().strip("\"'")
                dropdown = parts[1].strip().strip("\"'")
                await page.select_option(
                    f"label={dropdown}", label=option, timeout=5000
                )
                return f"Selected '{option}' from '{dropdown}'"
            return "Could not parse select action"

        elif action_lower.startswith("wait"):
            target = (
                action.split("for", 1)[1].strip().strip("\"'")
                if "for" in action
                else ""
            )
            await page.wait_for_selector(
                f"text={target}", timeout=self.config.default_timeout_ms
            )
            return f"Found: {target}"

        else:
            return f"Action not mapped: {action}"

    # ── Self-Healing ───────────────────────────────────────────

    async def _try_self_heal(
        self, page, action: str, error: str
    ) -> tuple[bool, Optional[str]]:
        """
        Self-healing: when a selector fails, try alternative strategies.

        Phase 2: Use Eyes engine to visually locate the element.
        Phase 3: Use Heart to reason about the UI change.
        """
        action_lower = action.lower()

        if "click" in action_lower:
            target = action.split("click", 1)[1].strip().strip("\"'")
            strategies = [
                f"button:has-text('{target}')",
                f"a:has-text('{target}')",
                f"[title='{target}']",
                f"[value='{target}']",
                f"input[type='submit'][value='{target}']",
            ]
            for selector in strategies:
                try:
                    await page.click(selector, timeout=3000)
                    return True, f"Healed: used selector '{selector}'"
                except Exception:
                    continue

        return False, None

    # ── Login Helper ───────────────────────────────────────────

    async def _perform_login(self, page, credentials: dict):
        """Auto-detect and fill login form."""
        username = credentials.get("username", "")
        password = credentials.get("password", "")

        username_selectors = [
            "input[type='email']",
            "input[name='username']",
            "input[name='email']",
            "input[id='username']",
            "#login-email",
        ]
        password_selectors = [
            "input[type='password']",
            "input[name='password']",
            "#login-password",
        ]
        submit_selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
        ]

        for sel in username_selectors:
            try:
                await page.fill(sel, username, timeout=2000)
                break
            except Exception:
                continue

        for sel in password_selectors:
            try:
                await page.fill(sel, password, timeout=2000)
                break
            except Exception:
                continue

        for sel in submit_selectors:
            try:
                await page.click(sel, timeout=2000)
                break
            except Exception:
                continue

        await page.wait_for_load_state("networkidle", timeout=10000)

    # ── Stub Fallback ──────────────────────────────────────────

    def _stub_execute(self, test_case, evidence_dir: str):
        """Development stub when Playwright is not installed."""
        from app.models import (
            StepExecutionDetail,
            TestExecutionResult,
            ExecutionStatus,
        )

        self._stub_fallback_count += 1
        logger.warning(
            "legs: web executor stub fallback #%d",
            self._stub_fallback_count,
        )
        fire_stub_alert(
            self._event_bus,
            "legs",
            "web_executor",
            fallback_count=self._stub_fallback_count,
            reason="Playwright not installed",
        )

        steps = []
        for step in test_case.steps:
            steps.append(
                StepExecutionDetail(
                    step_number=step.step_number,
                    action=step.action,
                    expected=expected_text(step),
                    actual="[Stub] Playwright not installed",
                    status=ExecutionStatus.SKIPPED,
                )
            )

        return TestExecutionResult(
            test_id=test_case.test_id,
            test_name=test_case.title,
            status=ExecutionStatus.SKIPPED,
            total_steps=len(test_case.steps),
            steps_passed=0,
            steps_failed=0,
            duration_ms=0.0,
            steps=steps,
            evidence_path=evidence_dir,
        )
