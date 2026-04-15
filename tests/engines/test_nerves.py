"""
Nerves Engine — Unit tests.

Tests all 5 connectors (Jira, GitHub, Slack, Teams, Webhook),
connection handling, action dispatch, and enums.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "nerves-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


@pytest.fixture(autouse=True)
def _force_stub_mode(monkeypatch):
    """Force all connectors into stub mode by hiding httpx from the engine."""
    import main as nerves_main
    monkeypatch.setattr(nerves_main, "httpx", None)
    # With modular refactor, also patch each connector sub-module's httpx
    from app.connectors import jira, github, slack, teams, webhook
    for mod in [jira, github, slack, teams, webhook]:
        monkeypatch.setattr(mod, "httpx", None)


# ─── Enums & Models ───────────────────────────────────────────


class TestConnectorStatus:

    def test_values(self):
        from main import ConnectorStatus
        assert ConnectorStatus.CONNECTED == "connected"
        assert ConnectorStatus.DISCONNECTED == "disconnected"
        assert ConnectorStatus.ERROR == "error"
        assert ConnectorStatus.NOT_CONFIGURED == "not_configured"


class TestConnectorAction:

    def test_create(self):
        from main import ConnectorAction
        action = ConnectorAction(connector="jira", action="create_issue", parameters={"project": "NQA"})
        assert action.connector == "jira"
        assert action.parameters["project"] == "NQA"

    def test_defaults(self):
        from main import ConnectorAction
        action = ConnectorAction(connector="slack", action="send_message")
        assert action.parameters == {}


class TestConnectorResult:

    def test_success_result(self):
        from main import ConnectorResult
        r = ConnectorResult(connector="jira", action="create_issue", success=True, data={"key": "NQA-1"})
        assert r.success is True
        assert r.error is None

    def test_failure_result(self):
        from main import ConnectorResult
        r = ConnectorResult(connector="jira", action="create_issue", success=False, error="Connection timeout")
        assert r.success is False
        assert "timeout" in r.error.lower()


# ─── Jira Connector ───────────────────────────────────────────


class TestJiraConnector:

    def setup_method(self):
        from main import JiraConnector
        self.jira = JiraConnector()

    @pytest.mark.asyncio
    async def test_connect_success(self):
        result = await self.jira.connect({"url": "https://jira.example.com", "email": "a@b.com", "api_token": "tok"})
        assert result is True
        from main import ConnectorStatus
        assert self.jira.status == ConnectorStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_connect_missing_creds(self):
        result = await self.jira.connect({"url": "https://jira.example.com"})
        assert result is False
        from main import ConnectorStatus
        assert self.jira.status == ConnectorStatus.ERROR

    @pytest.mark.asyncio
    async def test_create_issue(self):
        await self.jira.connect({"url": "u", "email": "e", "api_token": "t"})
        result = await self.jira.execute("create_issue", {
            "project": "NQA", "summary": "Test bug", "description": "Desc", "issue_type": "Bug",
        })
        assert "issue_key" in result
        assert result["issue_key"].startswith("NQA-")

    @pytest.mark.asyncio
    async def test_update_issue(self):
        await self.jira.connect({"url": "u", "email": "e", "api_token": "t"})
        create = await self.jira.execute("create_issue", {"project": "NQA", "summary": "Orig"})
        key = create["issue_key"]
        update = await self.jira.execute("update_issue", {"issue_key": key, "fields": {"summary": "Updated"}})
        assert update["updated"] is True

    @pytest.mark.asyncio
    async def test_update_nonexistent_issue(self):
        await self.jira.connect({"url": "u", "email": "e", "api_token": "t"})
        update = await self.jira.execute("update_issue", {"issue_key": "FAKE-999", "fields": {}})
        assert update["updated"] is False

    @pytest.mark.asyncio
    async def test_search_issues(self):
        await self.jira.connect({"url": "u", "email": "e", "api_token": "t"})
        await self.jira.execute("create_issue", {"project": "NQA", "summary": "S1"})
        result = await self.jira.execute("search_issues", {"jql": "project=NQA", "max_results": 10})
        assert "issues" in result
        assert len(result["issues"]) >= 1

    @pytest.mark.asyncio
    async def test_unknown_action_raises(self):
        with pytest.raises(ValueError, match="Unknown Jira action"):
            await self.jira.execute("nonexistent_action", {})

    def test_available_actions(self):
        actions = self.jira.get_available_actions()
        action_names = [a["action"] for a in actions]
        assert "create_issue" in action_names
        assert "update_issue" in action_names
        assert "search_issues" in action_names

    def test_initial_status(self):
        from main import ConnectorStatus
        assert self.jira.status == ConnectorStatus.NOT_CONFIGURED


# ─── GitHub Connector ─────────────────────────────────────────


class TestGitHubConnector:

    def setup_method(self):
        from main import GitHubConnector
        self.gh = GitHubConnector()

    @pytest.mark.asyncio
    async def test_connect_success(self):
        result = await self.gh.connect({"token": "ghp_xxx", "repo": "org/repo"})
        assert result is True

    @pytest.mark.asyncio
    async def test_connect_missing_token(self):
        result = await self.gh.connect({"repo": "org/repo"})
        assert result is False

    @pytest.mark.asyncio
    async def test_create_branch(self):
        await self.gh.connect({"token": "t", "repo": "r"})
        result = await self.gh.execute("create_branch", {"branch_name": "nexus-tests"})
        assert result["created"] is True
        assert result["branch"] == "nexus-tests"

    @pytest.mark.asyncio
    async def test_commit_file(self):
        await self.gh.connect({"token": "t", "repo": "r"})
        result = await self.gh.execute("commit_file", {"path": "tests/new.py", "content": "pass"})
        assert result["committed"] is True
        assert "sha" in result

    @pytest.mark.asyncio
    async def test_create_pr(self):
        await self.gh.connect({"token": "t", "repo": "org/repo"})
        result = await self.gh.execute("create_pr", {"title": "Add tests", "body": "..."})
        assert result["created"] is True
        assert "url" in result

    @pytest.mark.asyncio
    async def test_unknown_action_raises(self):
        with pytest.raises(ValueError, match="Unknown GitHub action"):
            await self.gh.execute("bad_action", {})


# ─── Slack Connector ──────────────────────────────────────────


class TestSlackConnector:

    def setup_method(self):
        from main import SlackConnector
        self.slack = SlackConnector()

    @pytest.mark.asyncio
    async def test_connect_success(self):
        result = await self.slack.connect({"bot_token": "xoxb-xxx"})
        assert result is True

    @pytest.mark.asyncio
    async def test_connect_missing_token(self):
        result = await self.slack.connect({})
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message(self):
        await self.slack.connect({"bot_token": "xoxb-xxx"})
        result = await self.slack.execute("send_message", {"channel": "#nexus-qa", "text": "Hello"})
        assert result["sent"] is True
        assert result["channel"] == "#nexus-qa"

    @pytest.mark.asyncio
    async def test_send_dm(self):
        await self.slack.connect({"bot_token": "xoxb-xxx"})
        result = await self.slack.execute("send_dm", {"user_id": "U123"})
        assert result["sent"] is True

    @pytest.mark.asyncio
    async def test_send_confirmation(self):
        await self.slack.connect({"bot_token": "xoxb-xxx"})
        result = await self.slack.execute("send_confirmation", {"channel": "#qa", "question": "OK?"})
        assert "confirmation_id" in result

    @pytest.mark.asyncio
    async def test_upload_file(self):
        await self.slack.connect({"bot_token": "xoxb-xxx"})
        result = await self.slack.execute("upload_file", {"channel": "#qa", "file_content": "data"})
        assert result["uploaded"] is True

    @pytest.mark.asyncio
    async def test_unknown_action_raises(self):
        with pytest.raises(ValueError, match="Unknown Slack action"):
            await self.slack.execute("nonexistent", {})


# ─── Teams Connector ──────────────────────────────────────────


class TestTeamsConnector:

    def setup_method(self):
        from main import TeamsConnector
        self.teams = TeamsConnector()

    @pytest.mark.asyncio
    async def test_connect_success(self):
        result = await self.teams.connect({"client_id": "cid", "client_secret": "cs"})
        assert result is True

    @pytest.mark.asyncio
    async def test_connect_missing_creds(self):
        result = await self.teams.connect({"client_id": "cid"})
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message(self):
        await self.teams.connect({"client_id": "c", "client_secret": "s"})
        result = await self.teams.execute("send_message", {"channel_id": "ch1", "text": "Hi"})
        assert result["sent"] is True

    @pytest.mark.asyncio
    async def test_unknown_action_raises(self):
        with pytest.raises(ValueError, match="Unknown Teams action"):
            await self.teams.execute("bad", {})


# ─── Webhook Connector ────────────────────────────────────────


class TestWebhookConnector:

    def setup_method(self):
        from main import WebhookConnector
        self.wh = WebhookConnector()

    @pytest.mark.asyncio
    async def test_connect_with_url(self):
        result = await self.wh.connect({"url": "https://hooks.example.com/notify"})
        assert result is True

    @pytest.mark.asyncio
    async def test_connect_missing_url(self):
        result = await self.wh.connect({})
        assert result is False

    @pytest.mark.asyncio
    async def test_execute_send(self):
        await self.wh.connect({"url": "https://hooks.example.com/notify"})
        result = await self.wh.execute("send", {"payload": {"event": "test_done"}})
        assert result["sent"] is True
        assert result["status_code"] == 200


# ─── Disconnect ────────────────────────────────────────────────


class TestDisconnect:

    @pytest.mark.asyncio
    async def test_disconnect_sets_status(self):
        from main import JiraConnector, ConnectorStatus
        jira = JiraConnector()
        await jira.connect({"url": "u", "email": "e", "api_token": "t"})
        assert jira.status == ConnectorStatus.CONNECTED
        await jira.disconnect()
        assert jira.status == ConnectorStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_get_status(self):
        from main import SlackConnector
        slack = SlackConnector()
        status = slack.get_status()
        assert status["connector"] == "slack"
        assert status["status"] == "not_configured"
        assert status["configured"] is False
