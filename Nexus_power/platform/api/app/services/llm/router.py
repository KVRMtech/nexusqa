"""LLM router — picks a tier per task and runs the completion with fallback.

Application services do not import providers.  They construct a
``CompletionRequest`` and call ``await router.complete(task=..., request=...)``.
The router:

1. Resolves the task name → tier name via ``LLMConfig.tier_for_task``.
2. Builds (or reuses) a provider for that tier.
3. Calls the provider with retry-on-transient-error.
4. On exhausted retries, follows the tier's ``fallback`` chain.
5. Returns a ``CompletionResponse`` annotated with ``tier``, ``provider``,
   ``retries``, ``fell_back``.

The router owns provider lifecycles — one instance per tier shared
across the application.  Call ``await router.close()`` during shutdown.

When ``LLM_TIERS`` is unset (no LLM configured), the router still works
but every ``complete()`` returns ``finish_reason=ERROR`` with an
``error_detail`` of ``"no_llm_configured"``.  Application services
check this and run their deterministic non-LLM fallback path.
"""

from __future__ import annotations

import asyncio
import logging
import random
import dataclasses
from typing import Awaitable, Callable

from .config import LLMConfig, LLMTierConfig
from .providers import LLMProvider, LLMProviderError, build_provider
from .types import CompletionRequest, CompletionResponse, FinishReason


logger = logging.getLogger(__name__)


class LLMRouterError(Exception):
    """Base class for router-level errors (config/runtime)."""


class NoLLMConfiguredError(LLMRouterError):
    """Raised only when callers explicitly demand an LLM and none is configured."""


# Maximum backoff between retries to avoid hammering a struggling provider.
_BACKOFF_BASE_S = 0.25
# Widened from 4s: with max_retries=6 a 4s cap spends all retries inside ~12s,
# so they hit the SAME rate-limit window and fail together. A larger cap lets
# the retries span long enough to outlast a transient per-window rate limit on
# the cloud vision endpoint (the cause of overflow cascading to the weak local
# model). Failed calls cost more wall-clock, but recover on the reliable model.
_BACKOFF_CAP_S = 16.0


def _is_transient(detail: str) -> bool:
    """Heuristic: is this error worth retrying?

    Transient errors include network resets, 5xx HTTP, rate limit
    (HTTP 429), and timeouts.  Non-transient errors (401, 400, model
    not found) are not worth retrying.
    """
    if not detail:
        return False
    lowered = detail.lower()
    if "transport_error" in lowered or "timeout" in lowered:
        return True
    # HTTP 5xx and 429 are transient
    for code in ("_500", "_502", "_503", "_504", "_429"):
        if code in lowered:
            return True
    return False


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter, capped at _BACKOFF_CAP_S."""
    base = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2 ** attempt))
    return base * (0.5 + random.random() * 0.5)


class LLMRouter:
    """Tier-aware LLM router.

    Construct once per process (typically held on FastAPI ``app.state``).
    Safe for concurrent use across coroutines.
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        # Lazy provider instantiation: a misconfigured tier does not
        # prevent other tiers from working.  Providers are cached per
        # tier name; rebuilds on tier swap are not supported (restart
        # the process).
        self._providers: dict[str, LLMProvider] = {}
        self._lock = asyncio.Lock()

    @property
    def config(self) -> LLMConfig:
        return self._config

    def has_tier_for_task(self, task: str) -> bool:
        """Whether the router can serve ``task``.

        Application services call this BEFORE attempting an LLM call so
        they can pick a deterministic fallback path when LLM is disabled.
        """
        return self._config.tier_for_task(task) is not None

    async def complete(
        self,
        *,
        task: str,
        request: CompletionRequest,
    ) -> CompletionResponse:
        """Run a completion for ``task`` with retry + fallback.

        Returns a ``CompletionResponse`` always — ``finish_reason=ERROR``
        with non-empty ``error_detail`` signals the call failed.
        """
        starting_tier = self._config.tier_for_task(task)
        if starting_tier is None:
            return self._no_llm_response(task)

        return await self._complete_with_fallback(
            task=task,
            tier_name=starting_tier,
            request=request,
            visited=set(),
            fell_back=False,
        )

    async def _complete_with_fallback(
        self,
        *,
        task: str,
        tier_name: str,
        request: CompletionRequest,
        visited: set[str],
        fell_back: bool,
    ) -> CompletionResponse:
        if tier_name in visited:
            # Cycle in fallback chain — config validation should have
            # caught this, but be defensive.
            return CompletionResponse(
                text="",
                finish_reason=FinishReason.ERROR,
                tier=tier_name,
                provider="(router)",
                model="",
                latency_ms=0,
                fell_back=fell_back,
                error_detail=f"router_fallback_cycle_visiting:{tier_name}",
            )
        visited.add(tier_name)

        tier = self._config.tiers.get(tier_name)
        if tier is None:
            return CompletionResponse(
                text="",
                finish_reason=FinishReason.ERROR,
                tier=tier_name,
                provider="(router)",
                model="",
                latency_ms=0,
                fell_back=fell_back,
                error_detail=f"router_unknown_tier:{tier_name}",
            )

        provider = await self._get_provider(tier)
        last_response: CompletionResponse | None = None

        for attempt in range(tier.max_retries + 1):
            response = await provider.complete(request)
            if response.finish_reason != FinishReason.ERROR:
                # Success — annotate retries / fall-back state and return.
                # Use dataclasses.replace() to copy ALL fields (including
                # tool_calls + cache_read_tokens + cache_creation_tokens
                # added in Phase 1) — forgetting any of these silently
                # drops them on the way out of the router.
                return dataclasses.replace(
                    response,
                    retries=attempt,
                    fell_back=fell_back,
                )

            last_response = response
            transient = _is_transient(response.error_detail)
            if not transient or attempt == tier.max_retries:
                break

            sleep_s = _backoff(attempt)
            logger.warning(
                "llm.router.retry",
                extra={
                    "task": task,
                    "tier": tier_name,
                    "provider": provider.name,
                    "attempt": attempt + 1,
                    "max_retries": tier.max_retries,
                    "backoff_s": round(sleep_s, 3),
                    "error_detail": response.error_detail[:200],
                },
            )
            await asyncio.sleep(sleep_s)

        # Retries exhausted — follow fallback if configured.
        if tier.fallback_tier and tier.fallback_tier in self._config.tiers:
            logger.warning(
                "llm.router.fallback",
                extra={
                    "task": task,
                    "from_tier": tier_name,
                    "to_tier": tier.fallback_tier,
                    "last_error": (last_response.error_detail if last_response else "")[:200],
                },
            )
            return await self._complete_with_fallback(
                task=task,
                tier_name=tier.fallback_tier,
                request=request,
                visited=visited,
                fell_back=True,
            )

        # No fallback configured — return the last error response
        # annotated with retry count + fall-back flag.
        if last_response is None:
            return CompletionResponse(
                text="",
                finish_reason=FinishReason.ERROR,
                tier=tier_name,
                provider=provider.name,
                model=tier.model,
                latency_ms=0,
                retries=0,
                fell_back=fell_back,
                error_detail="router_no_response",
            )
        return dataclasses.replace(
            last_response,
            retries=tier.max_retries,
            fell_back=fell_back,
        )

    async def _get_provider(self, tier: LLMTierConfig) -> LLMProvider:
        existing = self._providers.get(tier.name)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._providers.get(tier.name)
            if existing is not None:
                return existing
            try:
                provider = build_provider(tier)
            except LLMProviderError:
                raise  # config error — surface to caller
            self._providers[tier.name] = provider
            logger.info(
                "llm.router.provider_built",
                extra={"tier": tier.name, "provider": provider.name, "model": tier.model},
            )
            return provider

    @staticmethod
    def _no_llm_response(task: str) -> CompletionResponse:
        return CompletionResponse(
            text="",
            finish_reason=FinishReason.ERROR,
            tier="(none)",
            provider="(none)",
            model="",
            latency_ms=0,
            retries=0,
            fell_back=False,
            error_detail=f"no_llm_configured:task={task}",
        )

    async def health_check(self) -> dict[str, bool]:
        """Per-tier liveness check.  Used by the platform-api /health endpoint."""
        results: dict[str, bool] = {}
        for tier_name, tier in self._config.tiers.items():
            try:
                provider = await self._get_provider(tier)
                results[tier_name] = await provider.health_check()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "llm.router.health_check_failed",
                    extra={"tier": tier_name, "error": str(exc)[:200]},
                )
                results[tier_name] = False
        return results

    async def close(self) -> None:
        """Close every provider's HTTP client.  Call once at shutdown."""
        async with self._lock:
            for tier_name, provider in list(self._providers.items()):
                try:
                    await provider.close()
                except Exception as exc:  # pragma: no cover
                    logger.warning(
                        "llm.router.provider_close_failed",
                        extra={"tier": tier_name, "error": str(exc)[:200]},
                    )
            self._providers.clear()

    # Convenience for tests + the storyboard worker — call a single tier directly
    # without the per-task routing, bypassing fallback chains.  Useful when a
    # caller wants to know which tier produced a given output.
    async def complete_via_tier(
        self,
        *,
        tier_name: str,
        request: CompletionRequest,
        follow_fallback: bool = True,
    ) -> CompletionResponse:
        if tier_name not in self._config.tiers:
            return CompletionResponse(
                text="",
                finish_reason=FinishReason.ERROR,
                tier=tier_name,
                provider="(router)",
                model="",
                latency_ms=0,
                error_detail=f"router_unknown_tier:{tier_name}",
            )
        if follow_fallback:
            return await self._complete_with_fallback(
                task="(direct)",
                tier_name=tier_name,
                request=request,
                visited=set(),
                fell_back=False,
            )
        # Single-tier call without fallback
        provider = await self._get_provider(self._config.tiers[tier_name])
        return await provider.complete(request)


def build_router(config_arg: Callable[[], LLMConfig] | LLMConfig | None = None) -> LLMRouter:
    """Convenience constructor used by application startup.

    Accepts either a pre-built ``LLMConfig`` (for tests) or a callable
    that produces one (so application startup can defer env reading).
    ``None`` reads env at call time.
    """
    if config_arg is None:
        from .config import load_config

        return LLMRouter(load_config())
    if isinstance(config_arg, LLMConfig):
        return LLMRouter(config_arg)
    return LLMRouter(config_arg())


# Optional helper for tests: a stub-friendly router that returns canned
# responses without hitting a real provider.  Not used by production
# code, but documented here so test files do not need to roll their own.
class _CannedResponseRouter(LLMRouter):  # pragma: no cover - test helper
    """For unit tests only — returns the canned response on every call."""

    def __init__(self, canned: CompletionResponse) -> None:
        super().__init__(LLMConfig(tiers={}, task_routing={}, default_tier=""))
        self._canned = canned

    async def complete(self, *, task: str, request: CompletionRequest) -> CompletionResponse:
        return self._canned

    async def complete_via_tier(
        self,
        *,
        tier_name: str,
        request: CompletionRequest,
        follow_fallback: bool = True,
    ) -> CompletionResponse:
        return self._canned
