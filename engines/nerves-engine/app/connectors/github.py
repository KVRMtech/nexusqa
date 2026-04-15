"""
Nerves Engine — GitHub Connector.

GitHub integration via REST API v3.
Supports creating branches, committing files, creating PRs, triggering
workflows, and creating issues.
"""

from __future__ import annotations

import logging
import uuid
import time
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


class GitHubConnector(BaseConnector):
    """
    GitHub integration via REST API v3.

    Credentials: token, owner, repo
    """

    def __init__(self):
        super().__init__("github")
        self._client: Optional[Any] = None
        self._owner: str = ""
        self._repo: str = ""

    async def connect(self, credentials: dict) -> bool:
        if "token" not in credentials:
            self.status = ConnectorStatus.ERROR
            return False
        self.config = credentials
        self._owner = credentials.get("owner", "")
        self._repo = credentials.get("repo", "")
        if httpx is not None:
            self._client = httpx.AsyncClient(
                base_url="https://api.github.com",
                headers={
                    "Authorization": f"Bearer {credentials['token']}",
                    "Accept": "application/vnd.github.v3+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )
            try:
                resp = await self._client.get("/user")
                if resp.status_code == 200:
                    self.status = ConnectorStatus.CONNECTED
                    user = resp.json().get("login", "unknown")
                    logger.info("nerves: GitHub connected as %s", user)
                    return True
                logger.warning("nerves: GitHub auth failed (%s)", resp.status_code)
                self.status = ConnectorStatus.ERROR
                return False
            except Exception as exc:
                logger.warning("nerves: GitHub connection failed: %s", exc)
                self.status = ConnectorStatus.ERROR
                return False
        self.status = ConnectorStatus.CONNECTED
        logger.warning("nerves: GitHub running in stub mode")
        fire_stub_alert(_event_bus, "nerves", "github", reason="httpx not installed")
        return True

    async def execute(self, action: str, params: dict) -> dict:
        handler = {
            "create_branch": self._create_branch,
            "commit_file": self._commit_file,
            "create_pr": self._create_pr,
            "trigger_workflow": self._trigger_workflow,
            "create_issue": self._create_issue,
        }.get(action)
        if not handler:
            raise ValueError(f"Unknown GitHub action: {action}")
        return await handler(params)

    def get_available_actions(self) -> list[dict]:
        return [
            {"action": "create_branch", "params": ["branch_name", "from_branch"]},
            {"action": "commit_file", "params": ["branch", "path", "content", "message"]},
            {"action": "create_pr", "params": ["title", "head", "base", "body"]},
            {"action": "trigger_workflow", "params": ["workflow_id", "ref", "inputs"]},
            {"action": "create_issue", "params": ["title", "body", "labels"]},
        ]

    async def _create_branch(self, params: dict) -> dict:
        branch_name = params.get("branch_name", f"nexus/autogen-{uuid.uuid4().hex[:8]}")
        from_branch = params.get("from_branch", "main")
        if self._client:
            # Get SHA of source branch
            ref_resp = await self._client.get(
                f"/repos/{self._owner}/{self._repo}/git/ref/heads/{from_branch}"
            )
            if ref_resp.status_code != 200:
                return {"created": False, "error": f"Source branch '{from_branch}' not found"}
            sha = ref_resp.json()["object"]["sha"]
            # Create new branch
            create_resp = await self._client.post(
                f"/repos/{self._owner}/{self._repo}/git/refs",
                json={"ref": f"refs/heads/{branch_name}", "sha": sha},
            )
            if create_resp.status_code in (200, 201):
                return {"created": True, "branch": branch_name, "sha": sha}
            return {"created": False, "error": create_resp.text[:200]}
        return {"created": True, "branch": branch_name, "sha": "stub-sha"}

    async def _commit_file(self, params: dict) -> dict:
        if self._client:
            import base64
            content_b64 = base64.b64encode(params.get("content", "").encode()).decode()
            resp = await self._client.put(
                f"/repos/{self._owner}/{self._repo}/contents/{params.get('path', 'test.txt')}",
                json={
                    "message": params.get("message", "Auto-generated by Nexus QA"),
                    "content": content_b64,
                    "branch": params.get("branch", "main"),
                },
            )
            if resp.status_code in (200, 201):
                return {"committed": True, "sha": resp.json().get("commit", {}).get("sha", "")}
            return {"committed": False, "error": resp.text[:200]}
        return {"committed": True, "sha": f"stub-{uuid.uuid4().hex[:8]}"}

    async def _create_pr(self, params: dict) -> dict:
        if self._client:
            resp = await self._client.post(
                f"/repos/{self._owner}/{self._repo}/pulls",
                json={
                    "title": params.get("title", "Nexus QA Auto-Generated Tests"),
                    "head": params.get("head", "nexus/autogen"),
                    "base": params.get("base", "main"),
                    "body": params.get("body", ""),
                },
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return {"created": True, "pr_number": data["number"], "url": data["html_url"]}
            return {"created": False, "error": resp.text[:200]}
        return {"created": True, "pr_number": 1, "url": "https://github.com/example/pr/1"}

    async def _trigger_workflow(self, params: dict) -> dict:
        if self._client:
            resp = await self._client.post(
                f"/repos/{self._owner}/{self._repo}/actions/workflows/"
                f"{params.get('workflow_id', 'ci.yml')}/dispatches",
                json={
                    "ref": params.get("ref", "main"),
                    "inputs": params.get("inputs", {}),
                },
            )
            return {"triggered": resp.status_code == 204, "workflow": params.get("workflow_id")}
        return {"triggered": True, "workflow": params.get("workflow_id", "ci.yml")}

    async def _create_issue(self, params: dict) -> dict:
        if self._client:
            resp = await self._client.post(
                f"/repos/{self._owner}/{self._repo}/issues",
                json={
                    "title": params.get("title", ""),
                    "body": params.get("body", ""),
                    "labels": params.get("labels", []),
                },
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return {"created": True, "issue_number": data["number"], "url": data["html_url"]}
            return {"created": False, "error": resp.text[:200]}
        return {"created": True, "issue_number": 1, "url": "https://github.com/example/issues/1"}

    async def disconnect(self):
        if self._client:
            await self._client.aclose()
            self._client = None
        await super().disconnect()
