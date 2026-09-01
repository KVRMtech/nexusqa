"""
LLM cost tracking — per-provider $/token table + cost calculator.

Prices are listed per **1 million tokens** because that's how every
provider publishes them. Stored as USD floats; converted to per-token
internally for the math.

Sources (verify at audit time — providers change pricing without notice):
  - Anthropic:  https://docs.anthropic.com/en/api/pricing
  - OpenAI:     https://openai.com/api/pricing/
  - Azure:      https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/
  - Gemini:     https://ai.google.dev/pricing
  - Ollama:     $0 (self-hosted)
  - vLLM:       $0 (self-hosted)

These are list prices. Enterprise contracts may have negotiated rates;
operators can override per-tenant via the BillingProfile (Phase 16
work). Until then, list-price approximation is the visibility floor.

The table maps model-name-prefix → (input_price_per_1m, output_price_per_1m).
We match by prefix so model-version churn (e.g. claude-opus-4 →
claude-opus-4-20250514) doesn't break the lookup; the longest matching
prefix wins.

If a model isn't in the table, `cost_usd_for()` returns 0.0 and logs a
warning. Cost dashboards then show "$0" for that model, which is
visibly wrong and surfaces the gap. The alternative — guessing a
price — would silently mislead.
"""

from __future__ import annotations

import logging
import os
from typing import NamedTuple

logger = logging.getLogger(__name__)


class _Pricing(NamedTuple):
    """List price in USD per 1 million tokens."""
    input_per_1m_usd: float
    output_per_1m_usd: float


# Provider/model prefix → list price.
# Prefix match: longest match wins. Cite the source in the comment so
# anyone updating the table knows where they're going.
_PRICE_TABLE: dict[str, _Pricing] = {
    # ─── Anthropic Claude — anthropic.com/pricing ──
    "claude-opus-4":            _Pricing(15.00, 75.00),  # Opus 4 / 4.1 / 4.5 / 4.7
    "claude-sonnet-4":          _Pricing( 3.00, 15.00),
    "claude-haiku-4":           _Pricing( 0.80,  4.00),
    "claude-3-5-sonnet":        _Pricing( 3.00, 15.00),
    "claude-3-5-haiku":         _Pricing( 0.80,  4.00),
    "claude-3-opus":            _Pricing(15.00, 75.00),
    "claude-3-sonnet":          _Pricing( 3.00, 15.00),
    "claude-3-haiku":           _Pricing( 0.25,  1.25),

    # ─── OpenAI — openai.com/api/pricing ──
    "gpt-5":                    _Pricing( 5.00, 20.00),  # placeholder; verify on release
    "gpt-4o-mini":              _Pricing( 0.15,  0.60),
    "gpt-4o":                   _Pricing( 2.50, 10.00),
    "gpt-4-turbo":              _Pricing(10.00, 30.00),
    "gpt-4":                    _Pricing(30.00, 60.00),
    "gpt-3.5-turbo":            _Pricing( 0.50,  1.50),
    "o1-preview":               _Pricing(15.00, 60.00),
    "o1-mini":                  _Pricing( 3.00, 12.00),

    # ─── Google Gemini — ai.google.dev/pricing ──
    "gemini-2.5-pro":           _Pricing( 1.25,  5.00),
    "gemini-2.5-flash":         _Pricing( 0.30,  2.50),
    "gemini-2.0-flash":         _Pricing( 0.10,  0.40),
    "gemini-1.5-pro":           _Pricing( 1.25,  5.00),
    "gemini-1.5-flash":         _Pricing( 0.075, 0.30),

    # ─── Azure OpenAI ──
    # Azure publishes the same per-token list price as OpenAI direct,
    # but enterprise contracts often include a managed-service
    # surcharge applied by the bill processor (typically 5-15%
    # depending on commit + region). Model the list price here; layer
    # the surcharge via NEXUS_LLM_AZURE_SURCHARGE_PCT (default 0).
    #
    # Match on the Azure deployment name (env: AZURE_OPENAI_DEPLOYMENT).
    # Customers commonly name deployments after the underlying model
    # ("gpt-4o", "gpt-4o-mini"), in which case the OpenAI-direct rows
    # above already match by prefix. The entries below pin a few
    # Azure-flavored deployment naming conventions we've seen.
    "azure-gpt-4o-mini":        _Pricing( 0.15,  0.60),
    "azure-gpt-4o":             _Pricing( 2.50, 10.00),
    "azure-gpt-4-turbo":        _Pricing(10.00, 30.00),
    "azure-gpt-4":              _Pricing(30.00, 60.00),
    "azure-gpt-35-turbo":       _Pricing( 0.50,  1.50),

    # ─── Self-hosted: zero cost ──
    "llama":                    _Pricing(0.0, 0.0),  # Ollama
    "mistral":                  _Pricing(0.0, 0.0),
    "phi":                      _Pricing(0.0, 0.0),
    "moondream":                _Pricing(0.0, 0.0),  # vision (Ollama)
    "llava":                    _Pricing(0.0, 0.0),  # vision (Ollama)
    "qwen":                     _Pricing(0.0, 0.0),
}


def _provider_aware_key(provider: str, model: str) -> str:
    """Some models share names across providers (e.g. llama via Ollama
    vs llama via Bedrock). Use provider as a disambiguator when needed.

    Today the table is unique by model prefix, so this is mostly a
    pass-through. Hook for the future."""
    return (model or "").strip().lower()


def _azure_surcharge_multiplier() -> float:
    """Read NEXUS_LLM_AZURE_SURCHARGE_PCT once per call; cheap.

    Example: ``NEXUS_LLM_AZURE_SURCHARGE_PCT=10`` → 1.10x multiplier on
    every Azure request. Default 0 (no surcharge — list-price parity).
    """
    raw = os.environ.get("NEXUS_LLM_AZURE_SURCHARGE_PCT", "0").strip()
    try:
        pct = float(raw)
    except ValueError:
        return 1.0
    return 1.0 + max(0.0, pct) / 100.0


def cost_usd_for(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Return the list-price USD cost for one request.

    Returns 0.0 for unknown models (with a one-shot WARNING log). That
    is intentional — guessing would hide the gap. Operators should add
    the missing model to _PRICE_TABLE.

    Azure provider: applies the NEXUS_LLM_AZURE_SURCHARGE_PCT multiplier
    on top of the list price (default 0% = parity with OpenAI direct).

    Examples:
        >>> cost_usd_for("anthropic", "claude-opus-4-20250514", 1000, 500)
        # → 0.015 + 0.0375 = 0.0525
    """
    if not model:
        return 0.0
    pricing = _lookup(provider, model)
    if pricing is None:
        _log_missing_model(provider, model)
        return 0.0

    cost = (
        (input_tokens / 1_000_000.0) * pricing.input_per_1m_usd
        + (output_tokens / 1_000_000.0) * pricing.output_per_1m_usd
    )

    # Azure surcharge: managed-service uplift on top of list price.
    if (provider or "").strip().lower() == "azure":
        cost *= _azure_surcharge_multiplier()

    return round(cost, 6)


def _lookup(provider: str, model: str) -> _Pricing | None:
    """Longest-prefix match against the price table."""
    key = _provider_aware_key(provider, model)
    if not key:
        return None
    # Walk the table looking for the longest matching prefix.
    best: tuple[str, _Pricing] | None = None
    for prefix, pricing in _PRICE_TABLE.items():
        if key.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, pricing)
    return best[1] if best else None


_missing_logged: set[str] = set()


def _log_missing_model(provider: str, model: str) -> None:
    """One-shot per (provider, model) — don't spam on every request."""
    key = f"{provider}:{model}"
    if key in _missing_logged:
        return
    _missing_logged.add(key)
    logger.warning(
        "llm.pricing.unknown_model",
        extra={
            "provider": provider,
            "model": model,
            "advice": (
                "Add this model to nexus_sdk/llm/pricing.py:_PRICE_TABLE "
                "to enable cost tracking. Until then, "
                "nexus_llm_cost_usd_total reports $0 for this model."
            ),
        },
    )


def register_model_price(
    prefix: str,
    input_per_1m_usd: float,
    output_per_1m_usd: float,
) -> None:
    """Register a price at runtime — used by operators who don't want
    to fork the SDK to ship a new model. Call from engine startup."""
    _PRICE_TABLE[prefix.lower()] = _Pricing(
        input_per_1m_usd, output_per_1m_usd,
    )
    logger.info(
        "llm.pricing.registered",
        extra={
            "prefix": prefix,
            "input_per_1m_usd": input_per_1m_usd,
            "output_per_1m_usd": output_per_1m_usd,
        },
    )


def get_known_model_prefixes() -> list[str]:
    """Useful for operator audits — which models do we have pricing for?"""
    return sorted(_PRICE_TABLE.keys())


# ─── Env-driven price override (per-tenant negotiated rates) ────────


def load_price_overrides_from_env() -> None:
    """Parse NEXUS_LLM_PRICE_OVERRIDES env var of the form:

        NEXUS_LLM_PRICE_OVERRIDES="claude-opus-4:10.00:50.00,gpt-4o:2.00:8.00"

    Format per entry: `prefix:input_per_1m_usd:output_per_1m_usd`.
    Multiple entries comma-separated. Used to apply a customer's
    negotiated rates without rebuilding the image.
    """
    raw = os.environ.get("NEXUS_LLM_PRICE_OVERRIDES", "").strip()
    if not raw:
        return
    for entry in raw.split(","):
        parts = entry.strip().split(":")
        if len(parts) != 3:
            logger.warning(
                "llm.pricing.override_malformed",
                extra={"entry": entry, "expected": "prefix:input:output"},
            )
            continue
        try:
            register_model_price(parts[0], float(parts[1]), float(parts[2]))
        except ValueError as exc:
            logger.warning(
                "llm.pricing.override_parse_failed",
                extra={"entry": entry, "error": str(exc)},
            )


# Apply env overrides on import.
load_price_overrides_from_env()


__all__ = [
    "cost_usd_for",
    "register_model_price",
    "get_known_model_prefixes",
    "load_price_overrides_from_env",
]
