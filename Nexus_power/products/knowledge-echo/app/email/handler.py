"""Email SurfaceHandler factory."""

from __future__ import annotations

from typing import Optional

from ..surfaces import SurfaceHandler
from .composer import EmailComposer
from .dispatcher import EmailDispatcher, SesOutboundClient
from .installation import EmailInstallationLoader


def build_email_handler(
    *,
    installs: EmailInstallationLoader,
    composer: Optional[EmailComposer] = None,
    ses_client: Optional[SesOutboundClient] = None,
) -> SurfaceHandler:
    return SurfaceHandler(
        surface="email",
        composer=composer or EmailComposer(),
        dispatcher=EmailDispatcher(installs, client=ses_client),
    )
