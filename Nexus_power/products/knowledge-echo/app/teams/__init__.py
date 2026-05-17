"""Microsoft Teams surface — Source + Surface via Bot Framework.

Inbound: Bot Framework Connector POSTs activities to our webhook
URL with a JWT in the ``Authorization`` header signed by Microsoft.
``app/teams/auth.py`` validates the JWT against the Bot Framework's
OpenID metadata + JWKS.

Outbound: replies are POSTed back to the activity's ``serviceUrl``
with an OAuth2 client-credentials access token from Microsoft's
login endpoint. ``app/teams/dispatcher.py`` mints + caches that token
per-tenant.

Adaptive Card v1.4 is the rendering format Teams understands; the
composer produces a Bot Framework activity envelope around it.
"""

from __future__ import annotations

from .activity import (
    ParsedTeamsActivity,
    TeamsActivityKind,
    parse_teams_activity,
)
from .auth import (
    TeamsAuthError,
    TeamsTokenVerifier,
    TeamsMetadataLoader,
)
from .composer import TeamsComposer
from .dispatcher import (
    TeamsDispatcher,
    TeamsDispatchError,
    TeamsOutboundClient,
)
from .installation import (
    TeamsInstallation,
    TeamsInstallationError,
    TeamsInstallationLoader,
)
from .handler import build_teams_handler

__all__ = [
    "ParsedTeamsActivity",
    "TeamsActivityKind",
    "TeamsAuthError",
    "TeamsComposer",
    "TeamsDispatchError",
    "TeamsDispatcher",
    "TeamsInstallation",
    "TeamsInstallationError",
    "TeamsInstallationLoader",
    "TeamsMetadataLoader",
    "TeamsOutboundClient",
    "TeamsTokenVerifier",
    "build_teams_handler",
    "parse_teams_activity",
]
