"""Outbound to the Bot Framework Connector API.

The dispatcher does two things:

    1. Mint + cache an OAuth2 client-credentials access token from
       ``https://login.microsoftonline.com/<channel_auth_tenant>/oauth2/v2.0/token``
       using the bot's MS app id + secret. The token scope is
       ``https://api.botframework.com/.default``.
    2. POST a reply activity to
       ``{service_url}/v3/conversations/{conversation_id}/activities``
       (when no ``reply_to_id``) or
       ``{service_url}/v3/conversations/{conversation_id}/activities/{reply_to_id}``
       (when ``reply_to_id`` is set, i.e., posting into a thread).

DM-equivalent in Teams is a 1:1 ``personal`` conversation. The
orchestrator passes ``as_dm=True`` when the desired mode is DM; the
dispatcher only acts on the ``conversation_id`` it was given — for
true DM creation the caller can pre-create a conversation via the
``/conversations`` endpoint (out of scope for the v1 echo dispatcher).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from ..surfaces import (
    ComposedPayload,
    DispatchOutcome,
    SurfaceError,
    SurfaceUnavailable,
)
from .installation import (
    TeamsInstallation,
    TeamsInstallationError,
    TeamsInstallationLoader,
)

logger = logging.getLogger(__name__)


_TOKEN_HOST = "https://login.microsoftonline.com"
_TOKEN_PATH_TPL = "/{tenant}/oauth2/v2.0/token"
_DEFAULT_SCOPE = "https://api.botframework.com/.default"


class TeamsDispatchError(SurfaceError):
    """Bot Framework rejected our reply or transport failed."""


# ── Token cache ────────────────────────────────────────────────


@dataclass
class _TokenEntry:
    access_token: str
    expires_at: float


class TeamsOutboundClient:
    """Bot Framework Connector POST + token minting."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        client: Optional[httpx.AsyncClient] = None,
        token_safety_window_seconds: int = 60,
    ):
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds
        )
        self._max_retries = max(0, int(max_retries))
        self._safety = max(0, int(token_safety_window_seconds))
        self._token_cache: dict[str, _TokenEntry] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── Token minting ──────────────────────────────────────────

    async def get_access_token(self, installation: TeamsInstallation) -> str:
        cache_key = (
            f"{installation.ms_app_id}@{installation.channel_auth_tenant}"
        )
        cached = self._token_cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and cached.expires_at - self._safety > now:
            return cached.access_token

        token_url = (
            f"{_TOKEN_HOST}"
            f"{_TOKEN_PATH_TPL.format(tenant=installation.channel_auth_tenant)}"
        )
        data = {
            "grant_type": "client_credentials",
            "client_id": installation.ms_app_id,
            "client_secret": installation.ms_app_password,
            "scope": _DEFAULT_SCOPE,
        }
        attempt = 0
        backoff = 0.5
        last_text = ""
        while attempt <= self._max_retries:
            attempt += 1
            try:
                resp = await self._client.post(
                    token_url,
                    data=data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded"
                    },
                )
            except httpx.HTTPError as exc:
                if attempt > self._max_retries:
                    raise TeamsDispatchError(
                        f"token transport failure: {exc}"
                    ) from exc
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            last_text = (resp.text or "")[:512]
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise TeamsDispatchError(
                        f"token endpoint returned non-JSON: {exc}"
                    ) from exc
                access = body.get("access_token")
                expires_in = body.get("expires_in") or 3600
                if not isinstance(access, str) or not access:
                    raise TeamsDispatchError(
                        "token response missing access_token"
                    )
                self._token_cache[cache_key] = _TokenEntry(
                    access_token=access,
                    expires_at=now + float(expires_in),
                )
                return access
            if resp.status_code >= 500:
                if attempt > self._max_retries:
                    raise TeamsDispatchError(
                        f"token endpoint {resp.status_code}: {last_text}"
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            raise TeamsDispatchError(
                f"token endpoint rejected {resp.status_code}: {last_text}"
            )
        raise TeamsDispatchError(
            f"token endpoint exhausted retries: {last_text}"
        )

    # ── Reply POST ─────────────────────────────────────────────

    async def post_reply(
        self,
        *,
        installation: TeamsInstallation,
        service_url: str,
        conversation_id: str,
        reply_to_id: Optional[str],
        activity: dict[str, Any],
    ) -> dict[str, Any]:
        token = await self.get_access_token(installation)
        base = service_url.rstrip("/")
        if reply_to_id:
            url = (
                f"{base}/v3/conversations/{conversation_id}"
                f"/activities/{reply_to_id}"
            )
        else:
            url = f"{base}/v3/conversations/{conversation_id}/activities"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "nexus-knowledge-echo/1.0",
        }
        attempt = 0
        backoff = 0.5
        last_text = ""
        last_status: Optional[int] = None
        while attempt <= self._max_retries:
            attempt += 1
            try:
                resp = await self._client.post(url, json=activity, headers=headers)
            except httpx.HTTPError as exc:
                if attempt > self._max_retries:
                    raise TeamsDispatchError(
                        f"reply transport failure: {exc}"
                    ) from exc
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            last_status = resp.status_code
            last_text = (resp.text or "")[:512]
            if 200 <= resp.status_code < 300:
                try:
                    body = resp.json() if resp.text else {}
                except ValueError:
                    body = {}
                return {
                    "id": body.get("id") if isinstance(body, dict) else None,
                    "raw": body,
                    "status_code": resp.status_code,
                }
            if resp.status_code == 401:
                # Token rejected — drop cache once and retry.
                self._token_cache.clear()
                if attempt > self._max_retries:
                    raise TeamsDispatchError(
                        f"unauthorised after refresh: {last_text}"
                    )
                continue
            if resp.status_code >= 500:
                if attempt > self._max_retries:
                    raise TeamsDispatchError(
                        f"connector {resp.status_code}: {last_text}"
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            raise TeamsDispatchError(
                f"connector rejected {resp.status_code}: {last_text}"
            )
        raise TeamsDispatchError(
            f"connector exhausted retries; last_status={last_status}"
        )


# ── Surface dispatcher ────────────────────────────────────────


class TeamsDispatcher:
    """Implements ``SurfaceDispatcher`` for the Teams surface.

    Routing data — ``conversation_id``, ``service_url``, ``reply_to_id`` —
    is carried in ``payload.primary_candidate["__teams_route"]`` set by
    the route handler before invoking the orchestrator. The orchestrator
    doesn't need to know about these surface specifics; they round-trip
    via the payload envelope.
    """

    def __init__(
        self,
        installs: TeamsInstallationLoader,
        *,
        client: Optional[TeamsOutboundClient] = None,
    ):
        self._installs = installs
        self._client = client or TeamsOutboundClient()

    async def dispatch(
        self,
        *,
        tenant_id: str,
        payload: ComposedPayload,
        as_dm: bool,
        is_live: bool,
        user_id_ext: Optional[str],
        channel_id_ext: Optional[str],
        thread_ts: Optional[str],
    ) -> DispatchOutcome:
        try:
            install = await self._installs.for_tenant(tenant_id)
        except TeamsInstallationError as exc:
            raise SurfaceUnavailable(str(exc)) from exc

        route = payload.primary_candidate.get("__teams_route") \
            if isinstance(payload.primary_candidate, dict) else None
        if not isinstance(route, dict):
            raise SurfaceError(
                "teams dispatch requires __teams_route metadata on payload"
            )
        service_url = route.get("service_url")
        conversation_id = route.get("conversation_id") or channel_id_ext
        reply_to_id = route.get("reply_to_id") or thread_ts
        if not isinstance(service_url, str) or not service_url:
            raise SurfaceError("teams dispatch missing service_url")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise SurfaceError("teams dispatch missing conversation_id")

        result = await self._client.post_reply(
            installation=install,
            service_url=service_url,
            conversation_id=conversation_id,
            reply_to_id=reply_to_id if isinstance(reply_to_id, str) else None,
            activity=payload.payload,
        )
        decision = "posted_dm" if as_dm else "posted_channel"
        return DispatchOutcome(
            decision=decision,
            message_ref=str(result.get("id") or ""),
            raw=result,
        )
