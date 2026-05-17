"""Nexus Plugin SDK — write integration plugins for the Nexus platform.

Quick start::

    from nexus_plugin_sdk import BasePlugin, on_event, action

    class AcmeHrPlugin(BasePlugin):
        manifest = "plugin.yaml"

        @on_event("hr.policy.updated")
        async def on_policy(self, event):
            await self.publish("knowledge.policy_change", event.payload)

        @action("send_to_hr_portal")
        async def push(self, params):
            return {"ok": True, "ref": "..."}

The runtime calls ``BasePlugin.connect()`` once, then dispatches each
inbound event to a matching ``@on_event`` handler and each outbound
action invocation to a matching ``@action`` handler. The plugin author
writes pure async functions; the SDK handles dispatch, logging,
manifest validation, and the test harness.

Public surface:
    * ``BasePlugin``           — subclass it, declare manifest path
    * ``on_event`` / ``action`` / ``scheduled`` — decorators
    * ``PluginEvent``          — typed input passed to event handlers
    * ``ActionInput`` /
      ``ActionResult``         — handler IO
    * ``PluginContext``        — runtime context (HTTP client, logger,
                                  outbound publish callable)
    * ``PluginTestHarness``    — drive a plugin from tests without a bus
    * ``load_manifest``        — re-exported from ``nexus_sdk.integrations``
"""

from __future__ import annotations

from .base import (
    ActionInput,
    ActionResult,
    BasePlugin,
    PluginContext,
    PluginEvent,
    PluginRegistrationError,
    PluginRuntimeError,
)
from .decorators import action, on_event, scheduled
from .harness import PluginTestHarness, RecordedAction, RecordedPublish

__all__ = [
    "ActionInput",
    "ActionResult",
    "BasePlugin",
    "PluginContext",
    "PluginEvent",
    "PluginRegistrationError",
    "PluginRuntimeError",
    "PluginTestHarness",
    "RecordedAction",
    "RecordedPublish",
    "action",
    "on_event",
    "scheduled",
]


# Re-export the manifest loader so plugin authors don't need to depend on
# ``nexus_sdk.integrations`` separately. Defer the import so the SDK can
# be installed in environments where the nexus_sdk is not present, with
# a clear error.
try:
    from nexus_sdk.integrations import load_manifest as load_manifest  # noqa: F401
except ImportError:  # pragma: no cover — diagnostic message only

    def load_manifest(*_args, **_kwargs):  # type: ignore[no-redef]
        raise ImportError(
            "nexus_sdk.integrations is required to load manifests. "
            "Install the nexus-sdk package alongside nexus-plugin-sdk."
        )
