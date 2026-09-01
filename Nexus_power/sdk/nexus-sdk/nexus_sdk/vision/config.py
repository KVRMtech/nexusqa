"""Vision tier configuration.

The vision router reads tier specs from environment variables so operators
can switch between local (Ollama) and cloud (Anthropic / OpenAI / Gemini)
backends without code or container rebuilds.

Convention — the vision tiers mirror the LLM tiers used by Brain and
Heart engines:

    EYES_VISION_TIER1_PROVIDER   = anthropic | openai | gemini | vllm | ollama
    EYES_VISION_TIER1_MODEL      = (provider-specific model id)
    EYES_VISION_TIER1_API_KEY    = (when the provider requires it)
    EYES_VISION_TIER1_BASE_URL   = (override default endpoint)
    EYES_VISION_TIER1_TIMEOUT    = (per-call timeout in seconds)

    ... same set with TIER2 / TIER3 ...

When a tier is unset the router skips it.  When all three are unset the
router falls back to the legacy single-Ollama configuration so existing
deployments continue to work without any migration.

The ``EYES_VISION_TIER_ORDER`` env var lets operators override the default
1→2→3 fallback order, e.g. ``"3,1,2"`` to prefer local Ollama and use
cloud only on local failure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VisionTierSpec:
    """Configuration for one tier of the vision router.

    Tiers are referenced positionally (1, 2, 3) but backed by named env
    vars so a config dump shows which tier is which provider.  ``api_key``
    is read at construction time and never logged; the router stores the
    spec but providers are responsible for keeping the secret out of error
    messages.
    """

    tier: int
    provider: str
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    timeout_seconds: float = 120.0
    # Per-tier per-call retries.  The router adds inter-tier fallback on
    # top, so default 1 (= up to 2 attempts) keeps individual tier latency
    # bounded.  Cloud tiers can be tuned higher when network conditions
    # warrant it.
    retries: int = 1
    # Provider-specific extra knobs (e.g. num_predict for ollama,
    # max_image_dimension for cloud APIs).  Producers and consumers must
    # both opt into whatever keys they put here — the router does not
    # interpret them.
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def is_configured(self) -> bool:
        """True when this tier has enough information to instantiate a provider."""
        return bool(self.provider) and bool(self.model)


@dataclass
class VisionConfig:
    """Top-level vision configuration loaded from environment.

    The recommended way to construct this is via :meth:`from_env` which
    reads the standard ``EYES_VISION_*`` env vars.  Tests and unit-style
    callers can pass a hand-built instance directly to the router for
    deterministic behaviour.
    """

    tiers: list[VisionTierSpec] = field(default_factory=list)
    tier_order: list[int] = field(default_factory=lambda: [1, 2, 3])
    # Global per-tier-call timeout fallback used when a tier did not set
    # its own.  Independent of any provider-internal retry logic.
    default_timeout_seconds: float = 120.0

    def ordered_tiers(self) -> list[VisionTierSpec]:
        """Return configured tiers in router-preferred order, configured first."""
        by_tier = {t.tier: t for t in self.tiers if t.is_configured}
        return [by_tier[i] for i in self.tier_order if i in by_tier]

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "VisionConfig":
        """Build a :class:`VisionConfig` from process environment.

        ``env`` lets tests inject a dict; production passes ``None`` so
        ``os.environ`` is used.
        """
        e = dict(env if env is not None else os.environ)

        tiers: list[VisionTierSpec] = []
        for tier_num in (1, 2, 3):
            provider = e.get(f"EYES_VISION_TIER{tier_num}_PROVIDER", "").strip().lower()
            model = e.get(f"EYES_VISION_TIER{tier_num}_MODEL", "").strip()
            if not provider or not model:
                continue

            api_key = e.get(f"EYES_VISION_TIER{tier_num}_API_KEY") or None
            base_url = e.get(f"EYES_VISION_TIER{tier_num}_BASE_URL") or None
            timeout_raw = e.get(f"EYES_VISION_TIER{tier_num}_TIMEOUT", "")
            try:
                timeout_s = float(timeout_raw) if timeout_raw else 120.0
            except ValueError:
                timeout_s = 120.0
            retries_raw = e.get(f"EYES_VISION_TIER{tier_num}_RETRIES", "")
            try:
                retries = int(retries_raw) if retries_raw else 1
            except ValueError:
                retries = 1

            tiers.append(VisionTierSpec(
                tier=tier_num,
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                timeout_seconds=timeout_s,
                retries=retries,
            ))

        order_raw = e.get("EYES_VISION_TIER_ORDER", "1,2,3")
        try:
            order = [int(s.strip()) for s in order_raw.split(",") if s.strip()]
        except ValueError:
            order = [1, 2, 3]
        order = [n for n in order if 1 <= n <= 3] or [1, 2, 3]

        return cls(tiers=tiers, tier_order=order)
