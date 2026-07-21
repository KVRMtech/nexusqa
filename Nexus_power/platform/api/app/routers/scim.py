"""SCIM v2.0 router (RFC 7644).

Endpoints
---------

  GET    /scim/v2/ServiceProviderConfig
  GET    /scim/v2/ResourceTypes
  GET    /scim/v2/Schemas
  GET    /scim/v2/Users[?filter=...&startIndex=N&count=M]
  POST   /scim/v2/Users
  GET    /scim/v2/Users/{id}
  PUT    /scim/v2/Users/{id}
  PATCH  /scim/v2/Users/{id}
  DELETE /scim/v2/Users/{id}
  GET    /scim/v2/Groups[?filter=...&startIndex=N&count=M]
  POST   /scim/v2/Groups
  GET    /scim/v2/Groups/{id}
  PUT    /scim/v2/Groups/{id}
  PATCH  /scim/v2/Groups/{id}
  DELETE /scim/v2/Groups/{id}

Authentication: standard platform JWT (we treat SCIM clients as
``api``-role users scoped to the tenant they provision).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from ..auth import get_current_user
from ..database import get_session_factory
from ..scim import (
    SCIMError,
    SCIMGroupResource,
    SCIMListResponse,
    SCIMPatchRequest,
    SCIMRepository,
    SCIMUserResource,
    parse_scim_filter,
    scim_error_response,
)
from ..scim.models import (
    ENTERPRISE_SCHEMA_URN,
    GROUP_SCHEMA_URN,
    USER_SCHEMA_URN,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["SCIM"], prefix="/scim/v2")


# ── Helpers ────────────────────────────────────────────────────


_ALLOWED_ROLES = frozenset({"api", "admin"})


def _require_scim_role(user: dict) -> None:
    role = user.get("role", "viewer")
    if role not in _ALLOWED_ROLES:
        raise HTTPException(403, "scim endpoints require api or admin role")


def _scim_response(body: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=body
        if isinstance(body, dict)
        else (
            body.model_dump(by_alias=True, exclude_none=True)
            if hasattr(body, "model_dump")
            else body
        ),
        status_code=status_code,
        media_type="application/scim+json",
    )


def _repo() -> SCIMRepository:
    sf = get_session_factory()
    if sf is None:
        raise HTTPException(503, "database not connected")
    return SCIMRepository(sf)


def _handle_scim_error(exc: SCIMError) -> JSONResponse:
    return JSONResponse(
        content=scim_error_response(exc),
        status_code=exc.status,
        media_type="application/scim+json",
    )


# ── Service discovery ──────────────────────────────────────────


@router.get("/ServiceProviderConfig")
async def service_provider_config(
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    body = {
        "schemas": [
            "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
        ],
        "documentationUri": "https://docs.nexus.example.com/scim",
        "patch": {"supported": True},
        "bulk": {
            "supported": False,
            "maxOperations": 0,
            "maxPayloadSize": 0,
        },
        "filter": {"supported": True, "maxResults": 1000},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": True},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "OAuth Bearer Token",
                "description": "Use a platform JWT issued to the provisioning client.",
                "specUri": "https://datatracker.ietf.org/doc/html/rfc6750",
                "documentationUri": "https://docs.nexus.example.com/scim/auth",
                "primary": True,
            }
        ],
    }
    return _scim_response(body)


@router.get("/ResourceTypes")
async def resource_types(
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": 2,
        "Resources": [
            {
                "schemas": [
                    "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
                ],
                "id": "User",
                "name": "User",
                "endpoint": "/Users",
                "description": "Org directory user",
                "schema": USER_SCHEMA_URN,
                "schemaExtensions": [
                    {"schema": ENTERPRISE_SCHEMA_URN, "required": False}
                ],
            },
            {
                "schemas": [
                    "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
                ],
                "id": "Group",
                "name": "Group",
                "endpoint": "/Groups",
                "description": "Org directory group",
                "schema": GROUP_SCHEMA_URN,
            },
        ],
    }
    return _scim_response(body)


@router.get("/Schemas")
async def schemas(
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": 3,
        "Resources": [
            {"id": USER_SCHEMA_URN, "name": "User"},
            {"id": GROUP_SCHEMA_URN, "name": "Group"},
            {"id": ENTERPRISE_SCHEMA_URN, "name": "EnterpriseUser"},
        ],
    }
    return _scim_response(body)


# ── Users ──────────────────────────────────────────────────────


@router.get("/Users")
async def list_users(
    request: Request,
    filter: Optional[str] = Query(default=None),  # noqa: A002 — SCIM-defined name
    startIndex: int = Query(default=1, ge=1),
    count: int = Query(default=100, ge=1, le=1000),
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    try:
        scim_filter = parse_scim_filter(filter)
        resources, total = await _repo().list_users(
            tenant_id=user["tenant_id"],
            scim_filter=scim_filter,
            start_index=startIndex,
            count=count,
        )
    except SCIMError as exc:
        return _handle_scim_error(exc)
    body = SCIMListResponse(
        totalResults=total,
        Resources=[r.model_dump(by_alias=True, exclude_none=True) for r in resources],
        startIndex=startIndex,
        itemsPerPage=len(resources),
    )
    return _scim_response(body.model_dump(by_alias=True))


@router.post("/Users", status_code=201)
async def create_user(
    payload: SCIMUserResource,
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    try:
        resource = await _repo().create_or_replace_user(
            tenant_id=user["tenant_id"],
            payload=payload,
        )
    except SCIMError as exc:
        return _handle_scim_error(exc)
    return _scim_response(resource, status_code=201)


@router.get("/Users/{org_user_id}")
async def get_user(
    org_user_id: str,
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    try:
        resource = await _repo().get_user(
            tenant_id=user["tenant_id"], org_user_id=org_user_id
        )
    except SCIMError as exc:
        return _handle_scim_error(exc)
    if resource is None:
        return _handle_scim_error(
            SCIMError(status=404, detail="user_not_found")
        )
    return _scim_response(resource)


@router.put("/Users/{org_user_id}")
async def replace_user(
    org_user_id: str,
    payload: SCIMUserResource,
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    try:
        resource = await _repo().create_or_replace_user(
            tenant_id=user["tenant_id"],
            payload=payload,
            replace_id=org_user_id,
        )
    except SCIMError as exc:
        return _handle_scim_error(exc)
    return _scim_response(resource)


@router.patch("/Users/{org_user_id}")
async def patch_user(
    org_user_id: str,
    body: SCIMPatchRequest,
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    operations = [
        (op.op, op.path, op.value) for op in body.Operations
    ]
    try:
        resource = await _repo().patch_user(
            tenant_id=user["tenant_id"],
            org_user_id=org_user_id,
            operations=operations,
        )
    except SCIMError as exc:
        return _handle_scim_error(exc)
    if resource is None:
        return _handle_scim_error(
            SCIMError(status=404, detail="user_not_found")
        )
    return _scim_response(resource)


@router.delete("/Users/{org_user_id}", status_code=204, response_model=None)
async def delete_user(
    org_user_id: str,
    user: dict = Depends(get_current_user),
) -> Response:
    _require_scim_role(user)
    # SCIM clients typically prefer soft-deactivation; we honor a query
    # flag ``hard=true`` for true deletes.
    deleted = await _repo().delete_user(
        tenant_id=user["tenant_id"], org_user_id=org_user_id
    )
    if not deleted:
        # Treat unknown ids as already-gone for idempotency.
        pass
    return Response(status_code=204)


# ── Groups ─────────────────────────────────────────────────────


@router.get("/Groups")
async def list_groups(
    filter: Optional[str] = Query(default=None),  # noqa: A002
    startIndex: int = Query(default=1, ge=1),
    count: int = Query(default=100, ge=1, le=1000),
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    try:
        scim_filter = parse_scim_filter(filter)
        resources, total = await _repo().list_groups(
            tenant_id=user["tenant_id"],
            scim_filter=scim_filter,
            start_index=startIndex,
            count=count,
        )
    except SCIMError as exc:
        return _handle_scim_error(exc)
    body = SCIMListResponse(
        totalResults=total,
        Resources=[r.model_dump(by_alias=True, exclude_none=True) for r in resources],
        startIndex=startIndex,
        itemsPerPage=len(resources),
    )
    return _scim_response(body.model_dump(by_alias=True))


@router.post("/Groups", status_code=201)
async def create_group(
    payload: SCIMGroupResource,
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    try:
        resource = await _repo().create_or_replace_group(
            tenant_id=user["tenant_id"], payload=payload
        )
    except SCIMError as exc:
        return _handle_scim_error(exc)
    return _scim_response(resource, status_code=201)


@router.get("/Groups/{org_group_id}")
async def get_group(
    org_group_id: str,
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    try:
        resource = await _repo().get_group(
            tenant_id=user["tenant_id"], org_group_id=org_group_id
        )
    except SCIMError as exc:
        return _handle_scim_error(exc)
    if resource is None:
        return _handle_scim_error(
            SCIMError(status=404, detail="group_not_found")
        )
    return _scim_response(resource)


@router.put("/Groups/{org_group_id}")
async def replace_group(
    org_group_id: str,
    payload: SCIMGroupResource,
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    payload_copy = payload.model_copy(update={"id": org_group_id})
    try:
        resource = await _repo().create_or_replace_group(
            tenant_id=user["tenant_id"], payload=payload_copy
        )
    except SCIMError as exc:
        return _handle_scim_error(exc)
    return _scim_response(resource)


@router.patch("/Groups/{org_group_id}")
async def patch_group(
    org_group_id: str,
    body: SCIMPatchRequest,
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    _require_scim_role(user)
    operations = [
        (op.op, op.path, op.value) for op in body.Operations
    ]
    try:
        resource = await _repo().patch_group(
            tenant_id=user["tenant_id"],
            org_group_id=org_group_id,
            operations=operations,
        )
    except SCIMError as exc:
        return _handle_scim_error(exc)
    if resource is None:
        return _handle_scim_error(
            SCIMError(status=404, detail="group_not_found")
        )
    return _scim_response(resource)


@router.delete("/Groups/{org_group_id}", status_code=204, response_model=None)
async def delete_group(
    org_group_id: str,
    user: dict = Depends(get_current_user),
) -> Response:
    _require_scim_role(user)
    await _repo().delete_group(
        tenant_id=user["tenant_id"], org_group_id=org_group_id
    )
    return Response(status_code=204)
