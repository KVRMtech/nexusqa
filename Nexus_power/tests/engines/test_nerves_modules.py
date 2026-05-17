"""
Nerves Engine — Modular Sub-package Tests.

Tests the connector modules refactored from the monolithic
nerves-engine/main.py.

All tests exercise stub mode (no external services).
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "nerves-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── BaseConnector ─────────────────────────────────────────────


class TestBaseConnector:
    """Test BaseConnector ABC and ConnectorStatus from app.connectors."""

    def test_import(self):
        from app.connectors import BaseConnector, ConnectorStatus
        assert BaseConnector is not None
        assert ConnectorStatus is not None

    def test_connector_status_values(self):
        from app.connectors import ConnectorStatus
        assert hasattr(ConnectorStatus, "CONNECTED")
        assert hasattr(ConnectorStatus, "DISCONNECTED")
        assert hasattr(ConnectorStatus, "ERROR")

    def test_base_is_abstract(self):
        from app.connectors.base import BaseConnector
        # Cannot instantiate directly  — it's an ABC
        with pytest.raises(TypeError):
            BaseConnector()


# ─── JiraConnector ─────────────────────────────────────────────


class TestJiraConnector:
    """Test JiraConnector from app.connectors."""

    def test_import(self):
        from app.connectors import JiraConnector
        assert JiraConnector is not None

    def test_init(self):
        from app.connectors import JiraConnector
        jira = JiraConnector()
        assert jira is not None

    def test_has_required_methods(self):
        from app.connectors import JiraConnector
        jira = JiraConnector()
        assert callable(getattr(jira, "connect", None))
        assert callable(getattr(jira, "disconnect", None))
        assert callable(getattr(jira, "execute", None))

    def test_set_event_bus(self):
        import app.connectors.jira as jira_mod
        assert hasattr(jira_mod, "set_event_bus")
        assert callable(jira_mod.set_event_bus)
        jira_mod.set_event_bus(None)  # Should not raise


# ─── GitHubConnector ──────────────────────────────────────────


class TestGitHubConnector:
    """Test GitHubConnector from app.connectors."""

    def test_import(self):
        from app.connectors import GitHubConnector
        assert GitHubConnector is not None

    def test_init(self):
        from app.connectors import GitHubConnector
        gh = GitHubConnector()
        assert gh is not None

    def test_set_event_bus(self):
        import app.connectors.github as gh_mod
        assert hasattr(gh_mod, "set_event_bus")
        gh_mod.set_event_bus(None)


# ─── SlackConnector ───────────────────────────────────────────


class TestSlackConnector:
    """Test SlackConnector from app.connectors."""

    def test_import(self):
        from app.connectors import SlackConnector
        assert SlackConnector is not None

    def test_init(self):
        from app.connectors import SlackConnector
        slack = SlackConnector()
        assert slack is not None

    def test_has_required_methods(self):
        from app.connectors import SlackConnector
        s = SlackConnector()
        assert callable(getattr(s, "connect", None))
        assert callable(getattr(s, "disconnect", None))
        assert callable(getattr(s, "execute", None))

    def test_set_event_bus(self):
        import app.connectors.slack as slack_mod
        assert hasattr(slack_mod, "set_event_bus")
        slack_mod.set_event_bus(None)


# ─── TeamsConnector ───────────────────────────────────────────


class TestTeamsConnector:
    """Test TeamsConnector from app.connectors."""

    def test_import(self):
        from app.connectors import TeamsConnector
        assert TeamsConnector is not None

    def test_init(self):
        from app.connectors import TeamsConnector
        teams = TeamsConnector()
        assert teams is not None

    def test_set_event_bus(self):
        import app.connectors.teams as teams_mod
        assert hasattr(teams_mod, "set_event_bus")
        teams_mod.set_event_bus(None)


# ─── WebhookConnector ────────────────────────────────────────


class TestWebhookConnector:
    """Test WebhookConnector from app.connectors."""

    def test_import(self):
        from app.connectors import WebhookConnector
        assert WebhookConnector is not None

    def test_init(self):
        from app.connectors import WebhookConnector
        wh = WebhookConnector()
        assert wh is not None

    def test_set_event_bus(self):
        import app.connectors.webhook as wh_mod
        assert hasattr(wh_mod, "set_event_bus")
        wh_mod.set_event_bus(None)


# ─── Re-exports ───────────────────────────────────────────────


class TestConnectorsReExports:
    """Verify app.connectors.__init__ re-exports all connectors."""

    def test_all_connectors_exported(self):
        from app.connectors import (
            BaseConnector,
            ConnectorStatus,
            JiraConnector,
            GitHubConnector,
            SlackConnector,
            TeamsConnector,
            WebhookConnector,
        )
        assert all([
            BaseConnector,
            ConnectorStatus,
            JiraConnector,
            GitHubConnector,
            SlackConnector,
            TeamsConnector,
            WebhookConnector,
        ])


# ─── Integration: main.py v0.2.0 ─────────────────────────────


class TestNervesMainImports:
    """Verify main.py v0.2.0 correctly imports from sub-packages."""

    def test_main_version(self):
        from main import NervesEngine
        engine = NervesEngine()
        assert engine.version == "0.2.0"

    def test_main_imports_connectors(self):
        from main import (
            JiraConnector,
            GitHubConnector,
            SlackConnector,
            TeamsConnector,
            WebhookConnector,
        )
        assert JiraConnector is not None
        assert GitHubConnector is not None

    def test_main_config(self):
        from main import NervesConfig
        cfg = NervesConfig()
        assert cfg.engine_name == "nerves"
        assert cfg.engine_port == 8006

    def test_main_request_models(self):
        from main import ConfigureConnectorRequest, ExecuteActionRequest, BatchExecuteRequest
        assert ConfigureConnectorRequest is not None
        assert ExecuteActionRequest is not None
        assert BatchExecuteRequest is not None
