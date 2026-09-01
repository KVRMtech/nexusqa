"""SCIM error response per RFC 7644 §3.12."""

from __future__ import annotations

from typing import Any, Optional


class SCIMError(Exception):
    """Raised inside the SCIM stack — converted to RFC 7644 error JSON."""

    def __init__(
        self,
        *,
        status: int,
        detail: str,
        scim_type: Optional[str] = None,
    ):
        self.status = int(status)
        self.detail = detail
        self.scim_type = scim_type
        super().__init__(detail)


def scim_error_response(error: SCIMError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
        "detail": error.detail,
        "status": str(error.status),
    }
    if error.scim_type:
        body["scimType"] = error.scim_type
    return body
