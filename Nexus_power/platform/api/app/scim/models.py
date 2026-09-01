"""SCIM v2.0 Pydantic models — User, Group, ListResponse, Patch.

We model exactly the subset the platform persists. Unknown fields on
incoming payloads are *preserved* in ``scim_metadata`` so partial-update
semantics work even for attributes we don't surface natively.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
_ENTERPRISE_SCHEMA = (
    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
)
_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


class SCIMMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    resourceType: str
    created: Optional[datetime] = None
    lastModified: Optional[datetime] = None
    location: Optional[str] = None
    version: Optional[str] = None


class SCIMName(BaseModel):
    model_config = ConfigDict(extra="allow")

    formatted: Optional[str] = None
    familyName: Optional[str] = None
    givenName: Optional[str] = None
    middleName: Optional[str] = None
    honorificPrefix: Optional[str] = None
    honorificSuffix: Optional[str] = None


class SCIMEmail(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: str
    type: Optional[str] = None
    primary: Optional[bool] = None
    display: Optional[str] = None


class SCIMEnterpriseUser(BaseModel):
    model_config = ConfigDict(extra="allow")

    employeeNumber: Optional[str] = None
    costCenter: Optional[str] = None
    organization: Optional[str] = None
    division: Optional[str] = None
    department: Optional[str] = None
    manager: Optional[dict[str, Any]] = None  # {value, $ref, displayName}


class SCIMGroupMember(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: str  # the user/group id
    type: Optional[str] = None
    ref: Optional[str] = Field(default=None, alias="$ref")
    display: Optional[str] = None


class SCIMUserResource(BaseModel):
    """One SCIM User resource."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schemas: list[str] = Field(default_factory=lambda: [_USER_SCHEMA])
    id: Optional[str] = None
    externalId: Optional[str] = None
    userName: str = Field(min_length=1, max_length=256)
    name: Optional[SCIMName] = None
    displayName: Optional[str] = Field(default=None, max_length=256)
    title: Optional[str] = Field(default=None, max_length=256)
    active: bool = True
    emails: list[SCIMEmail] = Field(default_factory=list)
    locale: Optional[str] = None
    timezone: Optional[str] = None
    addresses: list[dict[str, Any]] = Field(default_factory=list)
    groups: list[dict[str, Any]] = Field(default_factory=list)
    enterpriseUser: Optional[SCIMEnterpriseUser] = Field(
        default=None, alias=_ENTERPRISE_SCHEMA
    )
    meta: Optional[SCIMMeta] = None

    @field_validator("userName")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    def primary_email(self) -> Optional[str]:
        for e in self.emails:
            if e.primary:
                return e.value
        return self.emails[0].value if self.emails else None


class SCIMGroupResource(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schemas: list[str] = Field(default_factory=lambda: [_GROUP_SCHEMA])
    id: Optional[str] = None
    externalId: Optional[str] = None
    displayName: str = Field(min_length=1, max_length=256)
    members: list[SCIMGroupMember] = Field(default_factory=list)
    meta: Optional[SCIMMeta] = None


class SCIMListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemas: list[str] = Field(default_factory=lambda: [_LIST_SCHEMA])
    totalResults: int = Field(ge=0)
    Resources: list[Any] = Field(default_factory=list)
    startIndex: int = Field(default=1, ge=1)
    itemsPerPage: int = Field(default=0, ge=0)


class SCIMPatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    op: str
    path: Optional[str] = None
    value: Optional[Any] = None

    @field_validator("op")
    @classmethod
    def _op_valid(cls, v: str) -> str:
        norm = (v or "").strip().lower()
        if norm not in ("add", "replace", "remove"):
            raise ValueError(f"unsupported PatchOp op: {v!r}")
        return norm


class SCIMPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemas: list[str] = Field(default_factory=lambda: [_PATCH_SCHEMA])
    Operations: list[SCIMPatchOperation] = Field(min_length=1, max_length=64)


# ── Schema constants exported for the /Schemas endpoint ────────


USER_SCHEMA_URN = _USER_SCHEMA
GROUP_SCHEMA_URN = _GROUP_SCHEMA
ENTERPRISE_SCHEMA_URN = _ENTERPRISE_SCHEMA
