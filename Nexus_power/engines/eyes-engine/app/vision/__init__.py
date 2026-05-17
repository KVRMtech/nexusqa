"""
Eyes Engine — Vision Analysis Module.

Combines OCR, application classification, and vision-language model analysis
for complete visual understanding of screenshots and video frames.

Components:
  - OCREngine: Text extraction via EasyOCR (on-prem, GPU-accelerated)
  - ApplicationClassifier: Heuristic + rule-based app type detection
  - VisualAnalyzer: Ollama LLaVA 7B multimodal analysis with heuristic fallback

All processing is local — no screenshots leave the datacenter.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

import structlog
from nexus_sdk.events import fire_stub_alert
from nexus_sdk.config import production_guard
from nexus_sdk.media.models import ApplicationType, UIElement
from nexus_sdk.media.vision import (
    OCRProvider,
    OCRResult,
    OCRRegion,
    VisionProvider,
    FrameAnalysisContext,
    FrameAnalysisResult,
)

logger = structlog.get_logger()


def _as_list(value: Any) -> list:
    """Return a list for untrusted model fields that may be scalar or JSON text."""
    if value is None or value == "" or value == "null":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == "null":
            return []
        if text[0] in "[{":
            try:
                return _as_list(json.loads(text))
            except json.JSONDecodeError:
                return []
    return []


def _as_dict(value: Any) -> dict:
    """Return a dict for untrusted model fields."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_text(value: Any) -> str:
    """Return a safe string for model text fields."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _table_dicts(value: Any) -> list[dict]:
    """FrameAnalysis requires tables to be a list of dictionaries."""
    return [item for item in _as_list(value) if isinstance(item, dict)]


# ─── OCR Engine ────────────────────────────────────────────────


class OCREngine(OCRProvider):
    """
    Extracts text from screenshots using EasyOCR.

    GPU-accelerated on-prem OCR. Falls back to stub
    when EasyOCR is not installed.

    Implements the SDK OCRProvider interface so the OCR backend
    can be swapped (e.g., Tesseract, Azure OCR) without rewriting
    Eyes engine business logic.
    """

    def __init__(
        self,
        languages: list[str] | None = None,
        gpu: bool = True,
        model_dir: str = "./models/easyocr",
        allow_remote_model_bootstrap: bool = True,
        load_timeout_seconds: float = 30.0,
        downscale_max_width: int = 1280,
    ):
        self.languages = languages or ["en"]
        self.gpu = gpu
        self.model_dir = model_dir
        self.allow_remote_model_bootstrap = allow_remote_model_bootstrap
        self.load_timeout_seconds = load_timeout_seconds
        # EasyOCR scans the full image; on a 1920x1080 screen capture this
        # runs 30-90s per frame on CPU. Downscaling to max 1280px width
        # gives 2-3x speedup with negligible quality loss for screen-capture
        # text (which is typically rendered at >= 12px nominal). Set 0 to
        # disable.
        self.downscale_max_width = int(downscale_max_width or 0)
        self.reader = None
        self._event_bus = None
        self._stub_fallback_count: int = 0

    async def load(self) -> bool:
        """
        Initialize OCR engine. Returns True if real model loaded.
        """
        if not self.allow_remote_model_bootstrap and not self._has_local_model_artifacts():
            logger.warning(
                "ocr.local_model_missing_bootstrap_disabled",
                languages=self.languages,
                model_dir=self.model_dir,
            )
            self.reader = None
            return False

        try:
            self.reader = await asyncio.wait_for(
                asyncio.to_thread(self._load_reader_sync),
                timeout=self.load_timeout_seconds,
            )
            logger.info(
                "ocr.loaded", languages=self.languages, gpu=self.gpu
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "ocr.load_timeout",
                languages=self.languages,
                model_dir=self.model_dir,
                timeout_seconds=self.load_timeout_seconds,
            )
            self.reader = None
            return False
        except ImportError:
            logger.warning("ocr.import_error: easyocr not installed — using stub")
            self.reader = None
            return False
        except Exception as e:
            logger.error("ocr.load_failed: %s — using stub", e)
            self.reader = None
            return False
        finally:
            # Production guard: refuse stub mode in production environments
            production_guard(
                "EasyOCR engine (eyes-engine)",
                available=(self.reader is not None),
            )

    def _load_reader_sync(self):
        import easyocr  # type: ignore[import-not-found]

        # P3 Fix: Verify GPU availability at runtime before requesting
        # GPU mode, preventing confusing errors from EasyOCR.
        use_gpu = self.gpu
        if use_gpu:
            try:
                import torch
                if not torch.cuda.is_available():
                    logger.warning(
                        "ocr.gpu_requested_but_unavailable: "
                        "EYES_OCR_GPU=True but torch.cuda.is_available()=False, "
                        "falling back to CPU"
                    )
                    use_gpu = False
            except ImportError:
                logger.warning(
                    "ocr.gpu_check_skipped: torch not available, "
                    "passing gpu=%s to EasyOCR", use_gpu,
                )

        return easyocr.Reader(
            self.languages,
            gpu=use_gpu,
            model_storage_directory=self.model_dir,
        )

    def _has_local_model_artifacts(self) -> bool:
        path = Path(self.model_dir)
        if not path.exists() or not path.is_dir():
            return False
        present = {child.name for child in path.iterdir() if child.is_file()}
        required = {"craft_mlt_25k.pth"}
        if "en" in self.languages:
            required.add("english_g2.pth")
        return required.issubset(present)

    @property
    def is_real(self) -> bool:
        return self.reader is not None

    def describe_mode(self) -> str:
        if not self.reader:
            return "stub"
        device = "cuda" if self.gpu else "cpu"
        languages = ",".join(self.languages)
        return f"easyocr device={device} languages={languages}"

    def extract_text(self, image_path: str) -> tuple[str, list[dict], float]:
        """
        Extract text from image.

        Returns (full_text, text_regions, avg_confidence).
        text_regions: [{text, bbox, confidence}]

        When ``downscale_max_width`` is set, large screen captures are
        resized in-memory before OCR so EasyOCR scans fewer pixels. The
        returned bounding boxes are then rescaled back into the original
        coordinate system so downstream consumers see the same geometry
        regardless of whether the resize happened.
        """
        if self.reader is None:
            return self._stub_ocr(image_path)

        scale = 1.0
        ocr_input = image_path
        if self.downscale_max_width > 0:
            try:
                import cv2  # local import — keeps OCR-less environments importable
                img = cv2.imread(image_path)
                if img is not None:
                    h, w = img.shape[:2]
                    if w > self.downscale_max_width:
                        scale = self.downscale_max_width / float(w)
                        new_w = self.downscale_max_width
                        new_h = max(1, int(round(h * scale)))
                        ocr_input = cv2.resize(
                            img, (new_w, new_h), interpolation=cv2.INTER_AREA,
                        )
            except Exception:
                # Any cv2 failure → fall back to original path; OCR still
                # works, just slower.
                scale = 1.0
                ocr_input = image_path

        results = self.reader.readtext(ocr_input)
        text_regions = []
        full_text_parts = []
        total_conf = 0.0

        # Inverse scale: bounding boxes from the resized image must be
        # rescaled back to original coords so callers don't need to know
        # we downscaled.
        inv_scale = 1.0 / scale if scale and scale != 1.0 else 1.0

        for bbox, text, confidence in results:
            text_regions.append({
                "text": text,
                "bbox": [
                    float(bbox[0][0]) * inv_scale, float(bbox[0][1]) * inv_scale,
                    float(bbox[2][0]) * inv_scale, float(bbox[2][1]) * inv_scale,
                ],
                "confidence": float(confidence),
            })
            full_text_parts.append(text)
            total_conf += confidence

        avg_conf = total_conf / len(results) if results else 0.0

        return " ".join(full_text_parts), text_regions, round(avg_conf, 3)

    def _stub_ocr(self, image_path: str) -> tuple[str, list[dict], float]:
        """Development stub."""
        self._stub_fallback_count += 1
        logger.warning("ocr.stub_fallback #%d", self._stub_fallback_count)
        fire_stub_alert(
            self._event_bus, "eyes", "ocr",
            fallback_count=self._stub_fallback_count,
            reason="easyocr not installed",
        )
        return (
            "[Stub] OCR not available. Install easyocr for text extraction.",
            [{"text": "[Stub]", "bbox": [0, 0, 100, 20], "confidence": 0.0}],
            0.0,
        )

    # ── OCRProvider interface implementation ──────────────────
    # These methods satisfy the abstract contract from
    # nexus_sdk.media.vision.OCRProvider, making OCREngine
    # a first-class pluggable provider.

    @property
    def provider_name(self) -> str:
        return "easyocr"

    @property
    def is_available(self) -> bool:
        return self.reader is not None

    async def extract_text_provider(self, image_path: str) -> OCRResult:
        """
        OCRProvider-conformant text extraction.

        Delegates to the existing extract_text() and wraps the result
        in SDK OCRResult for callers programming against the abstract interface.
        """
        full_text, text_regions, avg_conf = self.extract_text(image_path)
        regions = [
            OCRRegion(
                text=r["text"],
                bbox=[r["bbox"]] if isinstance(r["bbox"], list) else [],
                confidence=r.get("confidence", 0.0),
            )
            for r in text_regions
        ]
        return OCRResult(
            full_text=full_text,
            regions=regions,
            avg_confidence=avg_conf,
            provider=self.provider_name,
        )


# ─── Application Classifier ───────────────────────────────────


class ApplicationClassifier:
    """
    Classifies what application is shown in a screenshot.

    Uses heuristic keyword matching on OCR text.
    Designed to be replaced with a fine-tuned classifier in Phase 3.
    """

    BROWSER_INDICATORS = [
        "http://", "https://", "www.", ".com", ".org", ".net",
        ".html", "<html", "browser", "chrome", "firefox", "edge",
        "Ask Gemini", "Customize Chrome", "New Tab",
        # OCR often drops dots in URLs: "guardianlife com/path"
        "com/",
    ]
    # MAINFRAME: split into HIGH_PRECISION (unique terms — single match
    # is enough) and CONTEXTUAL (need ≥2 because they overlap with other
    # apps' login screens). Pre-architect-P0 the whole list required ≥3,
    # which made "CICS Transaction INSR01" classify as desktop_app — a
    # real customer-facing regression flagged by architect review.
    MAINFRAME_HIGH_PRECISION = [
        "CICS", "TSO/E", "ISPF", "3270", "MAPSET", "===>",
    ]
    MAINFRAME_INDICATORS = [
        "TSO", "PF1", "PF3", "PA1", "USERID", "LOGON", "MVS",
    ]
    EXCEL_INDICATORS = [
        "Sheet1", "Sheet2", "Cell", "Formula", "SUM(", "VLOOKUP(",
        "HLOOKUP(", "COUNTIF(", "SUMIF(", "Worksheet", "Workbook",
    ]
    # PDF: ".pdf" alone is enough — appears nowhere else. "Page X of Y"
    # in combination with PDF/Adobe is also enough.
    PDF_HIGH_PRECISION = [
        ".pdf", "Adobe Acrobat", "Adobe Reader",
    ]
    PDF_INDICATORS = [
        "Page 1 of", "Acrobat", "PDF Document",
    ]
    EMAIL_INDICATORS = [
        "From:", "To:", "Subject:", "Cc:", "Inbox", "Sent Items",
        "Outlook", "Gmail", "compose",
    ]
    # TERMINAL: shell prompts are unique. "$ " followed by a command is
    # also distinctive enough to single-match.
    TERMINAL_HIGH_PRECISION = [
        "PS C:\\", "root@", "$ ls", "$ cd", "$ cat", "$ pwd",
        "bash$", "zsh%",
    ]
    TERMINAL_INDICATORS = [
        "$ ", ">>> ", "bash", "powershell", "cmd>", "~$", "terminal",
    ]

    def classify(
        self,
        extracted_text: str,
        frame_metadata: dict | None = None,
    ) -> ApplicationType:
        """Classify application type from extracted text and metadata.

        Uses multi-indicator scoring: requires minimum match counts to avoid
        false positives (e.g., "ENTER" alone → MAINFRAME, "Gmail" mention in a
        browser → EMAIL_CLIENT).  Browser indicators are checked first because
        most web-app screens contain email/form keywords inside a browser.
        """
        text_lower = extracted_text.lower()

        def _count_matches(indicators: list[str]) -> int:
            return sum(1 for ind in indicators if ind.lower() in text_lower)

        def _any_match(indicators: list[str]) -> bool:
            return any(ind.lower() in text_lower for ind in indicators)

        # Score categories
        browser_hits = _count_matches(self.BROWSER_INDICATORS)
        mainframe_hp = _any_match(self.MAINFRAME_HIGH_PRECISION)
        mainframe_hits = _count_matches(self.MAINFRAME_INDICATORS)
        email_hits = _count_matches(self.EMAIL_INDICATORS)
        pdf_hp = _any_match(self.PDF_HIGH_PRECISION)
        pdf_hits = _count_matches(self.PDF_INDICATORS)
        excel_hits = _count_matches(self.EXCEL_INDICATORS)
        terminal_hp = _any_match(self.TERMINAL_HIGH_PRECISION)
        terminal_hits = _count_matches(self.TERMINAL_INDICATORS)

        # Mainframe: priority over everything except an unambiguous
        # browser tab. High-precision keywords (CICS, MAPSET, 3270)
        # appear nowhere outside mainframe screens — single match wins
        # over browser_hits unless we're clearly in a web URL.
        if mainframe_hp or mainframe_hits >= 3:
            if mainframe_hp or mainframe_hits > browser_hits:
                return ApplicationType.MAINFRAME_3270

        # Email: need ≥2 indicators AND NOT inside a browser context
        # (a browser with Gmail should stay WEB_UI unless purely email UI)
        if email_hits >= 2 and browser_hits < 2:
            return ApplicationType.EMAIL_CLIENT

        # PDF: single high-precision keyword OR multi-indicator.
        if pdf_hp or pdf_hits >= 2:
            return ApplicationType.PDF_DOCUMENT

        # Excel: need ≥2 indicators
        if excel_hits >= 2:
            return ApplicationType.EXCEL_SPREADSHEET

        # Browser / Web UI: single indicator is sufficient (most common case)
        if browser_hits >= 1:
            return ApplicationType.WEB_UI

        # Terminal: single high-precision (shell prompt) OR ≥2 generic.
        if terminal_hp or terminal_hits >= 2:
            return ApplicationType.TERMINAL

        # Check for database UI patterns
        if any(kw in text_lower for kw in [
            "select ", "insert ", "update ", "delete ", "schema",
            "table", "column", "query", "sql", "database",
        ]):
            return ApplicationType.DATABASE_UI

        return ApplicationType.DESKTOP_APP


# ─── Visual Analyzer ──────────────────────────────────────────


class VisualAnalyzer(VisionProvider):
    """
    Analyzes screenshots using a vision-language model via Ollama.

    Uses Llama 3.2 Vision (11B) through Ollama for real visual understanding.
    Falls back to heuristic analysis if Ollama is unavailable.

    Supports multi-endpoint failover: configure EYES_OLLAMA_FAILOVER_URLS
    (comma-separated) to provide backup Ollama instances so visual analysis
    survives a single-server outage.

    Implements the SDK VisionProvider interface so the vision backend
    can be swapped (e.g., GPT-4V, Gemini Vision) without rewriting
    Eyes engine business logic.
    """

    def __init__(
        self,
        ollama_base_url: str = "",
        ollama_model: str = "llama3.2-vision:11b",
        fast_ollama_model: str = "llava:7b",
        model_failure_backoff_seconds: float = 300.0,
    ):
        self.ollama_base_url = (
            ollama_base_url
            or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        )
        self.ollama_model = ollama_model
        self.fast_ollama_model = fast_ollama_model
        self.model_failure_backoff_seconds = model_failure_backoff_seconds
        self.inference_timeout = float(
            os.environ.get("EYES_OLLAMA_INFERENCE_TIMEOUT", "180")
        )
        # Keep the vision model resident in Ollama RAM between scenes/chunks so
        # we do not pay a 5-15 s reload tax on each call.  Accepts any Ollama
        # duration string ("10m", "30m", "-1" for forever).
        self.keep_alive = os.environ.get("EYES_OLLAMA_KEEP_ALIVE", "10m")
        # Cap generated tokens — the JSON schema we ask for fits comfortably
        # in <800 tokens; lowering num_predict cuts CPU inference time 20-40%.
        try:
            self.num_predict = int(os.environ.get("EYES_OLLAMA_NUM_PREDICT", "768"))
        except ValueError:
            self.num_predict = 768
        try:
            self.num_ctx = int(os.environ.get("EYES_OLLAMA_NUM_CTX", "4096"))
        except ValueError:
            self.num_ctx = 4096
        try:
            self.transition_num_predict = int(
                os.environ.get("EYES_TRANSITION_LLM_NUM_PREDICT", "384")
            )
        except ValueError:
            self.transition_num_predict = 384
        self._ollama_available = False
        self._available_models: set[str] = set()
        self._model_backoff_until: dict[str, float] = {}
        self._http_client = None

        # SDK vision tier router — initialised lazily on first use when any
        # ``EYES_VISION_TIER{1,2,3}_PROVIDER`` env var is set.  When the router
        # is in play the legacy direct-Ollama path below is bypassed; on
        # router failure we fall back to the legacy path so a transient
        # cloud outage does not break frame analysis.  None means "router
        # not requested" (legacy-only behaviour, fully backwards compat).
        self._tier_router = None
        self._tier_router_init_attempted = False
        self._tier_router_lock: asyncio.Lock | None = None

        # Multi-endpoint failover for production HA.
        # EYES_OLLAMA_FAILOVER_URLS="http://ollama-2:11434,http://ollama-3:11434"
        failover_raw = os.environ.get("EYES_OLLAMA_FAILOVER_URLS", "")
        self._failover_urls: list[str] = [
            u.strip() for u in failover_raw.split(",") if u.strip()
        ]
        self._failover_clients: list = []  # httpx.AsyncClient per failover URL
        self._active_client_index: int = 0  # index into [primary, *failover]
        self._all_clients: list = []  # [primary_client, *failover_clients]

        # Phase 2 — Distributed GPU pool.  When ``EYES_OLLAMA_LOAD_BALANCE``
        # is truthy the active client index advances after every successful
        # call (round-robin), so a long demo distributes LLaVA invocations
        # across all configured endpoints instead of pinning to one.  When
        # off (default), the index only moves on failover — preserving the
        # existing "sticky primary, fall through on outage" behaviour.
        lb_raw = os.environ.get("EYES_OLLAMA_LOAD_BALANCE", "").strip().lower()
        self._load_balance: bool = lb_raw in ("1", "true", "yes", "on")

    def _advance_active_client_on_success(self, succeeded_offset: int) -> None:
        """Update ``_active_client_index`` after a successful Ollama call.

        ``succeeded_offset`` is the position (relative to the current
        active index) of the client that actually returned a 200.

        * Failover mode (default): pin to the succeeding client so
          subsequent calls don't keep retrying the dead primary.
        * Load-balance mode (``EYES_OLLAMA_LOAD_BALANCE=1``): advance
          one slot past the succeeding client so the *next* call
          starts on the next host in the ring.  Combined with the
          existing fall-through-on-failure logic this gives
          round-robin under load + automatic skip of a dead host.
        """
        n = len(self._all_clients)
        if n <= 1:
            return
        succeeded_idx = (self._active_client_index + succeeded_offset) % n
        if self._load_balance:
            next_idx = (succeeded_idx + 1) % n
            self._active_client_index = next_idx
            self._http_client = self._all_clients[next_idx]
        else:
            if succeeded_idx != self._active_client_index:
                self._active_client_index = succeeded_idx
                self._http_client = self._all_clients[succeeded_idx]

    async def load_model(self) -> bool:
        """
        Connect to Ollama and verify LLaVA model availability.

        Initializes primary + failover clients. Returns True if at
        least one Ollama endpoint is reachable with the configured model.
        """
        import httpx

        # Build client list: primary + failover URLs
        all_urls = [self.ollama_base_url] + self._failover_urls
        self._all_clients = []

        for url in all_urls:
            try:
                client = httpx.AsyncClient(base_url=url, timeout=self.inference_timeout)
                resp = await client.get("/api/tags")
                if resp.status_code == 200:
                    models = resp.json().get("models", [])
                    model_names = [m.get("name", "") for m in models]
                    configured_models = [self.fast_ollama_model, self.ollama_model]
                    found = {
                        mn for mn in configured_models
                        if any(mn in candidate for candidate in model_names)
                    }
                    if found:
                        self._all_clients.append(client)
                        self._available_models.update(found)
                        logger.info(
                            "visual_analyzer.ollama_endpoint_ready",
                            url=url,
                            available_models=sorted(found),
                        )
                        continue
                    logger.warning(
                        "visual_analyzer.model_not_found_at_endpoint",
                        url=url,
                    )
                await client.aclose()
            except Exception as e:
                logger.warning(
                    "visual_analyzer.endpoint_unavailable",
                    url=url, error=str(e),
                )

        if self._all_clients:
            self._http_client = self._all_clients[0]
            self._active_client_index = 0
            self._ollama_available = True
            logger.info(
                "visual_analyzer.ollama_ready",
                default_model=self.ollama_model,
                fast_model=self.fast_ollama_model,
                available_models=sorted(self._available_models),
                endpoints_ready=len(self._all_clients),
                total_endpoints=len(all_urls),
            )
            return True

        # Production guard: refuse stub mode in production environments
        production_guard(
            "Ollama LLaVA visual analyzer (eyes-engine)",
            available=self._ollama_available,
        )

        return False

    @property
    def is_real(self) -> bool:
        return self._ollama_available

    def describe_mode(self) -> str:
        """Human-readable mode string for the engine health endpoint."""
        if not self._ollama_available:
            return "stub"
        models = ",".join(sorted(self._available_models))
        return (
            f"ollama endpoints={len(self._all_clients)} "
            f"models=[{models}] primary={self.ollama_model} fast={self.fast_ollama_model}"
        )

    async def _get_or_init_tier_router(self):
        """Return the SDK vision tier router when configured, else ``None``.

        The router is constructed lazily on first call so eyes-engine
        startup does not pay any latency when no tier is configured.
        Initialisation failures (no tiers configured, all tiers refusing)
        are recorded once and the router is left at ``None`` for the rest
        of the process — the caller falls back to the legacy direct-Ollama
        path automatically.
        """
        if self._tier_router_init_attempted:
            return self._tier_router

        # Auto-detect: any TIER{N}_PROVIDER env triggers router initialisation.
        any_tier_set = any(
            os.environ.get(f"EYES_VISION_TIER{n}_PROVIDER", "").strip()
            for n in (1, 2, 3)
        )
        if not any_tier_set:
            self._tier_router_init_attempted = True
            return None

        # Use a lock so concurrent first calls do not double-initialise.
        if self._tier_router_lock is None:
            self._tier_router_lock = asyncio.Lock()
        async with self._tier_router_lock:
            if self._tier_router_init_attempted:
                return self._tier_router
            self._tier_router_init_attempted = True
            try:
                from nexus_sdk.vision import build_vision_router
                router = build_vision_router()
                await router.initialize()
                self._tier_router = router
                logger.info(
                    "visual_analyzer.tier_router_ready",
                    healthy_tiers=router.healthy_tier_count,
                    configured_tiers=router.configured_tier_count,
                )
            except Exception as exc:
                logger.warning(
                    "visual_analyzer.tier_router_init_failed",
                    error=str(exc)[:300],
                )
                self._tier_router = None
        return self._tier_router

    async def analyze_frame(
        self,
        frame_path: str,
        ocr_text: str,
        app_type: ApplicationType,
        previous_description: str = "",
        processing_profile: str = "deep",
        previous_frame_path: str | None = None,
    ) -> dict:
        """
        Analyze a single frame using the configured vision provider.

        Resolution order:
          1. SDK ``VisionTierRouter`` when any ``EYES_VISION_TIER*_PROVIDER``
             env var is set — gives Tier-1/2/3 fallback across cloud +
             local providers.
          2. Direct Ollama call (legacy path) for backwards compatibility.
          3. Heuristic analyser when neither is available.

        Returns dict with: ui_elements, description, tables, page_title.
        """
        router = await self._get_or_init_tier_router()
        if router is not None:
            try:
                from nexus_sdk.vision import VisionAnalysisRequest
                app_type_value = (
                    app_type.value if hasattr(app_type, "value") else str(app_type)
                )
                response = await router.analyze_frame(VisionAnalysisRequest(
                    frame_path=frame_path,
                    ocr_text=ocr_text or "",
                    app_type=app_type_value,
                    previous_description=previous_description or "",
                    previous_frame_path=previous_frame_path,
                ))
                return response.to_eyes_dict()
            except Exception as exc:
                logger.warning(
                    "visual_analyzer.tier_router_call_failed_fallback",
                    error=str(exc)[:300],
                )
                # Intentional fallthrough — try the legacy path.

        if self._ollama_available and self._http_client is not None:
            preferred_models = self._preferred_models_for_profile(
                processing_profile
            )
            attempted_models: list[str] = []
            for model_name in preferred_models:
                if model_name in attempted_models:
                    continue
                if model_name not in self._available_models:
                    continue
                if self._is_model_in_backoff(model_name):
                    continue

                attempted_models.append(model_name)
                try:
                    return await self._ollama_analyze(
                        frame_path,
                        ocr_text,
                        app_type,
                        previous_description,
                        model_name,
                    )
                except Exception as e:
                    self._mark_model_failed(model_name, str(e))
                    logger.warning(
                        "visual_analyzer.ollama_failed",
                        model=model_name,
                        processing_profile=processing_profile,
                        error=str(e),
                    )

        return self._heuristic_analyze(
            frame_path, ocr_text, app_type, previous_description
        )

    async def analyze_transition_pair(
        self,
        before_frame_path: str,
        after_frame_path: str,
        ocr_before: str,
        ocr_after: str,
        url_changed: bool,
        processing_profile: str = "multimodal",
    ) -> dict:
        """Two-image LLM call: identify the user action between screenshots.

        Resolution order:
          1. SDK VisionTierRouter.analyze_transition() when configured —
             gives Tier-1/2/3 fallback across Anthropic / OpenAI / Ollama
             multi-image APIs.
          2. Direct Ollama 2-image chat call (legacy path) when no tier
             is configured.
          3. Empty dict {} when neither is available.

        Returns a dict with keys action_kind, action_label,
        target_element_label, observed_value, confidence, evidence_text —
        or {} on failure / stub mode.
        """
        # Try the tier router first when configured. The router does its
        # own per-tier failover; on TOTAL exhaustion we fall through to
        # the direct-Ollama path so behavior degrades gracefully if a
        # cloud tier is misconfigured.
        router = await self._get_or_init_tier_router()
        if router is not None:
            try:
                from nexus_sdk.vision import VisionTransitionRequest
                response = await router.analyze_transition(
                    VisionTransitionRequest(
                        before_frame_path=before_frame_path,
                        after_frame_path=after_frame_path,
                        ocr_before=ocr_before or "",
                        ocr_after=ocr_after or "",
                        url_changed=bool(url_changed),
                    )
                )
                return response.to_eyes_dict()
            except Exception as exc:
                logger.warning(
                    "visual_analyzer.tier_router_transition_failed_fallback",
                    error=str(exc)[:300],
                )
                # Fall through to the legacy direct-Ollama path.

        if not self._ollama_available or self._http_client is None:
            return {}
        import base64

        try:
            with open(before_frame_path, "rb") as f:
                b1 = base64.b64encode(f.read()).decode("utf-8")
            with open(after_frame_path, "rb") as f:
                b2 = base64.b64encode(f.read()).decode("utf-8")
        except OSError:
            return {}

        prompt = (
            "You are given TWO screenshots in chronological order.\n"
            "Image 1 (first image) = UI state BEFORE the user's action.\n"
            "Image 2 (second image) = UI state AFTER the user's action.\n\n"
            f"OCR text from before (truncated): {ocr_before[:1200]}\n"
            f"OCR text from after  (truncated): {ocr_after[:1200]}\n"
            f"Visible URL or path changed between screens: {'yes' if url_changed else 'no'}\n\n"
            "What single user action best explains the transition from Image 1 to Image 2?\n"
            "Choose action_kind from exactly one of:\n"
            "click_cta, enter_text, select_option, toggle, navigate, submit_form, scroll, unknown\n\n"
            "Respond ONLY with valid JSON:\n"
            "{\n"
            '  "action_kind": "...",\n'
            '  "action_label": "short phrase e.g. Clicked Continue",\n'
            '  "target_element_label": "button or field name if visible",\n'
            '  "observed_value": "value entered or selected, else empty string",\n'
            '  "confidence": 0.85,\n'
            '  "evidence_text": "one short sentence citing visible UI change"\n'
            "}\n"
        )

        preferred_models = self._preferred_models_for_profile(processing_profile)
        import json

        for model_name in preferred_models:
            if model_name not in self._available_models:
                continue
            if self._is_model_in_backoff(model_name):
                continue
            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [b1, b2],
                    }
                ],
                "stream": False,
                "format": "json",
                "keep_alive": self.keep_alive,
                "options": {
                    "temperature": 0.1,
                    "num_predict": self.transition_num_predict,
                    "num_ctx": self.num_ctx,
                },
            }
            clients_to_try = (
                self._all_clients[self._active_client_index:]
                + self._all_clients[:self._active_client_index]
            ) if len(self._all_clients) > 1 else [self._http_client]
            for offset, client in enumerate(clients_to_try):
                try:
                    resp = await client.post(
                        "/api/chat", json=payload, timeout=self.inference_timeout
                    )
                    if resp.status_code != 200:
                        continue
                    content = resp.json().get("message", {}).get("content", "{}")
                    parsed = json.loads(content)
                    if not isinstance(parsed, dict):
                        continue
                    kind = str(parsed.get("action_kind", "unknown")).strip().lower()
                    conf = float(parsed.get("confidence", 0.0))
                    # Rotate the ring on success when load-balance mode
                    # is on so transition-LLM calls also distribute.
                    self._advance_active_client_on_success(offset)
                    return {
                        "action_kind": kind,
                        "action_label": str(parsed.get("action_label", "")).strip(),
                        "target_element_label": str(
                            parsed.get("target_element_label", "")
                        ).strip(),
                        "observed_value": str(
                            parsed.get("observed_value", "")
                        ).strip(),
                        "confidence": max(0.0, min(1.0, conf)),
                        "evidence_text": str(
                            parsed.get("evidence_text", "")
                        ).strip(),
                    }
                except Exception:
                    continue
        return {}
        if not self._ollama_available:
            return "heuristic"
        available_models = ",".join(sorted(self._available_models))
        return (
            f"ollama fast={self.fast_ollama_model} deep={self.ollama_model} "
            f"available={available_models}"
        )

    def _preferred_models_for_profile(self, processing_profile: str) -> list[str]:
        if processing_profile == "fast":
            return [self.fast_ollama_model, self.ollama_model]
        return [self.ollama_model, self.fast_ollama_model]

    def _is_model_in_backoff(self, model_name: str) -> bool:
        backoff_until = self._model_backoff_until.get(model_name, 0.0)
        if backoff_until <= 0:
            return False
        if backoff_until <= asyncio.get_running_loop().time():
            self._model_backoff_until.pop(model_name, None)
            return False
        return True

    def _mark_model_failed(self, model_name: str, error: str) -> None:
        self._model_backoff_until[model_name] = (
            asyncio.get_running_loop().time()
            + self.model_failure_backoff_seconds
        )
        logger.warning(
            "visual_analyzer.model_backoff",
            model=model_name,
            backoff_seconds=self.model_failure_backoff_seconds,
            error=error,
        )

    async def _ollama_analyze(
        self,
        frame_path: str,
        ocr_text: str,
        app_type: ApplicationType,
        previous_description: str,
        model_name: str,
    ) -> dict:
        """Analyze frame using Ollama LLaVA multimodal model.

        Tries the active client first. On HTTP failure, cycles through
        failover clients before raising.
        """
        import base64

        with open(frame_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        prompt = (
            "You are analyzing a screenshot from a software application for QA testing purposes.\n"
            f"Application type detected: {app_type.value}\n"
            f"OCR text extracted: {ocr_text[:1000]}\n\n"
            "Please analyze this screenshot and return a JSON object with:\n"
            '1. "ui_elements": array of objects with {element_type, text, confidence, properties} '
            "for each UI element (buttons, fields, labels, dropdowns, tables, menus, checkboxes, radio buttons, tabs, links).\n"
            "   - element_type: button, text_field, dropdown, label, table, menu, checkbox, radio, link, image, tab\n"
            "   - properties should include when detectable: {enabled: bool, selected: bool, value: string, options: string[], placeholder: string}\n"
            "   - include a 'location' field per element: header, sidebar, main_content, footer, modal, toolbar\n"
            '2. "description": a natural language description of what the screen shows '
            "and what workflow step it represents\n"
            '3. "tables": array of any data tables detected (empty array if none)\n'
            '4. "page_title": the page or dialog title if visible\n'
            "Return ONLY valid JSON."
        )

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [image_b64],
                }
            ],
            "stream": False,
            "format": "json",
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": 0.1,
                "num_predict": self.num_predict,
                "num_ctx": self.num_ctx,
            },
        }

        # Try active client, then failover clients
        clients_to_try = (
            self._all_clients[self._active_client_index:]
            + self._all_clients[:self._active_client_index]
        ) if len(self._all_clients) > 1 else [self._http_client]

        last_error: Exception | None = None
        for idx_offset, client in enumerate(clients_to_try):
            try:
                resp = await client.post(
                    "/api/chat", json=payload, timeout=self.inference_timeout
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("message", {}).get("content", "{}")
                    try:
                        parsed = json.loads(content)
                        # LLM sometimes returns a bare array instead of {"ui_elements": [...]}
                        if isinstance(parsed, list):
                            parsed = {"ui_elements": parsed}
                        elif not isinstance(parsed, dict):
                            parsed = {}
                        ui_elements = []
                        for elem in _as_list(parsed.get("ui_elements", [])):
                            if not isinstance(elem, dict):
                                continue
                            props = _as_dict(elem.get("properties", {}))
                            location = _as_text(elem.get("location", ""))
                            if location:
                                props["location"] = location
                            # Carry through action_kind / observed_value if LLaVA emits them;
                            # otherwise derive from element_type so downstream always has them.
                            if "action_kind" not in props:
                                _LLM_KIND_MAP = {
                                    "button": "click", "link": "click",
                                    "text_field": "enter_text", "input": "enter_text",
                                    "textarea": "enter_text",
                                    "dropdown": "select_option", "select": "select_option",
                                    "checkbox": "check", "radio": "select_option",
                                    "tab": "navigate", "step_indicator": "navigate",
                                    "section_header": "review",
                                }
                                el_type = _as_text(elem.get("element_type", "unknown")) or "unknown"
                                props["action_kind"] = _LLM_KIND_MAP.get(el_type, "interact")
                            if "observed_value" not in props:
                                props["observed_value"] = _as_text(props.get("value")).strip()
                            ui_elements.append(UIElement(
                                element_type=_as_text(elem.get("element_type", "unknown")) or "unknown",
                                text=_as_text(elem.get("text", "")),
                                confidence=elem.get("confidence", 0.8)
                                if isinstance(elem.get("confidence", 0.8), (int, float))
                                else 0.8,
                                properties=props,
                            ))
                        # Update active client based on the succeeding
                        # endpoint.  Failover mode pins; load-balance
                        # advances by one slot (round-robin).
                        prev_idx = self._active_client_index
                        self._advance_active_client_on_success(idx_offset)
                        if self._active_client_index != prev_idx:
                            logger.info(
                                "visual_analyzer.client_advanced",
                                from_idx=prev_idx,
                                to_idx=self._active_client_index,
                                mode="load_balance" if self._load_balance else "failover",
                            )
                        return {
                            "ui_elements": ui_elements,
                            "description": _as_text(parsed.get("description", "")),
                            "tables": _table_dicts(parsed.get("tables", [])),
                            "page_title": _as_text(parsed.get("page_title", "")),
                        }
                    except (json.JSONDecodeError, KeyError):
                        pass
                last_error = RuntimeError(f"Ollama returned status {resp.status_code}")
            except Exception as e:
                last_error = e
                logger.warning(
                    "visual_analyzer.endpoint_failed",
                    error=str(e),
                    trying_next=idx_offset < len(clients_to_try) - 1,
                )

        # All clients failed — fall back to heuristic
        return self._heuristic_analyze(
            frame_path, ocr_text, app_type, previous_description
        )

    def _heuristic_analyze(
        self,
        frame_path: str,
        ocr_text: str,
        app_type: ApplicationType,
        previous_description: str,
    ) -> dict:
        """Heuristic analysis when vision model is not available."""
        ui_elements = []

        # Detect buttons (expanded action verb set)
        button_patterns = re.findall(
            r"\b(Submit|Cancel|Save|Next|Back|Continue|OK|Apply|Search|"
            r"Login|Logout|Calculate|Quote|Bind|Issue|Renew|Endorse|"
            r"Add|Edit|Delete|Remove|Update|Create|View|Download|Upload|"
            r"Print|Export|Import|Approve|Reject|Reset|Clear|Close|"
            r"Confirm|Accept|Decline|Process|Generate|Run|Execute|"
            r"Send|Receive|Transfer|Pay|Claim|Enroll|Register|"
            r"Start|Stop|Finish|Complete)\b",
            ocr_text,
            re.IGNORECASE,
        )
        seen_buttons: set[str] = set()
        for btn_text in button_patterns:
            normalised = btn_text.strip().title()
            if normalised not in seen_buttons:
                seen_buttons.add(normalised)
                ui_elements.append(UIElement(
                    element_type="button",
                    text=normalised,
                    confidence=0.7,
                ))

        # Detect multi-word CTA buttons common in web/insurance/finance apps.
        # Single-word patterns above miss CTAs like "Get my quote", "Apply now",
        # "Sign in", "Get started" which are the most critical E2E test steps.
        cta_patterns = re.findall(
            r"\b("
            r"Get\s+(?:my|a|your|started|quote|results|coverage|plan|estimate|price)|\b"
            r"Apply\s+(?:now|here|online|for\s+\w+)|\b"
            r"See\s+(?:my|your|the)\s+\w+|\b"
            r"View\s+(?:my|your|the)\s+\w+|\b"
            r"Find\s+(?:a|my|your)\s+\w+|\b"
            r"Compare\s+(?:plans|options|quotes|rates)|\b"
            r"Calculate\s+(?:now|my|your)|\b"
            r"Learn\s+more|\b"
            r"Get\s+started|\b"
            r"Sign\s+(?:in|up|out)|\b"
            r"Log\s+(?:in|out)|\b"
            r"Try\s+(?:it|now|free)|\b"
            r"Schedule\s+(?:a|an)\s+\w+|\b"
            r"Request\s+(?:a|an)\s+\w+|\b"
            r"Show\s+(?:my|your|results|quote)|\b"
            r"Check\s+(?:my|your|eligibility|price|rate)\b"
            r")",
            ocr_text,
            re.IGNORECASE,
        )
        for cta_text in cta_patterns:
            normalised = cta_text.strip()
            key = normalised.lower()
            if key not in seen_buttons:
                seen_buttons.add(key)
                ui_elements.append(UIElement(
                    element_type="button",
                    text=normalised,
                    confidence=0.75,
                    properties={"action_kind": "click", "inferred_from": "cta_pattern"},
                ))

        # Detect dropdowns
        dropdown_patterns = re.findall(
            r"(\u25bc|\u25be|--\s*Select\s*--|Choose\b|Select\s+(?:one|an?)\b)",
            ocr_text,
            re.IGNORECASE,
        )
        if dropdown_patterns:
            # Try to find associated labels (word(s) before dropdown indicator)
            for match in re.finditer(
                r"([A-Za-z][A-Za-z\s]{1,30})\s*(?:\u25bc|\u25be|--\s*Select)",
                ocr_text,
                re.IGNORECASE,
            ):
                ui_elements.append(UIElement(
                    element_type="dropdown",
                    text=match.group(1).strip(),
                    confidence=0.6,
                    properties={"label": match.group(1).strip()},
                ))
            # If no label-matched dropdowns, add generic ones
            if not any(e.element_type == "dropdown" for e in ui_elements):
                for dd_text in dropdown_patterns[:5]:
                    ui_elements.append(UIElement(
                        element_type="dropdown",
                        text=dd_text.strip(),
                        confidence=0.6,
                    ))

        # Detect checkboxes
        checkbox_patterns = re.findall(
            r"(\u2610|\u2611|\u2713|\[\s*\]|\[x\]|\[X\])",
            ocr_text,
        )
        if checkbox_patterns:
            for match in re.finditer(
                r"(?:\u2610|\u2611|\u2713|\[\s*\]|\[x\]|\[X\])\s*([A-Za-z][A-Za-z\s]{1,40})",
                ocr_text,
            ):
                selected = match.group(0).startswith(("\u2611", "\u2713", "[x", "[X"))
                ui_elements.append(UIElement(
                    element_type="checkbox",
                    text=match.group(1).strip(),
                    confidence=0.6,
                    properties={"selected": selected},
                ))

        # Detect radio buttons
        radio_patterns = re.findall(
            r"(\u25cb|\u25c9|\(\s*\)|\(\u2022\))",
            ocr_text,
        )
        if radio_patterns:
            for match in re.finditer(
                r"(?:\u25cb|\u25c9|\(\s*\)|\(\u2022\))\s*([A-Za-z][A-Za-z\s]{1,40})",
                ocr_text,
            ):
                selected = match.group(0).startswith(("\u25c9", "(\u2022"))
                ui_elements.append(UIElement(
                    element_type="radio",
                    text=match.group(1).strip(),
                    confidence=0.6,
                    properties={"selected": selected},
                ))

        # Detect tabs (pipe-delimited or clearly grouped horizontal labels)
        tab_matches = re.findall(
            r"([A-Za-z][A-Za-z\s]{1,20})\s*\|\s*([A-Za-z][A-Za-z\s]{1,20})",
            ocr_text,
        )
        seen_tabs: set[str] = set()
        for left, right in tab_matches[:5]:
            for tab_label in (left.strip(), right.strip()):
                if tab_label not in seen_tabs:
                    seen_tabs.add(tab_label)
                    ui_elements.append(UIElement(
                        element_type="tab",
                        text=tab_label,
                        confidence=0.5,
                    ))

        # Detect links
        link_patterns = re.findall(
            r"(https?://\S+|www\.\S+|Click\s+here|Learn\s+more|See\s+details|View\s+more)",
            ocr_text,
            re.IGNORECASE,
        )
        for link_text in link_patterns[:10]:
            ui_elements.append(UIElement(
                element_type="link",
                text=link_text.strip(),
                confidence=0.5,
            ))

        # Detect labeled fields (Label: value pattern)
        field_patterns = re.findall(
            r"([A-Za-z\s]+)[:]\s*([\w\s@.\-]+)", ocr_text
        )
        for label, value in field_patterns[:20]:
            ui_elements.append(UIElement(
                element_type="text_field",
                text=f"{label.strip()}: {value.strip()}",
                confidence=0.6,
                properties={
                    "label": label.strip(),
                    "value": value.strip(),
                },
            ))

        # ── Enhanced form-semantic detection from OCR text ──────
        # Promotes question-phrased labels and known form-field names
        # into structured controls even without explicit glyph markers.
        seen_labels = {e.text.lower().strip() for e in ui_elements if e.element_type in ("text_field", "dropdown")}

        # Question-phrased labels → dropdown or text_field
        # e.g. "What state do you live in?", "Which plan do you prefer?"
        for match in re.finditer(
            r"(What|Which|Select your|Choose your|Pick your)\s+([A-Za-z\s]{2,40}?)(?:\?|\s*$)",
            ocr_text,
            re.IGNORECASE | re.MULTILINE,
        ):
            label = match.group(0).rstrip("? \t").strip()
            if label.lower() not in seen_labels:
                seen_labels.add(label.lower())
                ui_elements.append(UIElement(
                    element_type="dropdown",
                    text=label,
                    confidence=0.55,
                    properties={"label": label, "inferred_from": "question_pattern"},
                ))

        # Known form-field names → text_field or dropdown
        _FIELD_NAMES = (
            r"First\s*Name|Last\s*Name|Full\s*Name|Middle\s*Name|"
            r"Email(?:\s*Address)?|Phone(?:\s*Number)?|Date\s*of\s*Birth|DOB|"
            r"Address(?:\s*Line)?|City|Zip(?:\s*Code)?|Postal\s*Code|"
            r"SSN|Social\s*Security|Policy\s*Number|Account\s*Number|"
            r"Username|Password|Company|Employer|Occupation|"
            r"Coverage\s*Amount|Premium|Deductible|Effective\s*Date|"
            r"Beneficiary|Insured\s*Name|Agent\s*Name"
        )
        _DROPDOWN_NAMES = (
            r"State|Country|Gender|Marital\s*Status|"
            r"Coverage\s*Type|Plan\s*Type|Payment\s*Method|"
            r"Frequency|Tobacco\s*Use|Smoking\s*Status"
        )
        for match in re.finditer(
            rf"\b({_DROPDOWN_NAMES})\b", ocr_text, re.IGNORECASE,
        ):
            label = match.group(1).strip()
            if label.lower() not in seen_labels:
                seen_labels.add(label.lower())
                ui_elements.append(UIElement(
                    element_type="dropdown",
                    text=label,
                    confidence=0.55,
                    properties={"label": label, "inferred_from": "known_form_name"},
                ))
        for match in re.finditer(
            rf"\b({_FIELD_NAMES})\b", ocr_text, re.IGNORECASE,
        ):
            label = match.group(1).strip()
            if label.lower() not in seen_labels:
                seen_labels.add(label.lower())
                ui_elements.append(UIElement(
                    element_type="text_field",
                    text=label,
                    confidence=0.55,
                    properties={"label": label, "inferred_from": "known_form_name"},
                ))

        # "Enter your …" / "Provide your …" / "Type your …" → text_field
        for match in re.finditer(
            r"(?:Enter|Provide|Type|Input)\s+(?:your\s+)?([A-Za-z\s]{2,30})",
            ocr_text,
            re.IGNORECASE,
        ):
            label = match.group(1).strip()
            if label.lower() not in seen_labels and len(label) > 2:
                seen_labels.add(label.lower())
                ui_elements.append(UIElement(
                    element_type="text_field",
                    text=label,
                    confidence=0.5,
                    properties={"label": label, "inferred_from": "imperative_pattern"},
                ))

        # Step indicators → navigation element
        step_match = re.search(
            r"(?:Step|Page)\s+(\d+)\s*(?:of|/)\s*(\d+)",
            ocr_text,
            re.IGNORECASE,
        )
        if step_match:
            ui_elements.append(UIElement(
                element_type="step_indicator",
                text=step_match.group(0),
                confidence=0.7,
                properties={
                    "current_step": int(step_match.group(1)),
                    "total_steps": int(step_match.group(2)),
                },
            ))

        # Section headers — lines that look like form section titles
        for match in re.finditer(
            r"^([A-Z][A-Za-z\s]{3,35}(?:information|details|preferences|options|settings|needs|coverage|history|summary))\s*$",
            ocr_text,
            re.IGNORECASE | re.MULTILINE,
        ):
            label = match.group(1).strip()
            if label.lower() not in seen_labels:
                seen_labels.add(label.lower())
                ui_elements.append(UIElement(
                    element_type="section_header",
                    text=label,
                    confidence=0.6,
                ))

        # ── Page title inference ──────────────────────────────────
        # Try common patterns: first all-caps line, title-case first line,
        # "Page Title — breadcrumb", or HTML <title>-like text.
        #
        # Chrome browser tab titles include incognito / private mode labels and
        # recording tool overlays that must be stripped before use.
        _HEURISTIC_CHROME_BREAKS = [
            "New Incognito Tab", "New Tab", "Private Browsing", "InPrivate",
            "Incognito Window", "Incognito Mode",
            "Ask Gemini", "Customize Chrome", "Show more", "Al Mode",
            "Mostly cloudy", "Partly cloudy",
            "Zoom Meeting", "Teams Meeting", "Loom Recording", "is recording",
        ]
        page_title = ""

        # Pattern 1: "Title — subtitle" or "Title | subtitle" on first lines
        title_sep = re.search(
            r"^([A-Z][A-Za-z0-9\s]{2,40})\s*(?:[—\-|»]|>\s)",
            ocr_text,
            re.MULTILINE,
        )
        if title_sep:
            page_title = title_sep.group(1).strip()

        # Pattern 2: first prominent line (title-case, >=3 words, no colon)
        if not page_title:
            for line in ocr_text.split("\n")[:5]:
                line = line.strip()
                if (
                    len(line) >= 8
                    and len(line) <= 60
                    and ":" not in line
                    and re.match(r"^[A-Z][A-Za-z0-9\s\-/]+$", line)
                    and sum(1 for c in line if c.isupper()) >= 2
                ):
                    page_title = line
                    break

        # Pattern 3: step indicator as title (e.g. "Step 2 of 5 - Coverage Details")
        if not page_title and step_match:
            page_title = step_match.group(0)

        # Strip browser chrome suffixes from the extracted page title.
        # OCR reads browser tab text verbatim, so titles often trail off into
        # "New Incognito Tab X", "Ask Gemini", or recording tool overlays.
        if page_title:
            earliest = len(page_title)
            for kw in _HEURISTIC_CHROME_BREAKS:
                pos = page_title.find(kw)
                if 0 < pos < earliest:
                    earliest = pos
            page_title = page_title[:earliest].strip().rstrip(";:,.'\"- |")

        # ── Table detection (row×column pattern) ──────────────────
        tables: list[dict] = []
        # Look for grid-like structures: 2+ rows with consistent delimiter counts
        pipe_rows = [
            line.strip()
            for line in ocr_text.split("\n")
            if line.count("|") >= 2 or line.count("\t") >= 2
        ]
        if len(pipe_rows) >= 2:
            tables.append({
                "rows": len(pipe_rows),
                "sample": pipe_rows[0][:120],
            })

        # Build a meaningful description from actual detected UI elements
        element_labels = []
        for el in ui_elements[:6]:
            label = (el.text or "").strip()
            if label and len(label) >= 2:
                element_labels.append(f"{el.element_type}: {label}")

        # Richer description incorporating page_title and workflow context
        parts: list[str] = []
        if page_title:
            parts.append(f'"{page_title}"')
        parts.append(f"{app_type.value} screen")

        # Summarise element mix
        type_counts: dict[str, int] = {}
        for el in ui_elements:
            type_counts[el.element_type] = type_counts.get(el.element_type, 0) + 1
        mix_parts = []
        for etype in ("button", "text_field", "dropdown", "checkbox", "radio", "link", "tab"):
            cnt = type_counts.get(etype, 0)
            if cnt:
                mix_parts.append(f"{cnt} {etype}{'s' if cnt > 1 else ''}")
        if mix_parts:
            parts.append(f"with {', '.join(mix_parts)}")
        elif element_labels:
            parts.append(f"with {', '.join(element_labels[:4])}")

        if step_match:
            parts.append(f"(step {step_match.group(1)}/{step_match.group(2)})")
        if tables:
            parts.append(f"and {len(tables)} data table{'s' if len(tables) > 1 else ''}")

        description = " ".join(parts)

        # ── Enrich each element with action semantics ──────────
        # action_kind: the user interaction type this element represents.
        # observed_value: the value that was filled, selected, or checked —
        # extracted from the element's own structured properties (label: value
        # pattern from OCR, selected state from checkbox/radio).
        # These are evidence-derived, never hardcoded.
        _ACTION_KIND_MAP = {
            "button": "click",
            "link": "click",
            "text_field": "enter_text",
            "input": "enter_text",
            "textarea": "enter_text",
            "dropdown": "select_option",
            "select": "select_option",
            "checkbox": "check",
            "radio": "select_option",
            "tab": "navigate",
            "step_indicator": "navigate",
            "section_header": "review",
        }
        for el in ui_elements:
            action_kind = _ACTION_KIND_MAP.get(el.element_type, "interact")
            el.properties["action_kind"] = action_kind

            # observed_value: prefer explicit value field, then selected-state text
            observed_value = (el.properties.get("value") or "").strip()
            if not observed_value and el.element_type in ("checkbox", "radio"):
                selected = el.properties.get("selected", False)
                if selected:
                    observed_value = el.text.strip()
            el.properties["observed_value"] = observed_value

        return {
            "ui_elements": ui_elements,
            "description": description,
            "tables": tables,
            "page_title": page_title,
        }

    # ── VisionProvider interface implementation ────────────────
    # These methods satisfy the abstract contract from
    # nexus_sdk.media.vision.VisionProvider, making VisualAnalyzer
    # a first-class pluggable provider.

    @property
    def provider_name(self) -> str:
        return "ollama-llava"

    @property
    def is_available(self) -> bool:
        return self._ollama_available

    async def unload_model(self) -> None:
        """Release Ollama client resources (VisionProvider contract)."""
        for client in self._all_clients:
            try:
                await client.aclose()
            except Exception:
                pass
        self._all_clients.clear()
        self._http_client = None
        self._ollama_available = False

    async def analyze_frame_provider(
        self,
        frame_path: str,
        context: FrameAnalysisContext | None = None,
    ) -> FrameAnalysisResult:
        """
        VisionProvider-conformant frame analysis.

        Delegates to the existing analyze_frame() pipeline and wraps
        the result in SDK FrameAnalysisResult for callers programming
        against the abstract interface.
        """
        ctx = context or FrameAnalysisContext()

        # Map string app_type to ApplicationType enum
        try:
            app_type = ApplicationType(ctx.app_type)
        except ValueError:
            app_type = ApplicationType.DESKTOP_APP

        raw = await self.analyze_frame(
            frame_path=frame_path,
            ocr_text=ctx.ocr_text,
            app_type=app_type,
            previous_description=ctx.previous_description,
            processing_profile=ctx.processing_profile,
        )

        # Convert UIElement objects to dicts for the SDK result
        ui_elements = []
        for elem in _as_list(raw.get("ui_elements", [])):
            if isinstance(elem, UIElement):
                ui_elements.append({
                    "element_type": elem.element_type,
                    "text": elem.text,
                    "confidence": elem.confidence,
                    "properties": elem.properties,
                })
            elif isinstance(elem, dict):
                ui_elements.append(elem)

        return FrameAnalysisResult(
            description=_as_text(raw.get("description", "")),
            ui_elements=ui_elements,
            tables=_table_dicts(raw.get("tables", [])),
            page_title=_as_text(raw.get("page_title", "")),
            provider=self.provider_name,
            model=self.ollama_model,
        )
