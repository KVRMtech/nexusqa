"""
Eyes Engine — Modular Sub-package Tests.

Tests frame extraction, vision analysis, OCR, and application classification
modules that were refactored from the monolithic eyes-engine/main.py.

These tests run without GPU models (OpenCV, EasyOCR, Ollama)
and validate stub paths, heuristic analysis, and data-flow.
"""

import pytest
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "eyes-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Frame Extractor ──────────────────────────────────────────


class TestFrameExtractor:
    """Test FrameExtractor in stub mode (no OpenCV)."""

    def test_init_defaults(self):
        from app.frame_diff import FrameExtractor
        fe = FrameExtractor()
        assert fe.frame_diff_threshold == 0.05
        assert fe.max_fps_extract == 1.0
        assert fe.keyframe_only is False

    def test_init_custom(self):
        from app.frame_diff import FrameExtractor
        fe = FrameExtractor(
            frame_diff_threshold=0.10,
            max_fps_extract=1.0,
            keyframe_only=True,
        )
        assert fe.frame_diff_threshold == 0.10
        assert fe.max_fps_extract == 1.0
        assert fe.keyframe_only is True

    def test_stub_extract(self):
        """Internal stub method returns placeholder frames."""
        from app.frame_diff import FrameExtractor
        fe = FrameExtractor()
        # Call the internal stub directly (bypasses OpenCV which IS installed)
        result = fe._stub_extract("/tmp/test.mp4", "/tmp/frames")
        assert len(result) == 3
        assert result[0]["timestamp"] == 0.0
        assert result[1]["timestamp"] == 3.0
        assert result[2]["timestamp"] == 7.0
        for f in result:
            assert "frame_path" in f
            assert "index" in f

    def test_stub_increments_fallback_count(self):
        from app.frame_diff import FrameExtractor
        fe = FrameExtractor()
        fe._stub_extract("/tmp/a.mp4", "/tmp/out1")
        fe._stub_extract("/tmp/b.mp4", "/tmp/out2")
        assert fe._stub_fallback_count == 2


class TestProbeVideo:
    """Test probe_video with non-existent file."""

    def test_probe_returns_dict_with_expected_keys(self):
        from app.frame_diff import probe_video
        result = asyncio.get_event_loop().run_until_complete(
            probe_video("/tmp/nonexistent_video_12345.mp4")
        )
        # With OpenCV installed but file not found, returns zeros
        assert "duration_seconds" in result
        assert "width" in result
        assert "height" in result
        assert "fps" in result
        assert "total_frames" in result
        assert "codec" in result


# ─── OCR Engine ────────────────────────────────────────────────


class TestOCREngine:
    """Test OCREngine in stub mode (no EasyOCR)."""

    def test_init_defaults(self):
        from app.vision import OCREngine
        ocr = OCREngine()
        assert ocr.languages == ["en"]
        assert ocr.gpu is True
        assert ocr.reader is None
        assert ocr.is_real is False

    def test_load_stub_fallback(self):
        from app.vision import OCREngine
        ocr = OCREngine()
        result = asyncio.get_event_loop().run_until_complete(ocr.load())
        assert result is False
        assert ocr.is_real is False

    def test_stub_returns_3_tuple(self):
        """Stub OCR returns (text, regions, avg_confidence)."""
        from app.vision import OCREngine
        ocr = OCREngine()
        text, regions, confidence = ocr.extract_text("/tmp/frame.png")
        assert isinstance(text, str)
        assert "[Stub]" in text
        assert isinstance(regions, list)
        assert len(regions) >= 1
        assert isinstance(confidence, float)
        assert confidence == 0.0

    def test_stub_region_structure(self):
        from app.vision import OCREngine
        ocr = OCREngine()
        _, regions, _ = ocr.extract_text("/tmp/frame.png")
        region = regions[0]
        assert "text" in region
        assert "bbox" in region
        assert "confidence" in region
        assert len(region["bbox"]) == 4

    def test_stub_increments_count(self):
        from app.vision import OCREngine
        ocr = OCREngine()
        ocr.extract_text("/tmp/a.png")
        ocr.extract_text("/tmp/b.png")
        assert ocr._stub_fallback_count == 2


# ─── Application Classifier ───────────────────────────────────


class TestApplicationClassifier:
    """Test heuristic application classification."""

    def setup_method(self):
        from app.vision import ApplicationClassifier
        self.classifier = ApplicationClassifier()

    def test_web_ui_from_url(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("Navigate to https://portal.example.com/login")
        assert result == ApplicationType.WEB_UI

    def test_web_ui_from_domain(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("Visit www.insurance-portal.com for policy info")
        assert result == ApplicationType.WEB_UI

    def test_web_ui_from_html_tag(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("<html><body>Login</body></html>")
        assert result == ApplicationType.WEB_UI

    def test_mainframe_detection(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("CICS Transaction INSR01 MAPSET")
        assert result == ApplicationType.MAINFRAME_3270

    def test_mainframe_from_3270(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("===> LOGON TSO/E USERID(ADMIN)")
        assert result == ApplicationType.MAINFRAME_3270

    def test_excel_detection(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("Sheet1 Cell A1 =SUM(B1:B10)")
        assert result == ApplicationType.EXCEL_SPREADSHEET

    def test_excel_vlookup(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("=VLOOKUP(A1, Sheet2!B:C, 2, FALSE)")
        assert result == ApplicationType.EXCEL_SPREADSHEET

    def test_pdf_detection(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("Page 1 of 15 PDF Document Insurance Policy Form")
        assert result == ApplicationType.PDF_DOCUMENT

    def test_pdf_from_adobe(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("Adobe Acrobat Reader DC - policy_2024.pdf")
        assert result == ApplicationType.PDF_DOCUMENT

    def test_email_detection(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("From: admin@company.com Subject: Policy Update Inbox")
        assert result == ApplicationType.EMAIL_CLIENT

    def test_email_from_outlook(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("Outlook - Inbox (42) Sent Items Drafts")
        assert result == ApplicationType.EMAIL_CLIENT

    def test_terminal_detection(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("$ ls -la /home/user")
        assert result == ApplicationType.TERMINAL

    def test_terminal_powershell(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("PS C:\\Users\\admin> Get-Process")
        assert result == ApplicationType.TERMINAL

    def test_database_detection(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("SELECT * FROM policies WHERE status = 'active'")
        assert result == ApplicationType.DATABASE_UI

    def test_unknown_fallback(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("")
        assert result == ApplicationType.DESKTOP_APP

    def test_random_text_fallback(self):
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("Loading... please wait 42%")
        assert result == ApplicationType.DESKTOP_APP

    def test_classification_priority_mainframe_first(self):
        """Mainframe indicators take precedence over all others."""
        from nexus_sdk.media.models import ApplicationType
        result = self.classifier.classify("CICS https://example.com")
        assert result == ApplicationType.MAINFRAME_3270


# ─── Visual Analyzer ──────────────────────────────────────────


class TestVisualAnalyzer:
    """Test VisualAnalyzer in stub/heuristic mode (no Ollama)."""

    def test_init_defaults(self):
        from app.vision import VisualAnalyzer
        va = VisualAnalyzer()
        assert va.ollama_model == "llama3.2-vision:11b"
        assert va.fast_ollama_model == "llava:7b"
        assert va.is_real is False

    def test_eyes_config_prefers_service_scoped_model(self, monkeypatch):
        from main import EyesConfig
        monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:1b")
        monkeypatch.setenv("EYES_OLLAMA_MODEL", "llava:13b")
        monkeypatch.setenv("EYES_FAST_OLLAMA_MODEL", "llava:7b")
        cfg = EyesConfig()
        assert cfg.ollama_model == "llava:13b"
        assert cfg.fast_ollama_model == "llava:7b"

    def test_heuristic_analyze_buttons(self):
        """Heuristic detects button-like UI elements."""
        from app.vision import VisualAnalyzer
        from nexus_sdk.media.models import ApplicationType
        va = VisualAnalyzer()
        result = va._heuristic_analyze(
            frame_path="/tmp/frame.png",
            ocr_text="Submit Cancel Next Back Save",
            app_type=ApplicationType.WEB_UI,
            previous_description="",
        )
        assert "ui_elements" in result
        assert "description" in result
        assert "tables" in result
        # Should detect multiple buttons
        buttons = [e for e in result["ui_elements"] if e.element_type == "button"]
        assert len(buttons) >= 3

    def test_heuristic_analyze_fields(self):
        """Heuristic detects labeled text fields."""
        from app.vision import VisualAnalyzer
        from nexus_sdk.media.models import ApplicationType
        va = VisualAnalyzer()
        result = va._heuristic_analyze(
            frame_path="/tmp/frame.png",
            ocr_text="Name: John Smith\nEmail: john@example.com\nPolicy: POL-12345",
            app_type=ApplicationType.DESKTOP_APP,
            previous_description="",
        )
        fields = [e for e in result["ui_elements"] if e.element_type == "text_field"]
        assert len(fields) >= 2

    def test_heuristic_description_format(self):
        from app.vision import VisualAnalyzer
        from nexus_sdk.media.models import ApplicationType
        va = VisualAnalyzer()
        result = va._heuristic_analyze(
            frame_path="/tmp/frame.png",
            ocr_text="Submit",
            app_type=ApplicationType.WEB_UI,
            previous_description="",
        )
        assert "web_ui" in result["description"]

    def test_analyze_frame_falls_back_heuristic(self):
        """analyze_frame uses heuristic when Ollama unavailable."""
        from app.vision import VisualAnalyzer
        from nexus_sdk.media.models import ApplicationType
        va = VisualAnalyzer()
        result = asyncio.get_event_loop().run_until_complete(
            va.analyze_frame(
                frame_path="/tmp/frame.png",
                ocr_text="Login OK Cancel",
                app_type=ApplicationType.WEB_UI,
            )
        )
        assert "ui_elements" in result
        assert "description" in result


# ─── Main Module Re-exports ───────────────────────────────────


class TestMainModuleReexports:
    """Verify eyes main.py re-exports for backward compatibility."""

    def test_application_classifier(self):
        from main import ApplicationClassifier
        c = ApplicationClassifier()
        assert hasattr(c, "classify")

    def test_application_type(self):
        from main import ApplicationType
        assert ApplicationType.WEB_UI == "web_ui"

    def test_ui_element(self):
        from main import UIElement
        elem = UIElement(element_type="button", text="OK")
        assert elem.text == "OK"

    def test_frame_analysis(self):
        from main import FrameAnalysis, ApplicationType
        fa = FrameAnalysis(
            frame_id="f-001",
            frame_index=0,
            timestamp_seconds=5.5,
            application_type=ApplicationType.WEB_UI,
        )
        assert fa.frame_id == "f-001"


# ─── dHash Computation ────────────────────────────────────────


class TestFrameHash:
    """Test dHash computation helper (requires numpy for mock frames)."""

    def test_compute_hash_returns_hex(self):
        """Verify hash output format."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            pytest.skip("OpenCV not available")

        from app.frame_diff import FrameExtractor
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        h = FrameExtractor._compute_frame_hash(frame, cv2)
        assert isinstance(h, str)
        assert len(h) == 16  # 64-bit → 16 hex chars

    def test_same_frame_same_hash(self):
        """Identical frames produce identical hashes."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            pytest.skip("OpenCV not available")

        from app.frame_diff import FrameExtractor
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        h1 = FrameExtractor._compute_frame_hash(frame, cv2)
        h2 = FrameExtractor._compute_frame_hash(frame, cv2)
        assert h1 == h2

    def test_different_frames_different_hash(self):
        """Visually distinct frames should produce different hashes."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            pytest.skip("OpenCV not available")

        from app.frame_diff import FrameExtractor
        # Use a gradient vs uniform frame for a clear perceptual difference
        gradient = np.tile(
            np.linspace(0, 255, 640, dtype=np.uint8),
            (480, 1),
        )
        gradient = np.stack([gradient, gradient, gradient], axis=2)
        uniform = np.full((480, 640, 3), 128, dtype=np.uint8)
        h1 = FrameExtractor._compute_frame_hash(gradient, cv2)
        h2 = FrameExtractor._compute_frame_hash(uniform, cv2)
        assert h1 != h2
