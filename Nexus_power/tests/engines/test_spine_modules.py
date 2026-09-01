"""
Spine Engine — Modular Sub-package Tests.

Tests the parser modules, processor, and shared models refactored
from the monolithic spine-engine/main.py.

All tests exercise stub mode (no real file-parsing libraries).
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "spine-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Shared Models ─────────────────────────────────────────────


class TestSharedModels:
    """Test DocumentChunk and ExtractedTable from app.models."""

    def test_import(self):
        from app.models import DocumentChunk, ExtractedTable
        assert DocumentChunk is not None
        assert ExtractedTable is not None

    def test_document_chunk_creation(self):
        from app.models import DocumentChunk
        chunk = DocumentChunk(
            chunk_id="ch-001",
            document_id="doc-001",
            content="Test content",
            page_number=1,
            chunk_type="text",
        )
        assert chunk.chunk_id == "ch-001"
        assert chunk.document_id == "doc-001"
        assert chunk.content == "Test content"
        assert chunk.page_number == 1

    def test_extracted_table_creation(self):
        from app.models import ExtractedTable
        table = ExtractedTable(
            table_id="tbl-001",
            document_id="doc-001",
            headers=["Name", "Age"],
            rows=[["Alice", "30"], ["Bob", "25"]],
            page_number=2,
        )
        assert table.table_id == "tbl-001"
        assert table.document_id == "doc-001"
        assert len(table.headers) == 2
        assert len(table.rows) == 2


# ─── PDFParser ─────────────────────────────────────────────────


class TestPDFParser:
    """Test PDFParser from app.parsers (stub mode)."""

    def test_import(self):
        from app.parsers import PDFParser
        assert PDFParser is not None

    def test_init(self):
        from app.parsers import PDFParser
        parser = PDFParser()
        assert parser is not None

    def test_stub_parse(self):
        from app.parsers import PDFParser
        parser = PDFParser()
        result = parser.parse(b"fake pdf bytes", "doc-001")
        assert isinstance(result, dict)

    def test_set_event_bus(self):
        import app.parsers.pdf as pdf_mod
        assert hasattr(pdf_mod, "set_event_bus")
        pdf_mod.set_event_bus(None)  # Should not raise


# ─── ExcelParser ──────────────────────────────────────────────


class TestExcelParser:
    """Test ExcelParser from app.parsers (stub mode)."""

    def test_import(self):
        from app.parsers import ExcelParser
        assert ExcelParser is not None

    def test_init(self):
        from app.parsers import ExcelParser
        parser = ExcelParser()
        assert parser is not None

    def test_stub_parse(self):
        from app.parsers import ExcelParser
        import unittest.mock as mock
        # Force stub path by hiding openpyxl
        with mock.patch.dict("sys.modules", {"openpyxl": None}):
            result = ExcelParser.parse(b"fake xlsx bytes", "doc-001")
        assert isinstance(result, dict)
        assert "sheets" in result


# ─── WordParser ───────────────────────────────────────────────


class TestWordParser:
    """Test WordParser from app.parsers (stub mode)."""

    def test_import(self):
        from app.parsers import WordParser
        assert WordParser is not None

    def test_init(self):
        from app.parsers import WordParser
        parser = WordParser()
        assert parser is not None

    def test_stub_parse(self):
        from app.parsers import WordParser
        parser = WordParser()
        result = parser.parse(b"fake docx bytes", "doc-001")
        assert isinstance(result, dict)

    def test_set_event_bus(self):
        import app.parsers.word as word_mod
        assert hasattr(word_mod, "set_event_bus")
        word_mod.set_event_bus(None)


# ─── PowerPointParser ────────────────────────────────────────


class TestPowerPointParser:
    """Test PowerPointParser from app.parsers (stub mode)."""

    def test_import(self):
        from app.parsers import PowerPointParser
        assert PowerPointParser is not None

    def test_init(self):
        from app.parsers import PowerPointParser
        parser = PowerPointParser()
        assert parser is not None

    def test_stub_parse(self):
        from app.parsers import PowerPointParser
        parser = PowerPointParser()
        result = parser.parse(b"fake pptx bytes", "doc-001")
        assert isinstance(result, dict)

    def test_set_event_bus(self):
        import app.parsers.powerpoint as pptx_mod
        assert hasattr(pptx_mod, "set_event_bus")
        pptx_mod.set_event_bus(None)


# ─── CSVParser ────────────────────────────────────────────────


class TestCSVParser:
    """Test CSVParser from app.parsers."""

    def test_import(self):
        from app.parsers import CSVParser
        assert CSVParser is not None

    def test_init(self):
        from app.parsers import CSVParser
        parser = CSVParser()
        assert parser is not None


# ─── TextParser ───────────────────────────────────────────────


class TestTextParser:
    """Test TextParser from app.parsers."""

    def test_import(self):
        from app.parsers import TextParser
        assert TextParser is not None

    def test_init(self):
        from app.parsers import TextParser
        parser = TextParser()
        assert parser is not None


# ─── DocumentProcessor ───────────────────────────────────────


class TestDocumentProcessor:
    """Test DocumentProcessor from app.processor."""

    def test_import(self):
        from app.processor import DocumentProcessor
        assert DocumentProcessor is not None

    def test_init(self):
        from app.processor import DocumentProcessor
        proc = DocumentProcessor()
        assert proc is not None

    def test_parser_map_populated(self):
        from app.processor import DocumentProcessor
        proc = DocumentProcessor()
        assert hasattr(proc, "PARSER_MAP") or hasattr(DocumentProcessor, "PARSER_MAP")
        pmap = getattr(proc, "PARSER_MAP", getattr(DocumentProcessor, "PARSER_MAP", {}))
        assert len(pmap) >= 6  # pdf, xlsx, docx, pptx, csv, txt at minimum

    def test_classify_document(self):
        from app.processor import DocumentProcessor
        proc = DocumentProcessor()
        keywords = {
            "rate_filing": ["premium", "rate", "annual"],
            "claims": ["claim", "loss", "adjuster"],
        }
        doc_type = proc._classify_document(
            text="This premium rate table shows annual costs.",
            filename="rates.pdf",
            tables=[],
            classification_keywords=keywords,
        )
        assert doc_type is not None

    def test_chunk_text(self):
        from app.processor import DocumentProcessor
        proc = DocumentProcessor(chunk_size=30, chunk_overlap=5)
        chunks = proc._chunk_text(
            text="First paragraph of text. Second paragraph with more content here.",
            document_id="doc-001",
            tenant_id="t-1",
            session_id="s-1",
            parsed={"pages": []},
        )
        assert isinstance(chunks, list)
        assert len(chunks) >= 1
        for c in chunks:
            assert c.document_id == "doc-001"


# ─── Re-exports ───────────────────────────────────────────────


class TestParsersReExports:
    """Verify app.parsers.__init__ re-exports all parsers."""

    def test_all_parsers_exported(self):
        from app.parsers import (
            PDFParser,
            ExcelParser,
            WordParser,
            PowerPointParser,
            CSVParser,
            TextParser,
        )
        assert all([
            PDFParser,
            ExcelParser,
            WordParser,
            PowerPointParser,
            CSVParser,
            TextParser,
        ])


# ─── Integration: main.py v0.2.0 ─────────────────────────────


class TestSpineMainImports:
    """Verify main.py v0.2.0 correctly imports from sub-packages."""

    def test_main_version(self):
        from main import SpineEngine
        engine = SpineEngine()
        assert engine.version == "0.2.0"

    def test_main_config(self):
        from main import SpineConfig
        cfg = SpineConfig()
        assert cfg.engine_name == "spine"
        assert cfg.engine_port == 8009

    def test_main_enums(self):
        from main import DocumentType, ChunkType, ParseStatus
        assert DocumentType is not None
        assert ChunkType is not None
        assert ParseStatus is not None

    def test_main_document_type_count(self):
        from main import DocumentType
        assert len(DocumentType) >= 10  # at least 10 document types

    def test_main_classification_keywords(self):
        from main import CLASSIFICATION_KEYWORDS
        assert isinstance(CLASSIFICATION_KEYWORDS, dict)
        assert len(CLASSIFICATION_KEYWORDS) >= 10

    def test_main_request_models(self):
        from main import IngestDocumentResponse, DocumentStatusResponse
        assert IngestDocumentResponse is not None
        assert DocumentStatusResponse is not None
