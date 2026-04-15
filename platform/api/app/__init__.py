"""
Platform API — App Sub-packages.

Modular components for the Nexus Platform API:
  - config      : PlatformAPIConfig (all env settings)
  - database    : Async SQLAlchemy setup, helpers
  - auth        : JWT validation, middleware
  - middleware   : Security headers
  - routers/     : Domain-specific route modules
      sessions, sme, contradictions, guardrails,
      traceability, tests, data_forge, compliance,
      insights, admin
"""

from .config import PlatformAPIConfig
from .database import (
    init_db,
    close_db,
    get_session_factory,
    is_db_connected,
    require_db,
    new_id,
    utc_now,
    row_to_dict,
)
from .auth import get_current_user, jwt_auth_middleware, PUBLIC_PATHS
from .middleware import security_headers_middleware

__all__ = [
    "PlatformAPIConfig",
    "init_db",
    "close_db",
    "get_session_factory",
    "is_db_connected",
    "require_db",
    "new_id",
    "utc_now",
    "row_to_dict",
    "get_current_user",
    "jwt_auth_middleware",
    "PUBLIC_PATHS",
    "security_headers_middleware",
]
