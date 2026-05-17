"""FastAPI dependency factory for gating routes behind a feature flag.

Usage::

    from fastapi import Depends
    from nexus_sdk.feature_flags import require_feature, Mode

    @router.post("/api/v1/echo/post")
    async def post_echo(
        ctx = Depends(require_feature("knowledge_echo", min_mode=Mode.LIVE)),
        user: NexusUser = Depends(get_current_user),
    ):
        # ctx.state is a FlagState; ctx.mode is the effective mode.
        ...

The dependency raises ``HTTPException(503)`` with a structured detail
when the gate denies access — distinct codes for disabled vs. circuit
open so observability can split them.

The service instance is resolved from ``request.app.state.feature_flags``;
applications must wire this at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request

from .models import CircuitState, FlagState, Mode
from .service import FeatureFlagService


@dataclass(frozen=True)
class FeatureContext:
    """Result of a successful ``require_feature`` resolution."""

    state: FlagState
    mode: Mode

    @property
    def is_shadow(self) -> bool:
        return self.mode == Mode.SHADOW


def require_feature(
    feature_key: str,
    *,
    min_mode: Mode = Mode.SHADOW,
    tenant_attr: str = "tenant_id",
):
    """Return a FastAPI dependency that gates a route.

    Parameters
    ----------
    feature_key
        The flag key (e.g., ``"knowledge_echo"``).
    min_mode
        The minimum effective mode required. Defaults to SHADOW which
        permits even shadow-mode requests through (useful for endpoints
        that *should* run during shadow validation).
    tenant_attr
        Attribute name on ``request.state.user`` that holds the tenant id.
        Defaults to ``"tenant_id"`` matching the existing JWT payload
        produced by ``platform.api.app.auth``.
    """

    async def _dep(request: Request) -> FeatureContext:
        service: Optional[FeatureFlagService] = getattr(
            request.app.state, "feature_flags", None
        )
        if service is None:
            raise HTTPException(
                status_code=500,
                detail="feature_flag_service_not_configured",
            )

        user = getattr(request.state, "user", None)
        if not isinstance(user, dict) or tenant_attr not in user:
            raise HTTPException(
                status_code=401,
                detail="authenticated_tenant_required",
            )
        tenant_id = user[tenant_attr]

        state = await service.get(tenant_id, feature_key)

        if not state.enabled:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "feature_disabled",
                    "feature_key": feature_key,
                },
            )
        if state.circuit_state == CircuitState.OPEN:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "feature_circuit_open",
                    "feature_key": feature_key,
                    "cooldown_until": (
                        state.cooldown_until.isoformat()
                        if state.cooldown_until
                        else None
                    ),
                },
            )
        if not state.allows(min_mode):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "feature_mode_below_required",
                    "feature_key": feature_key,
                    "current_mode": state.effective_mode.value,
                    "required_mode": min_mode.value,
                },
            )

        return FeatureContext(state=state, mode=state.effective_mode)

    return _dep
