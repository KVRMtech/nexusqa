"""Outbound Slack Web API client.

Production qualities:

* Honors Slack's ``Retry-After`` header on 429 with exponential backoff
  (capped). The caller never has to think about rate limits.
* Returns a typed ``SlackPostResult`` carrying ``ts`` + ``channel`` so
  the orchestrator can persist a stable message reference.
* Token is per-call so a single client can serve multiple workspaces.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


_BASE = "https://slack.com/api"


class SlackClientError(Exception):
    """Wraps non-recoverable failures from the Slack Web API."""


class SlackRateLimited(SlackClientError):
    """Repeated 429s exhausted the retry budget."""


@dataclass(frozen=True)
class SlackPostResult:
    ok: bool
    channel: Optional[str]
    ts: Optional[str]
    raw: dict[str, Any]

    @property
    def message_ref(self) -> Optional[str]:
        """Canonical reference: ``{channel}/{ts}``."""
        if not self.channel or not self.ts:
            return None
        return f"{self.channel}/{self.ts}"


class SlackClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        base_url: str = _BASE,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
        )
        self._max_retries = max(0, max_retries)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post_message(
        self,
        *,
        token: str,
        channel: str,
        text: str,
        blocks: Optional[list[dict[str, Any]]] = None,
        thread_ts: Optional[str] = None,
        unfurl_links: bool = False,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SlackPostResult:
        body: dict[str, Any] = {
            "channel": channel,
            "text": text,
            "unfurl_links": unfurl_links,
        }
        if blocks:
            body["blocks"] = blocks
        if thread_ts:
            body["thread_ts"] = thread_ts
        if metadata:
            body["metadata"] = metadata
        return await self._call_returning_message("/chat.postMessage", token, body)

    async def post_ephemeral(
        self,
        *,
        token: str,
        channel: str,
        user: str,
        text: str,
        blocks: Optional[list[dict[str, Any]]] = None,
    ) -> SlackPostResult:
        body: dict[str, Any] = {
            "channel": channel,
            "user": user,
            "text": text,
        }
        if blocks:
            body["blocks"] = blocks
        return await self._call_returning_message(
            "/chat.postEphemeral", token, body
        )

    async def open_dm(
        self, *, token: str, user_id: str
    ) -> str:
        """Open a DM channel with a user, returning the channel id."""
        data = await self._call("/conversations.open", token, {"users": user_id})
        ch = (data.get("channel") or {}).get("id")
        if not ch:
            raise SlackClientError(
                f"conversations.open did not return a channel id: {data!r}"
            )
        return ch

    async def post_dm(
        self,
        *,
        token: str,
        user_id: str,
        text: str,
        blocks: Optional[list[dict[str, Any]]] = None,
    ) -> SlackPostResult:
        channel = await self.open_dm(token=token, user_id=user_id)
        return await self.post_message(
            token=token, channel=channel, text=text, blocks=blocks
        )

    async def auth_test(self, *, token: str) -> dict[str, Any]:
        return await self._call("/auth.test", token, {})

    # ── Internals ───────────────────────────────────────────────

    async def _call_returning_message(
        self, path: str, token: str, body: dict[str, Any]
    ) -> SlackPostResult:
        data = await self._call(path, token, body)
        return SlackPostResult(
            ok=bool(data.get("ok")),
            channel=data.get("channel") if isinstance(data.get("channel"), str)
                else (data.get("channel") or {}).get("id") if isinstance(data.get("channel"), dict)
                else None,
            ts=data.get("ts") or data.get("message_ts"),
            raw=data,
        )

    async def _call(
        self, path: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        attempt = 0
        backoff = 0.5
        last_error: Optional[str] = None
        while attempt <= self._max_retries:
            attempt += 1
            try:
                resp = await self._client.post(
                    path,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                )
            except httpx.HTTPError as exc:
                last_error = f"transport: {exc}"
                if attempt > self._max_retries:
                    raise SlackClientError(last_error) from exc
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "1") or "1")
                if attempt > self._max_retries:
                    raise SlackRateLimited(
                        f"slack rate-limited after {attempt - 1} retries"
                    )
                await asyncio.sleep(max(retry_after, backoff))
                backoff = min(backoff * 2, 8.0)
                continue

            if resp.status_code >= 500:
                last_error = f"slack {resp.status_code}: {resp.text[:512]}"
                if attempt > self._max_retries:
                    raise SlackClientError(last_error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue

            if resp.status_code >= 400:
                raise SlackClientError(
                    f"slack {path} returned {resp.status_code}: {resp.text[:512]}"
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise SlackClientError(
                    f"slack {path} returned non-JSON: {exc}"
                ) from exc

            if not isinstance(data, dict):
                raise SlackClientError(
                    f"slack {path} returned non-object body"
                )

            if not data.get("ok"):
                # Slack returns 200 with ok=false on auth / scope problems.
                # These are not transient; do not retry.
                err = data.get("error") or "unknown_error"
                raise SlackClientError(f"slack {path} ok=false error={err}")
            return data

        raise SlackClientError(
            f"slack {path} exhausted retries: {last_error or 'unknown'}"
        )
