"""
API Gateway — App Sub-packages.

Modular components for the Nexus API Gateway:
  - config       : GatewayConfig
  - routes       : Route table builder
  - rate_limiter : Sliding-window per-tenant rate limiter
  - proxy        : Reverse-proxy request handler
"""

from .config import GatewayConfig
from .routes import build_route_table
from .rate_limiter import RateLimiter
from .redis_rate_limiter import RedisRateLimiter
from .proxy import proxy_request

__all__ = [
    "GatewayConfig",
    "build_route_table",
    "RateLimiter",
    "RedisRateLimiter",
    "proxy_request",
]
