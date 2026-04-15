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
import os
import re
from pathlib import Path
from typing import Optional

import structlog
from nexus_sdk.events import fire_stub_alert
from nexus_sdk.config import production_guard
from nexus_sdk.media.models import ApplicationType, UIElement

logger = structlog.get_logger()


# ─── OCR Engine ────────────────────────────────────────────────


class OCREngine:
    """
    Extracts text from screenshots using EasyOCR.

    GPU-accelerated on-prem OCR. Falls back to stub
    when EasyOCR is not installed.
    """

    def __init__(
        self,
        languages: list[str] | None = None,
        gpu: bool = True,
        model_dir: str = "./models/easyocr",
        allow_remote_model_bootstrap: bool = True,
        load_timeout_seconds: float = 30.0,
    ):
        self.languages = languages or ["en"]
        self.gpu = gpu
        self.model_dir = model_dir
        self.allow_remote_model_bootstrap = allow_remote_model_bootstrap
        self.load_timeout_seconds = load_timeout_seconds
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

        return easyocr.Reader(
            self.languages,
            gpu=self.gpu,
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
        """
        if self.reader is None:
            return self._stub_ocr(image_path)

        results = self.reader.readtext(image_path)
        text_regions = []
        full_text_parts = []
        total_conf = 0.0

        for bbox, text, confidence in results:
            text_regions.append({
                "text": text,
                "bbox": [
                    float(bbox[0][0]), float(bbox[0][1]),
                    float(bbox[2][0]), float(bbox[2][1]),
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
    ]
    MAINFRAME_INDICATORS = [
        "CICS", "TSO", "ISPF", "3270", "===>", "PF1", "PF3",
        "ENTER", "PA1", "LOGON", "USERID",
    ]
    EXCEL_INDICATORS = [
        "Sheet1", "Sheet2", "Cell", "Formula", "SUM(", "VLOOKUP(",
        "HLOOKUP(", "COUNTIF(", "SUMIF(", "Worksheet", "Workbook",
    ]
    PDF_INDICATORS = [
        ".pdf", "Adobe", "Page 1 of", "Acrobat",
    ]
    EMAIL_INDICATORS = [
        "From:", "To:", "Subject:", "Cc:", "Inbox", "Sent Items",
        "Outlook", "Gmail", "compose",
    ]
    TERMINAL_INDICATORS = [
        "$ ", ">>> ", "bash", "powershell", "cmd>", "PS C:\\",
        "root@", "~$", "terminal",
    ]

    def classify(
        self,
        extracted_text: str,
        frame_metadata: dict | None = None,
    ) -> ApplicationType:
        """Classify application type from extracted text and metadata."""
        text_lower = extracted_text.lower()

        # Check in order of specificity (most specific first)
        for indicator in self.MAINFRAME_INDICATORS:
            if indicator.lower() in text_lower:
                return ApplicationType.MAINFRAME_3270

        for indicator in self.EMAIL_INDICATORS:
            if indicator.lower() in text_lower:
                return ApplicationType.EMAIL_CLIENT

        for indicator in self.PDF_INDICATORS:
            if indicator.lower() in text_lower:
                return ApplicationType.PDF_DOCUMENT

        for indicator in self.EXCEL_INDICATORS:
            if indicator.lower() in text_lower:
                return ApplicationType.EXCEL_SPREADSHEET

        for indicator in self.BROWSER_INDICATORS:
            if indicator.lower() in text_lower:
                return ApplicationType.WEB_UI

        for indicator in self.TERMINAL_INDICATORS:
            if indicator.lower() in text_lower:
                return ApplicationType.TERMINAL

        # Check for database UI patterns
        if any(kw in text_lower for kw in [
            "select ", "insert ", "update ", "delete ", "schema",
            "table", "column", "query", "sql", "database",
        ]):
            return ApplicationType.DATABASE_UI

        return ApplicationType.DESKTOP_APP


# ─── Visual Analyzer ──────────────────────────────────────────


class VisualAnalyzer:
    """
    Analyzes screenshots using a vision-language model via Ollama.

    Uses Llama 3.2 Vision (11B) through Ollama for real visual understanding.
    Falls back to heuristic analysis if Ollama is unavailable.
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
        self._ollama_available = False
        self._available_models: set[str] = set()
        self._model_backoff_until: dict[str, float] = {}
        self._http_client = None

    async def load_model(self) -> bool:
        """
        Connect to Ollama and verify LLaVA model availability.

        Returns True if Ollama + model are ready.
        """
        try:
            import httpx
            self._http_client = httpx.AsyncClient(
                base_url=self.ollama_base_url,
                timeout=120.0,
            )
            resp = await self._http_client.get("/api/tags")
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                model_names = [m.get("name", "") for m in models]
                configured_models = [self.fast_ollama_model, self.ollama_model]
                self._available_models = {
                    model_name
                    for model_name in configured_models
                    if any(model_name in candidate for candidate in model_names)
                }
                if self._available_models:
                    self._ollama_available = True
                    logger.info(
                        "visual_analyzer.ollama_ready",
                        default_model=self.ollama_model,
                        fast_model=self.fast_ollama_model,
                        available_models=sorted(self._available_models),
                        url=self.ollama_base_url,
                    )
                    return True
                logger.warning(
                    "visual_analyzer.model_not_found: Run 'ollama pull %s' and/or 'ollama pull %s'",
                    self.fast_ollama_model,
                    self.ollama_model,
                )
        except Exception as e:
            logger.warning(
                "visual_analyzer.ollama_unavailable: %s — using heuristic",
                e,
            )
            if self._http_client:
                await self._http_client.aclose()
                self._http_client = None

        # Production guard: refuse stub mode in production environments
        production_guard(
            "Ollama LLaVA visual analyzer (eyes-engine)",
            available=self._ollama_available,
        )

        return False

    @property
    def is_real(self) -> bool:
        return self._ollama_available

    async def analyze_frame(
        self,
        frame_path: str,
        ocr_text: str,
        app_type: ApplicationType,
        previous_description: str = "",
        processing_profile: str = "deep",
    ) -> dict:
        """
        Analyze a single frame using Ollama LLaVA or heuristic fallback.

        Returns dict with: ui_elements, description, tables, page_title
        """
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

    def describe_mode(self) -> str:
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
        """Analyze frame using Ollama LLaVA multimodal model."""
        import base64
        import json

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
            "options": {"temperature": 0.1, "num_predict": 2048},
        }

        resp = await self._http_client.post(
            "/api/chat", json=payload, timeout=120.0
        )
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("message", {}).get("content", "{}")
            try:
                parsed = json.loads(content)
                ui_elements = []
                for elem in parsed.get("ui_elements", []):
                    props = elem.get("properties", {})
                    location = elem.get("location", "")
                    if location:
                        props["location"] = location
                    ui_elements.append(UIElement(
                        element_type=elem.get("element_type", "unknown"),
                        text=elem.get("text", ""),
                        confidence=elem.get("confidence", 0.8),
                        properties=props,
                    ))
                return {
                    "ui_elements": ui_elements,
                    "description": parsed.get("description", ""),
                    "tables": parsed.get("tables", []),
                    "page_title": parsed.get("page_title", ""),
                }
            except (json.JSONDecodeError, KeyError):
                pass

        # Ollama returned non-JSON — fall back to heuristic
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

        description = (
            f"Screen showing {app_type.value} with "
            f"{len(ui_elements)} detected elements"
        )

        return {
            "ui_elements": ui_elements,
            "description": description,
            "tables": [],
            "page_title": "",
        }
