"""QE-Central — typed client dispatching a crawl to the contained explorer.

``POST {explorer_url}/api/v1/explore`` starts one crawl (design §3.2 API
surface).  Authenticated with the per-fleet HMAC shared secret sent as the
``X-QEC-Token`` header (RUNNER_TOKEN pattern); the explorer verifies it via its
own ``settings.token_matches``.  The explorer runs asynchronously and, on
completion, calls back ``POST /internal/crawls/{crawl_id}/complete`` on
qe-central (HMAC-signed) — see ``app/routers/internal.py``.

Isolation note (design §1.1): this dispatch travels over the internal
``qec-internal`` network only; the explorer has NO route to the nexus stack or
the internet (except through the squid egress proxy).  Credentials in the
payload are relayed to the explorer's in-memory use and are NEVER logged here.
"""
from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from .config import TOKEN_HEADER, phase1_settings

logger = logging.getLogger(__name__)

_EXPLORE_PATH = "/api/v1/explore"


class ExploreDispatchRequest(BaseModel):
    """The ``POST /api/v1/explore`` body (design §3.2 API surface).

    ``credentials`` and ``answer_key`` are sensitive and are never logged.
    ``attestation`` is REQUIRED by the explorer for the submit phase and is
    otherwise passed through untouched (Phase-1 dispatches ``phase='explore'``).
    """

    crawl_id: str = Field(min_length=1, max_length=36)
    tenant_id: str = Field(min_length=1, max_length=64)
    exploration_id: str = Field(min_length=1, max_length=64)
    target_url: str = Field(min_length=1, max_length=2000)
    credentials: dict | None = None
    answer_key: dict = Field(default_factory=dict)
    budgets: dict = Field(default_factory=dict)
    allowed_hosts: list[str] = Field(default_factory=list)
    phase: str = Field(default="explore")
    submit_approvals: list[str] = Field(default_factory=list)
    attestation: dict | None = None
    #: A pre-captured Playwright storageState to start the crawl authenticated
    #: (tier-4: logins the crawler cannot script). Resolved by qe-central from a
    #: stored client session or a fetched auth-hook; sensitive, never logged.
    session: dict | None = None
    #: Multi-env crawl bindings from the reference Environment Profile — routing
    #: cookies/headers select the env; http_credentials (secret) passes a dev gate.
    cookies: list[dict] = Field(default_factory=list)
    extra_http_headers: dict = Field(default_factory=dict)
    http_credentials: dict | None = None
    env_assertion: dict | None = None


class DispatchResult(BaseModel):
    """Outcome of a dispatch (the explorer 202-accepts the crawl)."""

    accepted: bool
    status_code: int
    crawl_id: str
    detail: str = ""


class ExplorerDispatchError(RuntimeError):
    """The explorer could not be dispatched (transport error, busy 409, or a
    non-2xx response).  Carries ``status_code`` so the router can map it to an
    honest HTTP status (502 transport, 409 busy, else 502)."""

    def __init__(self, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


def _log_safe(req: ExploreDispatchRequest) -> dict:
    """Loggable view of a dispatch — secrets stripped."""
    return {
        "crawl_id": req.crawl_id,
        "tenant_id": req.tenant_id,
        "exploration_id": req.exploration_id,
        "target_url": req.target_url,
        "allowed_hosts": req.allowed_hosts,
        "phase": req.phase,
        "has_credentials": bool(req.credentials),
        "has_answer_key": bool(req.answer_key),
        # Booleans only — never the raw routing cookies/headers/basic-auth (secret).
        "has_cookies": bool(req.cookies),
        "has_headers": bool(req.extra_http_headers),
        "has_http_credentials": bool(req.http_credentials),
    }


async def dispatch_crawl(
    request: ExploreDispatchRequest, *, explorer_url: str | None = None,
) -> DispatchResult:
    """Dispatch a crawl to an explorer WORKER; return the 202 acceptance.

    ``explorer_url`` targets a specific worker in the pool; ``None`` uses the
    single configured ``phase1_settings.explorer_url`` (byte-identical to the
    pre-pool behavior). The caller MUST have written THIS worker's own egress
    allowlist file before calling (per-worker isolation).

    Raises:
        ExplorerDispatchError: the token is unset (fail-closed), the explorer
            is unreachable (502), is busy (409), or returned any non-2xx status.
    """
    target = (explorer_url or phase1_settings.explorer_url)
    token = phase1_settings.token_value()
    if not token:
        # Fail-closed: never dispatch a crawl the explorer cannot authenticate.
        raise ExplorerDispatchError(
            "QEC_EXPLORER_TOKEN is unset — refusing to dispatch an "
            "unauthenticated crawl",
            status_code=503,
        )

    payload = request.model_dump()
    logger.info("qec.explorer.dispatch", extra={**_log_safe(request), "worker": target})
    try:
        async with httpx.AsyncClient(
            base_url=target,
            timeout=phase1_settings.dispatch_timeout_s,
        ) as client:
            response = await client.post(
                _EXPLORE_PATH, json=payload, headers={TOKEN_HEADER: token},
            )
    except Exception as exc:
        raise ExplorerDispatchError(
            f"explorer unreachable at {target}: {exc}",
            status_code=502,
        ) from exc

    if response.status_code == 409:
        raise ExplorerDispatchError(
            "explorer is busy (single-flight job lock) — try again later",
            status_code=409,
        )
    if response.status_code // 100 != 2:
        detail = ""
        try:
            detail = str(response.json().get("detail") or "")
        except Exception:
            detail = response.text[:300]
        raise ExplorerDispatchError(
            f"explorer rejected dispatch ({response.status_code}): {detail}"[:300],
            status_code=502,
        )

    return DispatchResult(
        accepted=True,
        status_code=response.status_code,
        crawl_id=request.crawl_id,
        detail="accepted",
    )
