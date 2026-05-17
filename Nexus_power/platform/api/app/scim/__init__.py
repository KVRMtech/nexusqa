"""SCIM v2.0 (RFC 7644) implementation for the org directory.

The provider supports the ``User`` and ``Group`` core schemas plus the
Enterprise User extension. List/filter queries honor a sane subset of
the SCIM filter grammar — enough for Okta / Azure AD / OneLogin /
Ping to do bulk provisioning and reconciliation.

Public surface:
    * ``SCIMUserResource`` / ``SCIMGroupResource`` — wire-format models
    * ``SCIMRepository``                            — DB persistence
    * ``SCIMError``                                 — error type
    * ``SCIMFilter``                                — parsed query filter
    * ``parse_scim_filter``                         — filter parser
"""

from __future__ import annotations

from .errors import SCIMError, scim_error_response
from .filters import SCIMFilter, parse_scim_filter
from .models import (
    SCIMEmail,
    SCIMEnterpriseUser,
    SCIMGroupMember,
    SCIMGroupResource,
    SCIMListResponse,
    SCIMMeta,
    SCIMName,
    SCIMPatchOperation,
    SCIMPatchRequest,
    SCIMUserResource,
)
from .repository import SCIMRepository

__all__ = [
    "SCIMEmail",
    "SCIMEnterpriseUser",
    "SCIMError",
    "SCIMFilter",
    "SCIMGroupMember",
    "SCIMGroupResource",
    "SCIMListResponse",
    "SCIMMeta",
    "SCIMName",
    "SCIMPatchOperation",
    "SCIMPatchRequest",
    "SCIMRepository",
    "SCIMUserResource",
    "parse_scim_filter",
    "scim_error_response",
]
