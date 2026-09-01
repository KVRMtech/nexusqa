"""
Tier Manager — Manages per-engine multi-tier LLM provider configurations.

Holds the recommended tier mapping for all 11 engines and exposes
status/health endpoints for the Brain to monitor.

P0: Separated into RUNTIME_LLM_TIERS (executable via ProviderRegistry)
    and TOOLING_RECOMMENDATIONS (architectural guidance only).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Runtime LLM Tiers — Engines that CURRENTLY use TieredLLMRouter.
# These providers MUST exist in nexus_sdk.llm.registry.
# Valid providers: ollama, openai, anthropic, azure, vllm, custom, gemini
# ═══════════════════════════════════════════════════════════════

RUNTIME_LLM_TIERS: dict[str, dict[str, dict[str, str]]] = {
    "brain": {
        "tier1": {"provider": "anthropic", "model": "claude-opus-4-20250514", "role": "Meta-reasoning, cross-engine coordination"},
        "tier2": {"provider": "openai", "model": "gpt-4o", "role": "Fallback reasoning"},
        "tier3": {"provider": "ollama", "model": "llama3.1:70b", "role": "Local backup"},
    },
    "heart": {
        "tier1": {"provider": "anthropic", "model": "claude-opus-4-20250514", "role": "Core reasoning, rule extraction, test generation"},
        "tier2": {"provider": "openai", "model": "gpt-4o", "role": "Fallback reasoning"},
        "tier3": {"provider": "ollama", "model": "llama3.1:70b", "role": "Local on-prem backup"},
    },
}


# ═══════════════════════════════════════════════════════════════
# Planned LLM Tiers — Engines that WILL use LLM providers once
# their code is upgraded from rule-based/template-based to LLM.
#
# Currently:
#   - eyes:  uses Ollama LLaVA directly (multimodal — needs router
#            extension for image+text before TieredLLMRouter works)
#   - hands: purely algorithmic generators (no LLM calls today)
#   - mouth: template-based report rendering (no LLM calls today)
#   - spine: library-based parsing + chunking (no LLM calls today)
#
# These are kept as recommended configurations for future wiring.
# ═══════════════════════════════════════════════════════════════

PLANNED_LLM_TIERS: dict[str, dict[str, dict[str, str]]] = {
    "eyes": {
        "tier1": {"provider": "gemini", "model": "gemini-3-pro", "role": "Best multimodal (UI, video, OCR, flows)"},
        "tier2": {"provider": "openai", "model": "gpt-4o", "role": "Fallback vision"},
        "tier3": {"provider": "ollama", "model": "llama3.2-vision:11b", "role": "Local vision — Meta Llama (enterprise-trusted provenance)"},
    },
    "hands": {
        "tier1": {"provider": "openai", "model": "gpt-4o-mini", "role": "Best cost vs realism for synthetic data"},
        "tier2": {"provider": "anthropic", "model": "claude-haiku", "role": "Fast, cheap generation"},
        "tier3": {"provider": "ollama", "model": "llama3.1:8b", "role": "Local synthetic data"},
    },
    "mouth": {
        "tier1": {"provider": "anthropic", "model": "claude-haiku", "role": "Best clarity + cost efficiency for reports"},
        "tier2": {"provider": "openai", "model": "gpt-4o-mini", "role": "Fallback report generation"},
        "tier3": {"provider": "ollama", "model": "llama3.1:8b", "role": "Local report generation"},
    },
    "spine": {
        "tier1": {"provider": "openai", "model": "text-embedding-3-large", "role": "Best semantic retrieval quality"},
        "tier2": {"provider": "ollama", "model": "nomic-embed-text", "role": "Open-source embeddings"},
        "tier3": {"provider": "ollama", "model": "nomic-embed-text", "role": "Same local embeddings"},
    },
}


# ═══════════════════════════════════════════════════════════════
# Tooling / Architecture Recommendations — Non-LLM engines.
# These are NOT executable via the LLM provider registry.
# They describe what runtime tools/libraries the engine depends on.
# ═══════════════════════════════════════════════════════════════

TOOLING_RECOMMENDATIONS: dict[str, dict[str, dict[str, str]]] = {
    "ears": {
        "tier1": {"tool": "whisper-v3-large", "type": "local", "role": "Near-SOTA speech-to-text (already local)"},
        "tier2": {"tool": "azure-ai-speech", "type": "cloud", "role": "Cloud fallback"},
        "tier3": {"tool": "whisper-base", "type": "local", "role": "Lightweight local fallback"},
    },
    "shield": {
        "tier1": {"tool": "azure-ai-language", "type": "cloud", "role": "Enterprise-grade PII/compliance"},
        "tier2": {"tool": "presidio+gliner", "type": "local", "role": "Open-source NER + Presidio"},
        "tier3": {"tool": "presidio", "type": "local", "role": "Regex + Presidio (fully local)"},
    },
    "backbone": {
        "tier1": {"tool": "neo4j+milvus", "type": "local", "role": "Enterprise-grade graph+vector (already local)"},
        "tier2": {"tool": "qdrant", "type": "local", "role": "Alternative vector store"},
        "tier3": {"tool": "neo4j+milvus", "type": "local", "role": "Same (already enterprise-grade)"},
    },
    "legs": {
        "tier1": {"tool": "playwright", "type": "local", "role": "Browser automation (no LLM needed)"},
        "tier2": {"tool": "selenium", "type": "local", "role": "Fallback browser automation"},
        "tier3": {"tool": "playwright", "type": "local", "role": "Same (no change needed)"},
    },
    "nerves": {
        "tier1": {"tool": "api-integrations", "type": "local", "role": "Jira, Slack, GitHub, Teams APIs"},
        "tier2": {"tool": "api-integrations", "type": "local", "role": "Same (standard API layer)"},
        "tier3": {"tool": "api-integrations", "type": "local", "role": "Same (no LLM needed)"},
    },
}


@dataclass
class EngineTierStatus:
    """Status of an engine's tier configuration."""
    engine: str
    tier1: dict[str, Any] = field(default_factory=dict)
    tier2: dict[str, Any] = field(default_factory=dict)
    tier3: dict[str, Any] = field(default_factory=dict)
    active_tier: str = "tier3"
    deployment_mode: str = "on-prem"  # "on-prem", "cloud", "hybrid"
    healthy: bool = True


class TierManager:
    """
    Manages the multi-tier provider configuration across all engines.

    P0: Clearly separates RUNTIME_LLM_TIERS (executable via registry)
    from TOOLING_RECOMMENDATIONS (architectural guidance for non-LLM engines).

    Provides:
    - Recommended LLM tiers (what SHOULD be deployed for LLM engines)
    - Tooling recommendations (what libraries/tools non-LLM engines use)
    - Active tier status (what IS deployed based on env vars)
    - Health aggregation across all engine tiers
    """

    def __init__(self):
        self._llm_tiers = dict(RUNTIME_LLM_TIERS)
        self._planned_tiers = dict(PLANNED_LLM_TIERS)
        self._tooling = dict(TOOLING_RECOMMENDATIONS)
        self._engine_status: dict[str, EngineTierStatus] = {}

    def get_recommended_tiers(self) -> dict[str, dict]:
        """Return the recommended tier configuration — LLM tiers + planned + tooling."""
        combined: dict[str, dict] = {}
        for name, tiers in self._llm_tiers.items():
            combined[name] = {"type": "llm", **tiers}
        for name, tiers in self._planned_tiers.items():
            combined[name] = {"type": "planned_llm", **tiers}
        for name, recs in self._tooling.items():
            combined[name] = {"type": "tooling", **recs}
        return combined

    def get_llm_tiers(self) -> dict[str, dict]:
        """Return only executable LLM provider tiers."""
        return dict(self._llm_tiers)

    def get_tooling_recommendations(self) -> dict[str, dict]:
        """Return only non-LLM tooling recommendations."""
        return dict(self._tooling)

    def get_engine_tiers(self, engine_name: str) -> dict[str, dict[str, str]] | None:
        """Get recommended tiers for a specific engine (LLM, planned, or tooling)."""
        return (
            self._llm_tiers.get(engine_name)
            or self._planned_tiers.get(engine_name)
            or self._tooling.get(engine_name)
        )

    def is_llm_engine(self, engine_name: str) -> bool:
        """Whether this engine actively uses an LLM provider via TieredLLMRouter."""
        return engine_name in self._llm_tiers

    def is_planned_llm_engine(self, engine_name: str) -> bool:
        """Whether this engine has planned but not yet wired LLM tiers."""
        return engine_name in self._planned_tiers

    def detect_active_tiers(self) -> dict[str, EngineTierStatus]:
        """
        Detect which tiers are actually configured based on env vars.

        Only LLM engines are checked against the provider registry.
        Tooling engines get a simplified status.
        """
        statuses = {}
        all_engines = {**self._llm_tiers, **self._planned_tiers, **self._tooling}

        for engine_name, tiers in all_engines.items():
            is_llm = engine_name in self._llm_tiers
            prefix = engine_name.upper().replace("-", "_")

            # Check which tiers have env vars set
            tier1_provider = os.environ.get(f"{prefix}_TIER1_PROVIDER", "")
            tier2_provider = os.environ.get(f"{prefix}_TIER2_PROVIDER", "")
            tier3_provider = os.environ.get(f"{prefix}_TIER3_PROVIDER", "")

            # Fallback to global
            global_provider = os.environ.get("LLM_PROVIDER", "")

            active_tier = "tier3"
            mode = "on-prem"

            if is_llm:
                if tier1_provider:
                    active_tier = "tier1"
                    if tier1_provider in ("openai", "anthropic", "azure", "gemini"):
                        mode = "cloud" if not tier3_provider else "hybrid"
                elif tier2_provider:
                    active_tier = "tier2"
                    if tier2_provider in ("openai", "anthropic", "azure", "gemini"):
                        mode = "hybrid"
                elif global_provider and global_provider not in ("ollama", "vllm", "stub", "local"):
                    mode = "cloud"
            else:
                # Non-LLM engines always run locally
                active_tier = "tier1"
                mode = "on-prem"

            status = EngineTierStatus(
                engine=engine_name,
                tier1={
                    **tiers.get("tier1", {}),
                    "configured": bool(tier1_provider) if is_llm else True,
                    "active_provider": tier1_provider if is_llm else tiers.get("tier1", {}).get("tool", ""),
                },
                tier2={
                    **tiers.get("tier2", {}),
                    "configured": bool(tier2_provider) if is_llm else True,
                    "active_provider": tier2_provider if is_llm else tiers.get("tier2", {}).get("tool", ""),
                },
                tier3={
                    **tiers.get("tier3", {}),
                    # P0 fix: no more `or True` — only configured if actually set
                    "configured": bool(tier3_provider or global_provider) if is_llm else True,
                    "active_provider": (tier3_provider or global_provider or "") if is_llm else tiers.get("tier3", {}).get("tool", ""),
                },
                active_tier=active_tier,
                deployment_mode=mode,
            )
            statuses[engine_name] = status

        self._engine_status = statuses
        return statuses

    def get_deployment_summary(self) -> dict[str, Any]:
        """Get a summary of the current deployment configuration."""
        if not self._engine_status:
            self.detect_active_tiers()

        cloud_engines = []
        hybrid_engines = []
        onprem_engines = []

        for name, status in self._engine_status.items():
            if status.deployment_mode == "cloud":
                cloud_engines.append(name)
            elif status.deployment_mode == "hybrid":
                hybrid_engines.append(name)
            else:
                onprem_engines.append(name)

        # Overall mode
        if cloud_engines and not onprem_engines:
            overall = "full-cloud"
        elif onprem_engines and not cloud_engines:
            overall = "full-on-prem"
        else:
            overall = "hybrid"

        return {
            "overall_mode": overall,
            "total_engines": len(self._engine_status),
            "cloud_engines": cloud_engines,
            "hybrid_engines": hybrid_engines,
            "onprem_engines": onprem_engines,
            "engines": {
                name: {
                    "active_tier": s.active_tier,
                    "mode": s.deployment_mode,
                    "tier1_provider": s.tier1.get("active_provider", ""),
                    "tier2_provider": s.tier2.get("active_provider", ""),
                    "tier3_provider": s.tier3.get("active_provider", ""),
                }
                for name, s in self._engine_status.items()
            },
        }

    def update_tier_recommendation(
        self,
        engine_name: str,
        tier: str,
        provider: str,
        model: str,
        role: str = "",
    ):
        """Update the recommended tier for an LLM engine (runtime reconfiguration)."""
        if engine_name in self._tooling:
            logger.warning(
                "brain.tier_manager: Cannot set LLM tier for non-LLM engine '%s'",
                engine_name,
            )
            return
        # Allow updating both active and planned LLM tiers
        target = self._llm_tiers if engine_name in self._llm_tiers else self._planned_tiers
        if engine_name not in target:
            target[engine_name] = {}
        target[engine_name][tier] = {
            "provider": provider,
            "model": model,
            "role": role,
        }
        logger.info(
            "brain.tier_manager.updated",
            extra={
                "engine": engine_name,
                "tier": tier,
                "provider": provider,
                "model": model,
            },
        )

    # ── P1: Real Health Probing ────────────────────────────────

    async def probe_tier_health(self, engine_name: str) -> dict[str, Any]:
        """
        Probe whether the configured LLM providers for an engine
        are actually reachable and responding.

        Returns per-tier health: {"tier1": {"healthy": bool, "latency_ms": float}, ...}
        """
        if engine_name not in self._llm_tiers:
            tier_type = "planned_llm" if engine_name in self._planned_tiers else "tooling"
            return {"engine": engine_name, "type": tier_type, "message": "Not an active LLM engine — no provider to probe"}

        import httpx

        results: dict[str, Any] = {"engine": engine_name}
        prefix = engine_name.upper().replace("-", "_")

        # Map provider names to health probe URLs
        _PROVIDER_HEALTH_URLS = {
            "ollama": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434") + "/api/tags",
            "vllm": os.environ.get("VLLM_BASE_URL", "http://localhost:8000") + "/health",
        }
        # Cloud providers: we can't cheaply probe without API keys, so just check env var presence
        _CLOUD_PROVIDERS = {"openai", "anthropic", "azure", "gemini"}

        for tier_num in (1, 2, 3):
            tier_key = f"tier{tier_num}"
            provider_env = os.environ.get(f"{prefix}_TIER{tier_num}_PROVIDER", "")
            if not provider_env:
                results[tier_key] = {"configured": False, "healthy": False}
                continue

            results[tier_key] = {"configured": True, "provider": provider_env}

            if provider_env in _CLOUD_PROVIDERS:
                # For cloud providers, check API key presence AND validate
                # with a lightweight API call to catch revoked/invalid keys.
                per_engine_key = f"{prefix}_TIER{tier_num}_API_KEY"
                global_key = f"{provider_env.upper()}_API_KEY"
                per_engine_val = os.environ.get(per_engine_key, "")
                global_val = os.environ.get(global_key, "")
                api_key = per_engine_val or global_val
                used_var = per_engine_key if per_engine_val else global_key

                if not api_key:
                    results[tier_key]["healthy"] = False
                    results[tier_key]["check"] = f"API key MISSING ({used_var})"
                else:
                    # Validate key by making a lightweight API call
                    import time as _time
                    start = _time.monotonic()
                    try:
                        validated = await self._validate_cloud_key(
                            provider_env, api_key, httpx,
                        )
                        latency_ms = (_time.monotonic() - start) * 1000
                        results[tier_key]["healthy"] = validated
                        results[tier_key]["latency_ms"] = round(latency_ms, 1)
                        results[tier_key]["check"] = (
                            f"API key valid ({used_var})"
                            if validated
                            else f"API key INVALID or expired ({used_var})"
                        )
                    except Exception as exc:
                        latency_ms = (_time.monotonic() - start) * 1000
                        results[tier_key]["healthy"] = False
                        results[tier_key]["latency_ms"] = round(latency_ms, 1)
                        results[tier_key]["check"] = f"API key validation failed: {exc}"
            elif provider_env in _PROVIDER_HEALTH_URLS:
                # For local providers, do an HTTP health check
                url = _PROVIDER_HEALTH_URLS[provider_env]
                try:
                    import time
                    start = time.monotonic()
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(url)
                    latency_ms = (time.monotonic() - start) * 1000
                    results[tier_key]["healthy"] = resp.status_code < 400
                    results[tier_key]["latency_ms"] = round(latency_ms, 1)
                    results[tier_key]["check"] = f"HTTP {resp.status_code}"
                except Exception as exc:
                    results[tier_key]["healthy"] = False
                    results[tier_key]["check"] = f"Unreachable: {exc}"
            else:
                results[tier_key]["healthy"] = None
                results[tier_key]["check"] = "Unknown provider — cannot probe"

        return results

    async def probe_all_health(self) -> dict[str, dict]:
        """Probe health for all LLM engines."""
        results = {}
        for engine_name in self._llm_tiers:
            results[engine_name] = await self.probe_tier_health(engine_name)
        return results

    @staticmethod
    async def _validate_cloud_key(
        provider: str, api_key: str, httpx_mod: Any,
    ) -> bool:
        """Validate a cloud provider API key with a lightweight API call.

        Uses the cheapest possible endpoint (list models) to confirm the
        key is valid without incurring inference cost.
        """
        async with httpx_mod.AsyncClient(timeout=10.0) as client:
            if provider == "openai":
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                return resp.status_code == 200
            elif provider == "anthropic":
                # Anthropic doesn't have a /models endpoint, so we use
                # a minimal messages call that will fail with a clear
                # auth error if the key is invalid.
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-3-haiku-20240307",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                # 200 = valid, 401 = invalid key, 400/429 = valid key but other issue
                return resp.status_code != 401
            elif provider == "gemini":
                resp = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                )
                return resp.status_code == 200
            elif provider == "azure":
                # Azure needs endpoint + deployment, hard to probe generically
                return True
        return False
