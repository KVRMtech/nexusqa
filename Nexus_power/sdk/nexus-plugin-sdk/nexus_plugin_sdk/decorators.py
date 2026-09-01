"""Decorators that register handler methods on a ``BasePlugin`` subclass.

Each decorator stamps a ``__nexus_plugin_meta__`` dict onto the method;
``BasePlugin.__init_subclass__`` collects them at class creation time.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .base import PluginRegistrationError, _attach_meta


_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.\-]{0,127}$")


def _validate_id(value: str, *, kind: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        raise PluginRegistrationError(
            f"{kind} identifier {value!r} must match {_ID_PATTERN.pattern}"
        )
    return value


def on_event(event_type: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a method as the handler for an inbound event type.

    The decorated method receives a single ``PluginEvent`` argument and
    may be sync or async; the runtime awaits both.
    """
    _validate_id(event_type, kind="event_type")

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        return _attach_meta(fn, kind="event", key=event_type)

    return _wrap


def action(action_id: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a method as the outbound action handler.

    The decorated method receives an ``ActionInput`` and must return
    an ``ActionResult``.
    """
    _validate_id(action_id, kind="action_id")

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        return _attach_meta(fn, kind="action", key=action_id)

    return _wrap


def scheduled(cron: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a method to run on a cron schedule.

    The cron string is opaque to the SDK — the runtime parses it.
    """
    if not isinstance(cron, str) or not cron.strip():
        raise PluginRegistrationError(
            "scheduled() requires a non-empty cron expression"
        )

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        return _attach_meta(fn, kind="schedule", key=cron.strip())

    return _wrap
