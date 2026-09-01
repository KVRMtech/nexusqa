"""
Nerves Engine — Jira Connector.

Jira integration via REST API v3.
Supports creating/updating issues, adding comments, searching, and creating test plans.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Any

from nexus_sdk.events import fire_stub_alert

from .base import BaseConnector, ConnectorStatus

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Module-level event bus reference set by the engine at startup
_event_bus = None


def set_event_bus(bus):
    """Called by engine to inject the event bus reference."""
    global _event_bus
    _event_bus = bus


class JiraConnector(BaseConnector):
    """
    Jira integration via REST API v3.

    Credentials: url, email, api_token
    """

    def __init__(self):
        super().__init__("jira")
        self._client: Optional[Any] = None
        self._base_url: str = ""
        self._issues: dict[str, dict] = {}  # stub fallback

    async def connect(self, credentials: dict) -> bool:
        required = ["url", "email", "api_token"]
        if not all(k in credentials for k in required):
            self.status = ConnectorStatus.ERROR
            return False
        self.config = credentials
        self._base_url = credentials["url"].rstrip("/")
        if httpx is not None:
            import base64
            token = base64.b64encode(
                f"{credentials['email']}:{credentials['api_token']}".encode()
            ).decode()
            self._client = httpx.AsyncClient(
                base_url=f"{self._base_url}/rest/api/3",
                headers={
                    "Authorization": f"Basic {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=30.0,
            )
            # Verify connectivity
            try:
                resp = await self._client.get("/myself")
                if resp.status_code == 200:
                    self.status = ConnectorStatus.CONNECTED
                    logger.info("nerves: Jira connected to %s", self._base_url)
                    return True
                else:
                    logger.warning("nerves: Jira auth failed (%s)", resp.status_code)
                    self.status = ConnectorStatus.ERROR
                    return False
            except Exception as exc:
                logger.warning("nerves: Jira connection failed: %s", exc)
                self.status = ConnectorStatus.ERROR
                return False
        # No httpx — stub mode
        self.status = ConnectorStatus.CONNECTED
        logger.warning("nerves: Jira running in stub mode (httpx not installed)")
        fire_stub_alert(_event_bus, "nerves", "jira", reason="httpx not installed")
        return True

    async def execute(self, action: str, params: dict) -> dict:
        handler = {
            "create_issue": self._create_issue,
            "update_issue": self._update_issue,
            "add_comment": self._add_comment,
            "search_issues": self._search_issues,
            "create_test_plan": self._create_test_plan,
        }.get(action)
        if not handler:
            raise ValueError(f"Unknown Jira action: {action}")
        return await handler(params)

    def get_available_actions(self) -> list[dict]:
        return [
            {"action": "create_issue", "params": ["project", "summary", "description", "issue_type", "priority"]},
            {"action": "update_issue", "params": ["issue_key", "fields"]},
            {"action": "add_comment", "params": ["issue_key", "comment"]},
            {"action": "search_issues", "params": ["jql", "max_results"]},
            {"action": "create_test_plan", "params": ["project", "name", "test_case_keys"]},
        ]

    async def _create_issue(self, params: dict) -> dict:
        if self._client:
            payload = {
                "fields": {
                    "project": {"key": params.get("project", "NQA")},
                    "summary": params.get("summary", ""),
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [{"type": "paragraph", "content": [
                            {"type": "text", "text": params.get("description", "")}
                        ]}],
                    },
                    "issuetype": {"name": params.get("issue_type", "Task")},
                }
            }
            if params.get("priority"):
                payload["fields"]["priority"] = {"name": params["priority"]}
            resp = await self._client.post("/issue", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return {
                "issue_key": data["key"],
                "url": f"{self._base_url}/browse/{data['key']}",
            }
        # Stub fallback
        issue_key = f"{params.get('project', 'NQA')}-{len(self._issues) + 1}"
        self._issues[issue_key] = {
            "key": issue_key,
            "summary": params.get("summary", ""),
            "status": "Open",
            "created": datetime.now(timezone.utc).isoformat(),
        }
        return {"issue_key": issue_key, "url": f"https://jira.example.com/browse/{issue_key}"}

    async def _update_issue(self, params: dict) -> dict:
        key = params.get("issue_key", "")
        if self._client:
            resp = await self._client.put(f"/issue/{key}", json={"fields": params.get("fields", {})})
            resp.raise_for_status()
            return {"updated": True, "issue_key": key}
        if key in self._issues:
            self._issues[key].update(params.get("fields", {}))
            return {"updated": True, "issue_key": key}
        return {"updated": False, "error": "Issue not found"}

    async def _add_comment(self, params: dict) -> dict:
        key = params.get("issue_key", "")
        if self._client:
            body = {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [{"type": "paragraph", "content": [
                        {"type": "text", "text": params.get("comment", "")}
                    ]}],
                }
            }
            resp = await self._client.post(f"/issue/{key}/comment", json=body)
            resp.raise_for_status()
            return {"commented": True, "issue_key": key}
        return {"commented": True, "issue_key": key}

    async def _search_issues(self, params: dict) -> dict:
        if self._client:
            resp = await self._client.get("/search", params={
                "jql": params.get("jql", ""),
                "maxResults": params.get("max_results", 10),
            })
            resp.raise_for_status()
            data = resp.json()
            return {"issues": data.get("issues", []), "total": data.get("total", 0)}
        return {"issues": list(self._issues.values())[:params.get("max_results", 10)]}

    async def _create_test_plan(self, params: dict) -> dict:
        # Test plans are essentially created as issues of type "Test Plan"
        return await self._create_issue({
            "project": params.get("project", "NQA"),
            "summary": params.get("name", "Test Plan"),
            "description": f"Test plan containing: {params.get('test_case_keys', [])}",
            "issue_type": "Test Plan",
        })

    async def disconnect(self):
        if self._client:
            await self._client.aclose()
            self._client = None
        await super().disconnect()
