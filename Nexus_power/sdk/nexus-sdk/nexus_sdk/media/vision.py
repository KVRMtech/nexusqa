"""
Nexus SDK — VisionProvider & OCRProvider Abstractions.

Defines abstract interfaces for vision analysis (e.g. LLaVA, GPT-4V,
Gemini Vision) and OCR (e.g. EasyOCR, Tesseract, Azure OCR) backends
so that the Eyes engine can swap providers without rewriting business
logic.

Usage:
    from nexus_sdk.media.vision import VisionProvider, OCRProvider

    class OllamaVisionProvider(VisionProvider):
        async def analyze_frame(self, frame_path, context):
            ...

    class EasyOCRProvider(OCRProvider):
        async def extract_text(self, image_path):
            ...
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Data classes ───────────────────────────────────────────────

@dataclass
class OCRRegion:
    """A single text region detected by OCR."""

    text: str
    bbox: list[list[float]] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class OCRResult:
    """Complete OCR output from an image."""

    full_text: str
    regions: list[OCRRegion] = field(default_factory=list)
    avg_confidence: float = 0.0
    provider: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FrameAnalysisContext:
    """Context passed to a vision provider for frame analysis."""

    ocr_text: str = ""
    app_type: str = "web"
    previous_description: str = ""
    processing_profile: str = "deep"


@dataclass
class FrameAnalysisResult:
    """Result from vision-based frame analysis."""

    description: str = ""
    ui_elements: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    page_title: str = ""
    provider: str = "unknown"
    model: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Abstract Providers ─────────────────────────────────────────

class OCRProvider(abc.ABC):
    """
    Abstract interface for OCR providers.

    Implementations must define:
        - extract_text()  : Run OCR on an image
        - load()          : Initialize models/libraries
        - provider_name   : Human-readable name for metrics
        - is_available    : Whether the provider is ready
    """

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name (e.g. 'easyocr', 'tesseract')."""
        ...

    @property
    @abc.abstractmethod
    def is_available(self) -> bool:
        """Whether the OCR engine is loaded and ready."""
        ...

    @abc.abstractmethod
    async def load(self) -> bool:
        """Initialize the OCR engine. Returns True on success."""
        ...

    @abc.abstractmethod
    async def extract_text(self, image_path: str) -> OCRResult:
        """
        Extract text from an image.

        Args:
            image_path: Path to the image file.

        Returns:
            OCRResult with extracted text and bounding boxes.
        """
        ...

    async def health_check(self) -> dict[str, Any]:
        """Return provider health status for monitoring."""
        return {
            "provider": self.provider_name,
            "available": self.is_available,
        }


class VisionProvider(abc.ABC):
    """
    Abstract interface for vision analysis providers.

    Implementations must define:
        - analyze_frame()  : Analyze a single frame/screenshot
        - load_model()     : Initialize the vision model
        - unload_model()   : Release resources
        - provider_name    : Human-readable name for metrics
        - is_available     : Whether the provider is ready
    """

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name (e.g. 'ollama-llava', 'gpt4v')."""
        ...

    @property
    @abc.abstractmethod
    def is_available(self) -> bool:
        """Whether the vision model is loaded and ready."""
        ...

    @abc.abstractmethod
    async def load_model(self) -> bool:
        """Initialize the vision model. Returns True on success."""
        ...

    @abc.abstractmethod
    async def unload_model(self) -> None:
        """Release model resources."""
        ...

    @abc.abstractmethod
    async def analyze_frame(
        self,
        frame_path: str,
        context: Optional[FrameAnalysisContext] = None,
    ) -> FrameAnalysisResult:
        """
        Analyze a single frame or screenshot.

        Args:
            frame_path: Path to the image file.
            context: Optional context (OCR text, app type, etc.)

        Returns:
            FrameAnalysisResult with description and UI elements.
        """
        ...

    async def health_check(self) -> dict[str, Any]:
        """Return provider health status for monitoring."""
        return {
            "provider": self.provider_name,
            "available": self.is_available,
        }


# ── Stub implementations ──────────────────────────────────────

class StubOCRProvider(OCRProvider):
    """Stub OCR for development when no real OCR engine is available."""

    @property
    def provider_name(self) -> str:
        return "stub"

    @property
    def is_available(self) -> bool:
        return True

    async def load(self) -> bool:
        logger.warning("StubOCRProvider: no real OCR engine, returning stub text")
        return True

    async def extract_text(self, image_path: str) -> OCRResult:
        return OCRResult(
            full_text="[Stub] OCR not configured.",
            regions=[],
            avg_confidence=0.0,
            provider="stub",
        )


class StubVisionProvider(VisionProvider):
    """Stub vision for development when no real vision model is available."""

    @property
    def provider_name(self) -> str:
        return "stub"

    @property
    def is_available(self) -> bool:
        return True

    async def load_model(self) -> bool:
        logger.warning("StubVisionProvider: no real vision model, returning stub analysis")
        return True

    async def unload_model(self) -> None:
        pass

    async def analyze_frame(
        self,
        frame_path: str,
        context: Optional[FrameAnalysisContext] = None,
    ) -> FrameAnalysisResult:
        return FrameAnalysisResult(
            description="[Stub] Vision provider not configured.",
            ui_elements=[],
            tables=[],
            page_title="",
            provider="stub",
            model="none",
        )
