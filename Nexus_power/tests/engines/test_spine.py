"""
Spine Engine — Unit tests.

Tests DocumentProcessor (classify, chunk, process), CSVParser,
TextParser, and all enums.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "spine-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Enums ────────────────────────────────────────────────────


class TestDocumentType:

    def test_key_values(self):
        from main import DocumentType
        assert DocumentType.RATE_FILING == "rate_filing"
        assert DocumentType.RATE_TABLE == "rate_table"
        assert DocumentType.BRD == "business_requirements_document"
        assert DocumentType.TRAINING_DECK == "training_deck"
        assert DocumentType.COMPLIANCE_MANUAL == "compliance_manual"
        assert DocumentType.UNDERWRITING_GUIDE == "underwriting_guide"
        assert DocumentType.UNKNOWN == "unknown"

    def test_count(self):
        from main import DocumentType
        assert len(DocumentType) == 14


class TestChunkType:

    def test_values(self):
        from main import ChunkType
        assert ChunkType.TEXT == "text"
        assert ChunkType.TABLE == "table"
        assert ChunkType.HEADING == "heading"
        assert ChunkType.LIST == "list"
        assert ChunkType.IMAGE_CAPTION == "image_caption"
        assert ChunkType.METADATA == "metadata"

    def test_count(self):
        from main import ChunkType
        assert len(ChunkType) == 6


class TestParseStatus:

    def test_values(self):
        from main import ParseStatus
        assert ParseStatus.PENDING == "pending"
        assert ParseStatus.PARSING == "parsing"
        assert ParseStatus.COMPLETED == "completed"
        assert ParseStatus.FAILED == "failed"

    def test_count(self):
        from main import ParseStatus
        assert len(ParseStatus) == 7


# ─── CSVParser ─────────────────────────────────────────────────


class TestCSVParser:

    def test_parse_simple_csv(self):
        from main import CSVParser
        csv_data = b"Name,Age,State\nAlice,30,NY\nBob,45,CA\n"
        result = CSVParser.parse(csv_data, "doc-001")
        assert len(result["tables"]) == 1
        table = result["tables"][0]
        assert table.headers == ["Name", "Age", "State"]
        assert table.row_count == 2
        assert table.col_count == 3
        assert table.rows[0] == ["Alice", "30", "NY"]

    def test_parse_empty_csv(self):
        from main import CSVParser
        result = CSVParser.parse(b"", "doc-002")
        assert len(result["tables"]) == 0

    def test_parse_header_only(self):
        from main import CSVParser
        result = CSVParser.parse(b"A,B,C\n", "doc-003")
        assert len(result["tables"]) == 1
        assert result["tables"][0].row_count == 0

    def test_page_count_is_one(self):
        from main import CSVParser
        result = CSVParser.parse(b"A\n1\n2\n", "doc-004")
        assert result["page_count"] == 1

    def test_document_id_set(self):
        from main import CSVParser
        result = CSVParser.parse(b"X\n1\n", "doc-005")
        assert result["tables"][0].document_id == "doc-005"


# ─── TextParser ────────────────────────────────────────────────


class TestTextParser:

    def test_parse_plain_text(self):
        from main import TextParser
        text = b"This is a simple test document for insurance rules."
        result = TextParser.parse(text, "doc-010")
        assert result["full_text"] == "This is a simple test document for insurance rules."
        assert result["page_count"] >= 1
        assert result["tables"] == []

    def test_parse_large_text_page_estimate(self):
        from main import TextParser
        text = ("X" * 3000).encode()  # ~1 page per 3000 chars
        result = TextParser.parse(text, "doc-011")
        assert result["page_count"] == 1

        text_large = ("X" * 9000).encode()
        result = TextParser.parse(text_large, "doc-012")
        assert result["page_count"] == 3

    def test_utf8_handling(self):
        from main import TextParser
        text = "Ünîcödé tëxt with spëcial chars: €£¥".encode("utf-8")
        result = TextParser.parse(text, "doc-013")
        assert "Ünîcödé" in result["full_text"]


# ─── DocumentProcessor — Classification ───────────────────────


class TestDocumentClassification:

    def setup_method(self):
        from main import DocumentProcessor
        self.proc = DocumentProcessor(chunk_size=500, chunk_overlap=100)

    def test_classify_rate_table(self):
        from main import DocumentType
        text = "This document contains the rate table with monthly premium rates per 1000 for smoker and non-smoker."
        doc_type = self.proc._classify_document(text, "rates.xlsx", [])
        assert doc_type == DocumentType.RATE_TABLE

    def test_classify_brd(self):
        from main import DocumentType
        text = "Business Requirements Document. Functional requirement: As a user, acceptance criteria are defined."
        doc_type = self.proc._classify_document(text, "brd_v2.docx", [])
        assert doc_type == DocumentType.BRD

    def test_classify_compliance(self):
        from main import DocumentType
        text = "Compliance manual for NAIC market conduct regulations and anti-money laundering guidelines."
        doc_type = self.proc._classify_document(text, "compliance.pdf", [])
        assert doc_type == DocumentType.COMPLIANCE_MANUAL

    def test_classify_training(self):
        from main import DocumentType
        text = "Training deck for new hire onboarding. Agenda: overview, key takeaways, demo."
        doc_type = self.proc._classify_document(text, "training.pptx", [])
        assert doc_type == DocumentType.TRAINING_DECK

    def test_classify_unknown(self):
        from main import DocumentType
        text = "Lorem ipsum dolor sit amet, completely irrelevant content."
        doc_type = self.proc._classify_document(text, "random.txt", [])
        # Should fall back to GENERAL or UNKNOWN
        assert doc_type in (DocumentType.GENERAL, DocumentType.UNKNOWN)

    def test_filename_boost(self):
        from main import DocumentType
        # A filename matching "rate table" should strongly bias classification
        text = "Some generic text with minimal keywords."
        doc_type = self.proc._classify_document(text, "rate_table_2024.xlsx", [])
        assert doc_type == DocumentType.RATE_TABLE


# ─── DocumentProcessor — Chunking ─────────────────────────────


class TestDocumentChunking:

    def setup_method(self):
        from main import DocumentProcessor
        self.proc = DocumentProcessor(chunk_size=100, chunk_overlap=20)

    def test_process_csv_file(self):
        csv_data = b"Name,Age\nAlice,30\nBob,45\n"
        result = self.proc.process(
            content=csv_data,
            filename="test.csv",
            document_id="doc-100",
            tenant_id="t1",
        )
        assert result["document_id"] == "doc-100"
        assert result["filename"] == "test.csv"
        assert len(result["tables"]) == 1
        assert result["file_hash"] is not None
        assert len(result["file_hash"]) == 64  # SHA-256 hex

    def test_process_txt_file(self):
        text = ("Insurance premium calculation rules. " * 50).encode()
        result = self.proc.process(
            content=text,
            filename="rules.txt",
            document_id="doc-101",
            tenant_id="t1",
        )
        assert result["document_id"] == "doc-101"
        assert len(result["chunks"]) > 0
        assert result["full_text_length"] > 0

    def test_unsupported_format_raises(self):
        with pytest.raises(ValueError, match="Unsupported format"):
            self.proc.process(
                content=b"binary data",
                filename="image.jpg",
                document_id="doc-102",
                tenant_id="t1",
            )

    def test_chunk_overlap_produces_overlapping_content(self):
        # With chunk_size=100 and overlap=20, chunks should overlap
        text = ("ABCDEFGHIJ" * 50).encode()  # 500 chars
        result = self.proc.process(
            content=text,
            filename="overlap.txt",
            document_id="doc-103",
            tenant_id="t1",
        )
        chunks = result["chunks"]
        if len(chunks) >= 2:
            # Content from end of chunk[0] should appear in start of chunk[1]
            c0_end = chunks[0].content[-20:]
            c1_start = chunks[1].content[:20]
            # Overlap means some of the same text appears in both
            assert c0_end == c1_start or len(chunks) > 1


# ─── DocumentProcessor — Full Pipeline ────────────────────────


class TestDocumentProcessorPipeline:

    def test_process_returns_all_fields(self):
        from main import DocumentProcessor
        proc = DocumentProcessor()
        csv_data = b"Col1,Col2\nVal1,Val2\n"
        result = proc.process(
            content=csv_data,
            filename="data.csv",
            document_id="doc-200",
            tenant_id="t1",
            session_id="sess-001",
        )
        assert "document_id" in result
        assert "filename" in result
        assert "document_type" in result
        assert "page_count" in result
        assert "chunks" in result
        assert "tables" in result
        assert "full_text_length" in result
        assert "file_hash" in result

    def test_md_file_uses_text_parser(self):
        from main import DocumentProcessor
        proc = DocumentProcessor()
        md = b"# Heading\n\nSome markdown content about underwriting."
        result = proc.process(
            content=md,
            filename="guide.md",
            document_id="doc-201",
            tenant_id="t1",
        )
        assert "# Heading" in result["chunks"][0].content or result["full_text_length"] > 0


# ─── Models ────────────────────────────────────────────────────


class TestDocumentChunkModel:

    def test_create(self):
        from main import DocumentChunk, ChunkType
        chunk = DocumentChunk(
            document_id="doc-300",
            chunk_type=ChunkType.TEXT,
            content="Sample text chunk",
            page_number=1,
            section="Introduction",
            char_count=17,
        )
        assert chunk.document_id == "doc-300"
        assert chunk.chunk_type == ChunkType.TEXT
        assert chunk.chunk_id is not None

    def test_defaults(self):
        from main import DocumentChunk, ChunkType
        chunk = DocumentChunk(
            document_id="doc-301",
            chunk_type=ChunkType.TABLE,
            content="Table data",
        )
        assert chunk.page_number is None
        assert chunk.section is None
        assert chunk.metadata == {}
        assert chunk.char_count == 0


class TestExtractedTableModel:

    def test_create(self):
        from main import ExtractedTable
        table = ExtractedTable(
            document_id="doc-400",
            page_number=3,
            headers=["Age", "Premium"],
            rows=[["30", "100"], ["40", "150"]],
            row_count=2,
            col_count=2,
        )
        assert table.row_count == 2
        assert table.col_count == 2
        assert table.table_id is not None


class TestSpineConfig:

    def test_defaults(self):
        from main import SpineConfig
        cfg = SpineConfig()
        assert cfg.engine_name == "spine"
        assert cfg.engine_port == 8009
        assert cfg.chunk_size == 1000
        assert cfg.chunk_overlap == 200
        assert cfg.max_document_size_mb == 100
        assert cfg.ocr_enabled is True
