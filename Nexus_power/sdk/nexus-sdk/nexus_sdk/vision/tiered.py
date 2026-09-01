"""Tiered vision router with automatic fallback.

Iterates configured providers in order; on retriable failure, advances
to the next tier.  Non-retriable failures (auth, missing model) are
recorded and the failing provider is taken out of rotation for the rest
of the process so a misconfigured cloud key does not cost latency on
every call.

The router is the public surface most eyes-engine callers should use
directly:

    router = build_vision_router()
    await router.initialize()

    response = await router.analyze_frame(VisionAnalysisRequest(...))

It also exposes ``stats()`` and ``health()`` for observability.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .base import (
    VisionAnalysisRequest,
    VisionAnalysisResponse,
    VisionProvider,
    VisionProviderError,
    VisionTransitionRequest,
    VisionTransitionResponse,
)


logger = logging.getLogger(__name__)


class VisionTierRouter:
    """Fallback router across multiple vision providers.

    The router holds its tiers in evaluation order.  At each call it tries
    providers in turn, skipping any that have been marked unhealthy and
    recording successful tier per call so observers can reason about
    cloud / local mix.

    Initialization is best-effort: a tier that fails to initialise is
    excluded from the rotation and recorded in :attr:`unhealthy_reasons`,
    but the router itself reports healthy as long as at least one tier
    initialises successfully.
    """

    def __init__(
        self,
        providers: list[VisionProvider],
        *,
        default_timeout: float = 120.0,
    ):
        self._providers = providers
        self._default_timeout = default_timeout
        self._initialized = False
        # Permanent (non-retriable) failures take a tier out of rotation.
        self._poisoned: set[int] = set()
        self.unhealthy_reasons: dict[str, str] = {}

    @property
    def configured_tier_count(self) -> int:
        """Number of providers the router was constructed with."""
        return len(self._providers)

    @property
    def healthy_tier_count(self) -> int:
        """Tiers that successfully initialised and have not been poisoned."""
        return sum(
            1 for i, _ in enumerate(self._providers) if i not in self._poisoned
        )

    async def initialize(self) -> None:
        if not self._providers:
            raise VisionProviderError(
                "no vision tiers configured — set EYES_VISION_TIER{1,2,3}_* env vars",
                provider="router",
                retriable=False,
            )

        for idx, provider in enumerate(self._providers):
            try:
                await provider.initialize()
            except VisionProviderError as exc:
                self._poisoned.add(idx)
                self.unhealthy_reasons[provider.name] = str(exc)
                logger.warning(
                    "vision.router.init_failed provider=%s reason=%s",
                    provider.name, exc,
                )
            except Exception as exc:
                self._poisoned.add(idx)
                self.unhealthy_reasons[provider.name] = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "vision.router.init_unexpected provider=%s reason=%s",
                    provider.name, exc,
                )

        if self.healthy_tier_count == 0:
            raise VisionProviderError(
                "all vision tiers failed initialisation: "
                + "; ".join(f"{k}={v}" for k, v in self.unhealthy_reasons.items()),
                provider="router",
                retriable=False,
            )

        self._initialized = True

    async def shutdown(self) -> None:
        for provider in self._providers:
            try:
                await provider.shutdown()
            except Exception:  # pragma: no cover — defensive
                logger.warning("vision.router.shutdown_failed provider=%s", provider.name)
        self._initialized = False

    async def health(self) -> dict[str, Any]:
        provider_health: list[dict[str, Any]] = []
        for idx, provider in enumerate(self._providers):
            if idx in self._poisoned:
                provider_health.append({
                    "provider": provider.name,
                    "healthy": False,
                    "tier": idx + 1,
                    "reason": self.unhealthy_reasons.get(provider.name, "poisoned"),
                })
                continue
            try:
                h = await asyncio.wait_for(provider.health(), timeout=5.0)
            except Exception as exc:
                h = {"healthy": False, "provider": provider.name, "reason": str(exc)[:200]}
            h.setdefault("tier", idx + 1)
            provider_health.append(h)
        return {
            "healthy": self.healthy_tier_count > 0,
            "configured_tiers": self.configured_tier_count,
            "healthy_tiers": self.healthy_tier_count,
            "providers": provider_health,
        }

    def stats(self) -> dict[str, Any]:
        return {
            "tiers": [p.stats() for p in self._providers],
            "poisoned_tiers": sorted(self._poisoned),
        }

    # ── Main API ─────────────────────────────────────────────────

    async def analyze_frame(
        self, request: VisionAnalysisRequest,
    ) -> VisionAnalysisResponse:
        """Analyse one frame using the configured tier order.

        Returns the first successful provider's response.  Non-retriable
        errors poison that tier for the remainder of the process; retriable
        errors are logged and the router moves to the next tier.

        Raises :class:`VisionProviderError` only if every tier is exhausted.
        """
        if not self._initialized:
            raise VisionProviderError(
                "vision router not initialized — call await router.initialize() first",
                provider="router",
                retriable=False,
            )

        last_error: Optional[VisionProviderError] = None
        attempted: list[str] = []

        for idx, provider in enumerate(self._providers):
            if idx in self._poisoned:
                continue
            attempted.append(provider.name)
            try:
                response = await provider.analyze(request, retries=provider.retries)
                if attempted[:-1]:
                    # Surface fallback usage in logs so operators notice
                    # cloud → local degradation in production.
                    logger.info(
                        "vision.router.fallback_succeeded provider=%s skipped=%s",
                        provider.name, attempted[:-1],
                    )
                return response
            except VisionProviderError as exc:
                last_error = exc
                if not exc.retriable:
                    self._poisoned.add(idx)
                    self.unhealthy_reasons[provider.name] = str(exc)
                    logger.warning(
                        "vision.router.tier_poisoned provider=%s reason=%s",
                        provider.name, exc,
                    )
                else:
                    logger.warning(
                        "vision.router.tier_failed provider=%s reason=%s",
                        provider.name, exc,
                    )
                continue

        raise VisionProviderError(
            f"all configured vision tiers failed: {attempted}; last={last_error}",
            provider="router",
            retriable=False,
        ) from last_error

    async def analyze_transition(
        self, request: VisionTransitionRequest,
    ) -> VisionTransitionResponse:
        """Two-image transition analysis with tier failover.

        Same poison-on-non-retriable semantics as :meth:`analyze_frame`.
        Per-transition poisoning is INDEPENDENT of per-frame poisoning:
        an Anthropic provider that fails the transition path because
        its API doesn't support multi-image with the model selected
        can still serve single-frame requests.

        Raises VisionProviderError when every tier exhausted.
        """
        if not self._initialized:
            raise VisionProviderError(
                "vision router not initialized — call await router.initialize() first",
                provider="router",
                retriable=False,
            )

        last_error: Optional[VisionProviderError] = None
        attempted: list[str] = []

        # Separate poisoning state for the transition path so that an
        # auth failure on the transition API doesn't also disable
        # single-frame calls (they may use a different model entirely).
        if not hasattr(self, "_poisoned_transition"):
            self._poisoned_transition: set[int] = set()

        for idx, provider in enumerate(self._providers):
            if idx in self._poisoned_transition:
                continue
            attempted.append(provider.name)
            try:
                response = await provider.analyze_transition(
                    request, retries=provider.retries,
                )
                if attempted[:-1]:
                    logger.info(
                        "vision.router.transition_fallback_succeeded "
                        "provider=%s skipped=%s",
                        provider.name, attempted[:-1],
                    )
                return response
            except VisionProviderError as exc:
                last_error = exc
                if not exc.retriable:
                    self._poisoned_transition.add(idx)
                    logger.warning(
                        "vision.router.transition_tier_poisoned "
                        "provider=%s reason=%s",
                        provider.name, exc,
                    )
                else:
                    logger.warning(
                        "vision.router.transition_tier_failed "
                        "provider=%s reason=%s",
                        provider.name, exc,
                    )
                continue

        raise VisionProviderError(
            f"all configured vision tiers failed transition: {attempted}; "
            f"last={last_error}",
            provider="router",
            retriable=False,
        ) from last_error
