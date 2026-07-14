"""Phase-1 bridge — reverse-proxy the artifact-keyed VKPower factory so the crawl
portal's Test Studio reaches it with a SINGLE portal login.

The two products are separate auth realms: the factory rejects the portal's verdict
token. This bridge closes that gap without touching the factory or the video portal.
The browser sends its verdict token; the bridge validates it (:func:`require_auth`,
tenant-scoped) and forwards the request to the factory using the INTERNAL service
token qe-central already mints to run cycles (:func:`mint_service_jwt`) — the exact,
proven factory-call seam the cycle driver uses.

SECURITY:
  * Tenant isolation — the service token is minted for the AUTHENTICATED tenant ONLY,
    so a user can only reach their own tenant's artifacts (the factory's RLS is keyed
    on that tenant). The browser never holds a raw factory token.
  * RBAC — the service token can mutate (run / generate / heal); so a MUTATING request
    is refused here unless the PORTAL user is admin/manager, mirroring the factory's
    own ``_PRIVILEGED`` gate. A viewer may read (browse cases/scripts) but not run.
  * Surface — only the four artifact-keyed factory roots the Test Studio needs are
    bridged (``test-factory`` / ``artifacts`` / ``test-runs`` / ``eyes``). Anything
    else 404s here; the bridge is not a general-purpose open proxy.

ADDITIVE: this is a new router mounted at absolute ``/api/v1/{root}/...`` paths that do
not collide with the existing ``/api/v1/qec/*`` routes. No factory code, no video code,
no existing qe-central route is changed.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..auth import require_auth
from ..config import settings
from ..observability import outbound_request_id_headers
from ..service_token import mint_service_jwt

logger = logging.getLogger("qec.factory_proxy")

router = APIRouter(tags=["Factory bridge (Test Studio)"])

#: The artifact-keyed factory path roots the Test Studio uses. Nothing else is bridged.
_ALLOWED_ROOTS = ("test-factory", "artifacts", "test-runs", "eyes")

#: Read methods are open to any authenticated tenant user; everything else mutates.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Roles allowed to trigger a mutating factory call (run/generate/heal/edit/approve).
_MUTATE_ROLES = frozenset({"admin", "manager"})

#: Hop-by-hop / auth headers we must not forward upstream or echo downstream.
_STRIP_REQUEST_HEADERS = frozenset({
    "host", "authorization", "content-length", "connection",
    "accept-encoding", "transfer-encoding", "expect",
})
_STRIP_RESPONSE_HEADERS = frozenset({
    "content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive",
})

#: Generous ceiling — a generate/run/bundle call is the slow path; the factory itself
#: returns a run_id quickly and the Studio then polls, so this is a safety bound.
_PROXY_TIMEOUT = 180.0


async def _forward(request: Request, user: dict, factory_path: str) -> Response:
    """Mint a tenant-scoped service token, forward the request to the factory, and
    stream the response back verbatim (status, body, content-type)."""
    tenant_id = user["tenant_id"]

    # RBAC: a mutating factory call requires an admin/manager portal user, since the
    # service token itself is privileged (matches the factory's own _PRIVILEGED gate).
    if request.method.upper() not in _READ_METHODS:
        if str(user.get("role", "viewer")).strip().lower() not in _MUTATE_ROLES:
            raise HTTPException(
                status_code=403,
                detail="running, generating or healing requires an admin or manager role",
            )

    try:
        token = mint_service_jwt(tenant_id)
    except ValueError as exc:  # misconfigured signing — fail closed, never leak
        logger.warning("qec.factory_proxy.mint_failed", extra={"error": str(exc)[:200]})
        raise HTTPException(status_code=503, detail="factory bridge unavailable")

    headers = outbound_request_id_headers({"Authorization": f"Bearer {token}"})
    for name, value in request.headers.items():
        if name.lower() not in _STRIP_REQUEST_HEADERS:
            headers[name] = value
    body = await request.body()

    url = settings.platform_api_url.rstrip("/") + factory_path
    try:
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as client:
            upstream = await client.request(
                request.method, url,
                params=list(request.query_params.multi_items()),
                content=body or None,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "qec.factory_proxy.transport_error",
            extra={"path": factory_path, "error": str(exc)[:200]},
        )
        raise HTTPException(status_code=502, detail="factory unreachable")

    out_headers = {
        name: value for name, value in upstream.headers.items()
        if name.lower() not in _STRIP_RESPONSE_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=out_headers,
        media_type=upstream.headers.get("content-type"),
    )


def _make_bridge(root: str):
    async def _bridge(rest: str, request: Request,
                      user: dict = Depends(require_auth)) -> Response:
        return await _forward(request, user, f"/api/v1/{root}/{rest}")
    _bridge.__name__ = f"factory_bridge_{root.replace('-', '_')}"
    return _bridge


# Explicit per-root routes (no catch-all): each is a distinct prefix that cannot shadow
# the existing /api/v1/qec/* routes, regardless of registration order.
for _root in _ALLOWED_ROOTS:
    router.add_api_route(
        f"/api/v1/{_root}/{{rest:path}}",
        _make_bridge(_root),
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        name=f"factory_bridge_{_root.replace('-', '_')}",
        include_in_schema=False,
    )
