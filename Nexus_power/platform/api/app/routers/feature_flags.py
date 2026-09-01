"""Platform API — Feature flag administration endpoints.

All endpoints require an admin or manager role and operate on the
caller's tenant_id (extracted from the JWT). Mutations carry an
optional ``expected_version`` for optimistic concurrency; without it,
the server uses the current version, which is fine for single-admin
workflows but unsafe under concurrent admin editing.

Production wiring (in platform/api/main.py)::

    from nexus_sdk.feature_flags import FeatureFlagService
    ...
    app.state.feature_flags = FeatureFlagService(
        session_factory=get_session_factory(),
        redis=redis_client,
        audit_sink=audit_to_integration_events_log,
    )
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from nexus_sdk.feature_flags import (
    CircuitState,
    FeatureFlagService,
    FlagState,
    FlagUpdate,
    Mode,
    OptimisticLockError,
    Outcome,
)

from ..auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Feature Flags"], prefix="/api/v1/feature-flags")


# ── Permission helpers ─────────────────────────────────────────


_PRIVILEGED_ROLES = frozenset({"admin", "manager"})


def _require_privileged(user: dict) -> None:
    role = user.get("role", "viewer")
    if role not in _PRIVILEGED_ROLES:
        raise HTTPException(
            status_code=403,
            detail="feature_flag mutations require admin or manager role",
        )


def _service(request: Request) -> FeatureFlagService:
    svc = getattr(request.app.state, "feature_flags", None)
    if svc is None:
        raise HTTPException(
            status_code=503, detail="feature_flag_service_unavailable"
        )
    return svc


# ── DTOs ────────────────────────────────────────────────────────


class FlagStateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    feature_key: str
    enabled: bool
    mode: str
    config: dict
    version: int
    circuit_state: str
    cooldown_until: Optional[str] = None
    effective_mode: str
    enabled_by: Optional[str] = None
    enabled_at: Optional[str] = None
    updated_at: Optional[str] = None


def _to_out(s: FlagState) -> FlagStateOut:
    return FlagStateOut(
        tenant_id=s.tenant_id,
        feature_key=s.feature_key,
        enabled=s.enabled,
        mode=s.mode.value,
        config=s.config,
        version=s.version,
        circuit_state=s.circuit_state.value,
        cooldown_until=s.cooldown_until.isoformat() if s.cooldown_until else None,
        effective_mode=s.effective_mode.value,
        enabled_by=s.enabled_by,
        enabled_at=s.enabled_at.isoformat() if s.enabled_at else None,
        updated_at=s.updated_at.isoformat() if s.updated_at else None,
    )


class SetFlagRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    mode: Optional[Mode] = None
    config: Optional[dict] = None
    expected_version: Optional[int] = Field(default=None, ge=1)


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Mode


class ForceTripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=256)


class RecordOutcomeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Outcome


# ── Endpoints ───────────────────────────────────────────────────


@router.get("", response_model=list[FlagStateOut])
async def list_flags(
    request: Request,
    user: dict = Depends(get_current_user),
) -> list[FlagStateOut]:
    """List all feature flags for the caller's tenant."""
    svc = _service(request)
    states = await svc.list_for_tenant(user["tenant_id"])
    return [_to_out(s) for s in states]


@router.get("/{feature_key}", response_model=FlagStateOut)
async def get_flag(
    feature_key: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> FlagStateOut:
    """Return the current state of one feature flag."""
    svc = _service(request)
    state = await svc.get(user["tenant_id"], feature_key)
    return _to_out(state)


@router.put("/{feature_key}", response_model=FlagStateOut)
async def set_flag(
    feature_key: str,
    body: SetFlagRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> FlagStateOut:
    """Set / patch a feature flag.

    The caller may supply any subset of ``enabled``, ``mode``,
    ``config``. Missing fields are left unchanged. Supply
    ``expected_version`` to enforce optimistic concurrency.
    """
    _require_privileged(user)
    svc = _service(request)
    try:
        state = await svc.set(
            user["tenant_id"],
            feature_key,
            FlagUpdate(
                enabled=body.enabled,
                mode=body.mode,
                config=body.config,
                actor=user["user_id"],
                expected_version=body.expected_version,
            ),
        )
    except OptimisticLockError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "version_conflict",
                "expected": exc.expected,
                "actual": exc.actual,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _to_out(state)


@router.post("/{feature_key}/promote", response_model=FlagStateOut)
async def promote_flag(
    feature_key: str,
    body: PromoteRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> FlagStateOut:
    """Atomically enable + promote the mode of a flag."""
    _require_privileged(user)
    svc = _service(request)
    state = await svc.promote(
        user["tenant_id"], feature_key, body.mode, actor=user["user_id"]
    )
    return _to_out(state)


@router.post("/{feature_key}/disable", response_model=FlagStateOut)
async def disable_flag(
    feature_key: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> FlagStateOut:
    """Disable a flag immediately (master switch off)."""
    _require_privileged(user)
    svc = _service(request)
    state = await svc.disable(
        user["tenant_id"], feature_key, actor=user["user_id"]
    )
    return _to_out(state)


@router.post("/{feature_key}/circuit/trip")
async def force_trip_circuit(
    feature_key: str,
    body: ForceTripRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Force the circuit breaker into OPEN with cooldown."""
    _require_privileged(user)
    svc = _service(request)
    state = await svc.force_trip(
        user["tenant_id"], feature_key, body.reason, actor=user["user_id"]
    )
    return {"feature_key": feature_key, "circuit_state": state.value}


@router.post("/{feature_key}/circuit/reset")
async def reset_circuit(
    feature_key: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Reset the circuit breaker to CLOSED, clear window counters."""
    _require_privileged(user)
    svc = _service(request)
    state = await svc.reset_circuit(
        user["tenant_id"], feature_key, actor=user["user_id"]
    )
    return {"feature_key": feature_key, "circuit_state": state.value}


@router.post("/{feature_key}/outcome")
async def record_outcome(
    feature_key: str,
    body: RecordOutcomeRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Record an outcome for the breaker.

    Service-internal callers should record outcomes directly via the
    service singleton. This endpoint exists for operational tooling
    and tests; the JWT must include an api-role to use it.
    """
    role = user.get("role", "viewer")
    if role not in (_PRIVILEGED_ROLES | {"api"}):
        raise HTTPException(
            status_code=403,
            detail="recording outcomes requires admin/manager/api role",
        )
    svc = _service(request)
    decision = await svc.record_outcome(
        user["tenant_id"], feature_key, body.outcome
    )
    return {
        "feature_key": feature_key,
        "circuit_state": decision.state.value,
        "tripped": decision.tripped,
        "transitioned": decision.transitioned,
        "failure_rate": decision.failure_rate,
        "window_samples": decision.window_samples,
        "cooldown_until": (
            decision.cooldown_until.isoformat()
            if decision.cooldown_until
            else None
        ),
    }
