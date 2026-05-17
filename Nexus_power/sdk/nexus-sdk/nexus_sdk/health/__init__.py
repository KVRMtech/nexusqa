"""
Nexus Health Checks — Standard health & readiness endpoints.

Every engine automatically gets:
- GET /health         → Basic liveness (is the process running?)
- GET /health/ready   → Readiness (are dependencies connected?)
- GET /health/detail  → Detailed status of all dependencies

These are used by:
- Docker health checks
- Kubernetes liveness/readiness probes
- Monitoring dashboards
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Callable, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

__all__ = [
    "DependencyStatus",
    "HealthResponse",
    "HealthCheck",
]


class DependencyStatus(BaseModel):
    name: str
    status: str  # healthy, unhealthy, degraded
    latency_ms: Optional[float] = None
    message: Optional[str] = None


class HealthResponse(BaseModel):
    status: str  # healthy, degraded, unhealthy
    engine: str
    version: str
    uptime_seconds: float
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    dependencies: list[DependencyStatus] = Field(default_factory=list)
    modes: dict[str, str] = Field(
        default_factory=dict,
        description="Component → mode (e.g. 'llm': 'ollama', 'graph': 'neo4j', 'llm': 'stub')",
    )


class HealthCheck:
    """
    Manages health checks for an engine.
    
    Usage:
        health = HealthCheck(engine_name="ears", version="0.1.0")
        
        # Register dependency checks
        health.add_check("redis", check_redis_connection)
        health.add_check("gpu", check_gpu_available)

        # Register component modes (stub vs real)
        health.set_mode("llm", "ollama")
        health.set_mode("graph_store", "in-memory")
        
        # Include routes in FastAPI app
        app.include_router(health.router)
    """

    def __init__(self, engine_name: str, version: str):
        self.engine_name = engine_name
        self.version = version
        self._start_time = datetime.now(timezone.utc)
        self._checks: dict[str, Callable] = {}
        self._modes: dict[str, str] = {}
        self.router = APIRouter(tags=["health"])
        self._setup_routes()

    def add_check(self, name: str, check_fn: Callable[[], Any]) -> None:
        """Register a dependency health check function."""
        self._checks[name] = check_fn

    def set_mode(self, component: str, mode: str) -> None:
        """Record whether a component is using a real backend or a stub."""
        self._modes[component] = mode

    # Modes that indicate a component is not running with real production capability.
    _DEGRADED_MODE_VALUES = frozenset({
        "stub", "in-memory", "degraded", "unavailable", "heuristic", "passthrough",
    })

    def _compute_status(self) -> str:
        """Derive overall status from registered modes.

        Returns 'degraded' if any mode value signals a non-production
        fallback (stub, in-memory, unavailable, etc.), 'healthy' otherwise.
        """
        for _component, mode_value in self._modes.items():
            if mode_value.lower() in self._DEGRADED_MODE_VALUES:
                return "degraded"
        return "healthy"

    def _setup_routes(self) -> None:
        @self.router.get("/health", response_model=HealthResponse)
        async def health():
            """Basic liveness check."""
            uptime = (datetime.now(timezone.utc) - self._start_time).total_seconds()
            return HealthResponse(
                status=self._compute_status(),
                engine=self.engine_name,
                version=self.version,
                uptime_seconds=uptime,
                modes=dict(self._modes),
            )

        @self.router.get("/health/ready", response_model=HealthResponse)
        async def readiness():
            """Readiness check — verifies all dependencies are connected."""
            uptime = (datetime.now(timezone.utc) - self._start_time).total_seconds()
            deps = []
            overall_status = "healthy"

            for name, check_fn in self._checks.items():
                try:
                    import time
                    start = time.monotonic()
                    result = check_fn()
                    # Handle async check functions
                    if hasattr(result, "__await__"):
                        result = await result
                    latency = (time.monotonic() - start) * 1000

                    deps.append(DependencyStatus(
                        name=name,
                        status="healthy",
                        latency_ms=round(latency, 2),
                        message=str(result) if result else None,
                    ))
                except Exception as e:
                    deps.append(DependencyStatus(
                        name=name,
                        status="unhealthy",
                        message=str(e),
                    ))
                    overall_status = "unhealthy"

            return HealthResponse(
                status=overall_status,
                engine=self.engine_name,
                version=self.version,
                uptime_seconds=uptime,
                dependencies=deps,
                modes=dict(self._modes),
            )

        @self.router.get("/health/detail", response_model=HealthResponse)
        async def detailed_health():
            """Detailed health check — same as readiness but always returns all info."""
            return await readiness()
