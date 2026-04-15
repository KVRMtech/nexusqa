"""
LLM Configuration — Single source of truth for provider settings.

All config is read from environment variables with sensible defaults.
Change the provider by setting LLM_PROVIDER env var.

Environment Variables:
  LLM_PROVIDER          : "ollama" | "vllm" | "openai" | "azure" | "anthropic" | "gemini" | "custom"
  LLM_MODEL             : Model name (e.g., "gpt-4o", "claude-3-5-sonnet-20241022", "llama3.1:70b")
  LLM_API_KEY           : API key for cloud providers (OpenAI, Anthropic, Azure)
  LLM_API_BASE_URL      : Base URL override (custom endpoints, Azure, Ollama, vLLM)
  LLM_TEMPERATURE       : Generation temperature (default: 0.1 for deterministic output)
  LLM_MAX_TOKENS        : Max output tokens (default: 4096)
  LLM_TIMEOUT           : Request timeout in seconds (default: 300)
  LLM_MAX_RETRIES       : Max retries on transient failure (default: 3)
  LLM_CONTEXT_WINDOW    : Context window size (default: 32768)
  LLM_JSON_MODE         : Request JSON output format (default: true)

  # Azure-specific
  AZURE_OPENAI_ENDPOINT : Azure OpenAI resource endpoint
  AZURE_OPENAI_API_VERSION : API version (default: "2024-06-01")
  AZURE_OPENAI_DEPLOYMENT : Deployment name

  # Ollama-specific
  OLLAMA_BASE_URL       : Ollama server URL (default: http://localhost:11434)
  OLLAMA_MODEL          : Ollama model name (default: llama3.1:8b)

  # vLLM-specific
  VLLM_BASE_URL         : vLLM server URL (default: http://localhost:8000)
  VLLM_MODEL_PATH       : Local model path for direct loading
"""

from __future__ import annotations

from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field


class LLMConfig(BaseSettings):
    """
    Unified LLM configuration. All settings can be overridden via environment variables.

    Provider selection cascade:
    1. Explicit LLM_PROVIDER env var
    2. Auto-detect based on API key presence
    3. Fallback to "ollama" (on-prem default)
    """

    # ─── Core settings ─────────────────────────────────────────
    provider: str = Field(
        default="ollama",
        alias="LLM_PROVIDER",
        description="LLM provider: ollama, vllm, openai, azure, anthropic, gemini, custom",
    )
    model: str = Field(
        default="",
        alias="LLM_MODEL",
        description="Model identifier (provider-specific)",
    )
    api_key: str = Field(
        default="",
        alias="LLM_API_KEY",
        description="API key for cloud providers",
    )
    api_base_url: str = Field(
        default="",
        alias="LLM_API_BASE_URL",
        description="Base URL override for provider endpoint",
    )

    # ─── Generation parameters ────────────────────────────────
    temperature: float = Field(
        default=0.1,
        alias="LLM_TEMPERATURE",
        description="Sampling temperature (0.0 = deterministic)",
    )
    max_tokens: int = Field(
        default=4096,
        alias="LLM_MAX_TOKENS",
        description="Maximum tokens in response",
    )
    context_window: int = Field(
        default=32768,
        alias="LLM_CONTEXT_WINDOW",
        description="Maximum context window size",
    )
    json_mode: bool = Field(
        default=True,
        alias="LLM_JSON_MODE",
        description="Request JSON structured output",
    )
    top_p: float = Field(
        default=1.0,
        alias="LLM_TOP_P",
        description="Nucleus sampling parameter",
    )

    # ─── Reliability ──────────────────────────────────────────
    timeout: int = Field(
        default=300,
        alias="LLM_TIMEOUT",
        description="Request timeout in seconds",
    )
    max_retries: int = Field(
        default=3,
        alias="LLM_MAX_RETRIES",
        description="Max retry attempts on transient error",
    )
    retry_delay: float = Field(
        default=1.0,
        alias="LLM_RETRY_DELAY",
        description="Base delay between retries (exponential backoff)",
    )

    # ─── Azure OpenAI specific ────────────────────────────────
    azure_endpoint: str = Field(
        default="",
        alias="AZURE_OPENAI_ENDPOINT",
        description="Azure OpenAI resource endpoint",
    )
    azure_api_version: str = Field(
        default="2024-06-01",
        alias="AZURE_OPENAI_API_VERSION",
        description="Azure OpenAI API version",
    )
    azure_deployment: str = Field(
        default="",
        alias="AZURE_OPENAI_DEPLOYMENT",
        description="Azure OpenAI deployment name",
    )

    # ─── Ollama specific ──────────────────────────────────────
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        alias="OLLAMA_BASE_URL",
        description="Ollama server URL",
    )
    ollama_model: str = Field(
        default="llama3.1:8b",
        alias="OLLAMA_MODEL",
        description="Ollama model name",
    )

    # ─── Gemini specific ──────────────────────────────────────
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com",
        alias="GEMINI_BASE_URL",
        description="Google Gemini API base URL",
    )

    # ─── vLLM specific ────────────────────────────────────────
    vllm_base_url: str = Field(
        default="http://localhost:8000",
        alias="VLLM_BASE_URL",
        description="vLLM server URL",
    )
    vllm_model_path: str = Field(
        default="",
        alias="VLLM_MODEL_PATH",
        description="Local model path for vLLM direct loading",
    )

    model_config = {"env_file": ".env", "extra": "ignore", "populate_by_name": True}

    # ─── Derived helpers ──────────────────────────────────────

    def get_effective_model(self) -> str:
        """Return the model name to use, applying provider-specific defaults."""
        if self.model:
            return self.model

        defaults = {
            "ollama": self.ollama_model or "llama3.1:8b",
            "vllm": "llama-3.1-70b-instruct",
            "openai": "gpt-4o",
            "azure": self.azure_deployment or "gpt-4o",
            "anthropic": "claude-sonnet-4-20250514",
            "gemini": "gemini-3-pro",
            "custom": "default",
        }
        return defaults.get(self.provider, "gpt-4o")

    def get_effective_base_url(self) -> str:
        """Return the base URL to use, applying provider-specific defaults."""
        if self.api_base_url:
            return self.api_base_url

        defaults = {
            "ollama": self.ollama_base_url,
            "vllm": self.vllm_base_url,
            "openai": "https://api.openai.com/v1",
            "azure": self.azure_endpoint,
            "anthropic": "https://api.anthropic.com",
            "gemini": self.gemini_base_url,
            "custom": "",
        }
        return defaults.get(self.provider, "")

    def validate_for_provider(self) -> list[str]:
        """Check if the config has all required fields for the selected provider."""
        errors: list[str] = []
        if self.provider in ("openai", "anthropic", "gemini") and not self.api_key:
            errors.append(f"LLM_API_KEY is required for provider '{self.provider}'")
        if self.provider == "azure":
            if not self.api_key:
                errors.append("LLM_API_KEY is required for Azure OpenAI")
            if not self.azure_endpoint:
                errors.append("AZURE_OPENAI_ENDPOINT is required")
            if not self.azure_deployment:
                errors.append("AZURE_OPENAI_DEPLOYMENT is required")
        if self.provider == "custom" and not self.api_base_url:
            errors.append("LLM_API_BASE_URL is required for custom provider")
        return errors
