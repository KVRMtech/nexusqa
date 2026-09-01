"""Surface abstraction — Composer + Dispatcher + Registry.

A *surface* is any external system that can deliver an inbound
question and accept an outbound echo. Slack, Teams, Email, and
generic Webhook are all surfaces. Each is implemented as a
``SurfaceHandler`` that bundles:

    * a ``SurfaceComposer``  — turns ``MatchResult`` into a surface-
      specific payload (Block Kit, Adaptive Card, HTML email, JSON…)
    * a ``SurfaceDispatcher`` — sends the payload via the surface's
      transport in the right mode (channel vs DM-equivalent).

The orchestrator picks the handler from the ``SurfaceRegistry`` based
on the inbound message's ``trigger_surface``. Adding a fifth surface
is a manifest + module pair; the orchestrator never changes.
"""

from __future__ import annotations

from .base import (
    ComposedPayload,
    DispatchOutcome,
    SurfaceComposer,
    SurfaceDispatcher,
    SurfaceError,
    SurfaceHandler,
    SurfaceRegistry,
    SurfaceUnavailable,
)

__all__ = [
    "ComposedPayload",
    "DispatchOutcome",
    "SurfaceComposer",
    "SurfaceDispatcher",
    "SurfaceError",
    "SurfaceHandler",
    "SurfaceRegistry",
    "SurfaceUnavailable",
]
