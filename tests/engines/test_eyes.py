"""
Eyes Engine — Unit tests.

Tests application classification and data models.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "eyes-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


class TestApplicationClassifier:
    """Test UI application type classification from OCR text."""

    def setup_method(self):
        from main import ApplicationClassifier
        self.classifier = ApplicationClassifier()

    def test_web_ui_detection_url(self):
        from main import ApplicationType
        result = self.classifier.classify("Navigate to https://portal.example.com/login")
        assert result == ApplicationType.WEB_UI

    def test_web_ui_detection_html(self):
        from main import ApplicationType
        result = self.classifier.classify("<html><body>Login</body></html>")
        assert result == ApplicationType.WEB_UI

    def test_excel_detection(self):
        from main import ApplicationType
        result = self.classifier.classify("Sheet1 Cell A1 =SUM(B1:B10)")
        assert result == ApplicationType.EXCEL_SPREADSHEET

    def test_mainframe_detection(self):
        from main import ApplicationType
        result = self.classifier.classify("CICS Transaction INSR01 MAPSET")
        assert result == ApplicationType.MAINFRAME_3270

    def test_terminal_detection(self):
        from main import ApplicationType
        result = self.classifier.classify("$ ls -la /home/user")
        assert result == ApplicationType.TERMINAL

    def test_pdf_detection(self):
        from main import ApplicationType
        result = self.classifier.classify("Page 1 of 15 PDF Document Insurance Policy Form")
        assert result == ApplicationType.PDF_DOCUMENT

    def test_email_detection(self):
        from main import ApplicationType
        result = self.classifier.classify("From: admin@company.com Subject: Policy Update Inbox")
        assert result == ApplicationType.EMAIL_CLIENT

    def test_unknown_fallback(self):
        from main import ApplicationType
        result = self.classifier.classify("")
        assert result in (ApplicationType.UNKNOWN, ApplicationType.DESKTOP_APP)


class TestApplicationTypeEnum:

    def test_all_values(self):
        from main import ApplicationType
        expected = {
            "web_ui", "desktop_app", "excel_spreadsheet",
            "mainframe_3270", "pdf_document", "email_client",
            "terminal", "database_ui", "unknown",
        }
        actual = {t.value for t in ApplicationType}
        assert actual == expected


class TestUIElement:

    def test_create(self):
        from main import UIElement
        elem = UIElement(
            element_type="button",
            text="Submit",
            bbox=[10.0, 20.0, 100.0, 50.0],
            confidence=0.95,
            properties={"enabled": True},
        )
        assert elem.element_type == "button"
        assert elem.text == "Submit"
        assert len(elem.bbox) == 4

    def test_empty_properties(self):
        from main import UIElement
        elem = UIElement(
            element_type="label", text="Name", bbox=[0, 0, 100, 20],
            confidence=0.8, properties={},
        )
        assert elem.properties == {}


class TestFrameAnalysis:

    def test_create(self):
        from main import FrameAnalysis, ApplicationType
        fa = FrameAnalysis(
            frame_id="f-001",
            frame_index=0,
            timestamp_seconds=5.5,
            application_type=ApplicationType.WEB_UI,
            page_title="Login Page",
            url_or_path="https://app.example.com/login",
            ui_elements=[],
            extracted_text="Username Password Login",
            tables=[],
            state_changes=[],
            description="Login screen",
        )
        assert fa.frame_id == "f-001"
        assert fa.application_type == ApplicationType.WEB_UI
