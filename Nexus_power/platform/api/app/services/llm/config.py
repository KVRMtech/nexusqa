"""Env-driven configuration for the LLM router.

The router supports an arbitrary number of tiers and an arbitrary
mapping from task names to tiers.  Every parameter is read from the
environment at startup; nothing is hardcoded.

Env contract
============

Tier enrollment
---------------
LLM_TIERS                      comma-separated tier names (e.g. ``tier_fast,tier_balanced,tier_premium``).
                               When empty, the router operates in "no LLM configured" mode and every
                               call returns ``finish_reason=ERROR``.  Application services check for
                               this case via ``router.has_tier(task)`` and fall back deterministically.

LLM_DEFAULT_TIER               tier name used when no per-task override matches.

Per-tier config (replace ``<TIER>`` with the upper-cased tier name —
spaces and hyphens normalised to underscores)
---------------------------------------------
LLM_TIER_<TIER>_PROVIDER       one of: ``ollama``, ``openai_compat``, ``anthropic``.
LLM_TIER_<TIER>_BASE_URL       provider base URL (e.g. ``http://ollama:11434``,
                               ``https://api.openai.com/v1``, ``https://api.anthropic.com``).
LLM_TIER_<TIER>_API_KEY        provider API key (empty for Ollama; required for OpenAI/Anthropic).
LLM_TIER_<TIER>_MODEL          model identifier (e.g. ``llama3.2:3b``, ``gpt-4o-mini``,
                               ``claude-sonnet-4-6``).
LLM_TIER_<TIER>_TIMEOUT_S      per-call timeout in seconds (default 30.0).
LLM_TIER_<TIER>_MAX_RETRIES    retry count for transient failures (default 2).
LLM_TIER_<TIER>_FALLBACK       fallback tier name when this tier exhausts retries (empty = no fallback).
LLM_TIER_<TIER>_EXTRA_HEADERS  optional JSON object of extra headers
                               (e.g. {"OpenAI-Beta": "responses=1"}).

Per-task routing
----------------
LLM_TASK_<TASK_NAME>           tier name to use for this task (e.g. ``LLM_TASK_CAPTION=tier_fast``).
                               Unknown task names fall back to ``LLM_DEFAULT_TIER``.

Examples
--------
Minimal single-tier (Ollama only):

    LLM_TIERS=tier_fast
    LLM_DEFAULT_TIER=tier_fast
    LLM_TIER_TIER_FAST_PROVIDER=ollama
    LLM_TIER_TIER_FAST_BASE_URL=http://ollama:11434
    LLM_TIER_TIER_FAST_MODEL=llama3.2:3b

Three-tier with fallback chain:

    LLM_TIERS=tier_fast,tier_balanced,tier_premium
    LLM_DEFAULT_TIER=tier_balanced

    LLM_TIER_TIER_FAST_PROVIDER=ollama
    LLM_TIER_TIER_FAST_BASE_URL=http://ollama:11434
    LLM_TIER_TIER_FAST_MODEL=llama3.2:3b

    LLM_TIER_TIER_BALANCED_PROVIDER=openai_compat
    LLM_TIER_TIER_BALANCED_BASE_URL=https://api.openai.com/v1
    LLM_TIER_TIER_BALANCED_API_KEY=${OPENAI_API_KEY}
    LLM_TIER_TIER_BALANCED_MODEL=gpt-4o-mini
    LLM_TIER_TIER_BALANCED_FALLBACK=tier_fast

    LLM_TIER_TIER_PREMIUM_PROVIDER=anthropic
    LLM_TIER_TIER_PREMIUM_BASE_URL=https://api.anthropic.com
    LLM_TIER_TIER_PREMIUM_API_KEY=${ANTHROPIC_API_KEY}
    LLM_TIER_TIER_PREMIUM_MODEL=claude-sonnet-4-6
    LLM_TIER_TIER_PREMIUM_FALLBACK=tier_balanced

    LLM_TASK_CAPTION=tier_fast
    LLM_TASK_STORY=tier_balanced
    LLM_TASK_BPMN_REFINE=tier_balanced
    LLM_TASK_KG_REASONING=tier_premium
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace as _dc_replace
from typing import Mapping


class LLMConfigError(ValueError):
    """Raised when LLM env vars cannot be parsed.

    A separate exception type so application code can distinguish
    "no LLM configured" (silent fallback) from "LLM env malformed"
    (fail-fast).
    """


_NORM_TIER_RE = re.compile(r"[^A-Z0-9_]")


def _normalise_tier_name(tier: str) -> str:
    """Normalise a tier name for env-var lookup.

    ``tier-fast`` and ``tier fast`` both map to ``TIER_FAST`` so the
    env vars are predictable regardless of how the user wrote the name.
    """
    return _NORM_TIER_RE.sub("_", tier.upper().strip())


def _env_str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def _env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise LLMConfigError(f"{name} must be >= {min_value}, got {value}")
    return value


def _env_float(name: str, default: float, *, min_value: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(f"{name} must be a float, got {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise LLMConfigError(f"{name} must be >= {min_value}, got {value}")
    return value


def _env_json(name: str, default: dict | None = None) -> dict:
    """Parse a JSON object env var.  Empty value returns ``default or {}``."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default or {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMConfigError(f"{name} must be valid JSON, got {raw!r}") from exc
    if not isinstance(value, dict):
        raise LLMConfigError(f"{name} must be a JSON object, got {type(value).__name__}")
    return value


_SUPPORTED_PROVIDERS = {"ollama", "openai_compat", "anthropic"}


@dataclass(frozen=True)
class LLMTierConfig:
    """One tier's settings.

    Tiers are the unit of provider-swappability.  Two different tier
    names CAN point at the same provider (e.g. ``tier_fast`` and
    ``tier_balanced`` both using OpenAI with different model names).
    """

    name: str
    provider: str
    base_url: str
    api_key: str
    model: str
    timeout_s: float
    max_retries: int
    fallback_tier: str
    extra_headers: Mapping[str, str]

    def redacted(self) -> dict:
        """Return a logging-safe representation (no API keys)."""
        return {
            "name": self.name,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "fallback_tier": self.fallback_tier or "(none)",
            "api_key_present": bool(self.api_key),
            "extra_header_keys": sorted(self.extra_headers.keys()),
        }


@dataclass(frozen=True)
class LLMConfig:
    """Aggregate config — produced by ``load_config()``."""

    tiers: Mapping[str, LLMTierConfig]
    task_routing: Mapping[str, str]   # task_name -> tier_name
    default_tier: str

    def tier_for_task(self, task: str) -> str | None:
        """Return the tier configured for ``task``, falling back to default.

        Returns ``None`` only when no tiers are configured (i.e. the
        env has ``LLM_TIERS`` unset or empty).  Application services
        should check this and run their deterministic fallback path.
        """
        if not self.tiers:
            return None
        # Per-task override
        mapped = self.task_routing.get(task.lower())
        if mapped and mapped in self.tiers:
            return mapped
        # Default tier
        if self.default_tier and self.default_tier in self.tiers:
            return self.default_tier
        # First tier in insertion order
        return next(iter(self.tiers.keys()))

    def redacted(self) -> dict:
        return {
            "tiers": {name: tier.redacted() for name, tier in self.tiers.items()},
            "default_tier": self.default_tier or "(none)",
            "task_routing": dict(self.task_routing),
        }


def _load_tier(tier_name: str) -> LLMTierConfig:
    norm = _normalise_tier_name(tier_name)
    prefix = f"LLM_TIER_{norm}"

    provider = _env_str(f"{prefix}_PROVIDER").lower()
    if not provider:
        raise LLMConfigError(
            f"Tier {tier_name!r} declared in LLM_TIERS but {prefix}_PROVIDER is unset"
        )
    if provider not in _SUPPORTED_PROVIDERS:
        raise LLMConfigError(
            f"{prefix}_PROVIDER={provider!r} unknown; supported: {sorted(_SUPPORTED_PROVIDERS)}"
        )

    base_url = _env_str(f"{prefix}_BASE_URL").rstrip("/")
    if not base_url:
        raise LLMConfigError(f"{prefix}_BASE_URL is required for provider {provider}")

    model = _env_str(f"{prefix}_MODEL")
    if not model:
        raise LLMConfigError(f"{prefix}_MODEL is required for tier {tier_name}")

    api_key = _env_str(f"{prefix}_API_KEY")
    # Ollama on local network is fine without API key; OpenAI/Anthropic
    # require one in production but we do not fail-fast here — the
    # provider will surface a 401 at call time and the router will
    # fall back.  Failing-fast would make local development noisy.

    return LLMTierConfig(
        name=tier_name,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_s=_env_float(f"{prefix}_TIMEOUT_S", 30.0, min_value=0.5),
        max_retries=_env_int(f"{prefix}_MAX_RETRIES", 2, min_value=0),
        fallback_tier=_env_str(f"{prefix}_FALLBACK"),
        extra_headers=_env_json(f"{prefix}_EXTRA_HEADERS"),
    )


_TASK_RE = re.compile(r"^LLM_TASK_(.+)$")


def _load_task_routing(known_tiers: set[str]) -> dict[str, str]:
    """Collect every LLM_TASK_<NAME>=<tier> from the environment.

    Tier values that do not match a known tier are skipped with a
    warning (raises in dev to surface typos, silent in prod).
    """
    routing: dict[str, str] = {}
    for env_key, raw_value in os.environ.items():
        match = _TASK_RE.match(env_key)
        if not match:
            continue
        task_name = match.group(1).lower()
        tier_value = raw_value.strip()
        if not tier_value:
            continue
        if tier_value not in known_tiers:
            raise LLMConfigError(
                f"{env_key}={tier_value!r} but that tier is not declared in LLM_TIERS "
                f"(known: {sorted(known_tiers)})"
            )
        routing[task_name] = tier_value
    return routing


def load_config() -> LLMConfig:
    """Read LLM_* env vars and return an immutable ``LLMConfig``.

    Returns an empty config (no tiers) when ``LLM_TIERS`` is unset —
    the router treats this as "LLM disabled, all calls return ERROR".
    Raises ``LLMConfigError`` on malformed values so misconfigured
    deployments fail fast.
    """
    tier_csv = _env_str("LLM_TIERS")
    if not tier_csv:
        return LLMConfig(tiers={}, task_routing={}, default_tier="")

    tier_names = [t.strip() for t in tier_csv.split(",") if t.strip()]
    if not tier_names:
        return LLMConfig(tiers={}, task_routing={}, default_tier="")

    tiers: dict[str, LLMTierConfig] = {}
    for name in tier_names:
        if name in tiers:
            raise LLMConfigError(f"LLM_TIERS contains duplicate tier {name!r}")
        tiers[name] = _load_tier(name)

    # Validate fallback chain references are well-formed before exposing.
    for tier in tiers.values():
        if tier.fallback_tier and tier.fallback_tier not in tiers:
            raise LLMConfigError(
                f"LLM_TIER_{_normalise_tier_name(tier.name)}_FALLBACK="
                f"{tier.fallback_tier!r} but that tier is not declared in LLM_TIERS"
            )
        if tier.fallback_tier == tier.name:
            raise LLMConfigError(
                f"Tier {tier.name!r} declares itself as its own fallback"
            )

    default_tier = _env_str("LLM_DEFAULT_TIER", next(iter(tiers.keys())))
    if default_tier not in tiers:
        raise LLMConfigError(
            f"LLM_DEFAULT_TIER={default_tier!r} but that tier is not declared in LLM_TIERS"
        )

    task_routing = _load_task_routing(set(tiers.keys()))

    return _harden_for_resilience(
        LLMConfig(
            tiers=tiers,
            task_routing=task_routing,
            default_tier=default_tier,
        )
    )


# Cloud models known to be retired/non-functional as of 2026-06 (Google
# deprecated gemini-2.0-flash → HTTP 404 "no longer available"; every current
# Gemini variant also fails the OpenAI-compat vision+tool call). Listed here so
# a transient gpt-4o blip can't cascade THROUGH a dead tier to the weak local
# model and silently emit garbage. Code-level (env-independent) so it ships via
# docker cp without a container recreate.
_DEAD_MODELS = frozenset({"gemini-2.0-flash", "gemini-2.0-flash-001"})
_CLOUD_MIN_RETRIES = 6


def _harden_for_resilience(cfg: "LLMConfig") -> "LLMConfig":
    """Post-load hardening (additive; no-op when nothing matches):

    1. Splice known-dead tiers OUT of every fallback chain (point past them),
       so a fallback never wastes a round-trip on a 404 model.
    2. Give the working cloud workhorse tiers more retries, so transient
       rate-limits/timeouts recover on the RELIABLE model instead of cascading
       to the weak local fallback (the cause of the llava-garbage Pages & Forms).
    """
    if not cfg.tiers:
        return cfg
    tiers = dict(cfg.tiers)
    dead = {name for name, t in tiers.items() if t.model in _DEAD_MODELS}

    # Resilient fallback chain: route through EVERY working CLOUD tier before
    # the weak local model. A transient rate limit on the primary (e.g. OpenAI
    # gpt-4o TPM) then recovers on the OTHER cloud model (e.g. claude) instead of
    # cascading to the schema-garbling local model (llava). Roles are discovered
    # by task routing + provider — no hardcoded tier names or per-artifact data.
    primary = (
        cfg.task_routing.get("page_action_extraction")
        or cfg.task_routing.get("form_snapshot_extraction")
        or cfg.task_routing.get("page_visit_identity")
    )
    cloud = [
        n for n, t in tiers.items()
        if t.provider in ("openai_compat", "anthropic") and n not in dead
    ]
    local = (
        next((n for n, t in tiers.items() if t.provider == "ollama" and "vision" in n), None)
        or next((n for n, t in tiers.items() if t.provider == "ollama"), None)
    )
    if primary in cloud:
        ordered = [primary] + [n for n in cloud if n != primary]
        chain = ordered + ([local] if local else [])
        for i in range(len(chain) - 1):
            tiers[chain[i]] = _dc_replace(tiers[chain[i]], fallback_tier=chain[i + 1])

    # Splice any remaining dead tiers out of fallback chains not covered above.
    for name, t in list(tiers.items()):
        fb = t.fallback_tier
        guard = 0
        while fb in dead and fb in tiers and guard <= len(tiers):
            fb = tiers[fb].fallback_tier
            guard += 1
        if fb != t.fallback_tier:
            tiers[name] = _dc_replace(tiers[name], fallback_tier=fb)
    for name, t in list(tiers.items()):
        if (
            t.provider in ("openai_compat", "anthropic")
            and name not in dead
            and t.max_retries < _CLOUD_MIN_RETRIES
        ):
            tiers[name] = _dc_replace(tiers[name], max_retries=_CLOUD_MIN_RETRIES)
    return _dc_replace(cfg, tiers=tiers)
