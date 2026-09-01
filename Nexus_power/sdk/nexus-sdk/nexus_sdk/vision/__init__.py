"""Nexus Vision Layer — multimodal LLM abstraction with tiered fallback.

Provides a uniform :class:`VisionProvider` interface that the eyes-engine
calls in place of a hard-coded Ollama client.  Operators can swap from
local Ollama (Tier 3) to cloud Anthropic / OpenAI / Gemini (Tier 1) by
changing env vars — no eyes-engine code changes required.

The shape of the analysis dict (``ui_elements``, ``description``,
``tables``, ``page_title``) is the persistent contract every provider
must satisfy.  This is what the rest of the canonical pipeline expects.

Usage from a service::

    from nexus_sdk.vision import build_vision_router

    router = build_vision_router()           # reads env vars
    await router.initialize()

    analysis = await router.analyze_frame(VisionAnalysisRequest(
        frame_path="/data/frames/scene_42_rep.png",
        ocr_text="Submit Cancel Annual Premium 5500",
        app_type="web_ui",
        previous_description="User entered birthdate",
    ))
    # analysis.ui_elements, analysis.description, analysis.tables, analysis.page_title

The router falls back from Tier 1 to Tier 2 to Tier 3 on provider failure
so a transient cloud outage does not stall the pipeline.
"""

from .base import (
    VisionAnalysisRequest,
    VisionAnalysisResponse,
    VisionProvider,
    VisionProviderError,
    VisionTransitionRequest,
    VisionTransitionResponse,
    VisionUIElement,
)
from .config import VisionConfig, VisionTierSpec
from .registry import (
    REGISTERED_VISION_PROVIDERS,
    build_vision_router,
    register_vision_provider,
)
from .tiered import VisionTierRouter

__all__ = [
    "VisionAnalysisRequest",
    "VisionAnalysisResponse",
    "VisionConfig",
    "VisionProvider",
    "VisionProviderError",
    "VisionTierRouter",
    "VisionTierSpec",
    "VisionTransitionRequest",
    "VisionTransitionResponse",
    "VisionUIElement",
    "REGISTERED_VISION_PROVIDERS",
    "build_vision_router",
    "register_vision_provider",
]
