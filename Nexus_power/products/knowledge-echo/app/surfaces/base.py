"""Surface protocols, DTOs, and the registry.

This module defines the boundary between the surface-agnostic
orchestrator and the surface-specific composer + dispatcher
implementations. Every surface module in ``app/<surface>/`` provides
exactly one ``SurfaceHandler`` and registers it at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from ..matcher import MatchResult


# ── Exceptions ─────────────────────────────────────────────────


class SurfaceError(Exception):
    """A surface refused or failed to dispatch."""


class SurfaceUnavailable(SurfaceError):
    """The surface's installation/credentials are missing or invalid."""


# ── DTOs ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ComposedPayload:
    """Surface-neutral payload envelope.

    ``surface``                — the surface id (``"slack"``, ``"teams"``…).
    ``text``                   — plain-text fallback for previews / audit.
    ``payload``                — the surface-specific structured payload
                                  (Block Kit blocks, Adaptive Card body,
                                  HTML email body, generic JSON, etc.).
    ``payload_hash``           — deterministic SHA-256 of the payload, for
                                  audit + replay detection.
    ``similarity_pct``         — the top candidate's similarity, rounded.
    ``primary_candidate``      — dict of fields the orchestrator uses for
                                  logging / linking.
    """

    surface: str
    text: str
    payload: dict[str, Any]
    payload_hash: str
    similarity_pct: int
    primary_candidate: dict[str, Any]


@dataclass(frozen=True)
class DispatchOutcome:
    """What happened when the surface tried to deliver the echo."""

    decision: str            # "posted_channel" | "posted_dm" | "shadow_logged"
    message_ref: Optional[str]
    raw: dict[str, Any]


# ── Protocols ──────────────────────────────────────────────────


@runtime_checkable
class SurfaceComposer(Protocol):
    """Build a ``ComposedPayload`` for a surface from a MatchResult."""

    def compose(
        self,
        *,
        dispatch_id: str,
        question_text: str,
        match: MatchResult,
    ) -> Optional[ComposedPayload]: ...


@runtime_checkable
class SurfaceDispatcher(Protocol):
    """Send a ComposedPayload through the surface's transport.

    The dispatcher decides between channel-post and DM-equivalent based
    on the ``as_dm`` flag; ``is_live`` is provided as additional context
    for surfaces that need it (e.g., a webhook surface may want to
    suppress the call entirely in non-live modes — that's the
    dispatcher's call).
    """

    async def dispatch(
        self,
        *,
        tenant_id: str,
        payload: ComposedPayload,
        as_dm: bool,
        is_live: bool,
        user_id_ext: Optional[str],
        channel_id_ext: Optional[str],
        thread_ts: Optional[str],
    ) -> DispatchOutcome: ...


# ── SurfaceHandler ─────────────────────────────────────────────


@dataclass(frozen=True)
class SurfaceHandler:
    """Bundle of (composer, dispatcher) for one surface."""

    surface: str
    composer: SurfaceComposer
    dispatcher: SurfaceDispatcher

    def __post_init__(self) -> None:
        if not isinstance(self.surface, str) or not self.surface.strip():
            raise ValueError("surface must be a non-empty string")


# ── Registry ───────────────────────────────────────────────────


class SurfaceRegistry:
    """Maps ``trigger_surface`` → ``SurfaceHandler``.

    Construction-time validation rejects duplicate or malformed
    registrations; the registry is otherwise immutable for the
    lifetime of the orchestrator.
    """

    def __init__(self, handlers: list[SurfaceHandler]):
        store: dict[str, SurfaceHandler] = {}
        for h in handlers:
            key = h.surface.lower()
            if key in store:
                raise ValueError(
                    f"duplicate surface handler for {key!r}"
                )
            store[key] = h
        if not store:
            raise ValueError("SurfaceRegistry requires at least one handler")
        self._handlers = store

    def get(self, surface: str) -> SurfaceHandler:
        key = (surface or "").lower()
        h = self._handlers.get(key)
        if h is None:
            raise SurfaceUnavailable(f"no handler for surface={surface!r}")
        return h

    def has(self, surface: str) -> bool:
        return (surface or "").lower() in self._handlers

    def known_surfaces(self) -> list[str]:
        return sorted(self._handlers.keys())
