"""QE-Central — typed client for the UNCHANGED VKPower factory (read + generate).

S4 consumes two more VKPower endpoints over HTTP with a minted ``role=manager``
service JWT (R-13), so every call rides the same audited path a human uses:

  * ``GET  /api/v1/test-factory/{artifact_id}/rtm``      — the read-only
    Requirement→Step→Assertion matrix the tier labeller reads (test_factory.py:
    3691-3712).  Returns 200 with ``tests: []`` when the artifact has no cases.
  * ``POST /api/v1/test-factory/{artifact_id}/generate`` — the materialize
    bridge: compile grounded cases from the artifact's substrate (test_factory.py:
    181-199).

Every non-2xx raises :class:`FactoryClientError` carrying the upstream status +
detail so the router can map it honestly (a 404 artifact stays a 404, an LLM/gate
failure surfaces its reason) — never a silent success.  This module is separate
from ``platform_api.py`` (the E3 auth-import relay) so each VKPower seam is a
small, independently-pinned surface.
"""
from __future__ import annotations

import logging

import httpx

from ..config import settings
from ..service_token import mint_service_jwt

logger = logging.getLogger(__name__)

#: Read timeout for the factory calls (generate compiles synchronously).
_RTM_TIMEOUT_S = 30.0
_GENERATE_TIMEOUT_S = 120.0


class FactoryClientError(Exception):
    """A non-2xx (or transport) failure talking to the VKPower factory.

    ``status_code`` is the upstream HTTP status (0 for a transport error) so the
    router can propagate it faithfully; ``detail`` is the upstream honest reason.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"factory call failed ({status_code}): {detail}")


def _rtm_path(artifact_id: str) -> str:
    return f"/api/v1/test-factory/{artifact_id}/rtm"


def _generate_path(artifact_id: str) -> str:
    return f"/api/v1/test-factory/{artifact_id}/generate"


async def _call(
    *, method: str, path: str, tenant_id: str, timeout_s: float,
) -> dict:
    """Issue one authenticated factory call; raise :class:`FactoryClientError`
    on transport failure or any non-200 response."""
    token = mint_service_jwt(tenant_id)
    try:
        async with httpx.AsyncClient(
            base_url=settings.platform_api_url, timeout=timeout_s,
        ) as client:
            response = await client.request(
                method, path, headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:  # transport failure — honest, typed
        logger.warning(
            "qec.factory.transport_error",
            extra={"tenant_id": tenant_id, "path": path, "error": str(exc)[:300]},
        )
        raise FactoryClientError(0, f"transport error: {exc}"[:300]) from exc

    if response.status_code == 200:
        try:
            body = response.json()
        except Exception as exc:
            raise FactoryClientError(502, "factory returned a non-JSON 200 body") from exc
        return body if isinstance(body, dict) else {"result": body}

    detail = ""
    try:
        detail = str(response.json().get("detail") or "")
    except Exception:
        detail = response.text[:300]
    logger.warning(
        "qec.factory.rejected",
        extra={"tenant_id": tenant_id, "path": path,
               "status_code": response.status_code, "detail": detail[:300]},
    )
    raise FactoryClientError(response.status_code, detail[:300] or "factory call failed")


async def get_rtm(*, tenant_id: str, artifact_id: str) -> dict:
    """Fetch the ``/rtm`` traceability matrix for an artifact (service JWT).

    Returns ``{artifact_id, tests: [...]}``.  A 404 (unknown/foreign artifact)
    propagates as :class:`FactoryClientError` with status 404.
    """
    return await _call(
        method="GET", path=_rtm_path(artifact_id),
        tenant_id=tenant_id, timeout_s=_RTM_TIMEOUT_S,
    )


async def generate(*, tenant_id: str, artifact_id: str) -> dict:
    """Trigger ``POST …/generate`` for an artifact (service JWT).

    Returns the factory's generate summary (``{success, generated, demonstrated,
    …}``).  A 404 / 403 / 503 propagates as :class:`FactoryClientError`.
    """
    return await _call(
        method="POST", path=_generate_path(artifact_id),
        tenant_id=tenant_id, timeout_s=_GENERATE_TIMEOUT_S,
    )


__all__ = ["FactoryClientError", "get_rtm", "generate"]
