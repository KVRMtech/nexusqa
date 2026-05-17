"""Reference plugin smoke test — proves the SDK is usable end-to-end."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_PLUGIN_SDK_PATH = os.path.join(
    _PROJECT_ROOT, "sdk", "nexus-plugin-sdk"
)
if _PLUGIN_SDK_PATH not in sys.path:
    sys.path.insert(0, _PLUGIN_SDK_PATH)


def _read_lines(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_echo_logger_event_logged(tmp_path):
    from examples.echo_logger import EchoLoggerPlugin
    from nexus_plugin_sdk import PluginTestHarness

    log_path = tmp_path / "echo_logger.ndjson"
    plugin = EchoLoggerPlugin()
    async with PluginTestHarness(
        plugin,
        tenant_id="t-acme",
        config={"log_path": str(log_path), "include_payload": True},
    ) as h:
        await h.deliver_event(
            "knowledge.echo.dispatched",
            {"echo_id": "E-1", "question_text": "policy change?"},
            trace_id="trace-123",
        )

    assert log_path.exists(), "log file was not created"
    records = _read_lines(log_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["kind"] == "event"
    assert rec["event_type"] == "knowledge.echo.dispatched"
    assert rec["tenant_id"] == "t-acme"
    assert rec["trace_id"] == "trace-123"
    assert rec["payload"]["echo_id"] == "E-1"


@pytest.mark.asyncio
async def test_echo_logger_action_logs_and_publishes(tmp_path):
    from examples.echo_logger import EchoLoggerPlugin
    from nexus_plugin_sdk import PluginTestHarness

    log_path = tmp_path / "actions.ndjson"
    plugin = EchoLoggerPlugin()
    async with PluginTestHarness(
        plugin,
        tenant_id="t-acme",
        config={"log_path": str(log_path)},
    ) as h:
        result = await h.call_action(
            "log_echo",
            {"echo_id": "E-2", "question_text": "renewal date?", "topic": "renewals"},
        )

    assert result.ok
    assert result.output["logged"] is True
    assert result.external_ref and result.external_ref.startswith("file://")
    rec = h.assert_published(
        "echo_logger.logged", payload_contains={"echo_id": "E-2"}
    )
    assert rec.payload["tenant_id"] == "t-acme"

    records = _read_lines(log_path)
    assert len(records) == 1
    assert records[0]["kind"] == "action"
    assert records[0]["params"]["echo_id"] == "E-2"
    assert records[0]["params"]["topic"] == "renewals"


@pytest.mark.asyncio
async def test_echo_logger_action_rejects_missing_params(tmp_path):
    from examples.echo_logger import EchoLoggerPlugin
    from nexus_plugin_sdk import PluginTestHarness

    log_path = tmp_path / "rejected.ndjson"
    plugin = EchoLoggerPlugin()
    async with PluginTestHarness(
        plugin,
        config={"log_path": str(log_path)},
    ) as h:
        result = await h.call_action("log_echo", {"echo_id": "E-3"})

    assert not result.ok
    assert "question_text" in (result.error or "")
    assert not log_path.exists() or _read_lines(log_path) == []


def test_echo_logger_manifest_validates():
    """The bundled plugin.yaml validates against the Phase 0 schema."""
    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "sdk", "nexus-sdk"))
    from nexus_sdk.integrations import load_manifest

    manifest_path = (
        Path(_PROJECT_ROOT)
        / "sdk"
        / "nexus-plugin-sdk"
        / "examples"
        / "echo_logger"
        / "plugin.yaml"
    )
    manifest = load_manifest(manifest_path)
    assert manifest.id == "echo_logger"
    assert manifest.sink is not None
    action_ids = [a.id for a in manifest.sink.actions]
    assert "log_echo" in action_ids
