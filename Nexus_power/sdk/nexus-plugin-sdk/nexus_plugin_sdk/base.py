"""Core SDK classes — BasePlugin, PluginContext, PluginEvent.

The runtime instantiates one ``BasePlugin`` subclass per loaded plugin
and drives it through these lifecycle methods:

    1. ``connect(context)``       — once at startup
    2. ``handle_event(event)``    — once per inbound event
    3. ``handle_action(input_)``  — once per outbound action invocation
    4. ``disconnect()``           — at shutdown

The decorators (``@on_event``, ``@action``) register handlers on the
class. The base class collects them at definition time so dispatch is
O(1) without registry maintenance by the plugin author.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# ── Exceptions ─────────────────────────────────────────────────


class PluginRegistrationError(Exception):
    """Raised at class-definition time when a plugin is malformed."""


class PluginRuntimeError(Exception):
    """Wraps handler-level failures so the runtime can record them."""


# ── DTOs ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class PluginEvent:
    """Inbound event delivered to a plugin."""

    event_type: str
    tenant_id: str
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None
    received_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def with_metadata(self, **extras: Any) -> "PluginEvent":
        merged = {**self.metadata, **extras}
        return PluginEvent(
            event_type=self.event_type,
            tenant_id=self.tenant_id,
            payload=self.payload,
            metadata=merged,
            trace_id=self.trace_id,
            received_at=self.received_at,
        )


@dataclass(frozen=True)
class ActionInput:
    """Input to an outbound action handler."""

    action_id: str
    tenant_id: str
    params: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class ActionResult:
    """Result returned by an outbound action handler."""

    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    external_ref: Optional[str] = None

    @classmethod
    def success(
        cls,
        output: Optional[dict[str, Any]] = None,
        *,
        external_ref: Optional[str] = None,
    ) -> "ActionResult":
        return cls(ok=True, output=output or {}, external_ref=external_ref)

    @classmethod
    def failure(
        cls,
        error: str,
        output: Optional[dict[str, Any]] = None,
    ) -> "ActionResult":
        return cls(ok=False, output=output or {}, error=error)


# ── Runtime context ────────────────────────────────────────────


PublishCallable = Callable[[str, dict[str, Any]], Awaitable[None]]


class PluginContext:
    """What the runtime supplies to each plugin at connect time."""

    def __init__(
        self,
        *,
        plugin_id: str,
        plugin_version: str,
        tenant_id: Optional[str],
        config: Optional[dict[str, Any]] = None,
        publish: Optional[PublishCallable] = None,
        logger_obj: Optional[logging.Logger] = None,
    ):
        self.plugin_id = plugin_id
        self.plugin_version = plugin_version
        self.tenant_id = tenant_id
        self.config: dict[str, Any] = dict(config or {})
        self._publish = publish
        self.logger = logger_obj or logging.getLogger(
            f"nexus_plugin.{plugin_id}"
        )

    async def publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Emit an event onto the platform event bus.

        Raises ``PluginRuntimeError`` when the runtime didn't wire a
        publish callable (e.g. in a test harness without an event bus).
        """
        if self._publish is None:
            raise PluginRuntimeError(
                f"plugin {self.plugin_id} attempted to publish "
                f"{event_type!r} but no publish channel is wired"
            )
        await self._publish(event_type, payload)


# ── Decorators (class-level handler registration) ─────────────


def _attach_meta(
    fn: Callable[..., Any], *, kind: str, key: str, **extras: Any
) -> Callable[..., Any]:
    meta = getattr(fn, "__nexus_plugin_meta__", None)
    if meta is None:
        meta = {}
        fn.__nexus_plugin_meta__ = meta  # type: ignore[attr-defined]
    if kind in meta:
        raise PluginRegistrationError(
            f"handler {fn.__name__!r} is already registered as {kind}={meta[kind]!r}"
        )
    meta[kind] = key
    meta.setdefault("extras", {}).update(extras)
    return fn


# ── BasePlugin ─────────────────────────────────────────────────


class BasePlugin:
    """Subclass to write a plugin.

    Class-level attributes:
        manifest        — optional ``plugin.yaml`` path (relative or absolute)
        plugin_id       — overrides the manifest's plugin id; usually
                          left blank so the manifest is the source of truth.

    Lifecycle methods (override as needed):
        async def on_load(self): ...
        async def on_unload(self): ...
    """

    manifest: Optional[str] = None
    plugin_id: Optional[str] = None
    plugin_version: Optional[str] = None

    # Populated by ``__init_subclass__``.
    _event_handlers: dict[str, str]
    _action_handlers: dict[str, str]
    _scheduled_handlers: list[tuple[str, str]]  # (cron, method_name)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        event_handlers: dict[str, str] = {}
        action_handlers: dict[str, str] = {}
        scheduled_handlers: list[tuple[str, str]] = []
        for name, value in inspect.getmembers(cls, callable):
            meta = getattr(value, "__nexus_plugin_meta__", None)
            if not meta:
                continue
            if "event" in meta:
                event_type = meta["event"]
                if event_type in event_handlers:
                    raise PluginRegistrationError(
                        f"duplicate @on_event handler for {event_type!r}"
                    )
                event_handlers[event_type] = name
            if "action" in meta:
                action_id = meta["action"]
                if action_id in action_handlers:
                    raise PluginRegistrationError(
                        f"duplicate @action handler for {action_id!r}"
                    )
                action_handlers[action_id] = name
            if "schedule" in meta:
                scheduled_handlers.append((meta["schedule"], name))
        cls._event_handlers = event_handlers
        cls._action_handlers = action_handlers
        cls._scheduled_handlers = scheduled_handlers

    def __init__(self) -> None:
        self._context: Optional[PluginContext] = None
        self._connected = False

    # ── Lifecycle ──────────────────────────────────────────────

    async def connect(self, context: PluginContext) -> None:
        if self._connected:
            return
        self._context = context
        try:
            await self.on_load()
        except Exception as exc:
            raise PluginRuntimeError(
                f"on_load failed for {self.plugin_id or type(self).__name__}: {exc}"
            ) from exc
        self._connected = True

    async def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            await self.on_unload()
        finally:
            self._connected = False
            self._context = None

    async def on_load(self) -> None:
        """Override for plugin-specific setup."""

    async def on_unload(self) -> None:
        """Override for plugin-specific cleanup."""

    # ── Dispatch ───────────────────────────────────────────────

    async def handle_event(self, event: PluginEvent) -> None:
        method_name = self._event_handlers.get(event.event_type)
        if method_name is None:
            return  # silently ignore unsubscribed event types
        method = getattr(self, method_name)
        await self._invoke(method, event)

    async def handle_action(self, input_: ActionInput) -> ActionResult:
        method_name = self._action_handlers.get(input_.action_id)
        if method_name is None:
            return ActionResult.failure(
                f"unknown action: {input_.action_id!r}"
            )
        method = getattr(self, method_name)
        return await self._invoke(method, input_)

    def list_event_types(self) -> list[str]:
        return sorted(self._event_handlers.keys())

    def list_action_ids(self) -> list[str]:
        return sorted(self._action_handlers.keys())

    def list_scheduled(self) -> list[tuple[str, str]]:
        return list(self._scheduled_handlers)

    @property
    def context(self) -> PluginContext:
        if self._context is None:
            raise PluginRuntimeError(
                f"{type(self).__name__} accessed context before connect()"
            )
        return self._context

    # ── Internals ──────────────────────────────────────────────

    async def _invoke(
        self,
        method: Callable[..., Any],
        argument: Any,
    ) -> Any:
        if not self._connected:
            raise PluginRuntimeError(
                f"{type(self).__name__} invoked before connect()"
            )
        try:
            result = method(argument)
            if inspect.isawaitable(result):
                result = await result
            return result
        except PluginRuntimeError:
            raise
        except Exception as exc:
            raise PluginRuntimeError(
                f"handler {method.__name__!r} failed: {exc}"
            ) from exc
