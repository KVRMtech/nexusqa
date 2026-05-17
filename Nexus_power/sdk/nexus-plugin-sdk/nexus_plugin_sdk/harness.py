"""Test harness — drive a plugin from a test without a real event bus.

Usage::

    plugin = AcmeHrPlugin()
    async with PluginTestHarness(plugin, tenant_id="t1") as h:
        await h.deliver_event("hr.policy.updated", {"policy_id": "P-1"})
        result = await h.call_action("send_to_hr_portal", {"target": "..."})
        assert result.ok
        assert h.published == [("knowledge.policy_change", {"policy_id": "P-1"})]

Records every outbound publish + action call so the test can assert
on them without instrumenting the plugin under test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .base import (
    ActionInput,
    ActionResult,
    BasePlugin,
    PluginContext,
    PluginEvent,
)


@dataclass(frozen=True)
class RecordedPublish:
    event_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class RecordedAction:
    action_id: str
    params: dict[str, Any]
    result: ActionResult


class PluginTestHarness:
    """Lightweight async-context wrapping a plugin's lifecycle."""

    def __init__(
        self,
        plugin: BasePlugin,
        *,
        tenant_id: str = "test-tenant",
        plugin_id: Optional[str] = None,
        plugin_version: str = "0.0.0",
        config: Optional[dict[str, Any]] = None,
        logger_obj: Optional[logging.Logger] = None,
    ):
        self.plugin = plugin
        self._tenant_id = tenant_id
        self._plugin_id = (
            plugin_id or plugin.plugin_id or type(plugin).__name__
        )
        self._plugin_version = plugin_version
        self._config = dict(config or {})
        self._logger = logger_obj or logging.getLogger(
            f"test_harness.{self._plugin_id}"
        )
        self.published: list[RecordedPublish] = []
        self.actions: list[RecordedAction] = []
        self._connected = False

    # ── Async context manager ──────────────────────────────────

    async def __aenter__(self) -> "PluginTestHarness":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        if self._connected:
            return
        context = PluginContext(
            plugin_id=self._plugin_id,
            plugin_version=self._plugin_version,
            tenant_id=self._tenant_id,
            config=self._config,
            publish=self._record_publish,
            logger_obj=self._logger,
        )
        await self.plugin.connect(context)
        self._connected = True

    async def disconnect(self) -> None:
        if not self._connected:
            return
        await self.plugin.disconnect()
        self._connected = False

    # ── Test-side driving ──────────────────────────────────────

    async def deliver_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        metadata: Optional[dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        event = PluginEvent(
            event_type=event_type,
            tenant_id=self._tenant_id,
            payload=payload,
            metadata=metadata or {},
            trace_id=trace_id,
        )
        await self.plugin.handle_event(event)

    async def call_action(
        self,
        action_id: str,
        params: dict[str, Any],
        *,
        metadata: Optional[dict[str, Any]] = None,
        trace_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> ActionResult:
        action_input = ActionInput(
            action_id=action_id,
            tenant_id=self._tenant_id,
            params=params,
            metadata=metadata or {},
            trace_id=trace_id,
            idempotency_key=idempotency_key,
        )
        result = await self.plugin.handle_action(action_input)
        self.actions.append(
            RecordedAction(
                action_id=action_id,
                params=dict(params),
                result=result,
            )
        )
        return result

    def assert_published(self, event_type: str, *, payload_contains: Optional[dict[str, Any]] = None) -> RecordedPublish:
        """Find the first matching publish or raise ``AssertionError``."""
        for rec in self.published:
            if rec.event_type != event_type:
                continue
            if payload_contains:
                if not all(
                    rec.payload.get(k) == v for k, v in payload_contains.items()
                ):
                    continue
            return rec
        raise AssertionError(
            f"no published event matched event_type={event_type!r}"
            + (f" payload_contains={payload_contains!r}" if payload_contains else "")
        )

    # ── Internals ──────────────────────────────────────────────

    async def _record_publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        self.published.append(
            RecordedPublish(event_type=event_type, payload=dict(payload))
        )
