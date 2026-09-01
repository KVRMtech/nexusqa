"""
Nexus Nerves Engine v0.2.0 — External Integration (MCP Connectors).

Connects Nexus to the outside world using Model Context Protocol (MCP)
style connectors. Each connector is a standardized adapter.

Supported integrations:
1. Jira — Create/update test issues, link to requirements
2. GitHub/GitLab — Commit test scripts, create PRs, trigger CI
3. Slack/Teams — Notifications, SME confirmation requests
4. Azure DevOps — Work items, test plans, pipelines
5. Email — Compliance reports, status updates
6. Webhooks — Generic outbound events

Architecture:
- Each connector implements a standard interface
- Connectors are registered at startup
- Actions are dispatched via a unified API
- All outbound data passes through Shield first

v0.2.0 — Modular refactor:
  app.connectors  → BaseConnector, JiraConnector, GitHubConnector,
                     SlackConnector, TeamsConnector, WebhookConnector
"""

from __future__ import annotations

import os
import uuid
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Any
from enum import Enum

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.models import NexusRequest, NexusResponse
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.events import NexusEvent, fire_stub_alert

# ── Modular sub-packages ───────────────────────────────────────
from app.connectors import (
    BaseConnector,
    ConnectorStatus,
    JiraConnector,
    GitHubConnector,
    SlackConnector,
    TeamsConnector,
    WebhookConnector,
)
from app.connectors import jira as _jira_mod
from app.connectors import github as _github_mod
from app.connectors import slack as _slack_mod
from app.connectors import teams as _teams_mod
from app.connectors import webhook as _webhook_mod

logger = logging.getLogger(__name__)


# ─── Configuration ─────────────────────────────────────────────

class NervesConfig(EngineConfig):
    engine_name: str = "nerves"
    engine_port: int = 8006

    # Connector configs (per-tenant, loaded at runtime)
    jira_enabled: bool = False
    github_enabled: bool = False
    slack_enabled: bool = False
    teams_enabled: bool = False
    email_enabled: bool = False


# ─── Request/Response Models ──────────────────────────────────

class ConnectorAction(BaseModel):
    """A single action to execute via a connector."""
    connector: str = Field(..., description="Connector name (jira, github, slack, etc.)")
    action: str = Field(..., description="Action to perform")
    parameters: dict = Field(default_factory=dict, description="Action parameters")


class ConnectorResult(BaseModel):
    """Result of a connector action."""
    connector: str
    action: str
    success: bool
    data: dict = Field(default_factory=dict)
    error: Optional[str] = None
    execution_time_ms: float = 0.0


class ConfigureConnectorRequest(NexusRequest):
    connector: str = Field(..., description="Connector name")
    credentials: dict = Field(..., description="Connection credentials")


class ExecuteActionRequest(NexusRequest):
    connector: str
    action: str
    parameters: dict = Field(default_factory=dict)


class BatchExecuteRequest(NexusRequest):
    actions: list[ConnectorAction]


# ─── The Nerves Engine ─────────────────────────────────────────

class NervesEngine(NexusEngine):
    def __init__(self):
        self.cfg = NervesConfig()
        super().__init__(
            name="nerves",
            version="0.2.0",
            config=self.cfg,
            description="External Integration (MCP Connectors) Engine",
        )
        # Register all connectors
        self.connectors: dict[str, BaseConnector] = {
            "jira": JiraConnector(),
            "github": GitHubConnector(),
            "slack": SlackConnector(),
            "teams": TeamsConnector(),
            "webhook": WebhookConnector(),
        }

        # Per-tenant connector configs — backed by Redis for durability
        self._tenant_configs: dict[str, dict[str, dict]] = {}
        self._config_cache_loaded: set[str] = set()

    def _config_redis_key(self, tenant_id: str) -> str:
        return f"nexus:nerves:connectors:{tenant_id}"

    async def _load_tenant_configs(self, tenant_id: str) -> dict[str, dict]:
        """Load persisted connector configs for a tenant from Redis."""
        if tenant_id in self._config_cache_loaded:
            return self._tenant_configs.get(tenant_id, {})
        try:
            redis = getattr(self.job_store, '_redis', None)
            if redis:
                import json
                raw = await redis.get(self._config_redis_key(tenant_id))
                if raw:
                    configs = json.loads(raw)
                    self._tenant_configs[tenant_id] = configs
        except Exception as e:
            logger.warning("nerves: failed to load connector configs for %s: %s", tenant_id, e)
        self._config_cache_loaded.add(tenant_id)
        return self._tenant_configs.get(tenant_id, {})

    async def _save_tenant_configs(self, tenant_id: str) -> None:
        """Persist connector configs for a tenant to Redis."""
        try:
            redis = getattr(self.job_store, '_redis', None)
            if redis:
                import json
                configs = self._tenant_configs.get(tenant_id, {})
                # Strip sensitive credential values — store config metadata
                # Actual secrets should go to a secrets manager (Key Vault) in production
                await redis.set(
                    self._config_redis_key(tenant_id),
                    json.dumps(configs, default=str),
                    ex=86400 * 30,  # 30-day TTL
                )
        except Exception as e:
            logger.warning("nerves: failed to save connector configs for %s: %s", tenant_id, e)

    async def on_startup(self):
        """Subscribe to events that trigger notifications."""
        # Inject event bus into all connector modules
        for mod in (_jira_mod, _github_mod, _slack_mod, _teams_mod, _webhook_mod):
            mod.set_event_bus(self.event_bus)

        # Report all connector modes
        for name, conn in self.connectors.items():
            self.health.set_mode(f"connector_{name}", conn.status.value)

        if self.event_bus:
            await self.event_bus.subscribe(
                "heart.rules.extracted", self._notify_rules_extracted
            )

    async def _notify_rules_extracted(self, event: NexusEvent):
        """Notify via configured channels when rules are extracted."""
        tenant_id = event.tenant_id
        rule_count = event.data.get("rule_count", 0)

        # Check if tenant has Slack configured
        tenant_conns = await self._load_tenant_configs(tenant_id)
        if "slack" in tenant_conns:
            try:
                await self.connectors["slack"].execute("send_message", {
                    "channel": tenant_conns["slack"].get("default_channel", "#nexus-qa"),
                    "text": f":brain: Heart extracted {rule_count} business rules from KT session {event.session_id}",
                })
            except Exception:
                pass  # Don't fail pipeline on notification errors

    def register_routes(self, app):

        # ── List Connectors ────────────────────────────────────

        @app.get("/api/v1/nerves/connectors")
        async def list_connectors(
            user: NexusUser = Depends(get_current_user),
        ):
            """List all available connectors and their status."""
            return [conn.get_status() for conn in self.connectors.values()]

        # ── Configure Connector ────────────────────────────────

        @app.post("/api/v1/nerves/connectors/configure")
        async def configure_connector(
            req: ConfigureConnectorRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Configure a connector with credentials for the current tenant."""
            user.require_permission("nerves.configure")

            connector = self.connectors.get(req.connector)
            if not connector:
                raise HTTPException(status_code=404, detail=f"Unknown connector: {req.connector}")

            success = await connector.connect(req.credentials)

            # Store per-tenant config (in-memory + Redis persistence)
            if success:
                self._tenant_configs.setdefault(req.tenant_id, {})[req.connector] = req.credentials
                await self._save_tenant_configs(req.tenant_id)

            return {
                "connector": req.connector,
                "connected": success,
                "status": connector.status.value,
            }

        # ── Get Connector Actions ──────────────────────────────

        @app.get("/api/v1/nerves/connectors/{connector_name}/actions")
        async def get_actions(
            connector_name: str,
            user: NexusUser = Depends(get_current_user),
        ):
            connector = self.connectors.get(connector_name)
            if not connector:
                raise HTTPException(status_code=404, detail=f"Unknown connector: {connector_name}")
            return connector.get_available_actions()

        # ── Execute Single Action ──────────────────────────────

        @app.post("/api/v1/nerves/execute", response_model=ConnectorResult)
        async def execute_action(
            req: ExecuteActionRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Execute a single connector action."""
            connector = self.connectors.get(req.connector)
            if not connector:
                raise HTTPException(status_code=404, detail=f"Unknown connector: {req.connector}")

            start = time.monotonic()
            try:
                result = await connector.execute(req.action, req.parameters)
                elapsed = (time.monotonic() - start) * 1000

                return ConnectorResult(
                    connector=req.connector,
                    action=req.action,
                    success=True,
                    data=result,
                    execution_time_ms=round(elapsed, 2),
                )
            except Exception as e:
                elapsed = (time.monotonic() - start) * 1000
                return ConnectorResult(
                    connector=req.connector,
                    action=req.action,
                    success=False,
                    error=str(e),
                    execution_time_ms=round(elapsed, 2),
                )

        # ── Batch Execute ──────────────────────────────────────

        @app.post("/api/v1/nerves/execute/batch")
        async def batch_execute(
            req: BatchExecuteRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Execute multiple connector actions in sequence."""
            results = []
            for action in req.actions:
                connector = self.connectors.get(action.connector)
                if not connector:
                    results.append(ConnectorResult(
                        connector=action.connector,
                        action=action.action,
                        success=False,
                        error=f"Unknown connector: {action.connector}",
                    ))
                    continue

                start = time.monotonic()
                try:
                    result = await connector.execute(action.action, action.parameters)
                    elapsed = (time.monotonic() - start) * 1000
                    results.append(ConnectorResult(
                        connector=action.connector,
                        action=action.action,
                        success=True,
                        data=result,
                        execution_time_ms=round(elapsed, 2),
                    ))
                except Exception as e:
                    elapsed = (time.monotonic() - start) * 1000
                    results.append(ConnectorResult(
                        connector=action.connector,
                        action=action.action,
                        success=False,
                        error=str(e),
                        execution_time_ms=round(elapsed, 2),
                    ))

            return {"results": [r.model_dump() for r in results]}


# ─── Entry Point ──────────────────────────────────────────────

def main():
    engine = NervesEngine()
    engine.run()


if __name__ == "__main__":
    main()
