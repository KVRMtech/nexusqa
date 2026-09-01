"""
Tests for nexus_sdk.security.sanitization — Input sanitization utilities.

Verifies:
- Path traversal protection
- Filename sanitization
- String sanitization
- Request body size validation
- RequestSizeLimitMiddleware
"""
import os

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus_sdk.security.sanitization import (
    sanitize_path,
    sanitize_filename,
    sanitize_string,
    validate_request_size,
    RequestSizeLimitMiddleware,
)


class TestSanitizePath:
    """Tests for path traversal protection."""

    def test_clean_path_passes(self):
        """Normal paths are returned unchanged (after normalization)."""
        result = sanitize_path("documents/report.pdf")
        assert ".." not in result

    def test_blocks_dot_dot_slash(self):
        """../  traversal is blocked."""
        with pytest.raises(HTTPException) as exc_info:
            sanitize_path("../../etc/passwd")
        assert exc_info.value.status_code == 400

    def test_blocks_encoded_traversal(self):
        """URL-encoded traversal (%2e%2e%2f) is blocked."""
        with pytest.raises(HTTPException) as exc_info:
            sanitize_path("%2e%2e%2f%2e%2e%2fetc/passwd")
        assert exc_info.value.status_code == 400

    def test_blocks_backslash_traversal(self):
        """Backslash traversal is blocked."""
        with pytest.raises(HTTPException) as exc_info:
            sanitize_path("..\\..\\windows\\system32")
        assert exc_info.value.status_code == 400

    def test_base_dir_enforcement(self, tmp_path):
        """Path must stay within base_dir when specified."""
        base = str(tmp_path / "uploads")
        os.makedirs(base, exist_ok=True)

        # Valid path within base
        result = sanitize_path("file.txt", base_dir=base)
        assert result.startswith(base)

    def test_base_dir_escape_blocked(self, tmp_path):
        """Paths that escape base_dir are blocked."""
        base = str(tmp_path / "uploads")
        os.makedirs(base, exist_ok=True)

        with pytest.raises(HTTPException) as exc_info:
            sanitize_path("../../etc/passwd", base_dir=base)
        assert exc_info.value.status_code == 400


class TestSanitizeFilename:
    """Tests for filename sanitization."""

    def test_clean_filename(self):
        """Normal filenames pass through."""
        assert sanitize_filename("report.pdf") == "report.pdf"

    def test_strips_path_components(self):
        """Path separators are removed."""
        result = sanitize_filename("/etc/passwd")
        assert "/" not in result
        assert result == "passwd"

    def test_removes_unsafe_chars(self):
        """Special characters are replaced with underscore."""
        result = sanitize_filename('file<>:"|?*.txt')
        assert "<" not in result
        assert ">" not in result

    def test_empty_filename_rejected(self):
        """Empty filename raises HTTPException."""
        with pytest.raises(HTTPException) as exc_info:
            sanitize_filename("...")
        assert exc_info.value.status_code == 400


class TestSanitizeString:
    """Tests for string sanitization."""

    def test_truncates_long_strings(self):
        """Strings are truncated to max_length."""
        long = "a" * 20000
        result = sanitize_string(long, max_length=100)
        assert len(result) == 100

    def test_strips_null_bytes(self):
        """Null bytes are removed."""
        result = sanitize_string("hello\x00world")
        assert "\x00" not in result
        assert result == "helloworld"

    def test_strip_html(self):
        """HTML tags are removed when strip_html=True."""
        result = sanitize_string("<script>alert('xss')</script>Hello", strip_html=True)
        assert "<script>" not in result
        assert "Hello" in result

    def test_no_strip_html_by_default(self):
        """HTML tags are preserved by default."""
        result = sanitize_string("<b>bold</b>")
        assert "<b>" in result


class TestValidateRequestSize:
    """Tests for request size validation."""

    def test_valid_size(self):
        """Content under limit passes."""
        validate_request_size(1000, max_size=10000)

    def test_exceeds_limit(self):
        """Content over limit raises HTTPException."""
        with pytest.raises(HTTPException) as exc_info:
            validate_request_size(20_000_000, max_size=10_000_000)
        assert exc_info.value.status_code == 413

    def test_none_content_length(self):
        """None content length passes (chunked transfer)."""
        validate_request_size(None)


class TestRequestSizeLimitMiddleware:
    """Tests for RequestSizeLimitMiddleware."""

    def test_allows_small_requests(self):
        """Requests under limit pass through."""
        app = FastAPI()
        app.add_middleware(RequestSizeLimitMiddleware, max_body_size=1024)

        @app.post("/upload")
        async def upload():
            return {"status": "ok"}

        client = TestClient(app)
        resp = client.post("/upload", content=b"small data")
        assert resp.status_code == 200

    def test_rejects_large_requests(self):
        """Requests over limit get 413."""
        app = FastAPI()
        app.add_middleware(RequestSizeLimitMiddleware, max_body_size=100)

        @app.post("/upload")
        async def upload():
            return {"status": "ok"}

        client = TestClient(app)
        resp = client.post(
            "/upload",
            content=b"x" * 200,
            headers={"content-length": "200"},
        )
        assert resp.status_code == 413
