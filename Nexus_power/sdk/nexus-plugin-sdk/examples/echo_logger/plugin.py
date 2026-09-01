"""Echo Logger plugin implementation."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from nexus_plugin_sdk import (
    ActionInput,
    ActionResult,
    BasePlugin,
    PluginEvent,
    action,
    on_event,
    scheduled,
)


DEFAULT_LOG_FILENAME = "echo_logger.ndjson"


class EchoLoggerPlugin(BasePlugin):
    """Append a JSON line per inbound echo event to a log file.

    Configuration (from ``PluginContext.config``):

      * ``log_path`` — absolute path of the NDJSON log file. When
        absent, the plugin writes to ``<cwd>/echo_logger.ndjson``.
      * ``include_payload`` — if true (default), the full event payload
        is serialised alongside the metadata.
    """

    manifest = str(Path(__file__).with_name("plugin.yaml"))
    plugin_id = "echo_logger"
    plugin_version = "1.0.0"

    async def on_load(self) -> None:
        cfg = self.context.config
        raw_path = cfg.get("log_path")
        self._log_path = Path(raw_path) if raw_path else Path.cwd() / DEFAULT_LOG_FILENAME
        self._include_payload = bool(cfg.get("include_payload", True))
        # Ensure the directory exists; if it doesn't, fail loudly here
        # rather than at the first write.
        parent = self._log_path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self.context.logger.info(
            "echo_logger.loaded",
            extra={"log_path": str(self._log_path)},
        )

    async def on_unload(self) -> None:
        self.context.logger.info("echo_logger.unloaded")

    # ── Event handler ─────────────────────────────────────────

    @on_event("knowledge.echo.dispatched")
    async def on_echo_dispatched(self, event: PluginEvent) -> None:
        """Append a single NDJSON record for the inbound echo."""
        self._append({
            "kind": "event",
            "event_type": event.event_type,
            "tenant_id": event.tenant_id,
            "trace_id": event.trace_id,
            "received_at": event.received_at.isoformat(),
            "payload": event.payload if self._include_payload else None,
            "metadata": event.metadata if self._include_payload else None,
        })

    # ── Action handler ────────────────────────────────────────

    @action("log_echo")
    async def log_echo(self, input_: ActionInput) -> ActionResult:
        params = input_.params or {}
        required = ("echo_id", "question_text")
        missing = [k for k in required if k not in params]
        if missing:
            return ActionResult.failure(
                f"missing required params: {sorted(missing)}"
            )
        record = {
            "kind": "action",
            "action_id": input_.action_id,
            "tenant_id": input_.tenant_id,
            "trace_id": input_.trace_id,
            "idempotency_key": input_.idempotency_key,
            "params": params,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._append(record)
        await self._publish_ack(input_, params["echo_id"])
        return ActionResult.success(
            output={"logged": True, "echo_id": params["echo_id"]},
            external_ref=f"file://{self._log_path}",
        )

    # ── Scheduled handler ─────────────────────────────────────

    @scheduled("*/5 * * * *")
    async def periodic_flush(self) -> None:
        """No-op for the reference plugin; demonstrates the @scheduled
        decorator shape. A real plugin might rotate logs here."""
        self.context.logger.debug("echo_logger.heartbeat")

    # ── Internals ─────────────────────────────────────────────

    async def _publish_ack(self, input_: ActionInput, echo_id: str) -> None:
        """Optionally publish an ACK event back to the platform.

        Wrapped so a missing publish channel doesn't fail the action —
        the harness may not wire one.
        """
        try:
            await self.context.publish(
                "echo_logger.logged",
                {"echo_id": echo_id, "tenant_id": input_.tenant_id},
            )
        except Exception as exc:
            self.context.logger.warning(
                "echo_logger.publish_failed",
                extra={"error": str(exc)},
            )

    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")
