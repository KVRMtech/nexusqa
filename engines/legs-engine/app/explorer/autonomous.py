"""
AutonomousExplorer — BFS web application discovery.

Starting from a URL, autonomously crawls the application by:
1. Capturing page metadata (title, URL, depth)
2. Discovering all ``<a>`` links within the same origin
3. Finding all ``<form>`` elements with their fields
4. Taking screenshots of every visited page
5. Recording errors encountered during traversal

Exploration is breadth-first with configurable ``max_depth`` and
``max_exploration_branches`` (from LegsConfig).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from nexus_sdk.events import fire_stub_alert

logger = logging.getLogger(__name__)


class AutonomousExplorer:
    """
    Autonomous web exploration using Playwright.

    Parameters
    ----------
    config : LegsConfig
        Engine configuration.
    browser
        A Playwright ``Browser`` instance (or ``None`` for stub mode).
    event_bus
        Optional NexusEventBus for stub alerts.
    """

    def __init__(self, config, browser=None, event_bus=None):
        self.config = config
        self.browser = browser
        self._event_bus = event_bus
        self._stub_fallback_count: int = 0

    # ── Public API ─────────────────────────────────────────────

    async def explore(
        self,
        start_url: str,
        credentials: Optional[dict],
        max_depth: int,
        focus_areas: list[str],
        evidence_dir: str,
    ):
        """
        Autonomously explore a web application via BFS.

        Returns an ``ExplorationResult`` (imported from ``app.models``).
        """
        from app.models import ExplorationResult

        if self.browser is None:
            return self._stub_explore()

        context = await self.browser.new_context(
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
        )
        page = await context.new_page()

        pages_discovered: list[dict] = []
        forms_found: list[dict] = []
        links_followed: list[dict] = []
        errors_found: list[dict] = []
        visited_urls: set[str] = set()

        try:
            await page.goto(
                start_url, timeout=self.config.default_timeout_ms
            )

            if credentials:
                await self._perform_login(page, credentials)

            # BFS
            queue: list[tuple[str, int]] = [(start_url, 0)]

            while (
                queue
                and len(pages_discovered)
                < self.config.max_exploration_branches
            ):
                current_url, depth = queue.pop(0)

                if depth > max_depth or current_url in visited_urls:
                    continue

                visited_urls.add(current_url)

                try:
                    if current_url != page.url:
                        await page.goto(
                            current_url,
                            timeout=self.config.default_timeout_ms,
                        )

                    title = await page.title()
                    pages_discovered.append(
                        {
                            "url": current_url,
                            "title": title,
                            "depth": depth,
                        }
                    )

                    # Screenshot
                    ss_path = os.path.join(
                        evidence_dir,
                        f"explore_{len(pages_discovered)}.png",
                    )
                    await page.screenshot(path=ss_path)

                    # Discover links
                    links = await page.eval_on_selector_all(
                        "a[href]",
                        "elements => elements.map(e => "
                        "({href: e.href, text: e.textContent.trim()}))",
                    )
                    for link in links:
                        href = link.get("href", "")
                        if (
                            href
                            and href.startswith(start_url)
                            and href not in visited_urls
                        ):
                            links_followed.append(
                                {
                                    "from": current_url,
                                    "to": href,
                                    "text": link.get("text", ""),
                                }
                            )
                            queue.append((href, depth + 1))

                    # Discover forms
                    forms = await page.eval_on_selector_all(
                        "form",
                        """forms => forms.map(f => ({
                            action: f.action,
                            method: f.method,
                            fields: Array.from(f.elements).map(e => ({
                                type: e.type, name: e.name, id: e.id
                            }))
                        }))""",
                    )
                    for form in forms:
                        forms_found.append(
                            {"page_url": current_url, "form": form}
                        )

                except Exception as exc:
                    errors_found.append(
                        {
                            "url": current_url,
                            "error": str(exc),
                            "depth": depth,
                        }
                    )
        finally:
            await context.close()

        return ExplorationResult(
            pages_discovered=pages_discovered,
            forms_found=forms_found,
            links_followed=links_followed,
            errors_found=errors_found,
            total_pages=len(pages_discovered),
            total_interactions=len(links_followed) + len(forms_found),
        )

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

    def _stub_explore(self):
        """Development stub when Playwright is not installed."""
        from app.models import ExplorationResult

        self._stub_fallback_count += 1
        logger.warning(
            "legs: explorer stub fallback #%d", self._stub_fallback_count
        )
        fire_stub_alert(
            self._event_bus,
            "legs",
            "explorer",
            fallback_count=self._stub_fallback_count,
            reason="Playwright not installed",
        )

        return ExplorationResult(
            pages_discovered=[
                {
                    "url": "https://example.com/login",
                    "title": "[Stub] Login",
                    "depth": 0,
                },
                {
                    "url": "https://example.com/dashboard",
                    "title": "[Stub] Dashboard",
                    "depth": 1,
                },
            ],
            forms_found=[
                {
                    "page_url": "https://example.com/login",
                    "form": {"fields": ["username", "password"]},
                }
            ],
            links_followed=[
                {"from": "/login", "to": "/dashboard", "text": "Login"}
            ],
            errors_found=[],
            total_pages=2,
            total_interactions=2,
        )
