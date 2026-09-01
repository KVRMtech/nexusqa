"""Unit tests for the nexus-plugin-sdk core (Phase 8).

Covers:

  * Decorator metadata stamping + ``__init_subclass__`` collection.
  * Invalid identifier rejection.
  * Duplicate @on_event / @action raising at class-definition time.
  * @scheduled validation.
  * BasePlugin.connect/disconnect lifecycle gating.
  * Event dispatch + action dispatch + unknown-action fallback.
  * Sync handlers awaited correctly.
  * PluginContext.publish error when no channel wired.
"""

from __future__ import annotations

import os
import sys
import pytest

# Add nexus-plugin-sdk to path
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_PLUGIN_SDK_PATH = os.path.join(
    _PROJECT_ROOT, "sdk", "nexus-plugin-sdk"
)
if _PLUGIN_SDK_PATH not in sys.path:
    sys.path.insert(0, _PLUGIN_SDK_PATH)


def _import_sdk():
    from nexus_plugin_sdk import (
        ActionInput,
        ActionResult,
        BasePlugin,
        PluginContext,
        PluginEvent,
        PluginRegistrationError,
        PluginRuntimeError,
        action,
        on_event,
        scheduled,
    )

    return {
        "ActionInput": ActionInput,
        "ActionResult": ActionResult,
        "BasePlugin": BasePlugin,
        "PluginContext": PluginContext,
        "PluginEvent": PluginEvent,
        "PluginRegistrationError": PluginRegistrationError,
        "PluginRuntimeError": PluginRuntimeError,
        "action": action,
        "on_event": on_event,
        "scheduled": scheduled,
    }


# ── Decorators ────────────────────────────────────────────────


def test_on_event_validates_id():
    sdk = _import_sdk()
    with pytest.raises(sdk["PluginRegistrationError"]):
        sdk["on_event"]("INVALID_UPPERCASE")(lambda self, e: None)
    with pytest.raises(sdk["PluginRegistrationError"]):
        sdk["on_event"]("")(lambda self, e: None)
    with pytest.raises(sdk["PluginRegistrationError"]):
        sdk["on_event"]("9-starts-with-digit")(lambda self, e: None)


def test_action_validates_id():
    sdk = _import_sdk()
    with pytest.raises(sdk["PluginRegistrationError"]):
        sdk["action"]("bad-Char!")(lambda self, i: None)


def test_scheduled_requires_non_empty():
    sdk = _import_sdk()
    with pytest.raises(sdk["PluginRegistrationError"]):
        sdk["scheduled"]("")(lambda self: None)
    with pytest.raises(sdk["PluginRegistrationError"]):
        sdk["scheduled"]("   ")(lambda self: None)


def test_duplicate_event_handler_rejected():
    sdk = _import_sdk()
    with pytest.raises(sdk["PluginRegistrationError"]):

        class Dup(sdk["BasePlugin"]):
            @sdk["on_event"]("topic.a")
            async def first(self, ev):
                return None

            @sdk["on_event"]("topic.a")
            async def second(self, ev):
                return None


def test_duplicate_action_handler_rejected():
    sdk = _import_sdk()
    with pytest.raises(sdk["PluginRegistrationError"]):

        class DupAction(sdk["BasePlugin"]):
            @sdk["action"]("do_thing")
            async def alpha(self, inp):
                return sdk["ActionResult"].success()

            @sdk["action"]("do_thing")
            async def beta(self, inp):
                return sdk["ActionResult"].success()


# ── __init_subclass__ collection ─────────────────────────────


def test_handlers_collected_on_subclass():
    sdk = _import_sdk()

    class P(sdk["BasePlugin"]):
        @sdk["on_event"]("topic.a")
        async def on_a(self, ev):
            return None

        @sdk["on_event"]("topic.b")
        async def on_b(self, ev):
            return None

        @sdk["action"]("alpha")
        async def do_alpha(self, inp):
            return sdk["ActionResult"].success()

        @sdk["scheduled"]("*/1 * * * *")
        async def beat(self):
            return None

    assert P._event_handlers == {"topic.a": "on_a", "topic.b": "on_b"}
    assert P._action_handlers == {"alpha": "do_alpha"}
    assert P._scheduled_handlers == [("*/1 * * * *", "beat")]


def test_handler_listings_sorted():
    sdk = _import_sdk()

    class P(sdk["BasePlugin"]):
        @sdk["on_event"]("zoo")
        async def z(self, e):
            return None

        @sdk["on_event"]("apple")
        async def a(self, e):
            return None

        @sdk["action"]("zebra")
        async def z2(self, i):
            return sdk["ActionResult"].success()

        @sdk["action"]("ant")
        async def a2(self, i):
            return sdk["ActionResult"].success()

    p = P()
    assert p.list_event_types() == ["apple", "zoo"]
    assert p.list_action_ids() == ["ant", "zebra"]


# ── Lifecycle ─────────────────────────────────────────────────


def test_context_not_accessible_before_connect():
    sdk = _import_sdk()

    class P(sdk["BasePlugin"]):
        @sdk["action"]("noop")
        async def noop(self, inp):
            return sdk["ActionResult"].success()

    p = P()
    with pytest.raises(sdk["PluginRuntimeError"]):
        _ = p.context


def test_invoke_before_connect_raises():
    import asyncio
    sdk = _import_sdk()

    class P(sdk["BasePlugin"]):
        @sdk["action"]("a")
        async def a(self, inp):
            return sdk["ActionResult"].success()

    p = P()
    inp = sdk["ActionInput"](action_id="a", tenant_id="t1", params={})
    with pytest.raises(sdk["PluginRuntimeError"]):
        asyncio.run(p.handle_action(inp))


def test_unknown_action_returns_failure():
    import asyncio
    sdk = _import_sdk()

    class P(sdk["BasePlugin"]):
        @sdk["action"]("known")
        async def k(self, inp):
            return sdk["ActionResult"].success()

    p = P()
    ctx = sdk["PluginContext"](
        plugin_id="p", plugin_version="0.0.1", tenant_id="t1"
    )

    async def run():
        await p.connect(ctx)
        try:
            return await p.handle_action(
                sdk["ActionInput"](
                    action_id="missing",
                    tenant_id="t1",
                    params={},
                )
            )
        finally:
            await p.disconnect()

    res = asyncio.run(run())
    assert res.ok is False
    assert "unknown action" in (res.error or "")


def test_unsubscribed_event_silently_ignored():
    import asyncio
    sdk = _import_sdk()

    class P(sdk["BasePlugin"]):
        @sdk["on_event"]("known.evt")
        async def on_known(self, ev):
            self.context.config["seen"] = True

    p = P()
    ctx = sdk["PluginContext"](
        plugin_id="p", plugin_version="0.0.1", tenant_id="t1", config={}
    )

    async def run():
        await p.connect(ctx)
        await p.handle_event(
            sdk["PluginEvent"](
                event_type="unknown.evt", tenant_id="t1", payload={}
            )
        )
        return ctx.config.get("seen", False)

    assert asyncio.run(run()) is False


def test_sync_handler_supported():
    import asyncio
    sdk = _import_sdk()

    class P(sdk["BasePlugin"]):
        @sdk["action"]("sync_act")
        def sync_act(self, inp):
            return sdk["ActionResult"].success(output={"sync": True})

    p = P()
    ctx = sdk["PluginContext"](
        plugin_id="p", plugin_version="0.0.1", tenant_id="t1"
    )

    async def run():
        await p.connect(ctx)
        return await p.handle_action(
            sdk["ActionInput"](
                action_id="sync_act", tenant_id="t1", params={}
            )
        )

    res = asyncio.run(run())
    assert res.ok and res.output == {"sync": True}


def test_handler_exception_wrapped():
    import asyncio
    sdk = _import_sdk()

    class P(sdk["BasePlugin"]):
        @sdk["action"]("boom")
        async def boom(self, inp):
            raise ValueError("kaboom")

    p = P()
    ctx = sdk["PluginContext"](
        plugin_id="p", plugin_version="0.0.1", tenant_id="t1"
    )

    async def run():
        await p.connect(ctx)
        return await p.handle_action(
            sdk["ActionInput"](
                action_id="boom", tenant_id="t1", params={}
            )
        )

    with pytest.raises(sdk["PluginRuntimeError"]) as excinfo:
        asyncio.run(run())
    assert "boom" in str(excinfo.value)


def test_publish_without_channel_raises():
    import asyncio
    sdk = _import_sdk()
    ctx = sdk["PluginContext"](
        plugin_id="p", plugin_version="0.0.1", tenant_id="t1"
    )
    with pytest.raises(sdk["PluginRuntimeError"]):
        asyncio.run(ctx.publish("topic", {}))
