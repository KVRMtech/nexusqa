"""Teams SurfaceHandler factory."""

from __future__ import annotations

from typing import Optional

from ..surfaces import SurfaceHandler
from .composer import TeamsComposer
from .dispatcher import TeamsDispatcher, TeamsOutboundClient
from .installation import TeamsInstallationLoader


def build_teams_handler(
    *,
    installs: TeamsInstallationLoader,
    composer: Optional[TeamsComposer] = None,
    outbound: Optional[TeamsOutboundClient] = None,
) -> SurfaceHandler:
    return SurfaceHandler(
        surface="teams",
        composer=composer or TeamsComposer(),
        dispatcher=TeamsDispatcher(installs, client=outbound),
    )
