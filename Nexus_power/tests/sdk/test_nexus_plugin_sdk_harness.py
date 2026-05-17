"""Test harness assertions for nexus-plugin-sdk."""

from __future__ import annotations

import os
import sys
import pytest

_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_PLUGIN_SDK_PATH = os.path.join(
    _PROJECT_ROOT, "sdk", "nexus-plugin-sdk"
)
if _PLUGIN_SDK_PATH not in sys.path:
    sys.path.insert(0, _PLUGIN_SDK_PATH)


def _build_plugin():
    from nexus_plugin_sdk import (
        ActionInput,
        ActionResult,
        BasePlugin,
        PluginEvent,
        action,
        on_event,
    )

    class P(BasePlugin):
        @on_event("topic.notify")
        async def on_notify(self, event: PluginEvent) -> None:
            await self.context.publish(
                "downstream.echo", {"src": event.payload.get("src", "?")}
            )

        @action("compute")
        async def compute(self, inp: ActionInput) -> ActionResult:
            n = inp.params.get("n", 0)
            return ActionResult.success(output={"square": n * n})

        @action("fail_me")
        async def fail_me(self, inp: ActionInput) -> ActionResult:
            return ActionResult.failure("expected failure")

    return P, ActionInput, ActionResult, PluginEvent


@pytest.mark.asyncio
async def test_harness_lifecycle_records_publish():
    from nexus_plugin_sdk import PluginTestHarness

    P, *_ = _build_plugin()
    async with PluginTestHarness(P(), tenant_id="t1") as h:
        await h.deliver_event("topic.notify", {"src": "alpha"})
        assert len(h.published) == 1
        rec = h.published[0]
        assert rec.event_type == "downstream.echo"
        assert rec.payload == {"src": "alpha"}


@pytest.mark.asyncio
async def test_harness_records_actions():
    from nexus_plugin_sdk import PluginTestHarness

    P, *_ = _build_plugin()
    async with PluginTestHarness(P()) as h:
        r1 = await h.call_action("compute", {"n": 7})
        r2 = await h.call_action("fail_me", {})

    assert r1.ok and r1.output["square"] == 49
    assert not r2.ok and "expected failure" in (r2.error or "")
    assert [a.action_id for a in h.actions] == ["compute", "fail_me"]


@pytest.mark.asyncio
async def test_harness_assert_published_matches():
    from nexus_plugin_sdk import PluginTestHarness

    P, *_ = _build_plugin()
    async with PluginTestHarness(P()) as h:
        await h.deliver_event("topic.notify", {"src": "x"})
        rec = h.assert_published(
            "downstream.echo", payload_contains={"src": "x"}
        )
        assert rec.payload["src"] == "x"


@pytest.mark.asyncio
async def test_harness_assert_published_raises_on_miss():
    from nexus_plugin_sdk import PluginTestHarness

    P, *_ = _build_plugin()
    async with PluginTestHarness(P()) as h:
        await h.deliver_event("topic.notify", {"src": "x"})
        with pytest.raises(AssertionError):
            h.assert_published("nope")
        with pytest.raises(AssertionError):
            h.assert_published(
                "downstream.echo", payload_contains={"src": "WRONG"}
            )


@pytest.mark.asyncio
async def test_harness_disconnect_on_exit():
    from nexus_plugin_sdk import PluginTestHarness

    P, *_ = _build_plugin()
    p = P()
    async with PluginTestHarness(p, tenant_id="t1") as h:
        # _connected flag is private but observable via context access
        assert p.context.tenant_id == "t1"
    # After exit context() raises since we tore down
    from nexus_plugin_sdk import PluginRuntimeError

    with pytest.raises(PluginRuntimeError):
        _ = p.context
