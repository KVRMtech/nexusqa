"""Vision provider registry and router builder.

Maps the string ``provider`` field of a :class:`VisionTierSpec` to the
correct concrete :class:`VisionProvider` subclass.  Third-party providers
can register their classes via :func:`register_vision_provider` so
custom Tier-1 / Tier-2 backends drop in without modifying the SDK.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .base import VisionProvider, VisionProviderError
from .config import VisionConfig, VisionTierSpec
from .providers import (
    AnthropicVisionProvider,
    OllamaVisionProvider,
    OpenAIVisionProvider,
)
from .tiered import VisionTierRouter


logger = logging.getLogger(__name__)


# ── Provider factory map ─────────────────────────────────────────────────────

ProviderFactory = Callable[[VisionTierSpec], VisionProvider]


def _make_ollama(spec: VisionTierSpec) -> VisionProvider:
    return OllamaVisionProvider(spec)


def _make_anthropic(spec: VisionTierSpec) -> VisionProvider:
    return AnthropicVisionProvider(spec)


def _make_openai(spec: VisionTierSpec) -> VisionProvider:
    return OpenAIVisionProvider(spec)


REGISTERED_VISION_PROVIDERS: dict[str, ProviderFactory] = {
    "ollama": _make_ollama,
    "anthropic": _make_anthropic,
    "openai": _make_openai,
}


def register_vision_provider(name: str, factory: ProviderFactory) -> None:
    """Register a custom vision provider factory.

    ``name`` must be lower-cased and stable; it's the value operators set
    in ``EYES_VISION_TIER{N}_PROVIDER``.  Re-registering an existing name
    silently replaces the prior factory — useful for tests that want to
    inject a stub.
    """
    REGISTERED_VISION_PROVIDERS[name.strip().lower()] = factory


def _instantiate_provider(spec: VisionTierSpec) -> VisionProvider:
    factory = REGISTERED_VISION_PROVIDERS.get(spec.provider)
    if factory is None:
        raise VisionProviderError(
            f"unknown vision provider {spec.provider!r} for tier{spec.tier} "
            f"(registered: {sorted(REGISTERED_VISION_PROVIDERS)})",
            provider=spec.provider,
            retriable=False,
        )
    return factory(spec)


def build_vision_router(config: Optional[VisionConfig] = None) -> VisionTierRouter:
    """Build a :class:`VisionTierRouter` from the supplied or env-loaded config.

    The router is returned uninitialised; call ``await router.initialize()``
    before use.  A router with no configured tiers raises on initialise so
    callers fail fast when no vision backend is wired up.
    """
    cfg = config if config is not None else VisionConfig.from_env()

    providers: list[VisionProvider] = []
    for spec in cfg.ordered_tiers():
        try:
            providers.append(_instantiate_provider(spec))
        except VisionProviderError as exc:
            # Don't fail the whole router construction over one bad tier;
            # log and keep going so the remaining tiers can serve.
            logger.warning(
                "vision.tier.skipped tier=%s provider=%s reason=%s",
                spec.tier, spec.provider, exc,
            )

    return VisionTierRouter(providers=providers, default_timeout=cfg.default_timeout_seconds)
