"""SurfaceRegistry / SurfaceHandler basics."""

from __future__ import annotations

from typing import Optional

import pytest

from app.matcher import MatchResult
from app.surfaces import (
    ComposedPayload,
    DispatchOutcome,
    SurfaceHandler,
    SurfaceRegistry,
    SurfaceUnavailable,
)


class _NullComposer:
    def compose(self, **kwargs):  # noqa: ARG002
        return None


class _NullDispatcher:
    async def dispatch(self, **kwargs):  # noqa: ARG002
        return DispatchOutcome(decision="shadow_logged", message_ref=None, raw={})


def _handler(name: str) -> SurfaceHandler:
    return SurfaceHandler(
        surface=name, composer=_NullComposer(), dispatcher=_NullDispatcher()
    )


def test_registry_lookup_by_surface_name() -> None:
    reg = SurfaceRegistry([_handler("slack"), _handler("teams")])
    assert reg.get("slack").surface == "slack"
    assert reg.get("TEAMS").surface == "teams"
    assert reg.has("Slack")
    assert reg.known_surfaces() == ["slack", "teams"]


def test_registry_rejects_unknown_surface() -> None:
    reg = SurfaceRegistry([_handler("slack")])
    with pytest.raises(SurfaceUnavailable):
        reg.get("teams")


def test_registry_rejects_duplicates() -> None:
    with pytest.raises(ValueError):
        SurfaceRegistry([_handler("slack"), _handler("slack")])


def test_registry_rejects_empty() -> None:
    with pytest.raises(ValueError):
        SurfaceRegistry([])


def test_surface_handler_rejects_blank_name() -> None:
    with pytest.raises(ValueError):
        SurfaceHandler(surface="   ", composer=_NullComposer(), dispatcher=_NullDispatcher())
