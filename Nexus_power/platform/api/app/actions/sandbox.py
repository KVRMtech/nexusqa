"""Sandbox runner — bridge between the action layer and the Legs engine.

The runner:

    1. Validates the request shape (Pydantic).
    2. Enforces a per-tenant daily quota using the ``action_invocations``
       audit log (no extra schema needed).
    3. Opens an invocation row in ``queued`` state (idempotent via the
       request's ``idempotency_key`` when supplied).
    4. Promotes to ``running``, then POSTs the scenario to Legs.
    5. Closes the row with ``succeeded`` / ``failed`` + result payload.

The Legs HTTP shape is encapsulated in ``SandboxClient`` so tests can
substitute a fake. Real production wiring uses an authenticated httpx
client minted from the platform-API config.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .repository import ActionRepository

logger = logging.getLogger(__name__)


# ── Errors ─────────────────────────────────────────────────────


class SandboxClientError(Exception):
    """Underlying Legs transport / protocol failure."""


class SandboxQuotaExceeded(Exception):
    """Tenant burned through their daily sandbox budget."""

    def __init__(self, *, used: int, limit: int):
        self.used = used
        self.limit = limit
        super().__init__(
            f"sandbox quota exceeded: used={used} limit={limit}"
        )


# ── DTOs ───────────────────────────────────────────────────────


class SandboxRequest(BaseModel):
    """Strictly-validated request body passed to Legs.

    ``scenario_id`` keys into a Legs-known scenario template. ``params``
    is the scenario's input bag — Legs validates its own schema.
    ``timeout_seconds`` is honoured by Legs but echoed here so the
    invocation row records the contract.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    label: Optional[str] = Field(default=None, max_length=256)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)

    @field_validator("scenario_id")
    @classmethod
    def _scenario_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("scenario_id must not be whitespace")
        return v.strip()


@dataclass(frozen=True)
class SandboxResult:
    invocation_id: str
    status: str
    legs_run_id: Optional[str]
    output: dict[str, Any]
    error: Optional[str]
    latency_ms: int


# ── Legs HTTP client ───────────────────────────────────────────


class _LegsClientProtocol(Protocol):
    async def run_scenario(
        self,
        *,
        tenant_id: str,
        scenario_id: str,
        params: dict[str, Any],
        timeout_seconds: int,
        idempotency_key: Optional[str],
        trace_id: Optional[str],
    ) -> dict[str, Any]: ...


class SandboxClient:
    """httpx-backed client for the Legs ``/api/v1/legs/run`` endpoint.

    Authentication: the caller mints a JWT (same pattern as
    ``BackboneClient`` in the fusion engine) and passes it via
    ``access_token_resolver`` — a callable that returns a bearer token
    given a tenant_id.
    """

    def __init__(
        self,
        base_url: str,
        *,
        access_token_resolver,
        timeout_seconds: float = 60.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._tokens = access_token_resolver
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout_seconds
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def run_scenario(
        self,
        *,
        tenant_id: str,
        scenario_id: str,
        params: dict[str, Any],
        timeout_seconds: int,
        idempotency_key: Optional[str],
        trace_id: Optional[str],
    ) -> dict[str, Any]:
        body = {
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "scenario_id": scenario_id,
            "params": params,
            "timeout_seconds": int(timeout_seconds),
        }
        headers: dict[str, str] = {}
        token = await self._tokens(tenant_id)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            resp = await self._client.post(
                "/api/v1/legs/run", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise SandboxClientError(f"legs transport: {exc}") from exc
        if resp.status_code == 429:
            raise SandboxClientError("legs rate-limited (429)")
        if resp.status_code >= 400:
            raise SandboxClientError(
                f"legs {resp.status_code}: {resp.text[:512]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise SandboxClientError(
                f"legs returned non-JSON: {exc}"
            ) from exc


# ── Runner ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SandboxRunnerConfig:
    daily_quota_per_tenant: int = 100
    quota_window_hours: int = 24


class SandboxRunner:
    """End-to-end sandbox execution path."""

    def __init__(
        self,
        repo: ActionRepository,
        client: _LegsClientProtocol,
        *,
        config: Optional[SandboxRunnerConfig] = None,
    ):
        self._repo = repo
        self._client = client
        self._cfg = config or SandboxRunnerConfig()

    async def run(
        self,
        *,
        tenant_id: str,
        request: SandboxRequest,
        trigger_dispatch_id: Optional[str] = None,
        trigger_user_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> SandboxResult:
        # 1. Quota.
        await self._enforce_quota(tenant_id)

        # 2. Open invocation row.
        opened = await self._repo.open_invocation(
            tenant_id=tenant_id,
            kind="sandbox_run",
            request=request.model_dump(),
            trigger_dispatch_id=trigger_dispatch_id,
            trigger_user_id=trigger_user_id,
            trace_id=trace_id,
            idempotency_key=request.idempotency_key,
        )
        invocation_id = opened["invocation_id"]

        # 3. If this was an idempotent replay of a previous run, short-
        # circuit by returning the stored result.
        if opened["status"] in ("succeeded", "failed", "cancelled"):
            return _row_to_result(opened)

        await self._repo.mark_running(
            tenant_id=tenant_id, invocation_id=invocation_id
        )

        # 4. Execute against Legs.
        started = time.monotonic()
        try:
            legs_response = await self._client.run_scenario(
                tenant_id=tenant_id,
                scenario_id=request.scenario_id,
                params=request.params,
                timeout_seconds=request.timeout_seconds,
                idempotency_key=request.idempotency_key,
                trace_id=trace_id,
            )
        except SandboxClientError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            await self._repo.mark_completed(
                tenant_id=tenant_id,
                invocation_id=invocation_id,
                status="failed",
                result={},
                error=str(exc),
                latency_ms=latency_ms,
            )
            return SandboxResult(
                invocation_id=invocation_id,
                status="failed",
                legs_run_id=None,
                output={},
                error=str(exc),
                latency_ms=latency_ms,
            )
        latency_ms = int((time.monotonic() - started) * 1000)

        # 5. Persist outcome.
        ok = bool(legs_response.get("ok") if isinstance(legs_response, dict) else False)
        legs_run_id = (
            str(legs_response.get("run_id") or "")
            if isinstance(legs_response, dict)
            else ""
        )
        await self._repo.mark_completed(
            tenant_id=tenant_id,
            invocation_id=invocation_id,
            status="succeeded" if ok else "failed",
            result=legs_response if isinstance(legs_response, dict) else {},
            error=None if ok else str(legs_response.get("error") if isinstance(legs_response, dict) else "legs_unknown_error"),
            latency_ms=latency_ms,
        )
        return SandboxResult(
            invocation_id=invocation_id,
            status="succeeded" if ok else "failed",
            legs_run_id=legs_run_id or None,
            output=legs_response if isinstance(legs_response, dict) else {},
            error=None if ok else str(legs_response.get("error") if isinstance(legs_response, dict) else "legs_unknown_error"),
            latency_ms=latency_ms,
        )

    async def _enforce_quota(self, tenant_id: str) -> None:
        if self._cfg.daily_quota_per_tenant <= 0:
            return
        since = datetime.now(timezone.utc) - timedelta(
            hours=self._cfg.quota_window_hours
        )
        used = await self._repo.quota_count_since(
            tenant_id=tenant_id, kind="sandbox_run", since=since
        )
        if used >= self._cfg.daily_quota_per_tenant:
            raise SandboxQuotaExceeded(
                used=used, limit=self._cfg.daily_quota_per_tenant
            )


def _row_to_result(row: dict[str, Any]) -> SandboxResult:
    result = dict(row.get("result") or {})
    return SandboxResult(
        invocation_id=row["invocation_id"],
        status=row["status"],
        legs_run_id=(
            str(result.get("run_id"))
            if isinstance(result, dict) and result.get("run_id")
            else None
        ),
        output=result if isinstance(result, dict) else {},
        error=row.get("error"),
        latency_ms=int(row.get("latency_ms") or 0),
    )
